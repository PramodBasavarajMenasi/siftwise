"""Outcomes backfill: verdicts computed later for CI run attempts uploaded without an outcome
(#85, part 3; ``POST /repos/{repo}/outcomes/backfill``, ``siftwise outcomes backfill``).

An attempt is pending when it has an upload (``runs.rerun`` = 0, with a CI run ID) and no
``attempt_outcomes`` row: uploaded by a client older than 0.4, or a 0.4 run whose outcome step
failed. Each is compared by the server's own ``shadow.compare_ci_run`` with ``source``
``backfill``, oldest first, one commit per attempt, so a stopped backfill resumes where it
stopped and a second run finds nothing to do.

No peeking: triage already reads only runs that ran before the attempt (never its own other
legs), the change links and ``known_flaky`` the attempt's uploads stored, and file paths
reported by then. An attempt uploaded by CI itself (its last upload within ``LIVE_UPLOAD`` of
its run) also reads only runs uploaded by its last upload (``triage.Target.known_at``): what a
live outcome call would have had. An attempt imported later (``scripts/backfill.py``) had no
live moment: its history is every run that ran before it. What no stored row keeps is
replaced by today's: the server's stats window (``SIFTWISE_STATS_WINDOW_DAYS``).

A live outcome call and the backfill never write one attempt at once (``compare_ci_run``'s
advisory lock); the backfill skips an attempt that has an outcome once it holds the lock, so a
live one always wins.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from siftwise.core.shadow import OUTCOME_BACKFILL, OutcomeExists, compare_ci_run

logger = logging.getLogger(__name__)

LIVE_UPLOAD = timedelta(days=1)  # an attempt's last upload this soon after its run: by CI
MAX_LIMIT = 100  # attempts per request, at most
MAX_SECONDS = 60.0  # no new attempt is started after this long in one request


@dataclass(frozen=True)
class Cursor:
    """Where a backfill stopped: attempts after this one (in the backfill's order) are next."""

    run_at: datetime
    ci_run_id: str
    run_attempt: int


@dataclass(frozen=True)
class Pending:
    ci_run_id: str
    run_attempt: int
    run_at: datetime  # its newest upload's run time
    uploaded_at: datetime  # its last upload
    selection: bool  # a selection was recorded for it

    @property
    def cursor(self) -> Cursor:
        return Cursor(self.run_at, self.ci_run_id, self.run_attempt)

    @property
    def known_at(self) -> datetime | None:
        """Uploaded by CI itself: history is what was uploaded by its last upload. Imported
        later: no limit (there was no live moment)."""
        return self.uploaded_at if self.uploaded_at - self.run_at <= LIVE_UPLOAD else None


@dataclass
class BackfillResult:
    dry_run: bool
    pending: int  # attempts without an outcome, before this request
    computed_with_selection: int = 0  # "verdicts computed later"
    computed_without_selection: int = 0  # "verdicts computed later; no selection recorded"
    skipped_live: int = 0  # a live outcome got there first
    failed: list[tuple[str, int, str]] = field(default_factory=list)
    next: Cursor | None = None  # None: nothing left after this request's last attempt
    remaining: int = 0  # pending attempts after ``next``
    with_selection: int = 0  # of ``pending``: attempts with a recorded selection (dry run)

    @property
    def computed(self) -> int:
        return self.computed_with_selection + self.computed_without_selection


_PENDING_SQL = """
SELECT r.ci_run_id, r.run_attempt, max(COALESCE(r.started_at, r.created_at)) AS run_at,
       max(r.created_at) AS uploaded_at,
       EXISTS (SELECT 1 FROM ci_selections s WHERE s.repo_id = :repo_id
               AND s.ci_run_id = r.ci_run_id AND s.run_attempt = r.run_attempt) AS selection
FROM runs r
WHERE r.repo_id = :repo_id AND r.ci_run_id IS NOT NULL
GROUP BY r.ci_run_id, r.run_attempt
HAVING bool_or(r.rerun = 0) AND NOT EXISTS (
    SELECT 1 FROM attempt_outcomes o WHERE o.repo_id = :repo_id
      AND o.ci_run_id = r.ci_run_id AND o.run_attempt = r.run_attempt)
"""


def pending_attempts(session: Session, repo_id: int) -> list[Pending]:
    """Every attempt without an outcome, oldest first (by its newest upload's run time)."""
    rows = session.execute(
        text(f"{_PENDING_SQL} ORDER BY run_at, r.ci_run_id, r.run_attempt"), {"repo_id": repo_id}
    ).all()
    return [Pending(r.ci_run_id, r.run_attempt, r.run_at, r.uploaded_at, r.selection) for r in rows]


def _after(p: Pending, cursor: Cursor | None) -> bool:
    if cursor is None:
        return True
    return (p.run_at, p.ci_run_id, p.run_attempt) > (
        cursor.run_at, cursor.ci_run_id, cursor.run_attempt)  # fmt: skip


def backfill_outcomes(
    session: Session,
    repo: str,
    *,
    window_days: int,
    limit: int = 20,
    max_seconds: float = 30.0,
    after: Cursor | None = None,
    dry_run: bool = False,
) -> BackfillResult | None:
    """Compare up to ``limit`` pending attempts after ``after`` (no new one is started after
    ``max_seconds``); with ``dry_run``, only count them. None for an unknown repo."""
    repo_id = session.execute(
        text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
    ).scalar_one_or_none()
    if repo_id is None:
        return None
    limit = max(1, min(limit, MAX_LIMIT))
    max_seconds = max(0.0, min(max_seconds, MAX_SECONDS))
    pending = pending_attempts(session, repo_id)
    result = BackfillResult(dry_run, len(pending), with_selection=sum(p.selection for p in pending))
    todo = [p for p in pending if _after(p, after)]
    if dry_run:
        result.remaining = len(todo)
        return result
    started = time.monotonic()
    done = 0
    for p in todo[:limit]:
        if done and time.monotonic() - started >= max_seconds:
            break
        done += 1
        try:
            compare_ci_run(
                session, repo, p.ci_run_id, p.run_attempt, window_days,
                source=OUTCOME_BACKFILL, known_at=p.known_at, if_missing=True,
            )  # fmt: skip
        except OutcomeExists:
            result.skipped_live += 1
        except Exception as exc:  # one attempt's failure doesn't stop the others
            session.rollback()
            logger.exception("outcomes backfill failed for %s %s attempt %s", repo,
                             p.ci_run_id, p.run_attempt)  # fmt: skip
            result.failed.append((p.ci_run_id, p.run_attempt, type(exc).__name__))
        else:
            if p.selection:
                result.computed_with_selection += 1
            else:
                result.computed_without_selection += 1
    left = todo[done:]
    result.next = todo[done - 1].cursor if left and done else None
    result.remaining = len(left)
    return result
