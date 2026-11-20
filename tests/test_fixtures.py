"""Test fixtures (#73): .siftwise.toml `fixtures`, which tests reference a changed fixture
(cli/fixtures.py), how the selector runs them instead of the full suite, real build modules
keeping today's behaviour, and shadow mode's `fixture` miss cause."""

import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from siftwise.cli import main as cli
from siftwise.cli.fixtures import fixture_refs, fixture_units
from siftwise.cli.repoconfig import RepoConfig, RepoConfigError, loads_repo_config
from siftwise.core.selector import (
    ChangeLink,
    DependsRule,
    KnownTest,
    Mode,
    RepoHistory,
    Selection,
    SelectorConfig,
    Strength,
    select_tests,
)
from siftwise.core.shadow import RunChange, miss_cause
from siftwise.core.triage import TestHistory, TriageResult, Verdict

FIXTURES = ("tests/fixtures/**",)
ROOT_POM = "<project><modules><module>tests/fixtures/mvn</module></modules></project>"
CONFIG = RepoConfig(fixtures=FIXTURES)

# --- .siftwise.toml -----------------------------------------------------------------------


def test_fixtures_parse_and_match_like_depends_on() -> None:
    config = loads_repo_config('fixtures = ["tests/fixtures/**", "!tests/fixtures/real/**"]')

    assert config.fixtures == ("tests/fixtures/**", "!tests/fixtures/real/**")
    assert config.is_fixture("tests/fixtures/java_gradle/build.gradle.kts")
    assert not config.is_fixture("tests/fixtures/real/pom.xml")
    assert not config.is_fixture("build.gradle.kts")
    assert not RepoConfig().is_fixture("tests/fixtures/x")


@pytest.mark.parametrize(
    "content",
    ['fixtures = "tests/fixtures/**"', "fixtures = [1]", 'fixtures = [""]', 'fixtures = ["!a/**"]'],
)
def test_invalid_fixtures_are_rejected(content: str) -> None:
    with pytest.raises(RepoConfigError, match="fixtures must be a list of globs"):
        loads_repo_config(content)


def test_unknown_keys_still_error() -> None:
    with pytest.raises(RepoConfigError, match="unknown key"):
        loads_repo_config('fixtures = ["a/**"]\nfixture = ["b/**"]')


# --- the fixture unit and the match rule ---------------------------------------------------


@pytest.mark.parametrize(
    ("path", "globs", "units"),
    [
        ("tests/fixtures/java_gradle/build.gradle.kts", FIXTURES, ["tests/fixtures/java_gradle"]),
        ("tests/fixtures/pytest.xml", FIXTURES, ["tests/fixtures/pytest.xml"]),
        ("tests/fixtures/js/a/b.json", ("tests/fixtures/js/**",), ["tests/fixtures/js/a"]),
        ("pkg/x/fixtures/shop/pom.xml", ("**/fixtures/**",), ["pkg/x/fixtures/shop"]),
        ("data/sample.json", ("data/sample.json",), ["data/sample.json"]),
        (
            "tests/fixtures/a/b.txt",
            ("tests/**", "tests/fixtures/**"),
            ["tests/fixtures", "tests/fixtures/a"],
        ),
        ("tests/fixtures/a/b.txt", ("!tests/fixtures/**",), []),
    ],
)
def test_fixture_units(path: str, globs: tuple[str, ...], units: list[str]) -> None:
    assert fixture_units(path, globs) == units


