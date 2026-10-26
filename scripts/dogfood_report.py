"""siftwise's live dogfooding (.github/workflows/dogfood.yml): each CI run is a shadow run against a
server inside the job, and leaves a summary artifact; this script writes and reads them.

    python -m scripts.dogfood_report summary --out FILE
        In the dogfood job, after the rollup: this run attempt's shadow comparison and failure
        verdicts, from the job's own server (POST /repos/{repo}/shadow and /triage, the same
        calls the rollup made). Never fails the job: problems go into the summary.

    python -m scripts.dogfood_report report [--since DATE] [--until DATE] [--by week] [--json]
                                            [--from DIR] [--save DIR] [--exclude-forks]
        Downloads the summaries (GitHub API, GITHUB_TOKEN from the environment or .env, needs
        actions: read; or reads --from DIR), keeps one per workflow run (its first attempt
        with a summary), and prints recall over all and selective-only runs, time saved and
        misses with their causes, in total or per week: `siftwise report`'s own text, built
        from the same per-run comparisons the server's /recall would add up.

Why summaries and not one database: PR runs may read main's cache but their saves stay with
the PR (https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching),
so only the artifacts see every run. Fork PRs run their own code, so their summaries are
untrusted: the report counts them separately and --exclude-forks leaves them out.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from scripts.eval._github import client, github_token, ts
from siftwise.cli import gha
from siftwise.cli.main import SiftwiseError, post_json, recall_lines, weekly_lines
from siftwise.core.shadow import CAUSES, LINKED_NOT_SELECTED, UNCLASSIFIED, VERDICT_ORDER

Json = dict[str, Any]
SCHEMA = 1
ARTIFACT_PREFIX = "dogfood-summary"
SUMMARY_FILE = "dogfood-summary.json"
MAX_MISSES = 100


# --- summary (in the dogfood job) ----------------------------------------------------------


def build_summary(
    env: Mapping[str, str], server: str, token: str, now: datetime | None = None
) -> Json:
    """This run attempt's summary. Every server call that fails is a problem, not an error."""
    ctx = gha.load_context(env)
    summary: Json = {
        "schema": SCHEMA,
        "repo": ctx.repo,
        "run_id": ctx.run_id,
        "run_attempt": ctx.run_attempt,
        "event": ctx.event_name,
        "pr_number": ctx.pr_number,
        "fork": ctx.fork,
        "branch": ctx.branch,
        "is_main": ctx.is_main,
        "commit_sha": ctx.head_sha,
        "at": env.get("DOGFOOD_STARTED_AT") or (now or datetime.now(UTC)).isoformat(),
        "select": {
            "shadow": env.get("DOGFOOD_SHADOW", ""),
            "shadow_mode": env.get("DOGFOOD_SHADOW_MODE", ""),
            "selected": _int(env.get("DOGFOOD_SHADOW_SELECTED")),
            "total": _int(env.get("DOGFOOD_SHADOW_TOTAL")),
        },
        # The cache entry the history came from; "" = none (the run started empty).
        "history_from": env.get("DOGFOOD_HISTORY_FROM", ""),
        "tests_outcome": env.get("DOGFOOD_TESTS_OUTCOME", ""),
        "steps": dict(
            step.split("=", 1) for step in env.get("DOGFOOD_STEPS", "").split() if "=" in step
        ),
        "shadow": None,
        "triage": None,
        "problems": [],
    }
    # A failed siftwise step is a problem (the run failed open), never a failed job.
    for step, outcome in summary["steps"].items():
        if outcome not in ("success", "skipped"):
            summary["problems"].append(f"{step} step: {outcome or 'did not run'}")
    attempt = {"ci_run_id": ctx.run_id, "run_attempt": ctx.run_attempt}
    if not token:
        summary["problems"].append("no siftwise token in the job")
        return summary
    try:
        summary["shadow"] = post_json(server, token, f"/repos/{ctx.repo}/shadow", attempt)
    except SiftwiseError as exc:
        summary["problems"].append(f"shadow compare: {exc}")
    try:
        triage = post_json(server, token, f"/repos/{ctx.repo}/triage", attempt)
        summary["triage"] = {
            "failing_tests": triage.get("failing_tests"),
            "counts": triage.get("counts"),
            "results": [
                {key: result.get(key) for key in ("test_id", "variant", "verdict", "confidence")}
                for result in (triage.get("results") or [])[:MAX_MISSES]
            ],
        }
    except SiftwiseError as exc:
        summary["problems"].append(f"triage: {exc}")
    return summary


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def redact(text: str, token: str) -> str:
    """The job's token never leaves the job (the server never echoes it: belt and braces)."""
    return text.replace(token, "***") if token else text


