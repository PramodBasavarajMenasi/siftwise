"""Pydantic request/response schemas."""

from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
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


class SelectRequest(ChangeRequest):
    """Body of ``POST /select``."""

    always_run: list[Glob] = Field(default_factory=list, max_length=1000)
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
    stats: TestStatsOut
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
    window_days: int
    selection: ExplainSelectionOut