def write(root: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")


SHOP = {
    "tests/fixtures/java_gradle/build.gradle.kts": "plugins { java }\n",
    "tests/fixtures/java_gradle/src/test/java/a/FixtureTest.java": "class FixtureTest {}\n",
    "tests/fixtures/sample.xml": "<testsuite/>\n",
    "tests/test_graph.py": 'GRADLE = Path(__file__).parent / "fixtures" / "java_gradle"\n',
    "tests/test_paths.py": 'ROOT = "tests/fixtures/java_gradle/build.gradle.kts"\n',
    "tests/test_other.py": 'X = "java_gradle2"  # another fixture, not this one\n',
    "tests/test_joined.py": 'p = os.path.join(base, "sample.xml")\n',
    "tests/helpers.py": 'SAMPLE = FIXTURES / "sample.xml"\n',
    "tests/test_uses_helper.py": "from tests.helpers import SAMPLE\n",
    "tests/conftest.py": "",
    "tests/__init__.py": "",
    "src/shop/gradle_support.py": 'NAME = "java_gradle"  # production code: not a helper\n',
    "src/shop/__init__.py": "",
    "web/src/money.test.ts": "import data from '../../tests/fixtures/sample.xml'\n",
    "pkg/money_test.go": 'var f = "testdata/../../tests/fixtures/java_gradle"\n',
}


def test_test_files_naming_the_fixture_reference_it(tmp_path: Path) -> None:
    write(tmp_path, SHOP)

    refs = fixture_refs(
        tmp_path,
        ["tests/fixtures/java_gradle/build.gradle.kts", "tests/fixtures/sample.xml"],
        CONFIG,
    )

    assert refs == {
        "tests/fixtures/java_gradle/build.gradle.kts": [
            "pkg/money_test.go",
            "tests/test_graph.py",
            "tests/test_paths.py",
        ],
        # test_uses_helper.py: through the helper module that names it
        "tests/fixtures/sample.xml": [
            "tests/test_joined.py",
            "tests/test_uses_helper.py",
            "web/src/money.test.ts",
        ],
    }


def test_a_fixture_nothing_references_is_still_sent(tmp_path: Path) -> None:
    write(tmp_path, {**SHOP, "tests/fixtures/lonely/pom.xml": "<project/>"})

    refs = fixture_refs(tmp_path, ["tests/fixtures/lonely/pom.xml"], CONFIG)

    assert refs == {"tests/fixtures/lonely/pom.xml": []}  # the server then runs the full suite


def test_only_fixture_files_count_and_docs_are_skipped(tmp_path: Path) -> None:
    write(tmp_path, SHOP)

    assert fixture_refs(tmp_path, ["build.gradle.kts", "tests/fixtures/README.md"], CONFIG) == {}
    assert fixture_refs(tmp_path, ["tests/fixtures/sample.xml"], RepoConfig()) == {}


@pytest.mark.parametrize(
    ("files", "changed"),
    [
        (
            {
                "settings.gradle.kts": 'include("tests:fixtures:java_gradle")\n',
                "tests/fixtures/java_gradle/build.gradle.kts": "",
            },
            "tests/fixtures/java_gradle/build.gradle.kts",
        ),
        (
            {
                "pom.xml": ROOT_POM,
                "tests/fixtures/mvn/pom.xml": "<project/>",
            },
            "tests/fixtures/mvn/pom.xml",
        ),
        (
            {
                "go.work": "go 1.22\n\nuse (\n\t.\n\t./tests/fixtures/gomod\n)\n",
                "go.mod": "module example.com/m\n",
                "tests/fixtures/gomod/go.mod": "module x\n",
            },
            "tests/fixtures/gomod/go.mod",
        ),
        (
            {"go.mod": "module example.com/m\n", "tests/fixtures/lib/lib.go": "package lib\n"},
            "tests/fixtures/lib/lib.go",
        ),
        (
            {
                "package.json": json.dumps({"workspaces": ["tests/fixtures/*"]}),
                "tests/fixtures/web/package.json": json.dumps({"name": "web"}),
            },
            "tests/fixtures/web/package.json",
        ),
    ],
    ids=["gradle-module", "maven-module", "go-work-use", "go-package", "js-workspace"],
)
def test_a_fixture_that_is_a_real_module_keeps_todays_behaviour(
    tmp_path: Path, files: dict[str, str], changed: str
) -> None:
    write(tmp_path, {**files, "tests/test_x.py": f"P = {posixpath_dir(changed)!r}\n"})

    assert fixture_refs(tmp_path, [changed], CONFIG) == {}


def posixpath_dir(path: str) -> str:
    return path.rsplit("/", 1)[0]


def test_a_go_fixture_with_its_own_module_is_a_fixture(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "go.mod": "module example.com/m\n",
            "tests/fixtures/gomod/go.mod": "module fixture\n",
            "tests/fixtures/gomod/lib.go": "package lib\n",
            "tests/test_go.py": 'ROOT = FIXTURES / "gomod"\n',
        },
    )

    refs = fixture_refs(tmp_path, ["tests/fixtures/gomod/lib.go"], CONFIG)

    assert refs == {"tests/fixtures/gomod/lib.go": ["tests/test_go.py"]}


# --- the selector --------------------------------------------------------------------------

KNOWN = RepoHistory(
    tests={
        t.test_id: t
        for t in (
            KnownTest("tests.test_graph::test_build"),
            KnownTest("tests.test_graph::test_modules"),
            KnownTest("tests.test_other::test_x"),
            KnownTest("example.com/m/pkg::TestMoney"),
            KnownTest("money::adds", file_path="web/src/money.test.ts"),
        )
    }
)
GRADLE = "tests/fixtures/java_gradle/build.gradle.kts"


