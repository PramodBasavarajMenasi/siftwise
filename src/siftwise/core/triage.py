"""Failure triage: is a failing test a new regression, pre-existing on main, or flaky?

``triage`` is pure: given each failing test's ``TestHistory`` and the run's ``Change``, it
returns a verdict with a confidence, reasons and evidence. ``load_histories`` builds those
histories from the database, and ``triage_run`` does both.

**No peeking.** Only history strictly before the triaged run counts: runs earlier by
``(Run.occurred_at, id)`` than the cut-off (the recorded run, else ``as_of``, else now), and
never an upload of the triaged CI run attempt itself (its other matrix legs included).
Triaging an old run gives the answer it would have got live.

**Variants.** A failure is compared with its own variant's history: Linux failing on main
doesn't make a macOS failure pre-existing. Other variants failing on main are only noted.

Rules, in order; the first that applies decides:

1. The same commit and variant passed before (a re-run or a retry) -> **flaky**, high.
   Never for uncommitted code (``Target.commit_sha`` None): no earlier run tested it.
2. Failing on main for this variant, at the latest main run before the cut-off ->
   **pre-existing**: high if the streak is 2+ main runs or the message matches main's, else
   medium; medium at most if this change also links to the test.
3. The diff is unknown -> **unknown** (nothing can be linked).
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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
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
) -> list[TriageResult]:
    """A verdict for each failing test (the first of duplicate test IDs counts). ``variant``
    is the triaged run's, for wording only: ``histories`` already compare within it."""
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
        )
        for f in unique
    ]


