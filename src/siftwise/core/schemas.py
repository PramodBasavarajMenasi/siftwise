"""Pydantic request/response schemas."""

from datetime import datetime
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from siftwise.core.junit import Status, normalize_path
from siftwise.core.models import MESSAGE_MAX_BYTES, truncate_utf8

NonEmpty255 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


def _blank_to_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


# Matrix variant of a CI run, e.g. "ubuntu-py3.12". Blank means "no variant" (None).
Variant = Annotated[NonEmpty255 | None, BeforeValidator(_blank_to_none)]


Glob = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
Path4K = Annotated[str, StringConstraints(max_length=4096)]


class DependsRuleIn(BaseModel):
    """A ``[[depends]]`` rule from the repo's .siftwise.toml."""

    model_config = ConfigDict(extra="forbid")

    tests: Glob
    on: list[Glob] = Field(min_length=1, max_length=100)


class ChangeGraph(BaseModel):
    """What a change links to beyond path mapping: the CLI's import graphs and the repo's
    declared rules. Sent with ``/select`` and ``/triage`` (as top-level fields) and stored per
    run (``runs.affected``) so triage can tell later whether a failure was related to it."""

    model_config = ConfigDict(extra="forbid")

    affected_packages: dict[str, list[str]] = Field(
        default_factory=dict,
        max_length=20_000,
        description="Go package -> changed packages it imports, from `go list -deps -test`",
    )
    affected_files: dict[str, list[str]] = Field(
        default_factory=dict,
        max_length=50_000,
        description="Python test file -> changed modules it imports, from a static import graph",
    )
    depends: list[DependsRuleIn] = Field(default_factory=list, max_length=100)
    go_module: NonEmpty255 | None = None
    generated: list[Glob] = Field(
        default_factory=list,
        max_length=100,
        description="Globs of generated files (reports, caches) from .siftwise.toml, on top of "
        "the built-in list: never counted as changed source",
    )


class RunMetadata(BaseModel):
    """JSON metadata sent alongside the JUnit files in ``POST /runs``."""

    model_config = ConfigDict(extra="forbid")

    repo: NonEmpty255
    commit_sha: Annotated[
        str,
        # Full 40-char SHA-1 only: abbreviated SHAs are ambiguous as run identity.
        StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[0-9a-fA-F]{40}$"),
    ]
    branch: NonEmpty255
    is_main: bool
    ci_run_id: NonEmpty255 | None = None
    run_attempt: int = Field(default=1, ge=1)
    variant: Variant = Field(
        default=None,
        description="Matrix leg (OS, Python version...). Each variant of a CI run attempt is "
        "its own run; results are only compared within the same variant.",
    )
    started_at: datetime | None = None
    changed_files: list[str] = Field(default_factory=list)
    changed_files_known: bool = Field(
        default=True,
        description="False if the uploader could not determine the diff; forces full-suite "
        "selection for this run.",
    )
    affected: ChangeGraph | None = Field(
        default=None,
        description="The change's import graphs and declared rules, as sent to /select. Omit "
        "if they weren't computed (or failed): triage then treats this run's relatedness to "
        "its failures as unknown.",
    )
    known_flaky: list[Glob] = Field(
        default_factory=list,
        max_length=1000,
        description="The known_flaky globs triage applies to this run. For a PR, the base "
        "commit's list: a PR must not mark its own failures flaky.",
    )
    known_flaky_changed: bool = Field(
        default=False,
        description="The PR's own known_flaky differs from the list sent (it applies after merge)",
    )
    rerun: int = Field(
        default=0,
        ge=0,
        le=10,
        description="0 = the run itself; n = the n-th re-run of failing tests within the same CI "
        "run attempt and variant (needs ci_run_id)",
    )

    @model_validator(mode="after")
    def _rerun_needs_ci_run(self) -> Self:
        if self.rerun and self.ci_run_id is None:
            raise ValueError("a re-run (rerun > 0) needs ci_run_id")
        return self

    @field_validator("changed_files")
    @classmethod
    def _normalize_changed_files(cls, paths: list[str]) -> list[str]:
        # Normalized and de-duplicated (order kept): (run_id, path) is the table's primary key.
        normalized = (normalize_path(path) for path in paths)
        return list(dict.fromkeys(path for path in normalized if path))


