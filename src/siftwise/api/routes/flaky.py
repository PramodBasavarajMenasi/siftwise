"""GET /repos/{repo}/flaky: the repo's flaky tests over a window, for `siftwise flaky`."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from siftwise.api.deps import Caller, authorize
from siftwise.core.flaky import flaky_tests
from siftwise.core.schemas import FlakyResponse, FlakyTestOut
from siftwise.db import get_session

router = APIRouter()


@router.get("/repos/{repo:path}/flaky", responses={404: {"description": "Unknown repo"}})
def get_flaky(
    repo: str,
    caller: Caller,
    session: Annotated[Session, Depends(get_session)],
    days: Annotated[
        int, Query(ge=1, le=365, description="The days before the repo's newest run")
    ] = 30,
    min_runs: Annotated[
        int, Query(ge=1, le=10_000, description="Only tests that ran in at least this many runs")
    ] = 1,
    limit: Annotated[int, Query(ge=1, le=1000, description="At most this many tests")] = 200,
) -> FlakyResponse:
    """Tests that both failed and passed on the same commit and variant (retries, re-runs,
    re-run uploads) in the window, most flaky first, with the ``known_flaky`` list the newest
    main run applied. Read-only: it suggests, it changes nothing."""
    authorize(caller, repo)
    report = flaky_tests(session, repo, days, min_runs, limit)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {repo!r}")
    return FlakyResponse(
        repo=repo,
        days=days,
        window_start=report.window_start,
        window_end=report.window_end,
        tests=[
            FlakyTestOut(
                test_id=t.test_id,
                file_path=t.file_path,
                runs=t.runs,
                pairs=t.pairs,
                flaky=t.flaky,
                last_flaky_at=t.last_flaky_at,
            )
            for t in report.tests
        ],
        truncated=report.truncated,
        main_known_flaky=report.main_known_flaky,
        main_known_flaky_at=report.main_known_flaky_at,
    )
