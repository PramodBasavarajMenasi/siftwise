"""Risk order for selected tests: likely failures first (pure; no server dependencies).

The score only orders tests that were already selected: it never adds or drops one. It is a
fixed, documented sum (no learning), from what the selection and ``test_stats`` already hold:

    score = link + 3 * recency * (1 - flaky) + 2 * failure_rate * (1 - flaky) + new

* ``link``: how the change reaches the test, its strongest reason: 6 direct (the test's own
  file changed, or path mapping: ``test_x.py`` for ``x.py``, the changed file's Go package, a
  JS test named after it, or an import depth of 1), 3 indirect (a transitive import depth 2+,
  a declared ``[[depends]]`` rule, a nested build file), 0 history only (co-change, recently
  failed or broken on main, always-run). Without import depth (older clients), imports stay
  "indirect".
* ``recency``: ``0.5 ** (days / 7)``, ``days`` = from the test's last failure to the repo's
  newest run (not to now: a backfilled or quiet repo scores the same whenever it's asked); 0 if
  it never failed in the stats window.
* ``failure_rate``: failures / runs in the stats window.
* ``flaky``: ``test_stats.flaky_score`` (the share of commit+variant pairs where it both failed
  and passed). Recency and rate count only the failure evidence that isn't flakiness.
* ``new``: 2 if the test has fewer than 3 runs in the window (or none): no history to
  trust, and new tests often fail.

Ties: shorter average duration first (unknown last), then test ID. Scores are rounded to 3
decimals, so equal inputs give equal scores on every platform.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum


class Order(StrEnum):
    """How a selection's commands list their tests."""

    RISK = "risk"  # highest risk first
    DEFAULT = "default"  # the order before risk ordering: by path / package / test ID
    # Evaluated, not offered (docs/eval-results.md): risk per unit of estimated time, over
    # everything or within each link tier (direct before indirect before history).
    RISK_PER_TIME = "risk-per-time"
    RISK_PER_TIME_TIERED = "risk-per-time-tiered"


# What users can ask for (API, CLI, Action, GitLab, MCP). The risk-per-time orders lost to
# RISK on the replays (docs/eval-results.md): eval only.
OFFERED_ORDERS = (Order.RISK, Order.DEFAULT)


class OfferedOrder(StrEnum):
    """``OFFERED_ORDERS`` as a type, for command-line choices."""

    RISK = Order.RISK.value
    DEFAULT = Order.DEFAULT.value


class Link(IntEnum):
    """The strongest way a change reaches a selected test."""

    HISTORY = 0  # co-change, recently failed, broken on main, always-run
    INDIRECT = 1  # a transitive import, a declared rule, a nested build file
    DIRECT = 2  # its own file changed, or path mapping


LINK_POINTS = {Link.DIRECT: 6.0, Link.INDIRECT: 3.0, Link.HISTORY: 0.0}
LINK_WORDS = {Link.DIRECT: "direct link", Link.INDIRECT: "indirect link",
              Link.HISTORY: "history only"}  # fmt: skip
RECENCY_POINTS = 3.0
HALF_LIFE_DAYS = 7.0
RATE_POINTS = 2.0
NEW_POINTS = 2.0
NEW_RUNS = 3  # fewer runs than this in the window: a new test


@dataclass(frozen=True)
class RiskInputs:
    test_id: str
    link: Link
    runs: int | None = None
    failures: int | None = None
    last_failed_at: datetime | None = None
    flaky_score: float | None = None
    avg_duration_ms: float | None = None


@dataclass(frozen=True)
class Risk:
    score: float
    why: str  # the parts that scored, e.g. "direct link; failed 2 days before the latest run"


def score(inputs: RiskInputs, newest_run_at: datetime | None) -> Risk:
    flaky = min(max(inputs.flaky_score or 0.0, 0.0), 1.0)
    real = 1.0 - flaky
    points = LINK_POINTS[inputs.link]
    why = [LINK_WORDS[inputs.link]]
    if inputs.last_failed_at is not None:
        anchor = max(newest_run_at or inputs.last_failed_at, inputs.last_failed_at)
        days = (anchor - inputs.last_failed_at).total_seconds() / 86_400
        points += RECENCY_POINTS * 0.5 ** (days / HALF_LIFE_DAYS) * real
        why.append(f"failed {_days(days)} before the latest run")
    if inputs.runs and inputs.failures:
        rate = inputs.failures / inputs.runs
        points += RATE_POINTS * rate * real
        why.append(f"fails {rate:.0%} of runs")
    if flaky and inputs.failures:
        why.append(f"flaky {flaky:.0%}: failures discounted")
    if (inputs.runs or 0) < NEW_RUNS:
        points += NEW_POINTS
        why.append("new: little history")
    return Risk(round(points, 3), "; ".join(why))


def _days(days: float) -> str:
    if days < 1:
        return "less than a day"
    whole = int(days)
    return f"{whole} day{'' if whole == 1 else 's'}"


def rank(
    tests: Iterable[RiskInputs], newest_run_at: datetime | None
) -> dict[str, tuple[int, Risk]]:
    """Each test's rank (1 = run first) and risk: score descending, then shorter average
    duration (unknown last), then test ID."""
    scored = [(t, score(t, newest_run_at)) for t in tests]
    scored.sort(key=lambda pair: (
        -pair[1].score,
        pair[0].avg_duration_ms is None,
        pair[0].avg_duration_ms or 0.0,
        pair[0].test_id,
    ))  # fmt: skip
    return {t.test_id: (n, risk) for n, (t, risk) in enumerate(scored, start=1)}


# Below this, a measured duration is mostly noise (timer resolution, setup): every unit of
# work costs at least this much in the risk-per-time orders. Chosen up front, not fitted.
PER_TIME_FLOOR_MS = 100.0


@dataclass(frozen=True)
class Ranked:
    """A selected test as the command order sees it."""

    rank: int
    score: float
    link: Link
    # Average duration; None = unknown; 0 = inside another test's time (a Go subtest).
    ms: float | None


UnitKey = Callable[[Iterable[str]], tuple[float, ...]]


def unit_key(order: Order, ranked: Mapping[str, Ranked]) -> UnitKey:
    """The sort key of a unit of work (a pytest argument, a Go package, a Jest file, a whole
    command), from the selected tests it runs; smaller runs first. A unit with no ranked test
    (a changed or added test file, a changed module) comes first: it is the change itself.

    * ``RISK``: its best rank.
    * ``RISK_PER_TIME``: its best score divided by its estimated time (the sum of its tests'
      average durations, an unknown one counted as the selection's median, at least
      ``PER_TIME_FLOOR_MS``); then its best rank.
    * ``RISK_PER_TIME_TIERED``: the same within each link tier, strongest tier first.
    """
    known = [r.ms for r in ranked.values() if r.ms]
    typical = statistics.median(known) if known else PER_TIME_FLOOR_MS

    def key(test_ids: Iterable[str]) -> tuple[float, ...]:
        members = [ranked[i] for i in test_ids if i in ranked]
        if not members:
            return (-math.inf,)
        best = float(min(m.rank for m in members))
        if order is Order.RISK:
            return (best,)
        cost = max(sum(typical if m.ms is None else m.ms for m in members), PER_TIME_FLOOR_MS)
        value = max(m.score for m in members) / cost
        if order is Order.RISK_PER_TIME:
            return (-value, best)
        return (-float(max(m.link for m in members)), -value, best)

    return key