class StatusCounts(BaseModel):
    """Result rows per status. Each retry attempt counts separately."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    error: int = 0
    total: int = 0


class RunResponse(BaseModel):
    run_id: int
    repo: str
    created: bool
    counts: StatusCounts
    stats_deferred: bool = Field(
        default=False,
        description="True if uploaded with defer_rollup: test_stats are stale until "
        "POST /repos/{repo}/rollup",
    )


class RollupResponse(BaseModel):
    repo: str
    tests_updated: int
    window_days: int = Field(description="Stats window (0 = all history)")
    seconds: float


class RunVariantsResponse(BaseModel):
    repo: str
    ci_run_id: str
    run_attempt: int
    variants: list[str | None] = Field(description="Stored variants; null = no variant")


class RunLookupResponse(BaseModel):
    run_id: int
    repo: str
    ci_run_id: str
    run_attempt: int
    variant: str | None
    commit_sha: str
    rerun: int = 0


class ChangeRequest(ChangeGraph):
    """A change as the CLI describes it: the diff plus its ``ChangeGraph``."""

    repo: NonEmpty255
    changed_files: list[Path4K] = Field(default_factory=list, max_length=50_000)
    changed_files_known: bool = Field(
        default=True,
        description="False if the caller could not determine the diff; forces the full suite.",
    )
    deleted_files: list[Path4K] = Field(
        default_factory=list,
        max_length=50_000,
        description="Changed files that no longer exist at head; never named in a command",
    )


Sha40 = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[0-9a-fA-F]{40}$")
]


class ShadowIn(BaseModel):
    """A shadow run: the full suite runs, and the server stores this selection to compare
    with the recorded results (``POST /repos/{repo}/shadow``)."""

    model_config = ConfigDict(extra="forbid")

    ci_run_id: NonEmpty255
    run_attempt: int = Field(default=1, ge=1)
    variant: Variant = Field(
        default=None,
        description="Matrix leg the selection is for; null = every leg of the attempt",
    )
    commit_sha: Sha40 = Field(description="The commit being tested (the run must match it)")


class SelectRequest(ChangeRequest):
    """Body of ``POST /select``."""

    always_run: list[Glob] = Field(default_factory=list, max_length=1000)
    shadow: ShadowIn | None = Field(
        default=None,
        description="Store the selection for a shadow run (one running the full suite)",
    )
    junit_dir: Annotated[str, StringConstraints(min_length=1, max_length=1024)] | None = Field(
        default=None,
        description="Directory for pytest JUnit reports: each pytest command gets "
        "--junitxml=<junit_dir>/siftwise-<n>.xml",
    )


class EstimatedMs(BaseModel):
    total: float = Field(description="Average durations of all known tests (Go: top-level)")
    would_run: float = Field(description="Of those, the tests the commands would run")


class SelectedTestOut(BaseModel):
    test_id: str
    reasons: list[str]
    reason: str = Field(description="All reasons, joined with '; '")
    runner: str | None
    file_path: str | None


class SelectResponse(BaseModel):
    repo: str
    mode: str = Field(description="'full' or 'selective'")
    reason: str
    selected_count: int
    total_known: int
    command: str = Field(description="Shell line to run; empty when no tests are affected")
    commands: list[str]
    go_packages_run_whole: list[str] = Field(
        description="Go packages run without -run (changed, dependent or declared)"
    )
    python_files_run_whole: list[str] = Field(
        default_factory=list,
        description="Python files passed to pytest whole (changed or importing changed code, "
        "collection errors, doctest modules)",
    )
    estimated_ms: EstimatedMs = Field(
        description="Estimated test time from history; full mode runs everything"
    )
    tests: list[SelectedTestOut] = Field(description="Selected tests; empty in full mode")
    shadow_stored: bool | None = Field(
        default=None,
        description="Shadow runs only: the selection was stored (false: this run won't be "
        "counted in recall)",
    )
    shadow_reason: str | None = Field(
        default=None,
        description="Why a shadow selection wasn't stored: 'incomplete_selection' (a selected "
        "test unknown), 'commit_conflict' (the attempt holds another commit's selection) or "
        "'storage_error'; null when stored",
    )


class BrokenVariant(BaseModel):
    variant: str | None
    since_sha: str


class TestStatsOut(BaseModel):
    __test__ = False  # not a pytest test class
    model_config = ConfigDict(from_attributes=True)

    runs: int = Field(description="Runs (one per variant) where the test's outcome wasn't skipped")
    failures: int
    last_failed_at: datetime | None
    flaky_score: float = Field(
        description="Fraction of (commit, variant) pairs with both a failure and a pass"
    )
    avg_duration_ms: float | None
    broken_on_main_since_sha: str | None = Field(
        description="Earliest start of a failing streak on main, across variants"
    )
    broken_on_main_variants: list[BrokenVariant] = Field(
        default_factory=list, description="Each variant currently failing on main"
    )

    @field_validator("broken_on_main_variants", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return value or []


class TestResultOut(BaseModel):
    __test__ = False

    run_id: int
    commit_sha: str
    branch: str
    is_main: bool
    ci_run_id: str | None
    run_attempt: int
    variant: str | None
    occurred_at: datetime
    status: Status
    attempt: int
    duration_ms: int | None
    file_path: str | None = Field(
        description="The test's latest reported file path (results don't keep their own)"
    )
    message: str | None


class TestHistoryResponse(BaseModel):
    __test__ = False

    repo: str
    test_id: str
    stats: TestStatsOut | None = Field(
        description="Rolled-up stats; null when the test has no results in the stats window"
    )
    results: list[TestResultOut] = Field(description="Latest result rows, newest run first")


# --- triage -------------------------------------------------------------------------------


def _truncate_message(value: object) -> object:
    return truncate_utf8(value, MESSAGE_MAX_BYTES) if isinstance(value, str) else value


class FailureIn(BaseModel):
    """A failing test of the run being triaged (its final attempt failed or errored)."""

    model_config = ConfigDict(extra="forbid")

    test_id: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    message: Annotated[str | None, BeforeValidator(_truncate_message)] = Field(
        default=None, description="Failure message; evidence only, never decides a verdict"
    )
    file_path: Path4K | None = Field(default=None, description="The test's file, if reported")


class TriageRequest(ChangeRequest):
    """Body of ``POST /triage``: a run's change and its failing tests."""

    commit_sha: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[0-9a-fA-F]{40}$"),
        ]
        | None
    ) = Field(
        description="The tested commit; null for uncommitted code (a dirty working tree): then "
        "no earlier run tested the same code, and a pass on HEAD never makes a failure flaky",
    )
    variant: Variant = Field(
        default=None, description="Matrix leg: failures are compared with this variant's history"
    )
    ci_run_id: NonEmpty255 | None = Field(
        default=None,
        description="The run's CI run ID. Every upload of this CI run attempt is left out of "
        "the history, and a recorded run of this variant sets the cut-off time.",
    )
    run_attempt: int = Field(default=1, ge=1)
    known_flaky: list[Glob] = Field(
        default_factory=list,
        max_length=1000,
        description="known_flaky globs from .siftwise.toml (test IDs and file paths): a failing "
        "listed test not linked to the change is flaky",
    )
    as_of: datetime | None = Field(
        default=None,
        description="Only history strictly before this time counts (default: the recorded "
        "run's time, else now)",
    )
    failures: list[FailureIn] = Field(min_length=1, max_length=5000)


