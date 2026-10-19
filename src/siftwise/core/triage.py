"""Failure triage: is a failing test a new regression, pre-existing on main, or flaky?

``triage`` is pure: given each failing test's ``TestHistory`` and the run's ``Change``, it
returns a verdict with a confidence, reasons and evidence. ``load_histories`` builds those
histories from the database, and ``triage_run`` does both.

**No peeking.** Only history strictly before the triaged run counts: runs earlier by
``(Run.occurred_at, id)`` than the cut-off (the recorded run, else ``as_of``, else now), and
never an upload of the triaged CI run attempt itself (its other matrix legs included).
Triaging an old run gives the answer it would have got live. One exception, for rule 1 only:
the attempt's own re-runs of this variant (``runs.rerun`` > 0, uploaded by the Action after
the run), which re-test the same code.

**Variants.** A failure is compared with its own variant's history: Linux failing on main
doesn't make a macOS failure pre-existing. Other variants failing on main are only noted.

Rules, in order; the first that applies decides:

1. The same commit and variant passed before (a re-run or a retry), or passed on one of
   this attempt's re-runs -> **flaky**, high. Never for uncommitted code
   (``Target.commit_sha`` None): no earlier run tested it.
2. Failing on main for this variant, at the latest main run before the cut-off ->
   **pre-existing**: high if the streak is 2+ main runs or the message matches main's, else
   medium; medium at most if this change also links to the test. Listed in ``known_flaky``:
   a note only.
3. The diff is unknown -> **unknown** (nothing can be linked).
3b. Listed in ``known_flaky`` (``.siftwise.toml``) -> linked to this change: **unknown**
   ("re-run to tell"); else **flaky**, medium (high with flaky evidence rule 7 calls high).
4. No history before this run -> **new regression** (medium) if the test's own file changed,
   else **unknown**.
5. Linked to this change and known flaky -> **unknown** ("re-run to tell").
6. Linked to this change -> **new regression**, as strong as the strongest link (one level
   lower without a passing main run of this variant to compare with).
7. Not linked, with flaky evidence -> **flaky**: high with 2+ events or a matching message,
   else medium.
8. Not linked, no flaky evidence -> **unknown**.

"Linked" is the selector's change-based mapping (``selector.change_links``). Flaky evidence
lies in the stats window (``SIFTWISE_STATS_WINDOW_DAYS`` before the newest run before the
cut-off) and comes in two kinds, kept apart:

* ``same_commit``: on one commit and variant, the test both failed and passed (the flaky
  score's definition);
* ``unrelated_fail_then_pass``: it failed on a run whose change didn't link to it, then passed
  on the next run of the same branch and variant (by ``occurred_at``; the next one with a
  different commit, since same-commit re-runs are the first kind), whose change didn't link to
  it either. Both runs need ``changed_files_known`` and stored change links (``runs.affected``):
  without them a run's relatedness is unknown, and it is never evidence.

Failure messages are evidence only: normalised (numbers, addresses, temp paths, timestamps
stripped), a match strengthens a verdict; a mismatch is noted but never changes one.

The loader runs a fixed number of batched queries whatever the number of failures (see
``load_histories``): 500 failures on rdflib take about 0.25 s warm, 1 s cold
(``scripts/bench_triage.py``).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from siftwise.core.models import FAILED_CODES_SQL, PASSED_CODE, SKIPPED_CODE
from siftwise.core.selector import (
    LINK_PATH,
    LINK_TEST_FILE,
    ChangeLink,
    DependsRule,
    KnownTest,
    SelectorConfig,
    Strength,
    change_links,
    glob_match,
)


class Verdict(StrEnum):
    NEW_REGRESSION = "new_regression"
    PRE_EXISTING = "pre_existing"
    FLAKY = "flaky"
    UNKNOWN = "unknown"


# FlakyEvent.kind values.
SAME_COMMIT = "same_commit"
UNRELATED_FAIL_THEN_PASS = "unrelated_fail_then_pass"

# Flaky events listed per result (all are counted).
MAX_FLAKY_EVENTS = 10

# 0 means "no window": a span longer than any repo's history (as in core/history.py).
_ALL_HISTORY_DAYS = 365 * 100

# For runs stored without change links (runs.affected NULL): a run id -> its links (as
# runs.affected would hold them), or None if unknown. Only the eval passes one, to give
# backfilled history the links a live recording would have stored; production never does.
RunLinks = Callable[[int], Mapping[str, Any] | None]


@dataclass(frozen=True)
class RunRef:
    run_id: int
    commit_sha: str
    branch: str
    variant: str | None
    run_at: datetime
    ci_run_id: str | None = None


@dataclass(frozen=True)
class Change:
    """The triaged run's change, as ``/select`` gets it."""

    changed_files: tuple[str, ...]
    changed_files_known: bool
    config: SelectorConfig = field(default_factory=SelectorConfig)
    affected_packages: Mapping[str, Sequence[str]] = field(default_factory=dict)
    affected_files: Mapping[str, Sequence[str]] = field(default_factory=dict)
    deleted_files: tuple[str, ...] = ()

    def links(self, tests: Iterable[KnownTest]) -> dict[str, tuple[ChangeLink, ...]]:
        return change_links(
            tests,
            self.changed_files,
            self.config,
            self.affected_packages,
            self.affected_files,
            self.deleted_files,
        )

    @classmethod
    def from_graph(cls, changed_files: Iterable[str], graph: Mapping[str, Any]) -> Change:
        """A recorded run's change: its changed files and ``runs.affected``."""
        config = SelectorConfig(
            go_module=graph.get("go_module"),
            depends=tuple(
                DependsRule(rule["tests"], tuple(rule["on"])) for rule in graph.get("depends", ())
            ),
            generated=tuple(graph.get("generated") or ()),
        )
        return cls(
            tuple(changed_files),
            True,
            config,
            graph.get("affected_packages") or {},
            graph.get("affected_files") or {},
        )


@dataclass(frozen=True)
class Failure:
    test_id: str
    message: str | None = None
    file_path: str | None = None


@dataclass(frozen=True)
class Streak:
    """A failing streak on main, for one variant."""

    variant: str | None
    since: RunRef  # the streak's oldest failing main run
    main_runs_failing: int
    last_main_pass: RunRef | None
    latest_message: str | None  # the newest failure's message


@dataclass(frozen=True)
class FlakyEvent:
    kind: str
    commit_sha: str  # the commit it failed on
    variant: str | None
    failed: tuple[RunRef, ...]
    passed: tuple[RunRef, ...]
    messages: tuple[str, ...] = ()  # messages of the failures


@dataclass(frozen=True)
class TestHistory:
    """What history before the cut-off says about one test (one variant for main)."""

    __test__ = False  # not a pytest test class

    seen: bool = False
    file_path: str | None = None
    broken_on_main: Streak | None = None
    last_main_pass: RunRef | None = None
    other_variants_failing: tuple[RunRef, ...] = ()  # latest main run of each, where it failed
    same_commit_passes: tuple[RunRef, ...] = ()  # this commit and variant, any attempt passed
    flaky: tuple[FlakyEvent, ...] = ()  # in the window, newest first
    failures_in_window: int = 0
    last_failed_at: datetime | None = None
    # This CI run attempt's re-runs of the variant (``runs.rerun`` numbers): passed / failed.
    rerun_passes: tuple[int, ...] = ()
    rerun_failures: tuple[int, ...] = ()