def _verdict(
    failure: Failure,
    h: TestHistory,
    links: tuple[ChangeLink, ...],
    diff_known: bool,
    window_days: int,
    variant: str | None,
) -> TriageResult:
    window = f"in {window_days} days" if window_days else "in the history"
    flaky = tuple((event, _matches_any(failure.message, event.messages)) for event in h.flaky)
    links = tuple(sorted(links, key=lambda link: -link.strength.rank))
    strongest = links[0] if links else None
    notes = [
        f"also failing on main on {_variant(run.variant)} (latest main run {run.commit_sha[:7]})"
        for run in h.other_variants_failing
    ]

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
        )

    def linked() -> str:
        assert strongest is not None
        more = f" (+{len(links) - 1} more)" if len(links) > 1 else ""
        return f"{strongest.reason}{more}"

    # 1. Same code, different outcome.
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
        return done(Verdict.PRE_EXISTING, confidence, reasons, match)

    flaky_note = _flaky_note(h.flaky, window)
    # 3. Nothing can be linked without the diff.
    if not diff_known:
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
        strong = len(h.flaky) >= 2 or any(match for _, match in flaky)
        reasons = [flaky_note, "not linked to this change"]
        if any(match for _, match in flaky):
            reasons.append("same failure message as an earlier flaky failure")
        return done(Verdict.FLAKY, Strength.HIGH if strong else Strength.MEDIUM, reasons)
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
) -> Histories | None:
    """Each test's history before the cut-off, or None if the repo is unknown.

    A fixed number of batched queries, whatever the number of tests: the cut-off, the window's
    runs (metadata only), test keys, which tests have any earlier result, their failures (a
    partial index by test), the outcomes those failures need for flaky evidence (primary-key
    probes), main outcomes of this variant newest first (in at most three rounds, plus one
    look before the window for streaks older than it) and the changes of the runs that flaky
    evidence depends on. Passing results are only read by primary key, for named runs.
    """
    session.execute(text("SET LOCAL jit = off"))  # compiling costs more than these queries
    repo_id = session.execute(
        text("SELECT id FROM repos WHERE name = :name"), {"name": target.repo}
    ).scalar_one_or_none()
    if repo_id is None:
        return None

    cut_at = target.as_of or datetime.now(UTC)
    if cut_at.tzinfo is None:
        cut_at = cut_at.replace(tzinfo=UTC)
    cut_id, run_id = _NO_RUN, None
    if target.ci_run_id is not None:
        row = session.execute(
            text(
                "SELECT id, COALESCE(started_at, created_at) FROM runs WHERE repo_id = :repo_id"
                " AND ci_run_id = :ci AND run_attempt = :attempt"
                " AND COALESCE(variant, '') = :variant"
            ),
            {
                "repo_id": repo_id,
                "ci": target.ci_run_id,
                "attempt": target.run_attempt,
                "variant": target.variant or "",
            },
        ).first()
        if row is not None:
            run_id, cut_at, cut_id = row[0], row[1], row[0]
    params: dict[str, Any] = {
        "repo_id": repo_id,
        "cut_at": cut_at,
        "cut_id": cut_id,
        "ci_run_id": target.ci_run_id,
        "run_attempt": target.run_attempt,
    }

    days = window_days if window_days > 0 else _ALL_HISTORY_DAYS
    runs = {
        row[0]: _Run(*row)
        for row in session.execute(
            text(
                f"""
                WITH before AS MATERIALIZED (
                    SELECT r.id, r.commit_sha, r.branch, r.is_main, r.variant,
                           COALESCE(r.started_at, r.created_at) AS run_at, r.ci_run_id,
                           r.changed_files_known, r.affected IS NOT NULL AS has_links
                    FROM runs r WHERE r.repo_id = :repo_id AND {_BEFORE}
                )
                SELECT * FROM before
                WHERE run_at >= (SELECT max(run_at) FROM before) - make_interval(days => :days)
                """
            ),
            {**params, "days": days},
        )
    }
    window_start = max(r.run_at for r in runs.values()) - timedelta(days=days) if runs else cut_at

    keys: dict[int, str] = {}
    paths: dict[str, str | None] = {}
    for key, test_id, file_path, file_path_at in session.execute(
        text(
            "SELECT id, test_id, file_path, file_path_at FROM tests"
            " WHERE repo_id = :repo_id AND test_id = ANY(:ids)"
        ),
        {"repo_id": repo_id, "ids": list(dict.fromkeys(test_ids))},
    ):
        keys[key] = test_id
        # A path reported by the triaged run or a later one would be peeking.
        paths[test_id] = file_path if file_path_at is not None and file_path_at < cut_at else None
    seen: set[int] = set()
    if keys:
        seen.update(
            session.scalars(
                text(
                    f"""
                    SELECT k.key FROM unnest(CAST(:keys AS integer[])) AS k(key)
                    WHERE EXISTS (SELECT 1 FROM test_results tr JOIN runs r ON r.id = tr.run_id
                                  WHERE tr.test_key = k.key AND {_BEFORE})
                    """
                ),
                {**params, "keys": list(keys)},
            )
        )

    builder = _Builder(session, target, runs, params, window_start)
    histories = builder.build(seen, keys, paths)
    return Histories(cut_at, run_id, histories)


