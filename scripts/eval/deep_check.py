"""Deeper evidence for shortlisted eval repos (second pass, after ``find_candidates``).

For each repo: which workflow uploads the JUnit artifacts, how far back live artifacts go,
how many of that workflow's runs failed in the window and whether their JUnit shows real test
failures, and whether recent human PRs actually change source code.

For monorepo candidates it also reports the repo's packages (directories with a ``go.mod``,
``pyproject.toml`` or ``setup.py``), how many packages each sampled PR touches, and an
estimate of the backfill's size: results in one CI run (all its JUnit artifacts) times the
workflow's runs in the window, at ``BYTES_PER_RESULT`` each.

    uv run python -m scripts.eval.deep_check RDFLib/rdflib chunkhound/chunkhound --out deep.json

Needs GITHUB_TOKEN (env or .env).
"""

from __future__ import annotations

import json
import posixpath
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Annotated

import httpx
import typer

from scripts.eval._github import (
    Json,
    client,
    github_token,
    is_bot,
    is_junit_artifact,
    junit_sample,
    paged,
    since,
    ts,
)

# Stored size of one test result, indexes included: rdflib after migration 0006 (2,667 MB for
# 29.2M results).
BYTES_PER_RESULT = 92
MANIFESTS = frozenset({"go.mod", "pyproject.toml", "setup.py"})
# Manifests here are fixtures, examples or tooling, not packages a PR would change.
NOT_PACKAGES = frozenset({"testdata", "examples", "example", "docs", "doc", "tests", "test"})
NOT_PACKAGE_PATHS = frozenset({"internal/tools"})
SOURCE_EXTENSIONS = (".py", ".go")


def package_dirs(tree_paths: Iterable[str]) -> list[str]:
    """Directories holding a package manifest, below the repo root, deepest first."""
    dirs = set()
    for path in tree_paths:
        directory, name = posixpath.split(path)
        if name not in MANIFESTS or not directory:
            continue
        parts = directory.split("/")
        if NOT_PACKAGES.isdisjoint(parts) and not any(
            directory == p or directory.startswith(p + "/") or f"/{p}" in directory
            for p in NOT_PACKAGE_PATHS
        ):
            dirs.add(directory)
    return sorted(dirs, key=lambda d: (-d.count("/"), d))


def packages_touched(files: Iterable[str], dirs: Sequence[str]) -> set[str]:
    """The packages that changed source files belong to ("(root)" outside every package)."""
    touched = set()
    for name in files:
        if not name.endswith(SOURCE_EXTENSIONS):
            continue
        touched.add(next((d for d in dirs if name.startswith(d + "/")), "(root)"))
    return touched


def estimated_gb(results_per_run: int, runs: int) -> float:
    return round(results_per_run * runs * BYTES_PER_RESULT / 1e9, 2)


def run_results(gh: httpx.Client, repo: str, run_id: int, cap: int = 60) -> tuple[int, int, int]:
    """(results, JUnit artifacts, artifacts sampled) for one run: every JUnit artifact's test
    count, extrapolated from the first ``cap`` if there are more."""
    artifacts = [
        a
        for a in paged(gh, f"/repos/{repo}/actions/runs/{run_id}/artifacts", "artifacts", 3)
        if is_junit_artifact(a)
    ]
    counted = [junit_sample(gh, a, max_bytes=60_000_000) for a in artifacts[:cap]]
    tests = sum(sample.tests for sample in counted if sample)
    sampled = sum(1 for sample in counted if sample)
    if sampled and len(artifacts) > cap:
        tests = round(tests * len(artifacts) / sampled)
    return tests, len(artifacts), sampled


def run_test_failures(gh: httpx.Client, repo: str, run_id: int) -> tuple[int, int] | None:
    """(tests, failing tests) across up to 3 JUnit artifacts of a run; None if it has none."""
    response = gh.get(f"/repos/{repo}/actions/runs/{run_id}/artifacts")
    artifacts = [a for a in response.json().get("artifacts", []) if is_junit_artifact(a)]
    if not artifacts:
        return None
    tests = failures = 0
    for artifact in artifacts[:3]:
        sample = junit_sample(gh, artifact)
        if sample:
            tests += sample.tests
            failures += sample.failures
    return tests, failures