@dataclass(frozen=True)
class TriageResult:
    test_id: str
    verdict: Verdict
    confidence: Strength
    reasons: tuple[str, ...]
    links: tuple[ChangeLink, ...]
    history: TestHistory
    main_message_matches: bool | None = None
    # (event, whether the failure's message matches one of the event's)
    flaky: tuple[tuple[FlakyEvent, bool | None], ...] = ()
    known_flaky: str | None = None  # the known_flaky glob it matches

    @property
    def flaky_counts(self) -> dict[str, int]:
        counts = Counter(event.kind for event in self.history.flaky)
        return {kind: counts[kind] for kind in (SAME_COMMIT, UNRELATED_FAIL_THEN_PASS)}


# --- messages -----------------------------------------------------------------------------

_MESSAGE_NOISE = (
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<id>"),
    (
        re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "<time>",
    ),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<addr>"),
    (
        re.compile(
            r"(?:/tmp/|/var/folders/|/private/var/|[A-Za-z]:[\\/](?:[^\s'\"]*[\\/])?Temp[\\/])"
            r"[^\s'\":]*",
            re.I,
        ),
        "<tmp>",
    ),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
)


def normalize_message(message: str) -> str:
    """A failure message without what changes from run to run: ids, timestamps, addresses,
    temp paths and numbers (so ``assert 2 == 3`` and ``assert 5 == 7`` match)."""
    for pattern, replacement in _MESSAGE_NOISE:
        message = pattern.sub(replacement, message)
    return " ".join(message.split())[:1000]


def messages_match(a: str | None, b: str | None) -> bool | None:
    """Whether two failure messages are the same failure; None if either is missing."""
    if not a or not b or not a.strip() or not b.strip():
        return None
    return normalize_message(a) == normalize_message(b)


def _matches_any(message: str | None, others: Sequence[str]) -> bool | None:
    results = [m for m in (messages_match(message, other) for other in others) if m is not None]
    return any(results) if results else None


# --- verdicts -----------------------------------------------------------------------------


def triage(
    failures: Sequence[Failure],
    histories: Mapping[str, TestHistory],
    change: Change,
    window_days: int = 90,
    variant: str | None = None,
    known_flaky: Sequence[str] = (),
) -> list[TriageResult]:
    """A verdict for each failing test (the first of duplicate test IDs counts). ``variant``
    is the triaged run's, for wording only: ``histories`` already compare within it.
    ``known_flaky``: the repo's globs, matched against test IDs and file paths."""
    unique_by_id: dict[str, Failure] = {}
    for failure in failures:
        unique_by_id.setdefault(failure.test_id, failure)
    unique = list(unique_by_id.values())
    empty = TestHistory()
    tests = [
        KnownTest(f.test_id, f.file_path or histories.get(f.test_id, empty).file_path)
        for f in unique
    ]
    links = change.links(tests) if change.changed_files_known else {}
    return [
        _verdict(
            f,
            histories.get(f.test_id, empty),
            links.get(f.test_id, ()),
            change.changed_files_known,
            window_days,
            variant,
            known_flaky_match(test, known_flaky),
        )
        for f, test in zip(unique, tests, strict=True)
    ]


def known_flaky_match(test: KnownTest, patterns: Sequence[str]) -> str | None:
    """The first ``known_flaky`` glob matching the test's ID or file path (as ``always_run``)."""
    return next(
        (
            pattern
            for pattern in patterns
            if glob_match(test.test_id, pattern)
            or (test.file_path and glob_match(test.file_path, pattern))
        ),
        None,
    )


def _verdict(
    failure: Failure,
    h: TestHistory,
    links: tuple[ChangeLink, ...],
    diff_known: bool,
    window_days: int,
    variant: str | None,
    listed: str | None = None,
) -> TriageResult:
    window = f"in {window_days} days" if window_days else "in the history"
    flaky = tuple((event, _matches_any(failure.message, event.messages)) for event in h.flaky)
    links = tuple(sorted(links, key=lambda link: -link.strength.rank))
    strongest = links[0] if links else None
    notes = [
        f"also failing on main on {_variant(run.variant)} (latest main run {run.commit_sha[:7]})"
        for run in h.other_variants_failing
    ]
    if h.rerun_failures:
        notes.append(f"failed again on {_plural(len(h.rerun_failures), 're-run')}")
    listed_reason = f"listed in known_flaky ({listed!r})" if listed else ""

    def done(
        verdict: Verdict, confidence: Strength, reasons: list[str], main_match: bool | None = None
    ) -> TriageResult:
        return TriageResult(
            failure.test_id,
            verdict,
            confidence,
            tuple(reasons + notes),
            links,
            h,
            main_match,
            flaky,
            listed,
        )

    def linked() -> str:
        assert strongest is not None
        more = f" (+{len(links) - 1} more)" if len(links) > 1 else ""
        return f"{strongest.reason}{more}"

    # 1. Same code, different outcome.
    if h.rerun_passes:
        return done(
            Verdict.FLAKY,
            Strength.HIGH,
            [
                f"passed on re-run {h.rerun_passes[0]} of this commit and variant: same code, "
                "different outcome",
            ],
        )
    if h.same_commit_passes:
        run = h.same_commit_passes[0]
        return done(
            Verdict.FLAKY,
            Strength.HIGH,
            [
                f"passed on this commit and variant before (run {run.run_id}): same code, "
                "different outcome",
            ],
        )

    # 2. Already failing on main for this variant.
    streak = h.broken_on_main
    if streak is not None:
        match = messages_match(failure.message, streak.latest_message)
        confidence = Strength.HIGH if streak.main_runs_failing >= 2 or match else Strength.MEDIUM
        reasons = [
            f"failing on main{_on(streak.variant)} since {streak.since.commit_sha[:7]} "
            f"({_plural(streak.main_runs_failing, 'main run')})",
            f"last passed on main at {streak.last_main_pass.commit_sha[:7]}"
            if streak.last_main_pass
            else "no passing main run before the streak",
        ]
        if match:
            reasons.append("same failure message as on main")
        elif match is False:
            reasons.append("main fails with a different error")
        if strongest is not None:
            confidence = Strength.MEDIUM if confidence is Strength.HIGH else confidence
            reasons.append(f"this change also touches it: {linked()}")
        if listed:
            reasons.append(listed_reason)
        return done(Verdict.PRE_EXISTING, confidence, reasons, match)

    flaky_note = _flaky_note(h.flaky, window)
    # 3. Nothing can be linked without the diff.
    if not diff_known:
        if listed:
            return done(
                Verdict.UNKNOWN,
                Strength.LOW,
                [f"{listed_reason}, but the diff is unknown: re-run to tell"],
            )
        if h.flaky:
            return done(
                Verdict.UNKNOWN,
                Strength.LOW,
                [
                    f"known flaky ({flaky_note}), but the diff is unknown: re-run to tell",
                ],
            )
        return done(
            Verdict.UNKNOWN,
            Strength.LOW,
            [
                "diff unknown: can't tell if this change caused it",
            ],
        )

    # 3b. Declared flaky: the repo's own list, after the evidence rules 1-2.
    strong_flaky = len(h.flaky) >= 2 or any(match for _, match in flaky)
    if listed:
        if strongest is not None:
            return done(
                Verdict.UNKNOWN,
                Strength.LOW,
                [
                    "linked to this change but listed in known_flaky: re-run to tell",
                    linked(),
                    listed_reason,
                ],
            )
        reasons = [listed_reason, "not linked to this change"]
        if h.flaky:
            reasons.append(flaky_note)
        if any(match for _, match in flaky):
            reasons.append("same failure message as an earlier flaky failure")
        return done(Verdict.FLAKY, Strength.HIGH if strong_flaky else Strength.MEDIUM, reasons)

    # 4. No history to compare with.
    if not h.seen:
        # Its own file: the test file, or for Go (no file reported) a changed _test.go in its
        # package, where a new test has to live.
        own = next(
            (
                link
                for link in links
                if link.signal == LINK_TEST_FILE
                or (link.signal == LINK_PATH and link.changed.endswith("_test.go"))
            ),
            None,
        )
        if own is not None:
            return done(
                Verdict.NEW_REGRESSION,
                Strength.MEDIUM,
                [
                    f"new test: no history before this run, and its file changed ({own.changed})",
                ],
            )
        reasons = ["no history for this test before this run"]
        if strongest is not None:
            reasons.append(f"linked to this change ({linked()}), but there's nothing to compare")
        return done(Verdict.UNKNOWN, Strength.LOW, reasons)

    # 5-6. Linked to this change.
    on = _on(variant)
    if strongest is not None:
        if h.flaky:
            return done(
                Verdict.UNKNOWN,
                Strength.LOW,
                [
                    "linked to this change but known flaky: re-run to tell",
                    linked(),
                    flaky_note,
                ],
            )
        confidence = strongest.strength
        reasons = [linked()]
        if h.last_main_pass is not None:
            reasons.append(f"passes on main{on} (last main run {h.last_main_pass.commit_sha[:7]})")
        else:
            confidence = _weaker(confidence)
            reasons.append(f"no passing main run{on} to compare with")
        reasons.append(f"no flaky history {window}")
        return done(Verdict.NEW_REGRESSION, confidence, reasons)

    # 7-8. Not linked to this change.
    if h.flaky:
        reasons = [flaky_note, "not linked to this change"]
        if any(match for _, match in flaky):
            reasons.append("same failure message as an earlier flaky failure")
        return done(Verdict.FLAKY, Strength.HIGH if strong_flaky else Strength.MEDIUM, reasons)
    return done(
        Verdict.UNKNOWN,
        Strength.LOW,
        [
            "not linked to this change, and nothing shows it's flaky: infrastructure, the "
            "environment, or a dependency no signal sees",
        ],
    )