def write_summary(summary: Json, out: Path, token: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        redact(json.dumps(summary, indent=2, sort_keys=True), token) + "\n", encoding="utf-8"
    )


# --- report ---------------------------------------------------------------------------------


def deduplicate(summaries: Iterable[Json]) -> list[Json]:
    """One summary per workflow run: its first attempt (a re-run restores a database that
    already holds the first attempt's results, so it isn't an independent sample)."""
    first: dict[str, Json] = {}
    for summary in summaries:
        run = str(summary.get("run_id"))
        kept = first.get(run)
        if kept is None or _attempt(summary) < _attempt(kept):
            first[run] = summary
    return sorted(first.values(), key=lambda s: (str(s.get("at")), str(s.get("run_id"))))


def _attempt(summary: Json) -> int:
    attempt = summary.get("run_attempt")
    return attempt if isinstance(attempt, int) else 1


def _tally() -> Json:
    return {
        "runs": 0,
        "failures": 0,
        "flaky_excluded": 0,
        "caught": 0,
        "missed": 0,
        "recall": None,
        "missed_by_verdict": {},
        "missed_by_cause": {},
    }


def _time() -> Json:
    return {"total_ms": 0.0, "would_run_ms": 0.0, "saved": None}


def _week_start(at: datetime) -> datetime:
    day = at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def aggregate(
    summaries: Sequence[Json],
    repo: str,
    since: datetime,
    until: datetime,
    *,
    by_week: bool = False,
    exclude_forks: bool = False,
) -> Json:
    """A ``GET /repos/{repo}/recall``-shaped result from per-run summaries, plus a ``dogfood``
    part (what was read, skipped and why). Same units as the server's recall: each summary's
    shadow comparison already counts one test per CI run attempt."""
    result: Json = {
        "repo": repo,
        "days": max(1, (until - since).days),
        "since": since.isoformat(),
        "shadow_runs": 0,
        "selective_runs": 0,
        "full_runs": 0,
        "not_counted": Counter(),
        "in_progress": 0,
        "all": _tally(),
        "selective": _tally(),
        "time": _time(),
        "misses": [],
        "misses_truncated": False,
        "suggestions": [],
        "linked_not_selected": 0,
        "periods": None,
    }
    periods: dict[datetime, Json] = {}
    if by_week:
        start = _week_start(since)
        while start < until:
            periods[start] = {
                "start": start.isoformat(),
                "end": (start + timedelta(days=7)).isoformat(),
                "shadow_runs": 0,
                "selective_runs": 0,
                "full_runs": 0,
                "all": _tally(),
                "selective": _tally(),
                "time": _time(),
            }
            start += timedelta(days=7)
    info: Json = {
        "summaries": len(summaries),
        "runs": 0,
        "outside_range": 0,
        "forks": 0,
        "forks_excluded": exclude_forks,
        "malformed": 0,
        "verdicts": Counter(),
        "problems": Counter(),
    }
    for summary in deduplicate(summaries):
        try:
            at = ts(str(summary["at"]))
            if summary.get("schema") != SCHEMA or summary.get("repo") != repo:
                raise ValueError("another schema or repo")
        except (KeyError, ValueError):
            info["malformed"] += 1
            continue
        if not since <= at < until:
            info["outside_range"] += 1
            continue
        if summary.get("fork"):
            info["forks"] += 1
            if exclude_forks:
                continue
        info["runs"] += 1
        for problem in summary.get("problems") or []:
            info["problems"][str(problem).split(":")[0]] += 1
        for verdict, n in ((summary.get("triage") or {}).get("counts") or {}).items():
            info["verdicts"][verdict] += n
        targets = [result]
        if (period := periods.get(_week_start(at))) is not None:
            targets.append(period)
        try:
            _add_run(summary, at, targets, result)
        except (KeyError, TypeError, ValueError):
            info["malformed"] += 1
    for target in [result, *periods.values()]:
        _finish(target)
    result["not_counted"] = dict(result["not_counted"])
    result["misses"].sort(key=lambda m: (m["at"], m["test_id"]), reverse=True)
    result["misses_truncated"] = len(result["misses"]) > MAX_MISSES
    result["misses"] = result["misses"][:MAX_MISSES]
    if by_week:
        result["periods"] = list(periods.values())
    info["verdicts"] = {v: info["verdicts"][v] for v in VERDICT_ORDER if info["verdicts"][v]}
    info["problems"] = dict(info["problems"])
    result["dogfood"] = info
    return result


