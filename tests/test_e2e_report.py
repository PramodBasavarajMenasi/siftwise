"""scripts/e2e_report.py, and the e2e workflow's checks run on reports siftwise renders itself:
a formatting change that would break .github/workflows/action-e2e.yml fails here first."""

import re
import shlex
from pathlib import Path

import pytest

from scripts import e2e_report
from siftwise.cli import gha, gitlab
from tests.test_gha import RUN_URL, compare_response, failure, selection, triage_response

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "action-e2e.yml").read_text(encoding="utf-8")
FAILING = "test_e2e_failing::test_e2e_always_fails"
SHADOW_NEW = "test_e2e_shadow_new::test_e2e_shadow_new"


def failures_report() -> str:
    """What the e2e rollup's report holds: the always-failing test, unknown, one variant."""
    response = triage_response(
        failure(FAILING, "unknown", None, "low", reason="no history for this test before this run"),
        variants=(None,),
    )
    return gha.failures_markdown(response, RUN_URL)


def comment(failures: str) -> str:
    marker = "<!-- siftwise:failures sha=" + "a" * 40 + " attempt=1 -->"
    table = gha.selection_markdown(selection(), "a" * 40)
    return gha.compose_comment(table, f"{marker}\n{failures}")


def unavailable_report() -> str:
    return gha.unavailable_markdown("could not reach siftwise at http://127.0.0.1:1: refused")


def shadow_reports() -> list[str]:
    missed = {"test_id": SHADOW_NEW, "verdict": "unknown", "cause": "new_test", "variants": [None]}
    selective = compare_response(caught=1, missed=1, failing_tests=2, flaky_excluded=0,
                                 missed_tests=[missed])  # fmt: skip
    full = compare_response(mode="full", caught=2, missed=0, failing_tests=2, flaky_excluded=0,
                            missed_tests=[])  # fmt: skip
    shadow_run = gha.selection_markdown(gha.shadow_result(selection()), "", shadow=True,
                                        would_have=selection())  # fmt: skip
    return [gha.shadow_markdown(selective), gha.shadow_markdown(full), shadow_run]


@pytest.fixture(params=["github", "gitlab"])
def reports(request: pytest.FixtureRequest) -> dict[str, str]:
    """Every report the e2e checks read, as each provider renders it."""
    provider = gitlab.GitLabProvider("rollup", {}) if request.param == "gitlab" else gha.GITHUB
    with gha.use_provider(provider):
        failures = failures_report()
        return {
            "failures": failures,
            "comment": comment(failures),
            "unavailable": unavailable_report(),
            "shadow": "\n".join(shadow_reports()),
        }


def test_a_verdict_is_matched_by_meaning(reports: dict[str, str]) -> None:
    for report in (reports["failures"], reports["comment"]):
        assert e2e_report.has_verdict(report, FAILING, "unknown", "low")
        # Still strict where it matters.
        assert not e2e_report.has_verdict(report, FAILING, "new_regression", "low")
        assert not e2e_report.has_verdict(report, FAILING, "unknown", "high")
        assert not e2e_report.has_verdict(report, "test_e2e_failing::other", "unknown", "low")


def test_verdict_names_and_labels_both_match() -> None:
    response = triage_response(failure("t::a", "new_regression", None, "medium"), variants=(None,))
    report = gha.failures_markdown(response, RUN_URL)  # "**new regression**"

    assert e2e_report.has_verdict(report, "t::a", "new_regression", "medium")
    assert e2e_report.has_verdict(report, "t::a", "new regression", "medium")
    assert not e2e_report.has_verdict(report, "t::a", "unknown", "medium")


def test_ids_with_pipes_and_backticks_are_read_back() -> None:
    test_id = "t::x[a|b`c]"
    response = triage_response(failure(test_id, "flaky", None, "high"), variants=(None,))

    assert e2e_report.has_verdict(gha.failures_markdown(response, RUN_URL), test_id, "flaky",
                                  "high")  # fmt: skip


def test_a_missed_test_is_matched_by_cause_and_verdict(reports: dict[str, str]) -> None:
    assert e2e_report.has_missed(reports["shadow"], SHADOW_NEW, "new test", "unknown")
    assert e2e_report.has_missed(reports["shadow"], SHADOW_NEW, "new_test", "unknown")
    assert not e2e_report.has_missed(reports["shadow"], SHADOW_NEW, "no link", "unknown")
    assert not e2e_report.has_missed(reports["shadow"], SHADOW_NEW, "new test", "flaky")


def test_text_is_matched_without_formatting(reports: dict[str, str]) -> None:
    unavailable = "Failure verdicts unavailable: could not reach siftwise"
    assert e2e_report.has_text(reports["unavailable"], unavailable)
    assert not e2e_report.has_text(reports["unavailable"], "nothing recorded")


def workflow_checks() -> list[list[str]]:
    """Every `scripts/e2e_report.py` call in the workflow, as its arguments."""
    calls = re.findall(r"scripts/e2e_report\.py ((?:[^\n\\]|\\\n)+)", WORKFLOW)
    parsed = []
    for call in calls:
        args = shlex.split(call.replace("\\\n", " ").split("<<<")[0])
        parsed.append(args)
    return parsed


# Which rendered report each workflow check reads (by its first argument).
REPORT_OF = {"$REPORT": ("failures", "shadow"), "-": ("comment",), "$DOWN_REPORT": ("unavailable",)}


def test_the_workflows_checks_pass_on_siftwises_own_reports(
    reports: dict[str, str], tmp_path: Path
) -> None:
    checks = workflow_checks()
    assert {args[0] for args in checks} == {"verdict", "missed", "text"}
    for check, source, *rest in checks:
        candidates = [reports[name] for name in REPORT_OF[source]]
        results = []
        for report in candidates:
            path = tmp_path / "report.md"
            path.write_text(report, encoding="utf-8")
            results.append(e2e_report.main([check, str(path), *rest]))
        assert 0 in results, (check, source, rest)


def test_the_workflows_literal_greps_match_siftwises_own_reports(reports: dict[str, str]) -> None:
    """The remaining `grep -F` checks on siftwise's Markdown (its own wording, not test IDs)."""
    everything = "\n".join(reports.values())
    patterns = re.findall(
        r"grep -F (?:-- )?'([^']+)' (?:\"\$(?:REPORT|DOWN_REPORT)\"|<<< \"\$body\")", WORKFLOW
    )
    assert patterns
    for pattern in patterns:
        assert pattern in everything, pattern
    # No check may depend on how untrusted text is escaped.
    assert "<code>" not in WORKFLOW and "\\_" not in WORKFLOW