def _flaky_note(events: Sequence[FlakyEvent], window: str) -> str:
    if not events:
        return f"no flaky history {window}"
    counts = Counter(event.kind for event in events)
    kinds = [
        f"{counts[SAME_COMMIT]} same-commit" if counts[SAME_COMMIT] else "",
        f"{counts[UNRELATED_FAIL_THEN_PASS]} unrelated fail-then-pass"
        if counts[UNRELATED_FAIL_THEN_PASS]
        else "",
    ]
    return f"flaky {_plural(len(events), 'time')} {window} ({', '.join(k for k in kinds if k)})"


def _weaker(strength: Strength) -> Strength:
    return Strength.MEDIUM if strength is Strength.HIGH else Strength.LOW


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _variant(variant: str | None) -> str:
    return variant if variant else "the default variant"


def _on(variant: str | None) -> str:
    return f" on {variant}" if variant else ""


# --- history from the database ------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """The run being triaged."""

    repo: str
    # None: uncommitted code (a dirty working tree), so no earlier run tested the same code
    # and rule 1 can't apply.
    commit_sha: str | None
    variant: str | None = None
    ci_run_id: str | None = None
    run_attempt: int = 1
    as_of: datetime | None = None


@dataclass(frozen=True)
class Histories:
    cut_at: datetime  # only history strictly before this counts
    run_id: int | None  # the recorded run being triaged, if found
    tests: dict[str, TestHistory]


@dataclass(frozen=True)
class TriageReport:
    cut_at: datetime
    run_id: int | None
    results: list[TriageResult]


@dataclass(frozen=True)
class _Run:
    id: int
    commit_sha: str
    branch: str
    is_main: bool
    variant: str | None
    run_at: datetime
    ci_run_id: str | None
    changed_files_known: bool
    has_links: bool
    rerun: int = 0

    @property
    def ref(self) -> RunRef:
        return RunRef(
            self.id, self.commit_sha, self.branch, self.variant, self.run_at, self.ci_run_id
        )

    @property
    def order(self) -> tuple[datetime, int]:
        return (self.run_at, self.id)


# A run id above any real one: with no recorded run, the cut-off is a time alone.
_NO_RUN = 2**63 - 1

# Runs strictly before the cut-off, never an upload of the triaged CI run attempt.
_BEFORE = """
    (COALESCE(r.started_at, r.created_at), r.id) < (:cut_at, :cut_id)
    AND (CAST(:ci_run_id AS text) IS NULL OR r.ci_run_id IS DISTINCT FROM CAST(:ci_run_id AS text)
         OR r.run_attempt <> :run_attempt)
"""
_OUTCOME = "max(tr.attempt * 4 + tr.status) % 4"  # final attempt's status (core/history.py)
_FAILED = {int(code) for code in FAILED_CODES_SQL.strip("()").split(",")}

# Main runs read per round, newest first: most tests resolve (pass) in the first.
_MAIN_CHUNKS = (10, 50)


def load_histories(
    session: Session,
    target: Target,
    test_ids: Sequence[str],
    window_days: int = 90,
    run_links: RunLinks | None = None,
) -> Histories | None:
    """Each test's history before the cut-off, or None if the repo is unknown: the one-target
    case of ``load_histories_many``."""
    loaded = load_histories_many(session, [(target, test_ids)], window_days, run_links)
    return None if loaded is None else loaded[0]


