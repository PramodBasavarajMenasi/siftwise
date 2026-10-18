"""Time triage on a real repo's history: N failing tests of its newest PR run.

    uv run python scripts/bench_triage.py --repo RDFLib/rdflib --failures 500

Reads ``SIFTWISE_DATABASE_URL`` (the server's database, migrated to head). Read-only: the
work runs in one transaction that is rolled back. The failures are a random sample of the
tests in that run, triaged against its own change, as of that run (so its own results and
everything after it are left out, as in a live triage). Prints each repeat's time and the
verdicts.

Results (2026-10-18, Postgres 16 in Docker on a laptop; first run cold, then warm):
rdflib (2,959 runs, 27 variants, 40K test IDs), 500 failures: 0.98 s, then 0.23-0.27 s;
substrate (1,674 runs), 124 failures: 0.34 s, then 0.10 s. The budget is 3 s.
"""

from __future__ import annotations

import argparse
import random
import time
from collections import Counter
from collections.abc import Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from siftwise.config import get_settings
from siftwise.core.triage import Change, Failure, Target, triage_run


def bench(
    session: Session, repo: str, failures: int, repeat: int = 3, seed: int = 1
) -> list[float]:
    """Seconds per triage of ``failures`` tests of the repo's newest PR run."""
    row = session.execute(
        text(
            """
            SELECT r.id, r.commit_sha, r.variant, r.ci_run_id, r.run_attempt
            FROM runs r JOIN repos ON repos.id = r.repo_id
            WHERE repos.name = :repo AND NOT r.is_main AND r.changed_files_known
            ORDER BY COALESCE(r.started_at, r.created_at) DESC, r.id DESC LIMIT 1
            """
        ),
        {"repo": repo},
    ).one()
    run_id, commit, variant, ci_run_id, attempt = row
    tests = list(
        session.scalars(
            text(
                "SELECT DISTINCT t.test_id FROM test_results tr JOIN tests t ON t.id = tr.test_key"
                " WHERE tr.run_id = :run"
            ),
            {"run": run_id},
        )
    )
    changed = tuple(
        session.scalars(text("SELECT path FROM changed_files WHERE run_id = :run"), {"run": run_id})
    )
    sample = random.Random(seed).sample(sorted(tests), min(failures, len(tests)))
    target = Target(repo, commit, variant, ci_run_id, attempt)
    change = Change(changed, True)
    print(f"{repo}: run {run_id} ({variant}), {len(changed)} changed files, "
          f"{len(sample)} failures of {len(tests)} tests")  # fmt: skip

    times = []
    for n in range(repeat):
        start = time.perf_counter()
        report = triage_run(session, target, [Failure(t) for t in sample], change)
        times.append(time.perf_counter() - start)
        assert report is not None
        verdicts = Counter(r.verdict.value for r in report.results)
        print(f"  run {n + 1}: {times[-1]:.2f}s  {dict(sorted(verdicts.items()))}")
    return times


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--failures", type=int, default=500)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection, connection.begin() as transaction:
        try:
            bench(Session(bind=connection), args.repo, args.failures, args.repeat, args.seed)
        finally:
            transaction.rollback()


if __name__ == "__main__":
    main()
