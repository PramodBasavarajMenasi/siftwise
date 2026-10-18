"""POST /triage: is each failing test a new regression, pre-existing on main, or flaky?"""

from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from siftwise.api.deps import get_app_settings
from siftwise.config import Settings
from siftwise.core.schemas import (
    EvidenceOut,
    FlakyEventOut,
    HistoryOut,
    LinkOut,
    OtherVariantOut,
    PreExistingOut,
    RunRefOut,
    TriageRequest,
    TriageResponse,
    TriageResultOut,
)
from siftwise.core.selector import DependsRule, SelectorConfig
from siftwise.core.triage import (
    MAX_FLAKY_EVENTS,
    Change,
    Failure,
    RunRef,
    Target,
    TriageResult,
    Verdict,
    triage_run,
)
from siftwise.db import get_session

router = APIRouter()


@router.post("/triage", responses={404: {"description": "Unknown repo"}})
def post_triage(
    body: TriageRequest,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> TriageResponse:
    """A verdict per failing test, with confidence, reasons and evidence. Uses only history
    from before the run (see core/triage.py)."""
    change = Change(
        changed_files=tuple(body.changed_files),
        changed_files_known=body.changed_files_known,
        config=SelectorConfig(
            depends=tuple(DependsRule(rule.tests, tuple(rule.on)) for rule in body.depends),
            go_module=body.go_module,
        ),
        affected_packages=body.affected_packages,
        affected_files=body.affected_files,
        deleted_files=tuple(body.deleted_files),
    )
    target = Target(
        repo=body.repo,
        commit_sha=body.commit_sha,
        variant=body.variant,
        ci_run_id=body.ci_run_id,
        run_attempt=body.run_attempt,
        as_of=body.as_of,
    )
    failures = [Failure(f.test_id, f.message, f.file_path) for f in body.failures]
    report = triage_run(session, target, failures, change, settings.stats_window_days)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {body.repo!r}")
    counts = Counter(result.verdict for result in report.results)
    return TriageResponse(
        repo=body.repo,
        commit_sha=body.commit_sha,
        variant=body.variant,
        as_of=report.cut_at,
        run_id=report.run_id,
        window_days=settings.stats_window_days,
        counts={verdict.value: counts[verdict] for verdict in Verdict},
        results=[_result(result) for result in report.results],
    )


def _ref(run: RunRef) -> RunRefOut:
    return RunRefOut(
        run_id=run.run_id,
        commit_sha=run.commit_sha,
        branch=run.branch,
        variant=run.variant,
        occurred_at=run.run_at,
        ci_run_id=run.ci_run_id,
    )


def _result(result: TriageResult) -> TriageResultOut:
    h = result.history
    streak = h.broken_on_main
    return TriageResultOut(
        test_id=result.test_id,
        verdict=result.verdict.value,
        confidence=result.confidence.value,
        reasons=list(result.reasons),
        evidence=EvidenceOut(
            links=[
                LinkOut(
                    signal=link.signal,
                    changed=link.changed,
                    reason=link.reason,
                    strength=link.strength.value,
                )
                for link in result.links
            ],
            pre_existing=PreExistingOut(
                variant=streak.variant,
                since_sha=streak.since.commit_sha,
                since_run=_ref(streak.since),
                main_runs_failing=streak.main_runs_failing,
                last_main_pass_sha=streak.last_main_pass.commit_sha
                if streak.last_main_pass
                else None,
                message_matches=result.main_message_matches,
            )
            if streak
            else None,
            other_variants=[
                OtherVariantOut(variant=run.variant, run=_ref(run))
                for run in h.other_variants_failing
            ],
            flaky=[
                FlakyEventOut(
                    kind=event.kind,
                    commit_sha=event.commit_sha,
                    variant=event.variant,
                    failed=[_ref(r) for r in event.failed],
                    passed=[_ref(r) for r in event.passed],
                    message_matches=matches,
                )
                for event, matches in result.flaky[:MAX_FLAKY_EVENTS]
            ],
            history=HistoryOut(
                seen_before=h.seen,
                failures_in_window=h.failures_in_window,
                last_failed_at=h.last_failed_at,
                last_main_pass=_ref(h.last_main_pass) if h.last_main_pass else None,
                flaky_counts=result.flaky_counts,
            ),
        ),
    )