def load_histories_many(
    session: Session,
    requests: Sequence[tuple[Target, Sequence[str]]],
    window_days: int = 90,
    run_links: RunLinks | None = None,
) -> list[Histories] | None:
    """Each target's tests' histories before its own cut-off (in ``requests`` order), or None
    if the repo is unknown. Every target must be in the same repo.

    A fixed number of batched queries, whatever the number of targets and tests: the cut-offs,
    the runs before them (metadata only, each tagged with the targets whose window it is in),
    test keys, which tests have any earlier result, their failures (a partial index by test),
    the outcomes flaky evidence needs (primary-key probes), main outcomes newest first (at most
    three rounds, each run read once for every target's keys, plus one look before the
    windows for streaks older than them), the changes of the runs that flaky evidence depends
    on, and the targets' own re-runs. Each target then keeps only its own history (its
    cut-off, its attempt left out, its window), exactly as if it had been loaded alone.
    Passing results are only read by primary key, for named runs.
    """
    if not requests:
        return []
    repos = {target.repo for target, _ in requests}
    if len(repos) != 1:
        raise ValueError(f"targets of one repo only, got {sorted(repos)}")
    session.execute(text("SET LOCAL jit = off"))  # compiling costs more than these queries
    repo_id = session.execute(
        text("SELECT id FROM repos WHERE name = :name"), {"name": repos.pop()}
    ).scalar_one_or_none()
    if repo_id is None:
        return None
    now = datetime.now(UTC)
    loads = [_Load.of(i, target, test_ids, now) for i, (target, test_ids) in enumerate(requests)]
    days = window_days if window_days > 0 else _ALL_HISTORY_DAYS
    batch = _Batch(session, repo_id, loads, days, run_links)
    batch.cut_offs()
    batch.load_runs()
    batch.load_tests()
    batch.build()
    batch.add_reruns()
    return [Histories(load.cut_at, load.run_id, load.histories) for load in loads]


@dataclass
class _Load:
    """One target of a batched load, and what was loaded for it."""

    idx: int
    target: Target
    test_ids: list[str]  # unique, in request order
    cut_at: datetime
    cut_id: int = _NO_RUN
    run_id: int | None = None
    has_reruns: bool = False
    runs: dict[int, _Run] = field(default_factory=dict)
    window_start: datetime | None = None
    keys: dict[int, str] = field(default_factory=dict)
    paths: dict[str, str | None] = field(default_factory=dict)
    seen: set[int] = field(default_factory=set)
    histories: dict[str, TestHistory] = field(default_factory=dict)

    @classmethod
    def of(cls, idx: int, target: Target, test_ids: Sequence[str], now: datetime) -> _Load:
        cut_at = target.as_of or now
        if cut_at.tzinfo is None:
            cut_at = cut_at.replace(tzinfo=UTC)
        return cls(idx, target, list(dict.fromkeys(test_ids)), cut_at)


# The targets of a batch as a table, ``t(idx, cut_at, cut_id, ci, attempt, variant,
# window_start, commit)``, and the cut-off condition (``_BEFORE``) per target.
_TARGETS = """
    unnest(CAST(:t_idx AS integer[]), CAST(:t_cut_at AS timestamptz[]),
           CAST(:t_cut_id AS bigint[]), CAST(:t_ci AS text[]), CAST(:t_attempt AS integer[]),
           CAST(:t_variant AS text[]), CAST(:t_window_start AS timestamptz[]),
           CAST(:t_commit AS text[]))
        AS t(idx, cut_at, cut_id, ci, attempt, variant, window_start, commit)
"""
_BEFORE_T = """
    (COALESCE(r.started_at, r.created_at), r.id) < (t.cut_at, t.cut_id)
    AND (t.ci IS NULL OR r.ci_run_id IS DISTINCT FROM t.ci OR r.run_attempt <> t.attempt)
"""