def _add_run(summary: Json, at: datetime, targets: list[Json], result: Json) -> None:
    shadow = summary.get("shadow")
    if not shadow:
        result["not_counted"]["no_comparison"] += 1
        return
    if not shadow["counted"]:
        statuses = [s["status"] for s in shadow.get("selections") or []] or ["not_stored"]
        for status in statuses:
            result["not_counted"][status] += 1
        return
    selective = shadow["mode"] == "selective"
    compared = [v for v in shadow["variants"] if v["status"] == "compared"]
    misses = shadow.get("missed_tests") or []
    causes = Counter(m.get("cause") or UNCLASSIFIED for m in misses)
    causes[UNCLASSIFIED] += max(0, shadow["missed"] - len(misses))  # beyond the first 100
    for target in targets:
        target["shadow_runs"] += 1
        target["selective_runs" if selective else "full_runs"] += 1
        target["time"]["total_ms"] += sum(v["ms_total"] for v in compared)
        target["time"]["would_run_ms"] += sum(v["ms_would_run"] for v in compared)
        for tally in (target["all"], target["selective"]) if selective else (target["all"],):
            tally["runs"] += 1
            tally["caught"] += shadow["caught"]
            tally["missed"] += shadow["missed"]
            tally["flaky_excluded"] += shadow["flaky_excluded"]
            for verdict, n in shadow["missed_by_verdict"].items():
                tally["missed_by_verdict"][verdict] = tally["missed_by_verdict"].get(verdict, 0) + n
            for cause, n in causes.items():
                tally["missed_by_cause"][cause] = tally["missed_by_cause"].get(cause, 0) + n
    for miss in misses:
        result["misses"].append(
            {
                "test_id": miss["test_id"],
                "ci_run_id": shadow["ci_run_id"],
                "run_attempt": shadow["run_attempt"],
                "commit_sha": summary.get("commit_sha") or "",
                "variants": miss.get("variants") or [None],
                "verdict": miss["verdict"],
                "selection_mode": shadow["mode"],
                "at": at.isoformat(),
                "cause": miss.get("cause"),
                "cause_detail": miss.get("cause_detail"),
                "suggestion": None,
            }
        )
        if miss.get("cause_detail") == LINKED_NOT_SELECTED:
            result["linked_not_selected"] += 1


def _finish(target: Json) -> None:
    for tally in (target["all"], target["selective"]):
        tally["failures"] = tally["caught"] + tally["missed"] + tally["flaky_excluded"]
        counted = tally["caught"] + tally["missed"]
        tally["recall"] = tally["caught"] / counted if counted else None
        # The keys /recall lists (api/routes/shadow.py): every verdict but flaky, every cause,
        # and unclassified only when there are some.
        verdicts, causes = tally["missed_by_verdict"], tally["missed_by_cause"]
        tally["missed_by_verdict"] = {v: verdicts.get(v, 0) for v in VERDICT_ORDER if v != "flaky"}
        tally["missed_by_cause"] = {c: causes.get(c, 0) for c in CAUSES}
        if causes.get(UNCLASSIFIED):
            tally["missed_by_cause"][UNCLASSIFIED] = causes[UNCLASSIFIED]
    time = target["time"]
    if time["total_ms"]:
        time["saved"] = 1 - time["would_run_ms"] / time["total_ms"]


