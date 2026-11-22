"""GET /repos/{repo}/ci: what siftwise did in CI over a window (core/ci_stats.py)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from siftwise.api.deps import Caller, authorize
from siftwise.core.ci_stats import (
    COUNTING,
    Branch,
    CiStats,
    Period,
    TooManyPeriods,
    ci_stats,
    ci_stats_periods,
)
from siftwise.core.schemas import (
    CiFailuresOut,
    CiNumbersOut,
    CiPeriodOut,
    CiShadowOut,
    CiShareOut,
    CiStatsResponse,
    CiTimeSavedOut,
)
from siftwise.db import get_session

router = APIRouter()

RECALL_NOTE = (
    "Recall comes only from shadow runs (GET /repos/{repo}/recall): in a selective run a test "
    "that wasn't selected didn't run, so a failure it would have shown can't be seen."
)


@router.get("/repos/{repo:path}/ci", responses={404: {"description": "Unknown repo"}})
def get_ci(
    repo: str,
    caller: Caller,
    session: Annotated[Session, Depends(get_session)],
    days: Annotated[int, Query(ge=1, le=365, description="The last N days, from now")] = 30,
    branch: Annotated[
        Branch, Query(description="'main' (the default branch), 'prs' (every other run) or 'all'")
    ] = "all",
    by: Annotated[
        Period | None,
        Query(description="'day' or 'week': also the window per UTC day or week (Monday)"),
    ] = None,
) -> CiStatsResponse:
    """Coverage, selective and full-suite runs with their reasons, the share of tests
    selected, the test time saved (estimated), shadow runs apart, and failures in selective
    runs; with ``by``, the same per UTC day or week (empty ones included; they add up to the
    window). No recall figure: see /recall."""
    authorize(caller, repo)
    if by is None:
        stats = ci_stats(session, repo, days, branch)
        if stats is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {repo!r}")
        return ci_response(repo, stats)
    try:
        found = ci_stats_periods(session, repo, days, by, branch)
    except TooManyPeriods as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {repo!r}")
    response = ci_response(repo, found.total)
    response.by = by
    response.periods = [CiPeriodOut(**numbers(p).model_dump()) for p in found.periods]
    return response


def ci_response(repo: str, stats: CiStats) -> CiStatsResponse:
    assert stats.days is not None
    return CiStatsResponse(
        repo=repo,
        days=stats.days,
        branch=stats.branch,
        counting=COUNTING,
        recall_note=RECALL_NOTE,
        **numbers(stats).model_dump(),
    )


def numbers(stats: CiStats) -> CiNumbersOut:
    assert stats.since is not None
    f = stats.failures
    return CiNumbersOut(
        since=stats.since,
        until=stats.until,
        runs=stats.runs,
        recorded=stats.recorded,
        shadow_runs=stats.shadow_runs,
        selective_runs=stats.selective_runs,
        full_runs=stats.full_runs,
        full_reasons=dict(sorted(stats.full_reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        share_selected=CiShareOut(median=stats.median_share, runs=len(stats.shares)),
        time_saved=CiTimeSavedOut(
            sum_ms=stats.saved_ms,
            median_ms=stats.median_saved_ms,
            runs=len(stats.savings),
            left_out=stats.selective_runs - len(stats.savings),
            legs=stats.legs_counted,
            legs_left_out=stats.legs_left_out,
            baseline_ms=stats.baseline_ms,
            low_confidence=stats.low_confidence,
        ),
        shadow=CiShadowOut(
            runs=stats.shadow_runs,
            compared=stats.shadow_compared,
            measured_ms=stats.shadow_measured_ms,
            would_save_ms=stats.shadow_would_save_ms,
        ),
        failures=CiFailuresOut(
            runs=f.runs,
            runs_failing=f.runs_failing,
            failing=f.failing,
            in_selection=f.in_selection,
            outside=f.outside,
            ranked=len(f.ranks),
            median_rank=f.median_rank,
            first_rank_runs=len(f.first_ranks),
            median_first_rank=f.median_first_rank,
        ),
        backfilled=stats.backfilled,
        backfilled_without_selection=stats.backfilled_without_selection,
    )