class _Batch:
    """The queries behind ``load_histories_many``: each reads the union of what the targets
    need; each target's ``_Builder`` keeps its own part."""

    def __init__(
        self,
        session: Session,
        repo_id: int,
        loads: list[_Load],
        days: int,
        run_links: RunLinks | None,
    ) -> None:
        self.session = session
        self.repo_id = repo_id
        self.loads = loads
        self.days = days
        self.run_links = run_links

    def _targets(self, loads: Sequence[_Load] | None = None) -> dict[str, Any]:
        loads = self.loads if loads is None else loads
        return {
            "t_idx": [load.idx for load in loads],
            "t_cut_at": [load.cut_at for load in loads],
            "t_cut_id": [load.cut_id for load in loads],
            "t_ci": [load.target.ci_run_id for load in loads],
            "t_attempt": [load.target.run_attempt for load in loads],
            "t_variant": [load.target.variant for load in loads],
            "t_window_start": [load.window_start or load.cut_at for load in loads],
            "t_commit": [load.target.commit_sha for load in loads],
        }

    def cut_offs(self) -> None:
        """A recorded run (by CI run attempt and variant) sets its target's cut-off."""
        recorded = [load for load in self.loads if load.target.ci_run_id is not None]
        if not recorded:
            return
        rows = self.session.execute(
            text(
                f"""
                SELECT t.idx, r.id, COALESCE(r.started_at, r.created_at), EXISTS (
                    SELECT 1 FROM runs x WHERE x.repo_id = r.repo_id
                      AND x.ci_run_id = r.ci_run_id AND x.run_attempt = r.run_attempt
                      AND COALESCE(x.variant, '') = COALESCE(t.variant, '')
                      AND x.rerun > 0 AND x.commit_sha = r.commit_sha)
                FROM {_TARGETS}
                JOIN runs r ON r.repo_id = :repo_id AND r.ci_run_id = t.ci
                    AND r.run_attempt = t.attempt
                    AND COALESCE(r.variant, '') = COALESCE(t.variant, '') AND r.rerun = 0
                """
            ),
            {"repo_id": self.repo_id, **self._targets(recorded)},
        )
        for idx, run_id, run_at, has_reruns in rows:
            load = self.loads[idx]
            load.run_id, load.cut_at, load.cut_id, load.has_reruns = (
                run_id, run_at, run_id, has_reruns)  # fmt: skip

    def load_runs(self) -> None:
        """Every target's window: the runs before its cut-off (its own attempt left out) at
        most ``days`` older than the newest of them. One read, each run tagged with the
        targets whose window it is in."""
        rows = self.session.execute(
            text(
                f"""
                WITH t AS (SELECT * FROM {_TARGETS}),
                before AS MATERIALIZED (  -- narrow: (target, run) pairs
                    SELECT t.idx, r.id, COALESCE(r.started_at, r.created_at) AS run_at
                    FROM t JOIN runs r ON r.repo_id = :repo_id AND {_BEFORE_T}
                ),
                newest AS (SELECT idx, max(run_at) AS run_at FROM before GROUP BY idx),
                windows AS (  -- each run once, with the targets whose window it is in
                    SELECT b.id, array_agg(b.idx) AS targets
                    FROM before b JOIN newest n ON n.idx = b.idx
                    WHERE b.run_at >= n.run_at - make_interval(days => :days)
                    GROUP BY b.id
                )
                SELECT r.id, r.commit_sha, r.branch, r.is_main, r.variant,
                       COALESCE(r.started_at, r.created_at), r.ci_run_id,
                       r.changed_files_known, r.affected IS NOT NULL, r.rerun, w.targets
                FROM windows w JOIN runs r ON r.id = w.id
                """
            ),
            {"repo_id": self.repo_id, "days": self.days, **self._targets()},
        )
        for *columns, targets in rows:
            run = _Run(*columns)
            for idx in targets:
                self.loads[idx].runs[run.id] = run
        for load in self.loads:
            load.window_start = (
                max(r.run_at for r in load.runs.values()) - timedelta(days=self.days)
                if load.runs
                else load.cut_at
            )

    def load_tests(self) -> None:
        """Test keys and paths (as of each cut-off), and which tests ran before it."""
        ids = list(dict.fromkeys(t for load in self.loads for t in load.test_ids))
        found = {
            test_id: (key, file_path, file_path_at)
            for key, test_id, file_path, file_path_at in self.session.execute(
                text(
                    "SELECT id, test_id, file_path, file_path_at FROM tests"
                    " WHERE repo_id = :repo_id AND test_id = ANY(:ids)"
                ),
                {"repo_id": self.repo_id, "ids": ids},
            )
        }
        pairs: list[tuple[int, int]] = []
        for load in self.loads:
            for test_id in load.test_ids:
                if test_id not in found:
                    continue
                key, file_path, file_path_at = found[test_id]
                load.keys[key] = test_id
                # A path reported by the triaged run or a later one would be peeking.
                load.paths[test_id] = (
                    file_path if file_path_at is not None and file_path_at < load.cut_at else None
                )
                pairs.append((load.idx, key))
        if not pairs:
            return
        for idx, key in self.session.execute(
            text(
                f"""
                SELECT p.idx, p.key
                FROM unnest(CAST(:p_idx AS integer[]), CAST(:p_key AS integer[])) AS p(idx, key)
                JOIN {_TARGETS} ON t.idx = p.idx
                WHERE EXISTS (SELECT 1 FROM test_results tr JOIN runs r ON r.id = tr.run_id
                              WHERE tr.test_key = p.key AND {_BEFORE_T})
                """
            ),
            {
                "p_idx": [idx for idx, _ in pairs],
                "p_key": [key for _, key in pairs],
                **self._targets(),
            },
        ):
            self.loads[idx].seen.add(key)

    def build(self) -> None:
        """Every target's histories from shared reads, phase by phase."""
        for load in self.loads:
            load.histories = {
                test_id: TestHistory(file_path=load.paths.get(test_id))
                for test_id in load.keys.values()
            }
        builders = [_Builder(load, self.run_links) for load in self.loads if load.seen]
        if not builders:
            return
        failures = self._failures({key for b in builders for key in b.load.seen})
        pairs: set[tuple[int, int]] = set()
        for b in builders:
            pairs |= b.prepare(failures)
        outcomes = self._outcomes(pairs)

        for b in builders:
            b.main_start()
        for n in range(len(_MAIN_CHUNKS) + 1):
            asked = {b: b.main_request(n) for b in builders}
            main = self._main_outcomes({b: r for b, r in asked.items() if r is not None})
            for b, request in asked.items():
                if request is not None:
                    b.main_apply(n, main)
        older = {b: keys for b in builders if (keys := b.older_keys())}
        if older:
            for b, rows in self._older_main(older).items():
                b.older_apply(rows)

        candidates = {b: b.unrelated_candidates(outcomes) for b in builders}
        changes = self._run_changes({r.id for c in candidates.values() for x in c for r in x[:2]})
        for b in builders:
            b.finish(outcomes, candidates[b], changes)

    def add_reruns(self) -> None:
        """Each recorded target's own re-runs of its variant: rule 1's "same code, different
        outcome". Re-runs come after the run, so this is the one read past a cut-off."""
        loads = [load for load in self.loads if load.has_reruns and load.keys]
        if not loads:
            return
        pairs = [(load.idx, key) for load in loads for key in sorted(load.keys)]
        passes: dict[tuple[int, str], list[int]] = defaultdict(list)
        failures: dict[tuple[int, str], list[int]] = defaultdict(list)
        for idx, rerun, key, outcome in self.session.execute(
            text(
                f"""
                SELECT p.idx, r.rerun, tr.test_key, {_OUTCOME}
                FROM unnest(CAST(:p_idx AS integer[]), CAST(:p_key AS integer[])) AS p(idx, key)
                JOIN {_TARGETS} ON t.idx = p.idx
                JOIN runs r ON r.repo_id = :repo_id AND r.ci_run_id = t.ci
                    AND r.run_attempt = t.attempt
                    AND COALESCE(r.variant, '') = COALESCE(t.variant, '')
                    AND r.rerun > 0 AND r.commit_sha = t.commit
                JOIN test_results tr ON tr.run_id = r.id AND tr.test_key = p.key
                GROUP BY p.idx, r.id, r.rerun, tr.test_key
                ORDER BY p.idx, r.rerun
                """
            ),
            {
                "repo_id": self.repo_id,
                "p_idx": [idx for idx, _ in pairs],
                "p_key": [key for _, key in pairs],
                **self._targets(),
            },
        ):
            test_id = self.loads[idx].keys[key]
            if outcome == PASSED_CODE:
                passes[(idx, test_id)].append(rerun)
            elif outcome in _FAILED:
                failures[(idx, test_id)].append(rerun)
        for idx, test_id in passes.keys() | failures.keys():
            histories = self.loads[idx].histories
            histories[test_id] = replace(
                histories[test_id],
                rerun_passes=tuple(passes[(idx, test_id)]),
                rerun_failures=tuple(failures[(idx, test_id)]),
            )

    # -- shared reads --

    def _failures(self, keys: set[int]) -> set[tuple[int, int]]:
        rows = self.session.execute(
            text(
                f"SELECT run_id, test_key FROM test_results"
                f" WHERE status IN {FAILED_CODES_SQL} AND test_key = ANY(:keys)"
            ),
            {"keys": sorted(keys)},
        )
        return {(run_id, key) for run_id, key in rows}

    def _outcomes(self, pairs: set[tuple[int, int]]) -> dict[tuple[int, int], _Outcome]:
        if not pairs:
            return {}
        ordered = sorted(pairs)
        rows = self.session.execute(
            text(
                f"""
                SELECT p.run_id, p.key, {_OUTCOME}, bool_or(tr.status = {PASSED_CODE}),
                       (array_agg(tr.message ORDER BY tr.attempt DESC)
                            FILTER (WHERE tr.status IN {FAILED_CODES_SQL}))[1]
                FROM unnest(CAST(:runs AS bigint[]), CAST(:keys AS integer[])) AS p(run_id, key)
                JOIN test_results tr ON tr.run_id = p.run_id AND tr.test_key = p.key
                GROUP BY p.run_id, p.key
                """
            ),
            {"runs": [r for r, _ in ordered], "keys": [k for _, k in ordered]},
        )
        return {
            (r, k): _Outcome(outcome, passed, message) for r, k, outcome, passed, message in rows
        }

    def _main_outcomes(
        self, requests: Mapping[_Builder, tuple[list[int], list[int]]]
    ) -> dict[tuple[int, int], tuple[int, str | None]]:
        """Final outcomes of (run, test) for every builder's runs and keys. Builders asking
        for the same keys share a key group (a matrix's legs mostly fail the same tests), so
        the request stays as small as one builder's (runs + keys) per distinct key set."""
        groups: dict[frozenset[int], int] = {}
        run_groups: set[tuple[int, int]] = set()
        for run_ids, keys in requests.values():
            group = groups.setdefault(frozenset(keys), len(groups))
            run_groups.update((run_id, group) for run_id in run_ids)
        if not run_groups:
            return {}
        ordered = sorted(run_groups)
        key_groups = sorted((g, k) for keys, g in groups.items() for k in keys)
        rows = self.session.execute(
            text(
                f"""
                WITH g AS (
                    SELECT grp, array_agg(key) AS keys
                    FROM unnest(CAST(:g_grp AS integer[]), CAST(:g_key AS integer[]))
                        AS g(grp, key)
                    GROUP BY grp
                )
                SELECT r.run_id, x.test_key, x.outcome, x.message
                FROM unnest(CAST(:runs AS bigint[]), CAST(:grps AS integer[])) AS r(run_id, grp)
                JOIN g ON g.grp = r.grp
                CROSS JOIN LATERAL (
                    SELECT tr.test_key, {_OUTCOME} AS outcome,
                           (array_agg(tr.message ORDER BY tr.attempt DESC))[1] AS message
                    FROM test_results tr
                    WHERE tr.run_id = r.run_id AND tr.test_key = ANY(g.keys)
                    GROUP BY tr.test_key
                ) x
                """
            ),
            {
                "runs": [r for r, _ in ordered],
                "grps": [g for _, g in ordered],
                "g_grp": [g for g, _ in key_groups],
                "g_key": [k for _, k in key_groups],
            },
        )
        return {(r, k): (outcome, message) for r, k, outcome, message in rows}

    def _older_main(
        self, requests: Mapping[_Builder, list[int]]
    ) -> dict[_Builder, list[tuple[_Run, int, int]]]:
        """Main outcomes of this variant from before each builder's window, for the tests
        failing on main for the whole window (by ``test_key``)."""
        by_idx = {b.load.idx: b for b in requests}
        pairs = [(b.load.idx, key) for b, keys in requests.items() for key in keys]
        rows = self.session.execute(
            text(
                f"""
                SELECT p.idx, r.id, r.commit_sha, r.branch, r.is_main, r.variant,
                       COALESCE(r.started_at, r.created_at) AS run_at, r.ci_run_id,
                       r.changed_files_known, r.affected IS NOT NULL, r.rerun, tr.test_key,
                       {_OUTCOME}
                FROM unnest(CAST(:p_idx AS integer[]), CAST(:p_key AS integer[])) AS p(idx, key)
                JOIN {_TARGETS} ON t.idx = p.idx
                JOIN test_results tr ON tr.test_key = p.key
                JOIN runs r ON r.id = tr.run_id
                WHERE r.repo_id = :repo_id AND r.is_main
                  AND r.variant IS NOT DISTINCT FROM t.variant
                  AND COALESCE(r.started_at, r.created_at) < t.window_start AND {_BEFORE_T}
                GROUP BY p.idx, r.id, tr.test_key
                """
            ),
            {
                "repo_id": self.repo_id,
                "p_idx": [idx for idx, _ in pairs],
                "p_key": [key for _, key in pairs],
                **self._targets(),
            },
        ).all()
        found: dict[_Builder, list[tuple[_Run, int, int]]] = defaultdict(list)
        for row in rows:
            found[by_idx[row[0]]].append((_Run(*row[1:11]), row[11], row[12]))
        for items in found.values():
            items.sort(key=lambda item: item[0].order, reverse=True)
        return found

    def _run_changes(self, run_ids: set[int]) -> dict[int, Change]:
        if not run_ids:
            return {}
        rows = self.session.execute(
            text(
                "SELECT r.id, r.affected,"
                " ARRAY(SELECT c.path FROM changed_files c WHERE c.run_id = r.id)"
                " FROM runs r WHERE r.id = ANY(:ids)"
            ),
            {"ids": sorted(run_ids)},
        )
        changes = {}
        for run_id, graph, changed in rows:
            if graph is None and self.run_links is not None:
                graph = self.run_links(run_id)  # only runs _has_links let through
            changes[run_id] = Change.from_graph(changed, graph)
        return changes


