"""Proxy ground truth for the triage eval: what a failure turned out to be, from what happened
after it. Labels may use the future; verdicts never do (the eval computes them with the
production engine, cut off at the run).

Labels, the first that applies wins (``label``):

1. **flaky**: the same commit and variant also passed, in another run (a re-run, or another CI
   run of the commit). Identical code, different outcome. ``when`` says whether that pass came
   before the failure (the engine could see it) or only after (it couldn't).
2. **pre_existing**: for this variant, the latest main run before the failure had the test
   failing, in a streak of at least ``STREAK`` main commits (so not a one-off).
3. **regression**:
   (a) on a branch: main passed the test (latest main run of the variant before), no run of
   the commit passed it, and a later commit on the same branch and variant passed it after a
   diff (failing commit ... passing commit) that touches a file linked to the test;
   (b) on main: the previous main run passed the test, and it then failed on at least
   ``STREAK`` main commits in a row, starting here.

Everything else is **unlabelable** and excluded, with the reason (``Label.reason``): never a
guess. A one-off main failure followed by a pass is excluded too: calling it flaky would use
the engine's own fail-then-pass rule, a circular score.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

PASS, FAIL, SKIP = "pass", "fail", "skip"
REGRESSION, FLAKY, PRE_EXISTING = "regression", "flaky", "pre_existing"
LABELS = (REGRESSION, FLAKY, PRE_EXISTING)
VERDICTS = ("new_regression", "flaky", "pre_existing", "unknown")
# Main commits a failure must persist on to count as a regression (b) or pre-existing.
STREAK = 2


@dataclass(frozen=True)
class Run:
    id: int
    run_at: datetime
    commit: str
    branch: str
    is_main: bool
    variant: str  # '' = no variant

    @property
    def order(self) -> tuple[datetime, int]:
        return (self.run_at, self.id)


# One test's outcomes in one variant, oldest first: (run, PASS | FAIL | SKIP). Runs that
# didn't report the test are absent.
Timeline = Sequence[tuple[Run, str]]
# (test, failing run, passing run) -> whether the diff between them touches a file linked to
# the test; None if that can't be told (no compare, no import graph).
LinkedFix = Callable[[str, Run, Run], bool | None]


@dataclass(frozen=True)
class Label:
    kind: str | None  # None: unlabelable
    reason: str
    when: str = ""  # flaky: "before" or "after" the failure


def label(test: str, run: Run, timeline: Timeline, linked_fix: LinkedFix) -> Label:
    """The proxy label of ``test`` failing in ``run`` (one of ``timeline``'s runs)."""
    same = [(r, o) for r, o in timeline if r.commit == run.commit and r.id != run.id]
    passes = [r for r, o in same if o == PASS]
    if passes:
        when = "before" if passes[0].order < run.order else "after"
        return Label(FLAKY, f"the same commit passed in run {passes[0].id}", when)

    mains = [(r, o) for r, o in timeline if r.is_main and o != SKIP]
    before = [(r, o) for r, o in mains if r.order < run.order]
    if before and before[-1][1] == FAIL:
        commits = _streak_commits(mains, len(before) - 1)
        if commits >= STREAK:
            return Label(PRE_EXISTING, f"failing on main for {commits} commits")
        return Label(None, "main failed just before, once: a one-off")
    if not before:
        return Label(None, "no main history of the test before the run")

    if run.is_main:  # main passed just before
        index = next(i for i, (r, _) in enumerate(mains) if r.id == run.id)
        commits = _streak_commits(mains, index)
        if commits >= STREAK:
            return Label(REGRESSION, f"introduced on main, failing for {commits} commits")
        return Label(None, "a one-off failure on main")

    later = [(r, o) for r, o in timeline if r.order > run.order and r.branch == run.branch]
    fix = next((r for r, o in later if o == PASS and r.commit != run.commit), None)
    if fix is None:
        return Label(None, "never passed again on its branch")
    linked = linked_fix(test, run, fix)
    if linked is None:
        return Label(None, "fix diff unknown (no compare or import graph)")
    if not linked:
        return Label(None, "passed later after a change unrelated to the test")
    return Label(REGRESSION, f"fixed on the branch by {fix.commit[:7]}, a linked change")


def _streak_commits(mains: Sequence[tuple[Run, str]], index: int) -> int:
    """Distinct commits in the run of consecutive main failures through ``mains[index]``."""
    lo = index
    while lo > 0 and mains[lo - 1][1] == FAIL:
        lo -= 1
    hi = index
    while hi + 1 < len(mains) and mains[hi + 1][1] == FAIL:
        hi += 1
    return len({r.commit for r, _ in mains[lo : hi + 1]})


def fold_subtests(test_ids: Iterable[str]) -> list[str]:
    """Go subtests (``pkg::TestX/sub``) whose parent also failed count once, as the parent."""
    ids = set(test_ids)
    kept = []
    for test_id in sorted(ids):
        prefix, sep, name = test_id.partition("::")
        parent = f"{prefix}{sep}{name.split('/', 1)[0]}"
        if "/" in name and parent in ids:
            continue
        kept.append(test_id)
    return kept


# --- metrics --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scored:
    """One failure: its verdict (with confidence) and its label (kind None: excluded)."""

    test: str
    run_id: int
    verdict: str
    confidence: str
    label: Label


@dataclass(frozen=True)
class Metrics:
    failures: int
    labelled: int
    excluded: dict[str, int]
    confusion: dict[tuple[str, str], int]  # (label, verdict) -> count
    by_confidence: dict[tuple[str, str, str], int]  # (label, verdict, confidence) -> count
    flaky_when: dict[str, int]  # flaky labels: pass seen before / only after the failure
    regressions_called_flaky: list[Scored]
    unknown_all: int
    unknown_labelled: int

    def precision(
        self, verdict: str, label_: str, confidence: str | None = None
    ) -> tuple[int, int]:
        """(correct, called): of the labelled failures given ``verdict``, how many had
        ``label_``."""
        if confidence is None:
            called = sum(n for (_, v), n in self.confusion.items() if v == verdict)
            return self.confusion.get((label_, verdict), 0), called
        called = sum(
            n for (_, v, c), n in self.by_confidence.items() if v == verdict and c == confidence
        )
        return self.by_confidence.get((label_, verdict, confidence), 0), called

    def recall(self, label_: str, verdict: str) -> tuple[int, int]:
        """(found, total): of the failures labelled ``label_``, how many got ``verdict``."""
        total = sum(n for (lab, _), n in self.confusion.items() if lab == label_)
        return self.confusion.get((label_, verdict), 0), total


def metrics(scored: Sequence[Scored]) -> Metrics:
    labelled = [s for s in scored if s.label.kind is not None]
    excluded = Counter(s.label.reason for s in scored if s.label.kind is None)
    confusion: dict[tuple[str, str], int] = defaultdict(int)
    by_confidence: dict[tuple[str, str, str], int] = defaultdict(int)
    for s in labelled:
        assert s.label.kind is not None
        confusion[(s.label.kind, s.verdict)] += 1
        by_confidence[(s.label.kind, s.verdict, s.confidence)] += 1
    return Metrics(
        failures=len(scored),
        labelled=len(labelled),
        excluded=dict(excluded.most_common()),
        confusion=dict(confusion),
        by_confidence=dict(by_confidence),
        flaky_when=dict(Counter(s.label.when for s in labelled if s.label.kind == FLAKY)),
        regressions_called_flaky=[
            s for s in labelled if s.label.kind == REGRESSION and s.verdict == "flaky"
        ],
        unknown_all=sum(s.verdict == "unknown" for s in scored),
        unknown_labelled=sum(s.verdict == "unknown" for s in labelled),
    )