class _Builder:
    """The batched queries behind ``load_histories``, for the seen tests."""

    def __init__(
        self,
        session: Session,
        target: Target,
        runs: dict[int, _Run],
        params: dict[str, Any],
        window_start: datetime,
    ) -> None:
        self.session = session
        self.target = target
        self.runs = runs
        self.params = params
        self.window_start = window_start
        self.by_pair: dict[tuple[str, str | None], list[_Run]] = defaultdict(list)
        self.by_branch: dict[tuple[str, str | None], list[_Run]] = defaultdict(list)
        for run in sorted(runs.values(), key=lambda r: r.order):
            self.by_pair[(run.commit_sha, run.variant)].append(run)
            self.by_branch[(run.branch, run.variant)].append(run)
        self.branch_pos = {
            run.id: i for runs_ in self.by_branch.values() for i, run in enumerate(runs_)
        }

    def build(
        self, seen: set[int], keys: dict[int, str], paths: dict[str, str | None]
    ) -> dict[str, TestHistory]:
        histories = {
            test_id: TestHistory(file_path=paths.get(test_id)) for test_id in keys.values()
        }
        if not seen:
            return histories
        failures = self._failures(seen)  # (run, key) with any failed attempt, in the window
        commit = self.target.commit_sha
        same_commit = self.by_pair.get((commit, self.target.variant), []) if commit else []
        next_runs = {run_id: nxt for run_id, _ in failures if (nxt := self._next_on_branch(run_id))}
        pairs = {(r.id, k) for k in seen for r in same_commit}
        for run_id, key in failures:
            run = self.runs[run_id]
            pairs.update((r.id, key) for r in self.by_pair[(run.commit_sha, run.variant)])
            if run_id in next_runs:
                pairs.add((next_runs[run_id].id, key))
        outcomes = self._outcomes(pairs)
        main = self._main(seen)
        unrelated = self._unrelated_fail_then_pass(failures, next_runs, outcomes, keys, paths)

        failed_runs: dict[int, list[int]] = defaultdict(list)
        for run_id, key in failures:
            failed_runs[key].append(run_id)
        for key in seen:
            test_id = keys[key]
            failed = sorted(
                (self.runs[r] for r in failed_runs[key] if _final_failed(outcomes, r, key)),
                key=lambda r: r.order,
            )
            events = self._same_commit_events(key, failed_runs[key], failures, outcomes)
            events += unrelated.get(key, [])
            events.sort(key=lambda e: max(r.run_at for r in e.failed), reverse=True)
            streak, last_pass, others = main.get(key, (None, None, ()))
            histories[test_id] = TestHistory(
                seen=True,
                file_path=paths.get(test_id),
                broken_on_main=streak,
                last_main_pass=last_pass,
                other_variants_failing=others,
                same_commit_passes=tuple(
                    r.ref for r in same_commit if outcomes.get((r.id, key), _NONE).any_passed
                ),
                flaky=tuple(events),
                failures_in_window=len(failed),
                last_failed_at=failed[-1].run_at if failed else None,
            )
        return histories

    # -- queries --

    def _failures(self, keys: set[int]) -> set[tuple[int, int]]:
        rows = self.session.execute(
            text(
                f"SELECT run_id, test_key FROM test_results"
                f" WHERE status IN {FAILED_CODES_SQL} AND test_key = ANY(:keys)"
            ),
            {"keys": sorted(keys)},
        )
        return {(run_id, key) for run_id, key in rows if run_id in self.runs}

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
        self, run_ids: Sequence[int], keys: Iterable[int]
    ) -> dict[tuple[int, int], tuple[int, str | None]]:
        if not run_ids:
            return {}
        rows = self.session.execute(
            text(
                f"""
                SELECT r.id, x.test_key, x.outcome, x.message
                FROM unnest(CAST(:runs AS bigint[])) AS r(id)
                CROSS JOIN LATERAL (
                    SELECT tr.test_key, {_OUTCOME} AS outcome,
                           (array_agg(tr.message ORDER BY tr.attempt DESC))[1] AS message
                    FROM test_results tr
                    WHERE tr.run_id = r.id AND tr.test_key = ANY(CAST(:keys AS integer[]))
                    GROUP BY tr.test_key
                ) x
                """
            ),
            {"runs": list(run_ids), "keys": sorted(keys)},
        )
        return {(r, k): (outcome, message) for r, k, outcome, message in rows}

    def _main(
        self, keys: set[int]
    ) -> dict[int, tuple[Streak | None, RunRef | None, tuple[RunRef, ...]]]:
        """Per test: its failing streak on main (this variant), latest main pass, and other
        variants whose latest main run it failed in."""
        variant = self.target.variant
        mains = sorted(
            (r for r in self.runs.values() if r.is_main and r.variant == variant),
            key=lambda r: r.order,
            reverse=True,
        )
        latest_other: dict[str | None, _Run] = {}
        for run in sorted((r for r in self.runs.values() if r.is_main), key=lambda r: r.order):
            if run.variant != variant:
                latest_other[run.variant] = run
        walks = {key: _MainWalk() for key in keys}

        # The newest main runs first (with each other variant's latest), then older ones for
        # the tests that haven't passed yet.
        first, second = _MAIN_CHUNKS
        rounds = (mains[:first], mains[first : first + second], mains[first + second :])
        for n, chunk in enumerate(rounds):
            others = list(latest_other.values()) if n == 0 else []
            pending = [key for key, walk in walks.items() if not walk.resolved]
            if not others and not (chunk and pending):
                continue
            outcomes = self._main_outcomes(
                [r.id for r in chunk + others], keys if others else pending
            )
            for key in pending:
                for run in chunk:
                    if walks[key].step(run, *outcomes.get((run.id, key), _ABSENT)):
                        break
            for run in others:
                for key in keys:
                    if outcomes.get((run.id, key), _ABSENT)[0] in _FAILED:
                        walks[key].others.append(run.ref)

        # Failing for every main run of the window: the streak may have started before it.
        older = [key for key, walk in walks.items() if not walk.resolved and walk.failing]
        if older:
            for run, key, outcome in self._older_main(older):
                walks[key].step(run, outcome, None)

        return {key: walk.result(variant) for key, walk in walks.items()}

    def _older_main(self, keys: list[int]) -> list[tuple[_Run, int, int]]:
        rows = self.session.execute(
            text(
                f"""
                SELECT r.id, r.commit_sha, r.branch, r.is_main, r.variant,
                       COALESCE(r.started_at, r.created_at) AS run_at, r.ci_run_id,
                       r.changed_files_known, r.affected IS NOT NULL, tr.test_key, {_OUTCOME}
                FROM test_results tr JOIN runs r ON r.id = tr.run_id
                WHERE tr.test_key = ANY(:keys) AND r.repo_id = :repo_id AND r.is_main
                  AND r.variant IS NOT DISTINCT FROM CAST(:variant AS text)
                  AND COALESCE(r.started_at, r.created_at) < :window_start AND {_BEFORE}
                GROUP BY r.id, tr.test_key
                """
            ),
            {
                **self.params,
                "keys": keys,
                "variant": self.target.variant,
                "window_start": self.window_start,
            },
        ).all()
        found = [(_Run(*row[:9]), row[9], row[10]) for row in rows]
        found.sort(key=lambda item: item[0].order, reverse=True)
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
        return {run_id: Change.from_graph(changed, graph) for run_id, graph, changed in rows}

    # -- flaky evidence --

    def _next_on_branch(self, run_id: int) -> _Run | None:
        """The branch's next run (same variant) on a different commit, before the cut-off."""
        run = self.runs[run_id]
        later = self.by_branch[(run.branch, run.variant)][self.branch_pos[run_id] + 1 :]
        return next((r for r in later if r.commit_sha != run.commit_sha), None)

    def _same_commit_events(
        self,
        key: int,
        failed_runs: Sequence[int],
        failures: set[tuple[int, int]],
        outcomes: Mapping[tuple[int, int], _Outcome],
    ) -> list[FlakyEvent]:
        events = []
        groups = {(self.runs[r].commit_sha, self.runs[r].variant) for r in failed_runs}
        for commit, variant in sorted(groups, key=lambda g: (g[0], g[1] or "")):
            runs = self.by_pair[(commit, variant)]
            failed = [r for r in runs if (r.id, key) in failures]
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
        failures: set[tuple[int, int]],
        next_runs: Mapping[int, _Run],
        outcomes: Mapping[tuple[int, int], _Outcome],
        keys: Mapping[int, str],
        paths: Mapping[str, str | None],
    ) -> dict[int, list[FlakyEvent]]:
        candidates = []
        for run_id, key in sorted(failures):
            run, nxt = self.runs[run_id], next_runs.get(run_id)
            if (
                nxt is not None
                and _final_failed(outcomes, run_id, key)
                and outcomes.get((nxt.id, key), _NONE).outcome == PASSED_CODE
                and all(r.changed_files_known and r.has_links for r in (run, nxt))
            ):
                candidates.append((run, nxt, key))
        if not candidates:
            return {}
        changes = self._run_changes({r.id for run, nxt, _ in candidates for r in (run, nxt)})
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
) -> TriageReport | None:
    """Load the failing tests' histories and triage them; None if the repo is unknown."""
    histories = load_histories(session, target, [f.test_id for f in failures], window_days)
    if histories is None:
        return None
    return TriageReport(
        histories.cut_at,
        histories.run_id,
        triage(failures, histories.tests, change, window_days),
    )