# (failed run, the branch's next run, test key): a candidate unrelated fail-then-pass event.
_Candidate = tuple[_Run, _Run, int]


class _Builder:
    """One target's histories, from the batch's shared reads (no queries of its own)."""

    def __init__(self, load: _Load, run_links: RunLinks | None = None) -> None:
        self.load = load
        self.run_links = run_links
        self.target = load.target
        self.runs = load.runs
        self.by_pair: dict[tuple[str, str | None], list[_Run]] = defaultdict(list)
        self.by_branch: dict[tuple[str, str | None], list[_Run]] = defaultdict(list)
        for run in sorted(self.runs.values(), key=lambda r: r.order):
            self.by_pair[(run.commit_sha, run.variant)].append(run)
            self.by_branch[(run.branch, run.variant)].append(run)
        self.branch_pos = {
            run.id: i for runs_ in self.by_branch.values() for i, run in enumerate(runs_)
        }
        commit = self.target.commit_sha
        self.same_commit = self.by_pair.get((commit, self.target.variant), []) if commit else []
        self.failures: set[tuple[int, int]] = set()
        self.next_runs: dict[int, _Run] = {}

    def prepare(self, failures: set[tuple[int, int]]) -> set[tuple[int, int]]:
        """Keep this target's failures (seen tests, runs in its window); the (run, test)
        outcomes its flaky evidence needs."""
        seen = self.load.seen
        self.failures = {(r, k) for r, k in failures if r in self.runs and k in seen}
        self.next_runs = {
            run_id: nxt for run_id, _ in self.failures if (nxt := self._next_on_branch(run_id))
        }
        pairs = {(r.id, k) for k in seen for r in self.same_commit}
        for run_id, key in self.failures:
            run = self.runs[run_id]
            pairs.update((r.id, key) for r in self.by_pair[(run.commit_sha, run.variant)])
            if run_id in self.next_runs:
                pairs.add((self.next_runs[run_id].id, key))
        return pairs

    # -- the main streak: this variant's main runs, newest first, in rounds --

    def main_start(self) -> None:
        variant = self.target.variant
        self.mains = sorted(
            (r for r in self.runs.values() if r.is_main and r.variant == variant),
            key=lambda r: r.order,
            reverse=True,
        )
        latest_other: dict[str | None, _Run] = {}
        # A re-run holds a few tests only: never a variant's "latest main run".
        for run in sorted((r for r in self.runs.values() if r.is_main), key=lambda r: r.order):
            if run.variant != variant and not run.rerun:
                latest_other[run.variant] = run
        self.others = list(latest_other.values())
        self.walks = {key: _MainWalk() for key in self.load.seen}
        first, second = _MAIN_CHUNKS
        self.rounds = (
            self.mains[:first], self.mains[first : first + second], self.mains[first + second :]
        )  # fmt: skip
        self.pending: list[int] = []

    def main_request(self, n: int) -> tuple[list[int], list[int]] | None:
        """Round ``n``'s (runs, keys) to read, or None if this round has nothing to do: the
        newest main runs first (with each other variant's latest), then older ones for the
        tests that haven't passed yet."""
        chunk = self.rounds[n]
        others = self.others if n == 0 else []
        self.pending = [key for key, walk in self.walks.items() if not walk.resolved]
        if not others and not (chunk and self.pending):
            return None
        keys = sorted(self.walks) if others else self.pending
        return [r.id for r in chunk + others], keys

    def main_apply(
        self, n: int, outcomes: Mapping[tuple[int, int], tuple[int, str | None]]
    ) -> None:
        chunk = self.rounds[n]
        for key in self.pending:
            for run in chunk:
                if self.walks[key].step(run, *outcomes.get((run.id, key), _ABSENT)):
                    break
        if n == 0:
            for run in self.others:
                for key in self.walks:
                    if outcomes.get((run.id, key), _ABSENT)[0] in _FAILED:
                        self.walks[key].others.append(run.ref)

    def older_keys(self) -> list[int]:
        """Failing on main for every main run of the window: the streak may have started
        before it."""
        return [key for key, walk in self.walks.items() if not walk.resolved and walk.failing]

    def older_apply(self, rows: list[tuple[_Run, int, int]]) -> None:
        for run, key, outcome in rows:
            self.walks[key].step(run, outcome, None)

    # -- flaky evidence --

    def unrelated_candidates(
        self, outcomes: Mapping[tuple[int, int], _Outcome]
    ) -> list[_Candidate]:
        candidates = []
        for run_id, key in sorted(self.failures):
            run, nxt = self.runs[run_id], self.next_runs.get(run_id)
            if (
                nxt is not None
                and _final_failed(outcomes, run_id, key)
                and outcomes.get((nxt.id, key), _NONE).outcome == PASSED_CODE
                and all(r.changed_files_known and self._has_links(r) for r in (run, nxt))
            ):
                candidates.append((run, nxt, key))
        return candidates

    def finish(
        self,
        outcomes: Mapping[tuple[int, int], _Outcome],
        candidates: list[_Candidate],
        changes: Mapping[int, Change],
    ) -> None:
        load = self.load
        unrelated = self._unrelated_fail_then_pass(candidates, outcomes, changes)
        main = {key: walk.result(self.target.variant) for key, walk in self.walks.items()}
        failed_runs: dict[int, list[int]] = defaultdict(list)
        for run_id, key in self.failures:
            failed_runs[key].append(run_id)
        for key in load.seen:
            test_id = load.keys[key]
            failed = sorted(
                (self.runs[r] for r in failed_runs[key] if _final_failed(outcomes, r, key)),
                key=lambda r: r.order,
            )
            events = self._same_commit_events(key, failed_runs[key], outcomes)
            events += unrelated.get(key, [])
            events.sort(key=lambda e: max(r.run_at for r in e.failed), reverse=True)
            streak, last_pass, others = main.get(key, (None, None, ()))
            load.histories[test_id] = TestHistory(
                seen=True,
                file_path=load.paths.get(test_id),
                broken_on_main=streak,
                last_main_pass=last_pass,
                other_variants_failing=others,
                same_commit_passes=tuple(
                    r.ref for r in self.same_commit if outcomes.get((r.id, key), _NONE).any_passed
                ),
                flaky=tuple(events),
                failures_in_window=len(failed),
                last_failed_at=failed[-1].run_at if failed else None,
            )

    def _has_links(self, run: _Run) -> bool:
        """Stored links, or (eval only) links ``run_links`` can supply."""
        return run.has_links or (self.run_links is not None and self.run_links(run.id) is not None)

    def _next_on_branch(self, run_id: int) -> _Run | None:
        """The branch's next run (same variant) on a different commit, before the cut-off."""
        run = self.runs[run_id]
        later = self.by_branch[(run.branch, run.variant)][self.branch_pos[run_id] + 1 :]
        return next((r for r in later if r.commit_sha != run.commit_sha), None)

    def _same_commit_events(
        self,
        key: int,
        failed_runs: Sequence[int],
        outcomes: Mapping[tuple[int, int], _Outcome],
    ) -> list[FlakyEvent]:
        events = []
        groups = {(self.runs[r].commit_sha, self.runs[r].variant) for r in failed_runs}
        for commit, variant in sorted(groups, key=lambda g: (g[0], g[1] or "")):
            runs = self.by_pair[(commit, variant)]
            failed = [r for r in runs if (r.id, key) in self.failures]
            passed = [r for r in runs if outcomes.get((r.id, key), _NONE).any_passed]
            if failed and passed:
                messages = tuple(
                    m for r in failed if (m := outcomes.get((r.id, key), _NONE).message)
                )
                events.append(
                    FlakyEvent(
                        SAME_COMMIT,
                        commit,
                        variant,
                        tuple(r.ref for r in failed),
                        tuple(r.ref for r in passed),
                        messages,
                    )
                )
        return events

    def _unrelated_fail_then_pass(
        self,
        candidates: list[_Candidate],
        outcomes: Mapping[tuple[int, int], _Outcome],
        changes: Mapping[int, Change],
    ) -> dict[int, list[FlakyEvent]]:
        if not candidates:
            return {}
        keys, paths = self.load.keys, self.load.paths
        tests_by_run: dict[int, set[int]] = defaultdict(set)
        for run, nxt, key in candidates:
            tests_by_run[run.id].add(key)
            tests_by_run[nxt.id].add(key)
        linked: set[tuple[int, int]] = set()  # (run, key) whose change links to the test
        for run_id, run_keys in tests_by_run.items():
            known = [KnownTest(keys[k], paths.get(keys[k])) for k in run_keys]
            by_test = changes[run_id].links(known)
            linked.update((run_id, k) for k in run_keys if by_test.get(keys[k]))

        # One event per failing commit and the run that passed next: re-runs of the failing
        # commit before it are the same episode, not more evidence.
        episodes: dict[tuple[int, str, str | None, int], list[_Run]] = defaultdict(list)
        for run, nxt, key in candidates:
            if (run.id, key) not in linked and (nxt.id, key) not in linked:
                episodes[(key, run.commit_sha, run.variant, nxt.id)].append(run)
        events: dict[int, list[FlakyEvent]] = defaultdict(list)
        for (key, commit, variant, nxt_id), failed in episodes.items():
            messages = tuple(m for r in failed if (m := outcomes[(r.id, key)].message))
            events[key].append(
                FlakyEvent(
                    UNRELATED_FAIL_THEN_PASS,
                    commit,
                    variant,
                    tuple(r.ref for r in failed),
                    (self.runs[nxt_id].ref,),
                    messages,
                )
            )
        return events