class LinkOut(BaseModel):
    signal: str = Field(description="Which selector signal links the test to the change")
    changed: str = Field(description="The changed file (or module/package) it goes through")
    reason: str
    strength: str = Field(description="'high', 'medium' or 'low'")


class RunRefOut(BaseModel):
    run_id: int
    commit_sha: str
    branch: str
    variant: str | None
    occurred_at: datetime
    ci_run_id: str | None


class PreExistingOut(BaseModel):
    variant: str | None
    since_sha: str = Field(description="Commit of the first failing main run of the streak")
    since_run: RunRefOut
    main_runs_failing: int = Field(description="Main runs of this variant failing in the streak")
    last_main_pass_sha: str | None = Field(description="Latest main commit where it passed")
    message_matches: bool | None = Field(
        description="The failure message matches main's latest failure (null: nothing to compare)"
    )


class OtherVariantOut(BaseModel):
    variant: str | None
    run: RunRefOut = Field(description="That variant's latest main run, where the test failed")


class FlakyEventOut(BaseModel):
    kind: str = Field(
        description="'same_commit': failed and passed on one commit and variant; "
        "'unrelated_fail_then_pass': failed on a run whose change wasn't linked to it, then "
        "passed on the branch's next run (also unlinked)"
    )
    commit_sha: str = Field(description="The commit it failed on")
    variant: str | None
    failed: list[RunRefOut]
    passed: list[RunRefOut]
    message_matches: bool | None