def check(gh: httpx.Client, repo: str, days: int, failed_runs: int, pr_sample: int) -> Json:
    cutoff = since(days)
    meta = gh.get(f"/repos/{repo}").json()
    junit = [
        a
        for a in paged(gh, f"/repos/{repo}/actions/artifacts", "artifacts", 5)
        if is_junit_artifact(a)
    ]
    workflows: Counter[int] = Counter()
    for artifact in junit[:30]:
        run = gh.get(f"/repos/{repo}/actions/runs/{artifact['workflow_run']['id']}").json()
        if "workflow_id" in run:
            workflows[run["workflow_id"]] += 1
    if not workflows:
        return {"error": "no workflow with live JUnit artifacts"}
    workflow_id = workflows.most_common(1)[0][0]
    workflow = gh.get(f"/repos/{repo}/actions/workflows/{workflow_id}").json()

    runs = [
        r
        for r in paged(
            gh,
            f"/repos/{repo}/actions/workflows/{workflow_id}/runs",
            "workflow_runs",
            5,
            status="completed",
        )
        if ts(r["created_at"]) >= cutoff
    ]
    failed = [r for r in runs if r["conclusion"] == "failure"]
    checked = []
    for run in failed[:failed_runs]:
        result = run_test_failures(gh, repo, run["id"])
        if result is not None:
            checked.append(
                {
                    "run": run["id"],
                    "event": run["event"],
                    "tests": result[0],
                    "failing_tests": result[1],
                }
            )

    prs = [
        p
        for p in paged(
            gh, f"/repos/{repo}/pulls", None, 3, state="all", sort="created", direction="desc"
        )
        if ts(p["created_at"]) >= cutoff
    ]
    human = [p for p in prs if not is_bot(p)]
    tree = gh.get(f"/repos/{repo}/git/trees/{meta['default_branch']}", params={"recursive": 1})
    dirs = package_dirs(e["path"] for e in tree.json().get("tree", []) if e["type"] == "blob")
    sample = []
    for pr in human[:pr_sample]:
        names = [
            f["filename"] for f in paged(gh, f"/repos/{repo}/pulls/{pr['number']}/files", None, 1)
        ]
        packages = packages_touched(names, dirs)
        sample.append(
            {
                "number": pr["number"],
                "title": pr["title"][:70],
                "files": len(names),
                "source": sum(n.endswith(SOURCE_EXTENSIONS) and "test" not in n.lower()
                              for n in names),
                "tests": sum(n.endswith(SOURCE_EXTENSIONS) and "test" in n.lower()
                             for n in names),
                "packages": sorted(packages),
            }
        )  # fmt: skip
    code_prs = [s for s in sample if s["packages"]]
    # The size of one full CI run: the latest successful run of the JUnit workflow.
    full_run = next((r for r in runs if r["conclusion"] == "success"), runs[0] if runs else None)
    results_per_run, junit_artifacts, sampled = (
        run_results(gh, repo, full_run["id"]) if full_run else (0, 0, 0)
    )

    return {
        "stars": meta["stargazers_count"],
        "workflow": workflow.get("path"),
        "junit_artifact_names": sorted({a["name"] for a in junit})[:3],
        # Lower bound: only the 500 most recent artifacts are listed.
        "oldest_live_junit_artifact": min((a["created_at"] for a in junit), default=None),
        "workflow_runs": len(runs),
        "events": dict(Counter(r["event"] for r in runs)),
        "failed_runs": len(failed),
        "failed_runs_checked": checked,
        "prs": len(prs),
        "human_prs": len(human),
        "distinct_human_authors": len({p["user"]["login"] for p in human}),
        "top_authors": Counter(p["user"]["login"] for p in human).most_common(3),
        "packages": len(dirs),
        "package_examples": dirs[-5:],
        "code_prs_sampled": len(code_prs),
        "code_prs_touching_one_package": sum(len(s["packages"]) == 1 for s in code_prs),
        "size": {
            "results_per_run": results_per_run,
            "junit_artifacts_per_run": junit_artifacts,
            "artifacts_counted": sampled,
            "runs_in_window": len(runs),
            "runs_capped_at_500": len(runs) >= 500,
            "estimated_gb": estimated_gb(results_per_run, len(runs)),
        },
        "human_pr_sample": sample,
    }


def main(
    repos: Annotated[list[str], typer.Argument(help="owner/name ...")],
    out: Annotated[Path, typer.Option(help="Where to write the JSON results.")] = Path(
        "eval-deep-check.json"
    ),
    days: Annotated[int, typer.Option(help="Activity window.")] = 90,
    failed_runs: Annotated[int, typer.Option(help="Failed runs to inspect per repo.")] = 12,
    pr_sample: Annotated[int, typer.Option(help="Human PRs to inspect per repo.")] = 15,
) -> None:
    gh = client(github_token())
    report: dict[str, Json] = {}
    for repo in repos:
        report[repo] = check(gh, repo, days, failed_runs, pr_sample)
        typer.echo(f"{repo}: done", err=True)
    out.write_text(json.dumps(report, indent=2))
    typer.echo(f"written to {out}", err=True)


if __name__ == "__main__":
    typer.run(main)