@dataclass(frozen=True)
class _Outcome:
    outcome: int  # final attempt's status code
    any_passed: bool
    message: str | None  # the latest failed attempt's message


_NONE = _Outcome(SKIPPED_CODE, False, None)
_ABSENT: tuple[int, str | None] = (SKIPPED_CODE, None)  # not in the run: like skipped


def _final_failed(outcomes: Mapping[tuple[int, int], _Outcome], run_id: int, key: int) -> bool:
    return outcomes.get((run_id, key), _NONE).outcome in _FAILED


@dataclass
class _MainWalk:
    """One test's main outcomes for the target variant, read newest first."""

    failing: list[RunRef] = field(default_factory=list)  # the current streak, newest first
    message: str | None = None
    last_pass: RunRef | None = None
    resolved: bool = False  # reached a pass: nothing older matters
    others: list[RunRef] = field(default_factory=list)

    def step(self, run: _Run, outcome: int, message: str | None) -> bool:
        """Take the next older main outcome. True once resolved."""
        if self.resolved or outcome == SKIPPED_CODE:
            return self.resolved
        if outcome == PASSED_CODE:
            self.last_pass, self.resolved = run.ref, True
        elif outcome in _FAILED:
            if not self.failing:
                self.message = message
            self.failing.append(run.ref)
        return self.resolved

    def result(
        self, variant: str | None
    ) -> tuple[Streak | None, RunRef | None, tuple[RunRef, ...]]:
        streak = (
            Streak(variant, self.failing[-1], len(self.failing), self.last_pass, self.message)
            if self.failing
            else None
        )
        return streak, self.last_pass, tuple(self.others)


