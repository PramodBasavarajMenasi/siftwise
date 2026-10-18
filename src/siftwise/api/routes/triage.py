"""POST /triage: is each failing test a new regression, pre-existing on main, or flaky?
POST /repos/{repo}/triage: the same for every variant of a recorded CI run attempt."""

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
    RunTriageRequest,
    RunTriageResponse,
    RunTriageResultOut,
    TriageRequest,
    TriageResponse,
    TriageResultOut,
    VariantTriageOut,
)
from siftwise.core.selector import ChangeLink, DependsRule, SelectorConfig
from siftwise.core.triage import (
    MAX_FLAKY_EVENTS,
    Change,
    Failure,
    FlakyEvent,
    RunRef,
    Target,
    TriageResult,
    VariantTriage,
    Verdict,
    triage_ci_run,
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
            generated=tuple(body.generated),
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


# Worst first: what a reader must act on, then what can't be told, then what isn't theirs.
VERDICT_ORDER = (Verdict.NEW_REGRESSION, Verdict.UNKNOWN, Verdict.FLAKY, Verdict.PRE_EXISTING)
_VERDICT_RANK = {verdict.value: rank for rank, verdict in enumerate(VERDICT_ORDER)}
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


# Repo names contain "/", hence the path converter.
@router.post("/repos/{repo:path}/triage", responses={404: {"description": "Unknown repo or run"}})
def post_run_triage(
    repo: str,
    body: RunTriageRequest,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> RunTriageResponse:
    """Triage every recorded variant of a CI run attempt from what its uploads stored: the
    failing tests, the changed files and the change links (``runs.affected``). A run without
    stored links is triaged as an unknown change. For the GitHub Action, after all legs."""
    variants = triage_ci_run(
        session, repo, body.ci_run_id, body.run_attempt, settings.stats_window_days
    )
    if variants is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {repo!r}")
    if not variants:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no runs recorded for CI run {body.ci_run_id} attempt {body.run_attempt}",
        )
    rows = [
        (v.variant, result) for v in variants for result in (v.report.results if v.report else ())
    ]
    by_test: dict[str, list[RunTriageResultOut]] = {}
    for variant, result in rows:
        by_test.setdefault(result.test_id, []).append(
            RunTriageResultOut(variant=variant, **_result(result).model_dump())
        )
    for results in by_test.values():
        results.sort(key=lambda r: (*_rank(r), r.variant or ""))
    tests = sorted(by_test.items(), key=lambda item: (_rank(item[1][0]), item[0]))

    counts = Counter(results[0].verdict for _, results in tests)
    kept: list[RunTriageResultOut] = []
    for _, results in tests:
        if len(kept) + len(results) > body.limit:
            # Whole tests only, so a reader never sees half of a test's variants; a single
            # test with more variants than the limit is the one exception.
            kept = kept or results[: body.limit]
            break
        kept.extend(results)
    return RunTriageResponse(
        repo=repo,
        ci_run_id=body.ci_run_id,
        run_attempt=body.run_attempt,
        window_days=settings.stats_window_days,
        variants=[_variant_out(v) for v in variants],
        failing_tests=len(tests),
        counts={verdict.value: counts[verdict.value] for verdict in VERDICT_ORDER},
        results=kept,
        truncated=len(kept) < len(rows),
    )


def _rank(result: TriageResultOut) -> tuple[int, int]:
    return _VERDICT_RANK[result.verdict], _CONFIDENCE_RANK[result.confidence]


def _variant_out(v: VariantTriage) -> VariantTriageOut:
    results = v.report.results if v.report else []
    counts = Counter(result.verdict for result in results)
    return VariantTriageOut(
        variant=v.variant,
        run_id=v.run_id,
        commit_sha=v.commit_sha,
        as_of=v.report.cut_at if v.report else None,
        changed_files_known=v.changed_files_known,
        links_known=v.links_known,
        failing=len(results),
        counts={verdict.value: counts[verdict] for verdict in VERDICT_ORDER},
    )


def run_ref_out(run: RunRef) -> RunRefOut:
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
            links=[link_out(link) for link in result.links],
            pre_existing=PreExistingOut(
                variant=streak.variant,
                since_sha=streak.since.commit_sha,
                since_run=run_ref_out(streak.since),
                main_runs_failing=streak.main_runs_failing,
                last_main_pass_sha=streak.last_main_pass.commit_sha
                if streak.last_main_pass
                else None,
                message_matches=result.main_message_matches,
            )
            if streak
            else None,
            other_variants=[
                OtherVariantOut(variant=run.variant, run=run_ref_out(run))
                for run in h.other_variants_failing
            ],
            flaky=[
                flaky_event_out(event, matches)
                for event, matches in result.flaky[:MAX_FLAKY_EVENTS]
            ],
            history=HistoryOut(
                seen_before=h.seen,
                failures_in_window=h.failures_in_window,
                last_failed_at=h.last_failed_at,
                last_main_pass=run_ref_out(h.last_main_pass) if h.last_main_pass else None,
                flaky_counts=result.flaky_counts,
            ),
        ),
    )


def link_out(link: ChangeLink) -> LinkOut:
    return LinkOut(
        signal=link.signal, changed=link.changed, reason=link.reason, strength=link.strength.value
    )


def flaky_event_out(event: FlakyEvent, message_matches: bool | None = None) -> FlakyEventOut:
    return FlakyEventOut(
        kind=event.kind,
        commit_sha=event.commit_sha,
        variant=event.variant,
        failed=[run_ref_out(r) for r in event.failed],
        passed=[run_ref_out(r) for r in event.passed],
        message_matches=message_matches,
    )