class HistoryOut(BaseModel):
    seen_before: bool = Field(description="The test has results from before the cut-off")
    failures_in_window: int = Field(description="Runs it failed in (any variant), in the window")
    last_failed_at: datetime | None
    last_main_pass: RunRefOut | None = Field(
        description="Latest main run of this variant it passed"
    )
    flaky_counts: dict[str, int] = Field(description="Flaky events by kind, in the window")
    rerun_passes: list[int] = Field(
        default_factory=list,
        description="Re-runs (numbers) of this CI run attempt and variant where the test passed",
    )
    rerun_failures: list[int] = Field(
        default_factory=list, description="Re-runs where it failed again"
    )


class EvidenceOut(BaseModel):
    links: list[LinkOut] = Field(description="How this change reaches the test (empty: unrelated)")
    pre_existing: PreExistingOut | None
    other_variants: list[OtherVariantOut] = Field(
        description="Other variants failing on main in their latest main run"
    )
    flaky: list[FlakyEventOut] = Field(description="Flaky events, newest first (at most 10)")
    history: HistoryOut


class TriageResultOut(BaseModel):
    test_id: str
    verdict: str = Field(description="'new_regression', 'pre_existing', 'flaky' or 'unknown'")
    confidence: str = Field(description="'high', 'medium' or 'low'")
    reasons: list[str]
    known_flaky: str | None = Field(
        default=None, description="The known_flaky glob the test matches, if any"
    )
    evidence: EvidenceOut


class TriageResponse(BaseModel):
    repo: str
    commit_sha: str | None
    variant: str | None
    as_of: datetime = Field(description="Cut-off: only history strictly before it was used")
    run_id: int | None = Field(description="The recorded run being triaged, if found")
    window_days: int = Field(description="Flaky evidence window (0 = all history)")
    counts: dict[str, int] = Field(description="Results per verdict")
    results: list[TriageResultOut]


class RunTriageRequest(BaseModel):
    """Body of ``POST /repos/{repo}/triage``: a recorded CI run attempt to triage."""

    model_config = ConfigDict(extra="forbid")

    ci_run_id: NonEmpty255
    run_attempt: int = Field(default=1, ge=1)
    limit: int = Field(
        default=500,
        ge=1,
        le=2000,
        description="Most results returned (one per failing test and variant), worst verdicts "
        "first and never splitting a test's variants; counts always cover every failure",
    )
    variants: list[Variant] | None = Field(
        default=None,
        max_length=100,
        description="Only these variants (null in the list = the run without a variant); "
        "default every recorded variant",
    )


class VariantTriageOut(BaseModel):
    variant: str | None
    run_id: int
    commit_sha: str
    as_of: datetime | None = Field(description="Cut-off; null when nothing failed")
    changed_files_known: bool
    links_known: bool = Field(
        description="The run's change links were stored. If not, the change counts as unknown"
    )
    failing: int = Field(description="Failing tests in this variant")
    reruns: int = Field(default=0, description="Re-run uploads of this variant's failing tests")
    known_flaky_changed: bool = Field(
        default=False,
        description="The PR changes known_flaky; the change applies after merge",
    )
    counts: dict[str, int] = Field(description="This variant's failing tests per verdict")


