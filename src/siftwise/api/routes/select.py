"""POST /select: which tests to run for a change; from CI (``ci``) or for a shadow run, it
also stores the selection for that CI run."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from siftwise.api.deps import Caller, authorize
from siftwise.core.risk import Order
from siftwise.core.schemas import (
    BudgetOut,
    EstimatedMs,
    SelectedTestOut,
    SelectRequest,
    SelectResponse,
)
from siftwise.core.selector import (
    DependsRule,
    JavaBuildInfo,
    JsImport,
    JsImports,
    Selection,
    SelectorConfig,
    load_history,
    select_tests,
)
from siftwise.core.shadow import (
    READ_ONLY_TOKEN,
    STORAGE_ERROR,
    CiRun,
    ShadowTarget,
    store_selection,
)
from siftwise.core.tokens import Principal
from siftwise.db import get_session

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/select", responses={404: {"description": "Unknown repo"}})
def post_select(
    body: SelectRequest, session: Annotated[Session, Depends(get_session)], caller: Caller
) -> SelectResponse:
    """Select tests for a change. Falls back to the full suite whenever unsure. With ``ci``
    (the GitHub Action, the GitLab template), also records the selection for that CI run, if
    the token can write (a read token still gets its selection, ``recorded: false``). With
    ``shadow``, stores it for that (full-suite) run's recall: that needs a write token."""
    authorize(caller, body.repo, write=body.shadow is not None, what="storing a shadow selection")
    _same_run(body)
    config = SelectorConfig(
        always_run=tuple(body.always_run),
        depends=tuple(DependsRule(rule.tests, tuple(rule.on)) for rule in body.depends),
        go_module=body.go_module,
        go_modules=tuple(sorted((body.go_modules or {}).items())),
        generated=tuple(body.generated),
    )
    # A CI leg's variant (ci and shadow name the same run: _same_run) sizes the estimate and
    # the budget with that leg's tests; without one, every variant together, as before.
    run = body.shadow or body.ci
    history = load_history(session, body.repo, config, run.variant if run is not None else None)
    if history is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {body.repo!r}")
    selection = select_tests(
        history,
        body.changed_files,
        body.changed_files_known,
        config,
        affected_packages=body.affected_packages,
        affected_files=body.affected_files,
        deleted_files=body.deleted_files,
        junit_dir=body.junit_dir,
        order=Order(body.order),
        budget_ms=None if body.budget_seconds is None else body.budget_seconds * 1000,
        js_imports=None
        if body.js_graph is None
        else JsImports(
            nearest={
                test: JsImport(n.module, n.via, n.depth)
                for test, n in body.js_graph.nearest.items()
            },
            dynamic=dict(body.js_graph.dynamic),
            notes=tuple(body.js_graph.notes),
        ),
        java=_java(body),
        import_depths=body.import_depths,
        fixture_refs=body.fixture_refs,
    )
    reason = _store(session, body, selection, caller)
    return SelectResponse(
        repo=body.repo,
        mode=selection.mode.value,
        reason=selection.reason,
        selected_count=selection.selected_count,
        total_known=selection.total_known,
        command=selection.command,
        commands=list(selection.commands),
        go_packages_run_whole=list(selection.go_packages_run_whole),
        python_files_run_whole=list(selection.python_files_run_whole),
        estimated_ms=EstimatedMs(
            total=selection.estimated_ms_total,
            would_run=selection.estimated_ms_would_run,
            variant=selection.estimated_variant,
        ),
        tests=[
            SelectedTestOut(
                test_id=t.test_id,
                reasons=list(t.reasons),
                reason=t.reason,
                runner=t.runner.value if t.runner else None,
                file_path=t.file_path,
                score=t.score,
                rank=t.rank,
                rank_reason=t.rank_reason,
            )
            for t in selection.tests
        ],
        order=selection.order.value,
        budget_limited=bool(selection.budget and selection.budget.limited),
        budget=_budget(selection),
        shadow_stored=None if body.shadow is None else reason is None,
        shadow_reason=None if body.shadow is None else reason,
        recorded=None if body.ci is None else reason is None,
        record_reason=None if body.ci is None else reason,
    )


def _java(body: SelectRequest) -> JavaBuildInfo | None:
    return None if body.java is None else JavaBuildInfo.from_json(body.java.model_dump())


def _budget(selection: Selection) -> BudgetOut | None:
    budget = selection.budget
    if budget is None:
        return None
    return BudgetOut(
        seconds=budget.budget_ms / 1000,
        applied=budget.applied,
        limited=budget.limited,
        full_suite=budget.full_suite,
        note=budget.note,
        estimated_ms=budget.estimated_ms,
        left_out=budget.left_out,
        left_out_ms=budget.left_out_ms,
        left_out_risk_share=budget.left_out_risk_share,
        unknown_test_ms=budget.unknown_ms,
    )


def _same_run(body: SelectRequest) -> None:
    """``ci`` and ``shadow`` together (a 0.4 client's shadow run) must name one CI run."""
    if body.ci is None or body.shadow is None:
        return
    ci, shadow = body.ci, body.shadow
    if (ci.ci_run_id, ci.run_attempt, ci.variant, ci.commit_sha) != (
        shadow.ci_run_id, shadow.run_attempt, shadow.variant, shadow.commit_sha
    ):  # fmt: skip
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "ci and shadow name different CI runs (run ID, attempt, variant and commit must match)",
        )


def _store(
    session: Session, body: SelectRequest, selection: Selection, caller: Principal
) -> str | None:
    """Store the selection for its CI run: None, or why it wasn't stored. Never fails the
    request: the caller runs its tests whatever happens, and an unstored selection only means
    the dashboard (or recall) doesn't count the run."""
    if body.ci is None and body.shadow is None:
        return None
    if not caller.can_write():  # only with ci alone: shadow needed a write token above
        return READ_ONLY_TOKEN
    run = body.shadow or body.ci
    assert run is not None
    target = ShadowTarget(run.ci_run_id, run.run_attempt, run.variant, run.commit_sha)
    ci = None
    if body.ci is not None:
        ci = CiRun(
            provider=body.ci.provider,
            branch=body.ci.branch,
            is_main=body.ci.is_main,
            pr_number=body.ci.pr_number,
            event=body.ci.event,
            client_version=body.ci.client_version,
            changed_count=len({path for path in body.changed_files if path.strip()}),
        )
    try:
        return store_selection(session, body.repo, target, selection,
                               shadow=body.shadow is not None, ci=ci)  # fmt: skip
    except SQLAlchemyError:
        logger.exception("selection not stored for %s run %s", body.repo, run.ci_run_id)
        session.rollback()
        return STORAGE_ERROR
