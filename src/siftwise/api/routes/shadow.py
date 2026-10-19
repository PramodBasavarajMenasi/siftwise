"""Shadow mode. POST /repos/{repo}/shadow: compare a recorded shadow run with its stored
selection. GET /repos/{repo}/recall: live recall over the last days."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from siftwise.api.deps import get_app_settings
from siftwise.config import Settings
from siftwise.core.schemas import (
    EstimatedMs,
    RecallMissOut,
    RecallPeriodOut,
    RecallResponse,
    RecallStatsOut,
    ShadowCompareRequest,
    ShadowCompareResponse,
    ShadowMissOut,
    ShadowSelectionOut,
    ShadowTimeOut,
    ShadowVariantOut,
    SuggestionOut,
)
from siftwise.core.shadow import (
    CAUSES,
    MAX_MISSES,
    MISSED,
    UNCLASSIFIED,
    VERDICT_ORDER,
    Tally,
    Time,
    compare_ci_run,
)
from siftwise.core.shadow import recall as shadow_recall
from siftwise.db import get_session

router = APIRouter()


# Repo names contain "/", hence the path converter.
@router.post("/repos/{repo:path}/shadow", responses={404: {"description": "Unknown repo"}})
def post_shadow_compare(
    repo: str,
    body: ShadowCompareRequest,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> ShadowCompareResponse:
    """Compare every recorded variant of a shadow CI run attempt with the selection stored
    for it: each failure caught, missed, or flaky (not counted). Idempotent: a re-call
    replaces the stored comparison. ``shadow: false`` when the attempt wasn't a shadow run.
    For the GitHub Action, after all legs."""
    result = compare_ci_run(
        session, repo, body.ci_run_id, body.run_attempt, settings.stats_window_days
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {repo!r}")
    tally = result.tally
    missed = [u for u in result.units if u.outcome == MISSED]
    missed.sort(key=lambda u: (VERDICT_ORDER.index(u.verdict), u.test_id))
    return ShadowCompareResponse(
        repo=repo,
        ci_run_id=body.ci_run_id,
        run_attempt=body.run_attempt,
        shadow=result.shadow,
        counted=result.counted,
        mode=result.mode,
        selections=[
            ShadowSelectionOut(
                variant=s.variant,
                commit_sha=s.commit_sha,
                mode=s.mode,
                reason=s.reason,
                selected_count=s.selected_count,
                total_known=s.total_known,
                estimated_ms=EstimatedMs(
                    total=s.estimated_ms_total, would_run=s.estimated_ms_would_run
                ),
                status=s.status,
            )
            for s in result.selections
        ],
        variants=[ShadowVariantOut(**vars(v)) for v in result.variants],
        failing_tests=len(result.units),
        caught=tally.caught,
        missed=tally.missed,
        flaky_excluded=tally.flaky_excluded,
        missed_by_verdict=_by_verdict(tally),
        missed_tests=[
            ShadowMissOut(
                test_id=u.test_id,
                verdict=u.verdict,
                variants=list(u.variants),
                cause=u.cause.cause if u.cause else None,
                cause_detail=u.cause.detail if u.cause else None,
            )
            for u in missed[:MAX_MISSES]
        ],
        time=_time(result.time),
    )


@router.get("/repos/{repo:path}/recall", responses={404: {"description": "Unknown repo"}})
def get_recall(
    repo: str,
    session: Annotated[Session, Depends(get_session)],
    days: Annotated[int, Query(ge=1, le=365, description="Shadow runs of the last N days")] = 30,
    by: Annotated[
        Literal["week"] | None, Query(description="Also per week (Monday 00:00 UTC)")
    ] = None,
) -> RecallResponse:
    """Live recall from shadow runs: failures the selection would have caught or missed (flaky
    ones excluded), over every shadow run and over selective ones only, the measured time the
    selection would have saved, why each failure was missed, and rules to consider."""
    report = shadow_recall(session, repo, days, by_week=by == "week")
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {repo!r}")
    return RecallResponse(
        repo=repo,
        days=days,
        since=report.since,
        shadow_runs=report.all.runs,
        selective_runs=report.selective.runs,
        full_runs=report.full_runs,
        not_counted=dict(sorted(report.not_counted.items())),
        in_progress=report.in_progress,
        all=_stats(report.all),
        selective=_stats(report.selective),
        time=_time(report.time),
        misses=[
            RecallMissOut(
                test_id=m.test_id,
                ci_run_id=m.ci_run_id,
                run_attempt=m.run_attempt,
                commit_sha=m.commit_sha,
                variants=list(m.variants),
                verdict=m.verdict,
                selection_mode=m.selection_mode,
                at=m.at,
                cause=m.cause.cause if m.cause else None,
                cause_detail=m.cause.detail if m.cause else None,
                suggestion=m.cause.suggestion if m.cause else None,
            )
            for m in report.misses[:MAX_MISSES]
        ],
        misses_truncated=len(report.misses) > MAX_MISSES,
        suggestions=[
            SuggestionOut(rule=s.rule, misses=s.misses, test_ids=sorted(s.test_ids))
            for s in report.suggestions
        ],
        linked_not_selected=report.linked_not_selected,
        periods=None
        if report.periods is None
        else [
            RecallPeriodOut(
                start=p.start,
                end=p.end,
                shadow_runs=p.all.runs,
                selective_runs=p.selective.runs,
                full_runs=p.full_runs,
                all=_stats(p.all),
                selective=_stats(p.selective),
                time=_time(p.time),
            )
            for p in report.periods
        ],
    )


def _by_verdict(tally: Tally) -> dict[str, int]:
    """Misses per verdict (flaky failures are never misses)."""
    return {v: tally.missed_by_verdict[v] for v in VERDICT_ORDER if v != "flaky"}


def _stats(tally: Tally) -> RecallStatsOut:
    return RecallStatsOut(
        runs=tally.runs,
        failures=tally.failures,
        flaky_excluded=tally.flaky_excluded,
        caught=tally.caught,
        missed=tally.missed,
        recall=tally.recall,
        missed_by_verdict=_by_verdict(tally),
        missed_by_cause=_by_cause(tally),
    )


def _by_cause(tally: Tally) -> dict[str, int]:
    """Misses per cause; 'unclassified' only when some misses predate causes."""
    counts = {cause: tally.missed_by_cause[cause] for cause in CAUSES}
    if tally.missed_by_cause[UNCLASSIFIED]:
        counts[UNCLASSIFIED] = tally.missed_by_cause[UNCLASSIFIED]
    return counts


def _time(time: Time) -> ShadowTimeOut:
    return ShadowTimeOut(total_ms=time.total_ms, would_run_ms=time.would_run_ms, saved=time.saved)