class RunTriageResultOut(TriageResultOut):
    variant: str | None


class RunTriageResponse(BaseModel):
    repo: str
    ci_run_id: str
    run_attempt: int
    window_days: int = Field(description="Flaky evidence window (0 = all history)")
    variants: list[VariantTriageOut] = Field(description="Every recorded variant of the attempt")
    failing_tests: int = Field(description="Distinct failing tests over all variants")
    counts: dict[str, int] = Field(
        description="Distinct failing tests per verdict, each counted once under its worst "
        "verdict over the variants (new_regression, unknown, flaky, pre_existing)"
    )
    results: list[RunTriageResultOut] = Field(
        description="Worst first: tests by their worst verdict and confidence, then test ID"
    )
    truncated: bool = Field(description="Results were cut at the limit (the counts weren't)")


# --- explain ------------------------------------------------------------------------------


class ExplainRequest(ChangeRequest):
    """Body of ``POST /explain``: one test, and the change to explain its selection for."""

    test_id: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    variant: Variant = Field(default=None, description="Matrix leg for the main-streak evidence")
    always_run: list[Glob] = Field(default_factory=list, max_length=1000)
    known_flaky: list[Glob] = Field(
        default_factory=list,
        max_length=1000,
        description="known_flaky globs from .siftwise.toml (test IDs and file paths): a failing "
        "listed test not linked to the change is flaky",
    )


class OutcomeOut(BaseModel):
    run_id: int
    commit_sha: str
    branch: str
    is_main: bool
    variant: str | None
    occurred_at: datetime
    status: Status = Field(description="The final attempt's status")
    attempts: int


class ExplainSelectionOut(BaseModel):
    mode: str = Field(description="The selection's mode for this change: 'full' or 'selective'")
    selected: bool = Field(description="Whether the selection's commands would run this test")
    reasons: list[str] = Field(description="Why it runs (empty when it doesn't)")
    summary: str = Field(description="The selection's reason (why the full suite, or a summary)")
    links: list[LinkOut] = Field(description="How this change reaches the test (empty: it doesn't)")


class ExplainResponse(BaseModel):
    repo: str
    test_id: str
    known: bool = Field(description="The test has recorded results")
    file_path: str | None
    stats: TestStatsOut | None = Field(description="Rolled-up stats over the window (current)")
    recent: list[OutcomeOut] = Field(description="Latest runs it appeared in, newest first")
    flaky: list[FlakyEventOut] = Field(description="Flaky events in the window, newest first")
    flaky_counts: dict[str, int]
    known_flaky: str | None = Field(
        default=None, description="The known_flaky glob the test matches, if any"
    )
    window_days: int
    selection: ExplainSelectionOut


# --- shadow mode --------------------------------------------------------------------------


class ShadowCompareRequest(BaseModel):
    """Body of ``POST /repos/{repo}/shadow``: a recorded shadow CI run attempt to compare."""

    model_config = ConfigDict(extra="forbid")

    ci_run_id: NonEmpty255
    run_attempt: int = Field(default=1, ge=1)


class ShadowSelectionOut(BaseModel):
    variant: str | None = Field(description="null: the selection for every leg")
    commit_sha: str
    mode: str = Field(description="What siftwise would have done: 'full' or 'selective'")
    reason: str
    selected_count: int
    total_known: int
    estimated_ms: EstimatedMs = Field(description="Estimated at select time from history")
    status: str = Field(
        description="'compared', 'no_upload', 'commit_mismatch', 'compare_error' or 'pending'"
    )


class ShadowVariantOut(BaseModel):
    variant: str | None
    run_id: int
    status: str = Field(description="'compared', 'commit_mismatch' or 'no_selection'")
    failing: int
    caught: int
    missed: int
    flaky: int = Field(description="Failures triage calls flaky: neither caught nor missed")
    tests_total: int = Field(description="Tests in this run (Go subtests left out)")
    tests_would_run: int
    ms_total: float = Field(description="Measured durations in this run")
    ms_would_run: float


class ShadowTimeOut(BaseModel):
    total_ms: float = Field(description="Measured test time of the full-suite runs")
    would_run_ms: float = Field(description="Of that, the tests the selection would have run")
    saved: float | None = Field(description="1 - would_run / total; null without durations")


