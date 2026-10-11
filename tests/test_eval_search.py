"""Pure helpers of the eval-repo search scripts (scripts/eval/deep_check.py)."""

from scripts.eval import deep_check as dc


def test_package_dirs_finds_nested_packages_and_skips_fixtures() -> None:
    tree = [
        "pyproject.toml",  # the root is not a package of its own
        "libs/core/pyproject.toml",
        "libs/partners/openai/pyproject.toml",
        "receiver/otlp/go.mod",
        "receiver/otlp/testdata/go.mod",
        "examples/demo/go.mod",
        "internal/tools/go.mod",
        "libs/core/src/core/__init__.py",
    ]
    assert dc.package_dirs(tree) == ["libs/partners/openai", "libs/core", "receiver/otlp"]


def test_packages_touched_uses_the_deepest_package() -> None:
    dirs = dc.package_dirs(["libs/core/pyproject.toml", "libs/core/plugins/x/pyproject.toml"])
    files = [
        "libs/core/a.py",
        "libs/core/plugins/x/b.py",
        "libs/core/README.md",  # not source: ignored
        "scripts/release.py",
    ]
    assert dc.packages_touched(files, dirs) == {"libs/core", "libs/core/plugins/x", "(root)"}
    assert dc.packages_touched(["docs/index.md"], dirs) == set()


def test_estimated_gb() -> None:
    # rdflib: ~9,900 results a leg x 16 legs x 190 CI runs ~ 29M results -> ~2.7 GB.
    assert dc.estimated_gb(9_900 * 16, 190) == 2.77
