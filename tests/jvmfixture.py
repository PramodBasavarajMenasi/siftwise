"""Copying the JVM fixtures (tests/fixtures/java_*, kotlin_*) for tests that build or edit them.

Tests never build in tests/fixtures: they copy the fixture to a temp dir first. The copy
leaves out build outputs that an IDE or a manual build may have left in the fixture (they're
gitignored), so they can't change what a test sees.
"""

import shutil
from pathlib import Path

BUILD_OUTPUTS = ("bin", "target", "build", "out", ".gradle", ".kotlin", "*.class", "*.jar")


def copy_fixture(fixture: Path, root: Path) -> Path:
    shutil.copytree(fixture, root, ignore=shutil.ignore_patterns(*BUILD_OUTPUTS))
    return root
