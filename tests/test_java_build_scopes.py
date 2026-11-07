"""A module's build file (pom.xml, build.gradle[.kts]) runs its module's tests and its
dependents', not the full suite (#54); everything siftwise can't scope stays full."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from siftwise.cli import main as cli
from siftwise.cli.javagraph import (
    build_scopes,
    detect_build,
    java_change,
    snapshot_from_json,
    snapshot_json,
)
from siftwise.cli.javagraph import (
    test_classes_of as classes_of,
)
from siftwise.core.schemas import SelectRequest
from siftwise.core.selector import (
    JavaBuildInfo,
    KnownTest,
    Mode,
    RepoHistory,
    select_tests,
)

FIXTURES = Path(__file__).parent / "fixtures"
MAVEN_FIXTURE = FIXTURES / "java_maven"
GRADLE_FIXTURE = FIXTURES / "java_gradle"
POM = '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>{}</project>'
MAVEN_TESTS = (
    "acme.core.MoneyTest::plus",
    "acme.app.CartTest::total",
    "acme.app.ReceiptTest::renders",
    "acme.app.CartIT::emptyCart",
)


def write(root: Path, files: dict[str, str]) -> Path:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    return root


def scopes(root: Path, *changed: str) -> dict[str, list[str]]:
    build = detect_build(root)
    assert build is not None
    return build_scopes(build, changed)


# --- the build model ------------------------------------------------------------------------


def test_the_fixtures_dependents() -> None:
    maven = detect_build(MAVEN_FIXTURE)
    gradle = detect_build(GRADLE_FIXTURE)
    assert maven is not None and gradle is not None
    # app's POM names core's artifactId; every child names the root as its parent.
    assert dict(maven.dependents) == {"": ("app", "core", "extras"), "core": ("app",)}
    assert dict(maven.unscoped) == {"": "the root pom.xml"}
    # app/web's build file has project(":core").
    assert dict(gradle.dependents) == {"core": ("app/web",)}
    assert maven.scopes_known and gradle.scopes_known


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["core/pom.xml"], {"core/pom.xml": ["app", "core"]}),  # app depends on core
        (["app/pom.xml"], {"app/pom.xml": ["app"]}),  # nothing depends on app
        # extras isn't analysed (mapstruct generates sources), but its test classes are its
        # src/test/java files all the same.
        (["extras/pom.xml"], {"extras/pom.xml": ["extras"]}),
        (["pom.xml"], {}),  # the root: full suite
        (["core/src/test/resources/pom.xml"], {}),  # not a module the build model places
        (["core/build.gradle"], {}),  # not Maven's build file
    ],
)
def test_maven_scopes(changed: list[str], expected: dict[str, list[str]]) -> None:
    assert scopes(MAVEN_FIXTURE, *changed) == expected


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["core/build.gradle.kts"], {"core/build.gradle.kts": ["app/web", "core"]}),
        (["app/web/build.gradle.kts"], {"app/web/build.gradle.kts": ["app/web"]}),
        (["kt/build.gradle.kts"], {}),  # a Kotlin project isn't analysed
        (["build.gradle.kts"], {}),  # the root
        (["settings.gradle.kts", "gradle/libs.versions.toml"], {}),
    ],
)
def test_gradle_scopes(changed: list[str], expected: dict[str, list[str]]) -> None:
    assert scopes(GRADLE_FIXTURE, *changed) == expected


def maven(tmp_path: Path, extra: dict[str, str]) -> Path:
    files = {
        "pom.xml": POM.format(
            "<artifactId>root</artifactId><packaging>pom</packaging>"
            "<modules><module>a</module><module>b</module>"
            "<module>c</module></modules>"
        ),
        "a/pom.xml": POM.format("<artifactId>a</artifactId>"),
        "b/pom.xml": POM.format(
            "<artifactId>b</artifactId><dependencies><dependency>"
            "<artifactId>a</artifactId></dependency></dependencies>"
        ),
        "c/pom.xml": POM.format(
            "<artifactId>c</artifactId><build><plugins><plugin>"
            "<dependencies><dependency><artifactId>b</artifactId>"
            "</dependency></dependencies></plugin></plugins></build>"
        ),
    }
    return write(tmp_path, {**files, **extra})


def test_maven_dependents_are_transitive_and_count_plugin_dependencies(tmp_path: Path) -> None:
    root = maven(tmp_path, {})
    # c uses b as a plugin dependency; b depends on a: a change to a's POM reaches all three.
    assert scopes(root, "a/pom.xml") == {"a/pom.xml": ["a", "b", "c"]}
    assert scopes(root, "c/pom.xml") == {"c/pom.xml": ["c"]}


@pytest.mark.parametrize(
    ("pom", "why"),
    [
        ("<artifactId>a</artifactId><dependencyManagement/>", "manages versions"),
        ("<artifactId>a</artifactId><build><pluginManagement/></build>", "manages versions"),
        ("<artifactId>a</artifactId><packaging>pom</packaging>", "parent or aggregator"),
        ("<artifactId>a</artifactId><modules><module>x</module></modules>", "aggregator"),
    ],
)
def test_poms_others_may_inherit_from_stay_full(tmp_path: Path, pom: str, why: str) -> None:
    write(tmp_path, {"a/x/pom.xml": POM.format("<artifactId>x</artifactId>")})
    root = maven(tmp_path, {"a/pom.xml": POM.format(pom)})
    build = detect_build(root)
    assert build is not None
    assert why in build.unscoped["a"]
    assert scopes(root, "a/pom.xml") == {}


def test_a_parent_pom_stays_full(tmp_path: Path) -> None:
    child = POM.format("<parent><artifactId>a</artifactId><relativePath>../a</relativePath>"
                       "</parent><artifactId>b</artifactId>")  # fmt: skip
    root = maven(tmp_path, {"b/pom.xml": child})
    build = detect_build(root)
    assert build is not None and build.unscoped["a"] == "the parent POM of b"
    assert scopes(root, "a/pom.xml") == {}


@pytest.mark.parametrize(
    "pom",
    [
        "<artifactId>c</artifactId><dependencies><dependency><artifactId>${dep}</artifactId>"
        "</dependency></dependencies>",
        "<artifactId>${name}</artifactId>",
    ],
)
def test_an_artifact_id_built_from_a_property_scopes_nothing(tmp_path: Path, pom: str) -> None:
    root = maven(tmp_path, {"c/pom.xml": POM.format(pom)})
    build = detect_build(root)
    assert build is not None and not build.scopes_known
    assert scopes(root, "a/pom.xml", "b/pom.xml") == {}


def test_a_comment_names_no_dependency(tmp_path: Path) -> None:
    commented = POM.format("<artifactId>c</artifactId><!-- <artifactId>a</artifactId> -->")
    root = maven(tmp_path, {"c/pom.xml": commented})
    assert scopes(root, "a/pom.xml") == {"a/pom.xml": ["a", "b"]}


def gradle(tmp_path: Path, extra: dict[str, str]) -> Path:
    files = {
        "settings.gradle": "include 'a', 'b', 'c'",
        "build.gradle": "",
        "a/build.gradle": "",
        "b/build.gradle": "dependencies { implementation project(':a') }",
        "c/build.gradle.kts": 'dependencies { testImplementation(project(path = ":b")) }',
    }
    return write(tmp_path, {**files, **extra})


def test_gradle_dependents_are_transitive(tmp_path: Path) -> None:
    root = gradle(tmp_path, {})
    assert scopes(root, "a/build.gradle") == {"a/build.gradle": ["a", "b", "c"]}
    assert scopes(root, "c/build.gradle.kts") == {"c/build.gradle.kts": ["c"]}


def test_a_root_build_files_project_reference_counts_for_every_project(tmp_path: Path) -> None:
    root = gradle(tmp_path, {"build.gradle": "subprojects { dependencies { "
                             "implementation project(':a') } }"})  # fmt: skip
    # The root project counts too: the root build file can't say whose block it is in.
    assert scopes(root, "a/build.gradle") == {"a/build.gradle": ["", "a", "b", "c"]}
    assert scopes(root, "b/build.gradle") == {"b/build.gradle": ["b", "c"]}


@pytest.mark.parametrize(
    "build_file",
    [
        "subprojects { apply plugin: 'java' }",
        "allprojects { }",
        "configure(subprojects) { }",
        "evaluationDependsOn(':b')",
        "rootProject.ext.x = 1",
    ],
)
def test_a_gradle_build_file_that_configures_others_stays_full(
    tmp_path: Path, build_file: str
) -> None:
    root = gradle(tmp_path, {"a/build.gradle": build_file})
    assert scopes(root, "a/build.gradle") == {}


@pytest.mark.parametrize(
    "unread",
    [
        "dependencies { implementation(projects.a) }",  # typesafe project accessors
        "dependencies { implementation project(name) }",  # not a literal
    ],
)
def test_project_references_siftwise_cant_read_scope_nothing(tmp_path: Path, unread: str) -> None:
    root = gradle(tmp_path, {"c/build.gradle.kts": unread})
    build = detect_build(root)
    assert build is not None and not build.scopes_known
    assert scopes(root, "a/build.gradle") == {}


def test_snapshots_keep_the_scoping_and_an_old_one_scopes_nothing() -> None:
    build = detect_build(MAVEN_FIXTURE)
    assert build is not None
    data = snapshot_json(build, classes_of(MAVEN_FIXTURE, build), None)
    again, _, _ = snapshot_from_json(json.loads(json.dumps(data)))
    assert again == build
    for key in ("dependents", "unscoped", "scopes_known"):
        del data["build"][key]
    old, _, _ = snapshot_from_json(data)
    assert not old.scopes_known and build_scopes(old, ["core/pom.xml"]) == {}


# --- selection ------------------------------------------------------------------------------


def history(*ids: str) -> RepoHistory:
    return RepoHistory(tests={i: KnownTest(i, avg_duration_ms=10, runs=20) for i in ids})


def info(fixture: Path, *changed: str) -> JavaBuildInfo:
    change = java_change(fixture, list(changed))
    assert change is not None
    return JavaBuildInfo.from_json(change.payload())


def test_a_module_pom_runs_its_modules_and_dependents_tests() -> None:
    java = info(MAVEN_FIXTURE, "core/pom.xml")
    assert java.build_scopes == {"core/pom.xml": ("app", "core")}
    selection = select_tests(history(*MAVEN_TESTS), ["core/pom.xml"], True, java=java)
    assert selection.mode is Mode.SELECTIVE, selection.reason
    reasons = {t.test_id: t.reasons for t in selection.tests}
    assert set(reasons) == set(MAVEN_TESTS)
    assert reasons["acme.core.MoneyTest::plus"] == (
        "build file core/pom.xml changed (its module core)",
    )
    assert reasons["acme.app.CartTest::total"] == (
        "build file core/pom.xml changed (app depends on core)",
    )
    assert selection.commands == (
        "mvn -B -pl app,core -am verify "
        "-Dtest=acme.app.CartTest,acme.app.ReceiptTest,acme.core.MoneyTest "
        "-Dsurefire.failIfNoSpecifiedTests=false -Dit.test=acme.app.CartIT "
        "-Dit.failIfNoSpecifiedTests=false",
    )


def test_a_leaf_modules_pom_runs_only_its_module() -> None:
    java = info(MAVEN_FIXTURE, "app/pom.xml")
    selection = select_tests(history(*MAVEN_TESTS), ["app/pom.xml"], True, java=java)
    assert {t.test_id for t in selection.tests} == set(MAVEN_TESTS) - {"acme.core.MoneyTest::plus"}


def test_a_gradle_module_build_file_runs_its_projects_tests() -> None:
    ids = ("acme.core.MoneyTest::plus()", "acme.web.CartTest::total()")
    java = info(GRADLE_FIXTURE, "app/web/build.gradle.kts")
    selection = select_tests(history(*ids), ["app/web/build.gradle.kts"], True, java=java)
    # ReceiptTest has no history: a new class of the module, run as such.
    assert selection.commands == (
        "gradle :app:web:test --tests acme.web.CartTest --tests acme.web.ReceiptTest",
    )


@pytest.mark.parametrize(
    "changed",
    [["pom.xml"], ["core/pom.xml", "pom.xml"], [".mvn/maven.config"]],
)
def test_what_stays_full(changed: list[str]) -> None:
    java = info(MAVEN_FIXTURE, *changed)
    selection = select_tests(history(*MAVEN_TESTS), changed, True, java=java)
    assert selection.mode is Mode.FULL
    assert selection.reason.startswith("build/config file changed")


def test_without_scopes_a_module_pom_is_still_full() -> None:
    """An older CLI sends no build_scopes: as before."""
    payload: dict[str, Any] = java_change(MAVEN_FIXTURE, ["core/pom.xml"]).payload()  # type: ignore[union-attr]
    del payload["build_scopes"]
    selection = select_tests(history(*MAVEN_TESTS), ["core/pom.xml"], True,
                             java=JavaBuildInfo.from_json(payload))  # fmt: skip
    assert selection.mode is Mode.FULL


def test_the_payload_has_build_scopes_only_when_there_are_some() -> None:
    change = java_change(MAVEN_FIXTURE, ["core/src/main/java/acme/core/Money.java"])
    assert change is not None and "build_scopes" not in change.payload()


# --- the CLI and the API ----------------------------------------------------------------------


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_the_cli_sends_the_scope_of_a_changed_module_pom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    shutil.copytree(MAVEN_FIXTURE, root, ignore=shutil.ignore_patterns(".gradle", "target"))
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    pom = root / "core" / "pom.xml"
    pom.write_text(pom.read_text().replace("</project>", "<!-- bump -->\n</project>"))
    git(root, "commit", "-am", "bump core")
    monkeypatch.chdir(root)

    body = cli.selection_request("acme/shop", "HEAD~1", "HEAD")

    assert body["changed_files"] == ["core/pom.xml"]
    assert body["java"]["build_scopes"] == {"core/pom.xml": ["app", "core"]}
    request = SelectRequest.model_validate(body)  # the server's schema takes it
    assert request.java is not None and request.java.build_scopes == body["java"]["build_scopes"]


def test_the_schema_checks_build_scopes() -> None:
    base = {"repo": "a/b", "changed_files": ["core/pom.xml"],
            "java": {"tool": "maven", "build_scopes": {"core/pom.xml": ["core"]}}}  # fmt: skip
    assert SelectRequest.model_validate(base).java is not None
    with pytest.raises(ValidationError):
        SelectRequest.model_validate({**base, "java": {"tool": "maven", "build_scopes": ["x"]}})


def test_a_scope_with_tests_the_class_lists_may_miss_stays_full(tmp_path: Path) -> None:
    """b has Kotlin tests (not in the class lists): a's POM reaches b, so it stays full."""
    root = maven(tmp_path, {"b/src/test/kotlin/BTest.kt": "class BTest"})
    assert scopes(root, "a/pom.xml") == {}
    assert scopes(root, "c/pom.xml") == {"c/pom.xml": ["c"]}


def test_versions_a_bom_manages_are_no_dependency(tmp_path: Path) -> None:
    """A BOM lists every module in <dependencyManagement> and a parent imports it: neither
    uses the modules, so a module's POM change doesn't reach every child through them."""
    bom = POM.format("<artifactId>c</artifactId><dependencyManagement><dependencies>"
                     "<dependency><artifactId>a</artifactId></dependency>"
                     "</dependencies></dependencyManagement>")  # fmt: skip
    root = maven(tmp_path, {"c/pom.xml": bom})
    assert scopes(root, "a/pom.xml") == {"a/pom.xml": ["a", "b"]}
