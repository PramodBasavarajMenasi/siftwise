"""POST /select: which tests to run for a change (and, for a shadow run, store the selection)."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

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
    ImportDepths,
    JavaBuildInfo,
    JsImport,
    JsImports,
    Selection,
    SelectorConfig,
    load_history,
    select_tests,
)
from siftwise.core.shadow import STORAGE_ERROR, ShadowTarget, store_selection
from siftwise.db import get_session

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/select", responses={404: {"description": "Unknown repo"}})
def post_select(
    body: SelectRequest, session: Annotated[Session, Depends(get_session)]
) -> SelectResponse:
    """Select tests for a change. Falls back to the full suite whenever unsure. With
    ``shadow``, also stores the selection for that (full-suite) run's recall."""
    config = SelectorConfig(
        always_run=tuple(body.always_run),
        depends=tuple(DependsRule(rule.tests, tuple(rule.on)) for rule in body.depends),
        go_module=body.go_module,
        go_modules=tuple(sorted((body.go_modules or {}).items())),
        generated=tuple(body.generated),
    )
    history = load_history(session, body.repo, config)
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
        import_depths=None
        if body.import_depths is None
        else ImportDepths(
            files=dict(body.import_depths.affected_files),
            packages=dict(body.import_depths.affected_packages),
        ),
    )
    shadow_reason = _store_shadow(session, body, selection) if body.shadow else None
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
            total=selection.estimated_ms_total, would_run=selection.estimated_ms_would_run
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
        shadow_stored=None if body.shadow is None else shadow_reason is None,
        shadow_reason=shadow_reason,
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


def _store_shadow(session: Session, body: SelectRequest, selection: Selection) -> str | None:
    """Store a shadow run's selection; None, or why it wasn't stored. Never fails the request:
    the caller runs the full suite whatever happens, and an unstored selection only means the
    run isn't counted."""
    assert body.shadow is not None
    shadow = body.shadow
    target = ShadowTarget(shadow.ci_run_id, shadow.run_attempt, shadow.variant, shadow.commit_sha)
    try:
        return store_selection(session, body.repo, target, selection)
    except SQLAlchemyError:
        logger.exception("shadow selection not stored for %s run %s", body.repo, shadow.ci_run_id)
        session.rollback()
        return STORAGE_ERROR
