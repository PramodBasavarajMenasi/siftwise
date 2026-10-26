"""Checks on siftwise's Markdown reports (job summary, report file, PR comment) for
.github/workflows/action-e2e.yml, by meaning rather than punctuation: a failing test's row
(test ID, verdict, confidence), a missed test's line (cause, verdict), or a piece of text.
Code spans, bold and table escapes don't matter; a wrong verdict, confidence or cause does.

    python scripts/e2e_report.py verdict REPORT TEST_ID VERDICT CONFIDENCE
    python scripts/e2e_report.py missed REPORT TEST_ID CAUSE VERDICT
    python scripts/e2e_report.py text REPORT TEXT

REPORT is a file, or - for stdin. Verdicts as siftwise names them (new_regression) or shows
them (new regression). Exit 0 on a match, else 1 with what the report has for that test.
tests/test_e2e_report.py runs these checks on reports rendered by siftwise's own code, so a
formatting change fails locally, not only in the e2e workflow. Standard library only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# A code span: a run of backticks, the text, the same run (siftwise pads with one space).
_CODE_SPAN = re.compile(r"(`+)(.*?)\1(?!`)")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
# "- <test>: <cause> (verdict: <verdict>)" or, from an older server, "- <test> (<verdict>)".
_MISSED = re.compile(r"^- (`+)(?P<test>.*?)\1(?!`)(?::\s*(?P<cause>.*?))?\s*"
                     r"\((?:verdict: )?(?P<verdict>[^()]*)\)\s*$")  # fmt: skip


def _span_text(match: re.Match[str]) -> str:
    text = match[2]
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] == " " else text


def plain(markdown: str) -> str:
    """The text as read: code spans unwrapped, bold and table escapes removed."""
    return _CODE_SPAN.sub(_span_text, markdown).replace("**", "").replace("\\|", "|")


def words(value: str) -> str:
    """A verdict, confidence or cause, compared loosely: case, ``_`` and ``-`` don't matter."""
    return re.sub(r"[\s_-]+", " ", plain(value)).strip().lower()


def rows(markdown: str) -> list[list[str]]:
    """Every table row's cells, as read (header and separator rows included)."""
    found = []
    for line in markdown.splitlines():
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            cells = _CELL_SPLIT.split(line)[1:-1]
            found.append([plain(cell).strip() for cell in cells])
    return found


def rows_for(markdown: str, test_id: str) -> list[list[str]]:
    """The failures table rows of ``test_id``: [test, verdict, confidence, ..., reason]."""
    return [row for row in rows(markdown) if row and row[0] == test_id]


def has_verdict(markdown: str, test_id: str, verdict: str, confidence: str) -> bool:
    return any(len(row) >= 3 and words(row[1]) == words(verdict)
               and words(row[2]) == words(confidence)
               for row in rows_for(markdown, test_id))  # fmt: skip


def missed_lines(markdown: str, test_id: str) -> list[tuple[str, str]]:
    """The shadow run's "Missed:" lines of ``test_id``: (cause, verdict)."""
    found = []
    for line in markdown.splitlines():
        match = _MISSED.match(line.strip())
        if match is None:
            continue
        test = match["test"]
        if len(test) >= 2 and test[0] == test[-1] == " ":
            test = test[1:-1]
        if test == test_id:
            found.append((plain(match["cause"] or ""), plain(match["verdict"])))
    return found


def has_missed(markdown: str, test_id: str, cause: str, verdict: str) -> bool:
    return any(words(c) == words(cause) and words(v) == words(verdict)
               for c, v in missed_lines(markdown, test_id))  # fmt: skip


def has_text(markdown: str, text: str) -> bool:
    return text in plain(markdown)


def main(argv: list[str]) -> int:
    usage = __doc__.split("\n\n")[1]
    if len(argv) < 3 or argv[0] not in ("verdict", "missed", "text"):
        print(usage, file=sys.stderr)
        return 2
    check, source, *args = argv
    markdown = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    if check == "verdict" and len(args) == 3:
        if has_verdict(markdown, *args):
            return 0
        print(f"no row for {args[0]} with {args[1]}, {args[2]}; its rows: "
              f"{rows_for(markdown, args[0])}", file=sys.stderr)  # fmt: skip
    elif check == "missed" and len(args) == 3:
        if has_missed(markdown, *args):
            return 0
        print(f"no missed line for {args[0]} with {args[1]}, {args[2]}; its lines: "
              f"{missed_lines(markdown, args[0])}", file=sys.stderr)  # fmt: skip
    elif check == "text" and len(args) == 1:
        if has_text(markdown, args[0]):
            return 0
        print(f"not in the report: {args[0]!r}", file=sys.stderr)
    else:
        print(usage, file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