class ShadowMissOut(BaseModel):
    test_id: str
    verdict: str = Field(description="Triage's verdict (the worst over the missed variants)")
    variants: list[str | None] = Field(description="The variants whose selection missed it")
    cause: str | None = Field(
        default=None,
        description="Why it was missed: 'new_test', 'flaky_history', 'config_gap', 'no_link' "
        "or 'unknown'",
    )
    cause_detail: str | None = Field(default=None, description="The evidence, in a line")


class ShadowCompareResponse(BaseModel):
    repo: str
    ci_run_id: str
    run_attempt: int
    shadow: bool = Field(description="A selection was stored: this was a shadow run")
    counted: bool = Field(description="At least one variant was compared")
    mode: str | None = Field(
        description="'selective' if every compared variant's selection was; null if none was"
    )
    selections: list[ShadowSelectionOut]
    variants: list[ShadowVariantOut]
    failing_tests: int = Field(description="Distinct failing tests (flaky ones included)")
    caught: int
    missed: int
    flaky_excluded: int
    missed_by_verdict: dict[str, int]
    missed_tests: list[ShadowMissOut] = Field(description="At most 100")
    time: ShadowTimeOut


class RecallStatsOut(BaseModel):
    runs: int = Field(description="Shadow CI run attempts")
    failures: int = Field(description="Failing tests, one per test and run attempt")
    flaky_excluded: int = Field(description="Failures triage called flaky: not in recall")
    caught: int
    missed: int
    recall: float | None = Field(description="caught / (caught + missed); null with neither")
    missed_by_verdict: dict[str, int]
    missed_by_cause: dict[str, int] = Field(
        description="new_test, flaky_history, config_gap, no_link, unknown; plus "
        "'unclassified' for misses compared before causes existed"
    )


class RecallMissOut(BaseModel):
    test_id: str
    ci_run_id: str
    run_attempt: int
    commit_sha: str
    variants: list[str | None]
    verdict: str
    selection_mode: str
    at: datetime = Field(description="When the run happened")
    cause: str | None = Field(description="Why it was missed; null if unclassified")
    cause_detail: str | None
    suggestion: str | None = Field(
        description="A .siftwise.toml [[depends]] rule to consider (config gaps only)"
    )


class SuggestionOut(BaseModel):
    rule: str = Field(description="A .siftwise.toml [[depends]] rule, as TOML. Never applied")
    misses: int = Field(description="Config-gap misses it would have caught")
    test_ids: list[str]


class RecallPeriodOut(BaseModel):
    start: datetime = Field(description="Monday 00:00 UTC")
    end: datetime = Field(description="The next Monday 00:00 UTC")
    shadow_runs: int
    selective_runs: int
    full_runs: int
    all: RecallStatsOut
    selective: RecallStatsOut
    time: ShadowTimeOut


class RecallResponse(BaseModel):
    repo: str
    days: int
    since: datetime
    shadow_runs: int = Field(description="Compared shadow CI run attempts")
    selective_runs: int
    full_runs: int = Field(description="Shadow runs where siftwise would have run everything")
    not_counted: dict[str, int] = Field(
        description="Stored selections never compared, by reason: no_upload, commit_mismatch, "
        "compare_error, cancelled (the compare never ran, 6+ hours after select)"
    )
    in_progress: int = Field(description="Selections from the last 6 hours not compared yet")
    all: RecallStatsOut = Field(description="Every shadow run")
    selective: RecallStatsOut = Field(description="Selective shadow runs only")
    time: ShadowTimeOut
    misses: list[RecallMissOut] = Field(description="Newest first, at most 100")
    misses_truncated: bool
    suggestions: list[SuggestionOut] = Field(
        description="Rules that would have caught config-gap misses, most misses first"
    )
    linked_not_selected: int = Field(
        description="Misses linked to the change but not selected: possibly a selector bug"
    )
    periods: list[RecallPeriodOut] | None = Field(
        default=None, description="With by=week: each week, oldest first, empty ones included"
    )