def triage_run(
    session: Session,
    target: Target,
    failures: Sequence[Failure],
    change: Change,
    window_days: int = 90,
    run_links: RunLinks | None = None,
    known_flaky: Sequence[str] = (),
) -> TriageReport | None:
    """Load the failing tests' histories and triage them; None if the repo is unknown.
    ``run_links``: see ``RunLinks`` (the eval only)."""
    histories = load_histories(
        session, target, [f.test_id for f in failures], window_days, run_links
    )
    if histories is None:
        return None
    return TriageReport(
        histories.cut_at,
        histories.run_id,
        triage(failures, histories.tests, change, window_days, known_flaky=known_flaky),
    )


# --- a recorded CI run attempt --------------------------------------------------------------


@dataclass(frozen=True)
class VariantTriage:
    """One recorded variant (upload) of a CI run attempt, triaged."""

    run_id: int
    variant: str | None
    commit_sha: str
    changed_files_known: bool
    # The run's change links are stored (``runs.affected``): without them the change is
    # treated as unknown (rule 3), so a test linked only through an import graph can never
    # look unrelated and be called flaky.
    links_known: bool
    report: TriageReport | None  # None: no failing tests
    reruns: int = 0  # re-run uploads of this variant in the attempt
    known_flaky_changed: bool = False  # the PR's own known_flaky differs (applies after merge)


def triage_ci_run(
    session: Session,
    repo: str,
    ci_run_id: str,
    run_attempt: int = 1,
    window_days: int = 90,
    variants: Collection[str | None] | None = None,
) -> list[VariantTriage] | None:
    """Triage every recorded variant of a CI run attempt (or only ``variants``), from what
    was stored: the failing tests (final attempt failed or errored), the changed files, the
    change links the uploader computed (``runs.affected``) and the run's ``known_flaky``. None
    if the repo is unknown; an empty list if the attempt has no recorded runs (of
    ``variants``).

    Each variant is triaged as ``/triage`` would with the same change: the cut-off is that
    variant's run, and no upload of this CI run attempt is history, except the variant's own
    re-runs for rule 1. Re-run uploads (``runs.rerun`` > 0) are never variants themselves.
    """
    repo_id = session.execute(
        text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
    ).scalar_one_or_none()
    if repo_id is None:
        return None
    rows = session.execute(
        text(
            """
            SELECT id, variant, commit_sha, changed_files_known, affected, known_flaky,
                   known_flaky_changed, rerun
            FROM runs
            WHERE repo_id = :repo_id AND ci_run_id = :ci AND run_attempt = :attempt
            ORDER BY COALESCE(variant, ''), id
            """
        ),
        {"repo_id": repo_id, "ci": ci_run_id, "attempt": run_attempt},
    ).all()
    reruns = Counter(row.variant for row in rows if row.rerun)
    runs = [row for row in rows if not row.rerun and (variants is None or row.variant in variants)]
    if not runs:
        return []
    run_ids = [run.id for run in runs]
    changed: dict[int, list[str]] = defaultdict(list)
    for run_id, path in session.execute(
        text("SELECT run_id, path FROM changed_files WHERE run_id = ANY(:ids) ORDER BY path"),
        {"ids": run_ids},
    ):
        changed[run_id].append(path)
    failures = recorded_failures(session, run_ids)
    # Every failing variant's histories in one batched load: the query count doesn't grow
    # with the number of variants.
    failing = [run for run in runs if failures[run.id]]
    loaded = load_histories_many(
        session,
        [
            (Target(repo, run.commit_sha, run.variant, ci_run_id, run_attempt),
             [f.test_id for f in failures[run.id]])
            for run in failing
        ],
        window_days,
    )  # fmt: skip
    histories = dict(zip((run.id for run in failing), loaded or [], strict=True))

    results = []
    for run in runs:
        links_known = run.changed_files_known and run.affected is not None
        change = (
            Change.from_graph(changed[run.id], run.affected) if links_known else Change((), False)
        )
        report = None
        if run.id in histories:
            h = histories[run.id]
            results_ = triage(failures[run.id], h.tests, change, window_days,
                              known_flaky=run.known_flaky)  # fmt: skip
            report = TriageReport(h.cut_at, h.run_id, results_)
        results.append(VariantTriage(
            run.id, run.variant, run.commit_sha, run.changed_files_known, links_known, report,
            reruns[run.variant], run.known_flaky_changed,
        ))  # fmt: skip
    return results


def recorded_failures(session: Session, run_ids: Sequence[int]) -> dict[int, list[Failure]]:
    """Each run's failing tests as recorded: tests whose final attempt failed or errored, with
    that attempt's message and the test's file path as of the run."""
    failures: dict[int, list[Failure]] = defaultdict(list)
    # Failed rows through the partial index, then each one's final attempt by primary key: a
    # test that failed and then passed on a retry is not a failure. A path counts only if it
    # was reported by this run or an earlier one (a later run's path would be peeking).
    for run_id, test_id, file_path, message in session.execute(
        text(
            f"""
            WITH failed AS (
                SELECT DISTINCT run_id, test_key FROM test_results
                WHERE run_id = ANY(:ids) AND status IN {FAILED_CODES_SQL}
            )
            SELECT f.run_id, t.test_id,
                   CASE WHEN t.file_path_at <= COALESCE(r.started_at, r.created_at)
                        THEN t.file_path END,
                   final.message
            FROM failed f
            JOIN tests t ON t.id = f.test_key
            JOIN runs r ON r.id = f.run_id
            CROSS JOIN LATERAL (
                SELECT tr.status, tr.message FROM test_results tr
                WHERE tr.run_id = f.run_id AND tr.test_key = f.test_key
                ORDER BY tr.attempt DESC LIMIT 1
            ) final
            WHERE final.status IN {FAILED_CODES_SQL}
            ORDER BY f.run_id, t.test_id
            """
        ),
        {"ids": list(run_ids)},
    ):
        failures[run_id].append(Failure(test_id, message, file_path))
    return failures
