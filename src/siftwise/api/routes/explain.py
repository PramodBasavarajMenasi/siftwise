"""POST /explain: one test's history, flaky evidence and selection for a change."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from siftwise.api.deps import get_app_settings
from siftwise.api.routes.triage import flaky_event_out, link_out
from siftwise.config import Settings
from siftwise.core.history import get_recent_results, get_test, get_test_stats
from siftwise.core.schemas import (
    ExplainRequest,
    ExplainResponse,
    ExplainSelectionOut,
    OutcomeOut,
    TestStatsOut,
)
from siftwise.core.selector import (
    DependsRule,
    KnownTest,
    Mode,
    SelectorConfig,
    WouldRun,
    change_links,
    load_history,
    select_tests,
)
from siftwise.core.triage import (
    MAX_FLAKY_EVENTS,
    Change,
    Failure,
    Target,
    load_histories,
    triage,
)
from siftwise.db import get_session

router = APIRouter()

# Result rows read for "recent" (every attempt is a row); grouped into runs.
_RECENT_ROWS = 40
_RECENT_RUNS = 10


@router.post("/explain", responses={404: {"description": "Unknown repo"}})
def post_explain(
    body: ExplainRequest,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> ExplainResponse:
    """Explain one test: its recent outcomes and stats, flaky evidence (as triage counts it),
    broken-on-main status, and whether and why the selection for this change runs it."""
    config = SelectorConfig(
        always_run=tuple(body.always_run),
        depends=tuple(DependsRule(rule.tests, tuple(rule.on)) for rule in body.depends),
        go_module=body.go_module,
        generated=tuple(body.generated),
    )
    history = load_history(session, body.repo, config)
    if history is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {body.repo!r}")

    test = get_test(session, body.repo, body.test_id)
    stats = get_test_stats(session, body.repo, body.test_id)
    file_path = (test.file_path if test else None) or (stats.file_path if stats else None)

    # Flaky evidence and the main streak exactly as triage sees them now (no commit: nothing
    # is being triaged, so no "same commit passed" rule).
    histories = load_histories(
        session,
        Target(body.repo, None, body.variant),
        [body.test_id],
        settings.stats_window_days,
    )
    assert histories is not None  # the repo exists
    [as_triaged] = triage([Failure(body.test_id)], histories.tests, Change((), False))

    selection = select_tests(
        history,
        body.changed_files,
        body.changed_files_known,
        config,
        affected_packages=body.affected_packages,
        affected_files=body.affected_files,
        deleted_files=body.deleted_files,
    )
    selected = next((t for t in selection.tests if t.test_id == body.test_id), None)
    if selected is not None:
        reasons = list(selected.reasons)
    elif selection.mode is Mode.FULL:
        reasons = [f"the full suite runs: {selection.reason}"]
    elif WouldRun(selection)(body.test_id):
        reasons = ["its test file or Go package runs whole for this change"]
    else:
        reasons = []
    links = (
        change_links(
            [KnownTest(body.test_id, file_path)],
            body.changed_files,
            config,
            body.affected_packages,
            body.affected_files,
            body.deleted_files,
        )[body.test_id]
        if body.changed_files_known
        else ()
    )

    return ExplainResponse(
        repo=body.repo,
        test_id=body.test_id,
        known=test is not None,
        file_path=file_path,
        stats=TestStatsOut.model_validate(stats) if stats else None,
        recent=_recent(session, test.id) if test else [],
        flaky=[flaky_event_out(e, m) for e, m in as_triaged.flaky[:MAX_FLAKY_EVENTS]],
        flaky_counts=as_triaged.flaky_counts,
        window_days=settings.stats_window_days,
        selection=ExplainSelectionOut(
            mode=selection.mode.value,
            selected=bool(reasons),
            reasons=reasons,
            summary=selection.reason,
            links=[link_out(link) for link in links],
        ),
    )


def _recent(session: Session, test_key: int) -> list[OutcomeOut]:
    """The latest runs the test appeared in, one entry per run (its final attempt)."""
    runs: dict[int, OutcomeOut] = {}
    for result, run in get_recent_results(session, test_key, _RECENT_ROWS):
        if run.id in runs:
            runs[run.id].attempts += 1
            continue
        if len(runs) == _RECENT_RUNS:
            break
        runs[run.id] = OutcomeOut(
            run_id=run.id,
            commit_sha=run.commit_sha,
            branch=run.branch,
            is_main=run.is_main,
            variant=run.variant,
            occurred_at=run.occurred_at,
            status=result.status,  # rows come highest attempt first: the final one
            attempts=1,
        )
    return list(runs.values())