def report_lines(result: Json, *, by_week: bool) -> list[str]:
    info = result["dogfood"]
    untrusted = "excluded" if info["forks_excluded"] else "untrusted: --exclude-forks drops them"
    forks = f", {info['forks']} from forks ({untrusted})"
    lines = [
        f"siftwise dogfood, {result['repo']}, since {result['since'][:10]}: "
        f"{info['runs']} runs{forks if info['forks'] else ''}"
    ]
    skipped = [
        f"{info[k]} {k.replace('_', ' ')}" for k in ("outside_range", "malformed") if info[k]
    ]
    if skipped:
        lines.append(f"Skipped: {', '.join(skipped)}")
    if not info["runs"]:
        return [*lines, "No dogfood runs in this range."]
    lines += ["", *(weekly_lines(result) if by_week else recall_lines(result))]
    if info["verdicts"]:
        verdicts = ", ".join(f"{n} {v.replace('_', ' ')}" for v, n in info["verdicts"].items())
        lines += ["", f"Failure verdicts (distinct failing tests per run): {verdicts}"]
    if info["problems"]:
        problems = ", ".join(f"{n} {p}" for p, n in info["problems"].items())
        lines.append(f"Runs with problems (fail-open): {problems}")
    return lines


# --- fetching -------------------------------------------------------------------------------


def download_summaries(gh: httpx.Client, repo: str, since: datetime) -> list[Json]:
    """Every unexpired dogfood summary artifact created since ``since`` (newest first; stops
    paging at the first older one)."""
    summaries: list[Json] = []
    page = 1
    while True:
        response = gh.get(
            f"/repos/{repo}/actions/artifacts", params={"per_page": 100, "page": page}
        )
        response.raise_for_status()
        artifacts = response.json().get("artifacts") or []
        for artifact in artifacts:
            if ts(artifact["created_at"]) < since:
                return summaries
            if artifact["expired"] or not artifact["name"].startswith(ARTIFACT_PREFIX):
                continue
            archive = gh.get(artifact["archive_download_url"])
            if archive.status_code != httpx.codes.OK:
                continue
            summary = _summary_in(archive.content)
            if summary is not None:
                summaries.append(summary)
        if len(artifacts) < 100:
            return summaries
        page += 1


def _summary_in(data: bytes) -> Json | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            loaded = json.loads(archive.read(SUMMARY_FILE))
    except (zipfile.BadZipFile, KeyError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def read_dir(directory: Path) -> list[Json]:
    found = []
    for path in sorted(directory.glob("*.json")):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(loaded, dict):
            found.append(loaded)
    return found


def save_dir(summaries: Iterable[Json], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for summary in summaries:
        name = f"{summary.get('run_id')}-{_attempt(summary)}.json"
        (directory / name).write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


# --- command line ---------------------------------------------------------------------------


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.dogfood_report")
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("summary", help="write this run attempt's summary (CI)")
    summary.add_argument("--out", type=Path, default=Path(SUMMARY_FILE))
    summary.add_argument("--server", default=env.get("SIFTWISE_SERVER", "http://localhost:8000"))
    report = commands.add_parser("report", help="recall and time saved over the summaries")
    report.add_argument("--repo", default="PramodBasavarajMenasi/siftwise")
    report.add_argument("--since", type=_date, help="YYYY-MM-DD (default: 6 weeks ago)")
    report.add_argument("--until", type=_date, help="YYYY-MM-DD, exclusive (default: now)")
    report.add_argument("--by", choices=["week"], help="one row per week")
    report.add_argument("--json", action="store_true", help="the aggregated result as JSON")
    report.add_argument("--from", dest="source", type=Path, help="read summaries from a folder")
    report.add_argument("--save", type=Path, help="also save the summaries to a folder")
    report.add_argument("--exclude-forks", action="store_true", help="leave out fork PR runs")
    args = parser.parse_args(argv)

    if args.command == "summary":
        token = env.get("SIFTWISE_API_TOKEN", "")
        built = build_summary(env, args.server, token)
        write_summary(built, args.out, token)
        for problem in built["problems"]:
            print(redact(f"::warning::siftwise dogfood: {problem}", token))
        return 0

    now = datetime.now(UTC)
    until = args.until or now
    since = args.since or until - timedelta(weeks=6)
    if args.source:
        summaries = read_dir(args.source)
    else:
        with client(github_token()) as gh:
            summaries = download_summaries(gh, args.repo, since)
    if args.save:
        save_dir(summaries, args.save)
    result = aggregate(
        summaries,
        args.repo,
        since,
        until,
        by_week=args.by == "week",
        exclude_forks=args.exclude_forks,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("\n".join(report_lines(result, by_week=args.by == "week")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
