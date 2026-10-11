"""Store an uploaded CI run and its parsed test results."""

from collections.abc import Iterable

from psycopg.errors import UniqueViolation
from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from siftwise.core.history import recompute_test_stats
from siftwise.core.junit import ParsedTestResult
from siftwise.core.models import RUN_UNIQUE_INDEX, ChangedFile, Repo, Run, TestResult
from siftwise.core.schemas import RunMetadata, StatusCounts


def find_run(
    session: Session, repo: str, ci_run_id: str, run_attempt: int, variant: str | None = None
) -> Run | None:
    """The run stored for this CI run attempt and matrix variant, if any."""
    return session.scalars(
        select(Run)
        .join(Repo, Run.repo_id == Repo.id)
        .where(
            Repo.name == repo,
            Run.ci_run_id == ci_run_id,
            Run.run_attempt == run_attempt,
            Run.variant.is_(None) if variant is None else Run.variant == variant,
        )
    ).one_or_none()


def find_existing_run(session: Session, meta: RunMetadata) -> Run | None:
    """The run already stored for this upload, if any. Runs without a CI id never match."""
    if meta.ci_run_id is None:
        return None
    return find_run(session, meta.repo, meta.ci_run_id, meta.run_attempt, meta.variant)


def create_run(
    session: Session,
    meta: RunMetadata,
    results: Iterable[ParsedTestResult],
    *,
    rollup: bool = True,
    window_days: int = 90,
) -> tuple[Run, bool]:
    """Insert the run, its changed files and results, and roll up ``test_stats`` for the
    run's tests over the last ``window_days``, all in one transaction.

    With ``rollup=False`` (batch ingest) stats are left stale until
    ``history.recompute_repo_stats`` runs.

    Returns ``(run, created)``. If a concurrent upload of the same CI run attempt wins the
    race, this transaction is rolled back and the winner's run is returned with
    ``created=False``.
    """
    try:
        repo_id = _get_or_create_repo(session, meta.repo)
        run = Run(
            repo_id=repo_id,
            commit_sha=meta.commit_sha,
            branch=meta.branch,
            is_main=meta.is_main,
            ci_run_id=meta.ci_run_id,
            run_attempt=meta.run_attempt,
            variant=meta.variant,
            started_at=meta.started_at,
            changed_files_known=meta.changed_files_known,
        )
        session.add(run)
        session.flush()

        if meta.changed_files:
            session.execute(
                insert(ChangedFile), [{"run_id": run.id, "path": p} for p in meta.changed_files]
            )
        results = list(results)
        keys = _test_keys(session, repo_id, run.id, results)
        rows = [
            {
                "run_id": run.id,
                "test_key": keys[r.test_id],
                "status": r.status,
                "duration_ms": r.duration_ms,
                "attempt": r.attempt,
                "message": r.message,
            }
            for r in results
        ]
        if rows:
            # render_nulls: by default the ORM drops None-valued keys and starts a new batch
            # whenever the key set changes, so rows alternating message=None / message="..."
            # went one or two per round trip (~20s for 4k rows over Docker networking).
            session.execute(insert(TestResult).execution_options(render_nulls=True), rows)
        # Same transaction: stats never reflect a run that failed to commit, or vice versa.
        if rollup:  # skipped in batch mode; POST /repos/{repo}/rollup catches up later
            recompute_test_stats(session, repo_id, run.id, window_days)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if not _is_violation_of(exc, RUN_UNIQUE_INDEX):
            raise
        existing = find_existing_run(session, meta)
        if existing is None:  # pragma: no cover - the winner's row must be visible after commit
            raise
        return existing, False
    return run, True


def _test_keys(
    session: Session, repo_id: int, run_id: int, results: list[ParsedTestResult]
) -> dict[str, int]:
    """Test ID -> ``tests.id`` for the run's tests, creating missing ones, and record the file
    paths the run reports (kept only if no newer run reported one)."""
    test_ids = sorted({r.test_id for r in results})
    if not test_ids:
        return {}
    # Sorted, so concurrent uploads lock new keys in the same order (no deadlock).
    session.execute(
        text(
            "INSERT INTO tests (repo_id, test_id) "
            "SELECT :repo_id, t FROM unnest(CAST(:test_ids AS text[])) t ORDER BY t "
            "ON CONFLICT (repo_id, test_id) DO NOTHING"
        ),
        {"repo_id": repo_id, "test_ids": test_ids},
    )
    keys: dict[str, int] = {
        test_id: key
        for key, test_id in session.execute(
            text("SELECT id, test_id FROM tests WHERE repo_id = :repo_id AND test_id = ANY(:ids)"),
            {"repo_id": repo_id, "ids": test_ids},
        )
    }
    paths = {keys[r.test_id]: r.file_path for r in results if r.file_path}
    if paths:
        # Latest by run time, like everything "latest": a backfilled older run keeps its hands
        # off a path reported by a newer one. Ties go to the later upload.
        session.execute(
            text(
                """
                UPDATE tests t SET file_path = p.path, file_path_at = ru.run_at
                FROM unnest(CAST(:keys AS integer[]), CAST(:paths AS text[])) AS p(key, path),
                     (SELECT COALESCE(started_at, created_at) AS run_at
                      FROM runs WHERE id = :run_id) AS ru
                WHERE t.id = p.key AND (t.file_path_at IS NULL OR t.file_path_at <= ru.run_at)
                """
            ),
            {"keys": list(paths), "paths": list(paths.values()), "run_id": run_id},
        )
    return keys


def count_results(session: Session, run_id: int) -> StatusCounts:
    rows = session.execute(
        select(TestResult.status, func.count())
        .where(TestResult.run_id == run_id)
        .group_by(TestResult.status)
    ).tuples()
    counts = {status.value: n for status, n in rows}
    return StatusCounts(**counts, total=sum(counts.values()))


def _get_or_create_repo(session: Session, name: str) -> int:
    # ON CONFLICT so concurrent first uploads for a new repo don't fail.
    session.execute(
        pg_insert(Repo).values(name=name).on_conflict_do_nothing(index_elements=[Repo.name])
    )
    return session.scalars(select(Repo.id).where(Repo.name == name)).one()


def _is_violation_of(exc: IntegrityError, constraint: str) -> bool:
    orig = exc.orig
    return isinstance(orig, UniqueViolation) and orig.diag.constraint_name == constraint