def pick(
    *changed: str,
    refs: dict[str, list[str]] | None = None,
    depends: tuple[DependsRule, ...] = (),
) -> Selection:
    config = SelectorConfig(go_module="example.com/m", depends=depends)
    return select_tests(KNOWN, changed, True, config, fixture_refs=refs)


def test_a_fixture_build_file_runs_the_tests_that_reference_it() -> None:
    selection = pick(GRADLE, refs={GRADLE: ["tests/test_graph.py"]})

    assert selection.mode is Mode.SELECTIVE
    assert {t.test_id for t in selection.tests} == {
        "tests.test_graph::test_build",
        "tests.test_graph::test_modules",
    }
    assert {t.reason for t in selection.tests} == {
        f"fixture {GRADLE}, referenced by tests/test_graph.py"
    }
    assert selection.reason == (
        f"2 of 5 known tests selected; fixture {GRADLE}, referenced by tests/test_graph.py"
    )
    assert selection.python_files_run_whole == ("tests/test_graph.py",)
    assert selection.fixture_files == (GRADLE,)
    assert selection.commands == ("pytest tests/test_graph.py",)


def test_without_refs_a_fixture_build_file_is_a_build_file_as_before() -> None:
    selection = pick(GRADLE)

    assert selection.mode is Mode.FULL
    assert selection.reason == f"build/config file changed: {GRADLE}"


def test_a_fixture_no_test_references_runs_the_full_suite() -> None:
    selection = pick(GRADLE, refs={GRADLE: []})

    assert selection.mode is Mode.FULL
    assert selection.reason == (
        "fixture file changed and no test references its fixture (no "
        f"[[depends]] rule on it either): {GRADLE}"
    )


def test_a_referencing_file_with_no_runnable_tests_does_not_count() -> None:
    selection = pick(GRADLE, refs={GRADLE: ["tests/helpers_data.json"]})

    assert selection.mode is Mode.FULL
    assert "no test references its fixture" in selection.reason


def test_a_depends_rule_on_the_fixture_covers_it() -> None:
    rule = DependsRule("tests/test_other.py", ("tests/fixtures/**",))
    selection = pick(GRADLE, refs={GRADLE: []}, depends=(rule,))

    assert selection.mode is Mode.SELECTIVE
    assert [t.test_id for t in selection.tests] == ["tests.test_other::test_x"]


def test_go_and_jest_test_files_that_reference_a_fixture_run() -> None:
    selection = pick(GRADLE, refs={GRADLE: ["pkg/money_test.go", "web/src/money.test.ts"]})

    assert {t.test_id for t in selection.tests} == {"example.com/m/pkg::TestMoney", "money::adds"}
    assert selection.go_packages_run_whole == ("example.com/m/pkg",)


def test_a_new_test_file_referencing_a_fixture_runs_whole() -> None:
    selection = pick(GRADLE, refs={GRADLE: ["tests/test_new.py"]})

    assert selection.mode is Mode.SELECTIVE
    assert selection.commands == ("pytest tests/test_new.py",)


def test_other_build_files_still_force_the_full_suite() -> None:
    selection = pick(GRADLE, "build.gradle.kts", refs={GRADLE: ["tests/test_graph.py"]})

    assert selection.mode is Mode.FULL
    assert selection.reason == "build/config file changed: build.gradle.kts"


def test_a_nested_manifest_in_a_fixture_is_scoped_by_its_references() -> None:
    manifest = "tests/fixtures/js_monorepo/package.json"

    referenced = pick(manifest, refs={manifest: ["tests/test_graph.py"]})
    unreferenced = pick(manifest, refs={manifest: []})

    assert referenced.mode is Mode.SELECTIVE and len(referenced.tests) == 2
    # Without fixtures it selected nothing (tests under its directory: none); as a fixture
    # nothing referencing it is the fail-safe full suite.
    assert pick(manifest).reason == "no tests affected"
    assert unreferenced.mode is Mode.FULL


def test_a_deleted_referencing_test_file_is_not_run() -> None:
    selection = select_tests(
        KNOWN,
        [GRADLE, "tests/test_graph.py"],
        True,
        SelectorConfig(),
        deleted_files=["tests/test_graph.py"],
        fixture_refs={GRADLE: ["tests/test_graph.py"]},
    )

    assert selection.mode is Mode.FULL  # nothing left that references it


def test_a_fixture_file_that_is_a_known_test_file_still_runs_itself() -> None:
    test = "tests.fixtures.test_case::t"
    hist = RepoHistory(tests={test: KnownTest(test)})
    selection = select_tests(
        hist,
        ["tests/fixtures/test_case.py"],
        True,
        SelectorConfig(),
        fixture_refs={"tests/fixtures/test_case.py": []},
    )

    assert selection.mode is Mode.SELECTIVE
    assert [t.test_id for t in selection.tests] == [test]


