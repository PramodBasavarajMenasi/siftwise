"""The Mermaid diagrams in the repo's Markdown files stay parseable on GitHub.

Checked without Mermaid itself: two mistakes that broke ARCHITECTURE.md's diagrams
("Unable to render rich display"). A `;` in a sequence diagram's message or note ends the
statement, and a flowchart label starting with `/` or `\\` opens a parallelogram shape
(`[/text/]`) that the label never closes.
"""

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"node_modules", "venv", "__pycache__"}
BLOCK = re.compile(r"^```mermaid[ \t]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
# A message (`A->>B: text`, any arrow) or a note (`Note over A,B: text`).
SEQUENCE_TEXT = re.compile(r"^\s*(?:Note\b[^:]*|[\w ]+?\s*[-.=x)<>]+[+-]?\s*[\w ]+?)\s*:(.*)$")
# An unquoted node label opening with / or \ but not closing with one: [/x] ([/x/] is a shape).
SLANTED_LABEL = re.compile(r'\[[/\\][^\]"]*[^\]"/\\]\]')


def markdown_files() -> list[Path]:
    found = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
        found += [Path(folder, f) for f in files if f.endswith(".md")]
    return sorted(found)


def mermaid_blocks() -> list[tuple[str, str]]:
    return [
        (f"{path.relative_to(ROOT).as_posix()} block {n}", block)
        for path in markdown_files()
        for n, block in enumerate(BLOCK.findall(path.read_text(encoding="utf-8")), start=1)
    ]


def problems(block: str) -> list[str]:
    lines = block.splitlines()
    kind = next((line.split()[0] for line in lines if line.strip()), "")
    found = []
    for line in lines[1:]:
        if kind == "sequenceDiagram":
            text = SEQUENCE_TEXT.match(line)
            if text and ";" in text.group(1):
                found.append(f"';' ends the statement: {line.strip()}")
        elif kind in ("flowchart", "graph") and SLANTED_LABEL.search(line):
            found.append(f"label starting with / or \\ needs quotes: {line.strip()}")
    return found


def test_the_repo_has_mermaid_diagrams() -> None:
    names = [name for name, _ in mermaid_blocks()]
    assert any(name.startswith("ARCHITECTURE.md") for name in names)
    assert any(name.startswith("README.md") for name in names)


@pytest.mark.parametrize(("name", "block"), mermaid_blocks())
def test_mermaid_blocks_have_no_text_that_breaks_parsing(name: str, block: str) -> None:
    assert problems(block) == [], name


def test_the_check_finds_the_mistakes_that_broke_architecture_md() -> None:
    sequence = (
        "sequenceDiagram\n"
        "    Note over API,DB: the selection is recorded (ci_selections); a shadow run\n"
        "    API->>DB: verdicts vs its selection (rank, ran or not); shadow: caught\n"
        "    API-->>CLI: a verdict per failing test, fail-on\n"
    )
    flowchart = (
        "flowchart LR\n"
        "    D -->|valid session| U[/ui pages]\n"
        '    D -->|valid session| V["/ui pages"]\n'
        "    P[/parallelogram/] --> Q[(db)]\n"
    )

    assert len(problems(sequence)) == 2
    assert problems(flowchart) == [
        "label starting with / or \\ needs quotes: D -->|valid session| U[/ui pages]"
    ]