# --- the CLI end to end ---------------------------------------------------------------------


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_local_change_sends_fixture_refs_for_select_only(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    write(tmp_path, {**SHOP, ".siftwise.toml": 'fixtures = ["tests/fixtures/**"]\n'})
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    write(tmp_path, {GRADLE: "plugins { java; application }\n"})
    git(tmp_path, "commit", "-qam", "change the fixture's build")

    change = cli.local_change("HEAD~1", "HEAD", cwd=tmp_path)

    expected = {GRADLE: ["pkg/money_test.go", "tests/test_graph.py", "tests/test_paths.py"]}
    assert change.select_only()["fixture_refs"] == expected
    assert "fixture_refs" not in change.fields  # /triage and recorded runs don't take it
    assert "fixture_refs" in cli.OPTIONAL_SELECT_FIELDS  # an older server's 422 drops it


# --- shadow mode ----------------------------------------------------------------------------


def result(*links: ChangeLink, verdict: Verdict = Verdict.NEW_REGRESSION) -> TriageResult:
    history = TestHistory(seen=True, file_path="tests/test_other.py")
    return TriageResult("tests.test_other::test_x", verdict, Strength.LOW, ("r",), links, history)


CHANGE = RunChange((GRADLE,), links_known=True)
BUILD_LINK = ChangeLink("build file", GRADLE, f"build/config file changed: {GRADLE}", Strength.LOW)


def test_a_miss_in_a_fixture_selection_is_a_fixture_miss() -> None:
    cause = miss_cause(result(BUILD_LINK), CHANGE, fixtures=(GRADLE,))

    assert cause.cause == "fixture"
    assert cause.detail == (
        "fixture file(s) changed and this test doesn't name their fixture "
        f"(it wasn't among the tests referencing it): {GRADLE}"
    )
    assert cause.suggestion == (
        '[[depends]]\ntests = "tests/test_other.py"\non = ["tests/fixtures/java_gradle/**"]'
    )


def test_a_fixture_miss_linked_through_other_code_is_not_called_a_fixture_miss() -> None:
    code = ChangeLink("path mapping", "src/x.py", "maps", Strength.HIGH)
    cause = miss_cause(
        result(BUILD_LINK, code), RunChange((GRADLE, "src/x.py"), True), fixtures=(GRADLE,)
    )

    assert cause.cause == "unknown"  # linked but not selected: maybe a selector bug


def test_a_pre_existing_fixture_miss_gets_no_rule_and_without_fixtures_nothing_changes() -> None:
    pre = miss_cause(result(verdict=Verdict.PRE_EXISTING), CHANGE, fixtures=(GRADLE,))

    assert (pre.cause, pre.suggestion) == ("fixture", None)
    assert miss_cause(result(BUILD_LINK), CHANGE).cause == "unknown"


# --- the API and the database ---------------------------------------------------------------


def test_select_takes_fixture_refs_and_shadow_stores_the_fixture_files(
    db_session: Session,
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from siftwise.api.main import create_app
    from siftwise.config import Settings
    from siftwise.core.models import CiSelection
    from siftwise.db import get_session

    app = create_app(Settings(_env_file=None, api_token="t"))
    app.dependency_overrides[get_session] = lambda: db_session
    auth = {"Authorization": "Bearer t"}
    sha = "c" * 40
    with TestClient(app) as client:
        meta = {
            "repo": "acme/shop",
            "commit_sha": sha,
            "branch": "main",
            "is_main": True,
            "ci_run_id": "1",
        }
        xml = (Path(__file__).parent / "fixtures" / "pytest.xml").read_bytes()
        assert (
            client.post(
                "/runs",
                files=[("files", ("p.xml", xml, "application/xml"))],
                data={"metadata": json.dumps(meta)},
                headers=auth,
            ).status_code
            == 201
        )
        body = {
            "repo": "acme/shop",
            "changed_files": [GRADLE],
            "fixture_refs": {GRADLE: ["tests/test_math.py"]},
            "shadow": {"ci_run_id": "2", "run_attempt": 1, "commit_sha": sha},
        }
        response = client.post("/select", json=body, headers=auth).json()

    assert response["mode"] == "selective"
    assert response["command"].startswith("pytest tests/test_math.py")  # + recently failed
    assert response["shadow_stored"] is True
    stored = db_session.scalars(select(CiSelection.fixture_files)).one()
    assert stored == [GRADLE]
