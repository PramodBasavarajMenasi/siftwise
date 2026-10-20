"""Run the README Quickstart, then check its result.

    python scripts/run_quickstart.py [--published] [--port N] [--keep] [--bash PATH]

Takes the README's ```` ```sh quickstart ```` blocks in order and runs them in ONE bash (a
block's ``cd`` and ``export`` carry on), from the repo root, with ``set -euo pipefail``. Then
checks the selection the last block wrote (``selected.sh``) and prints each block's elapsed
time and the total. On failure it prints the server's logs; ``docker compose down -v`` at the
end, unless ``--keep``. CI runs it in a fresh checkout (.github/workflows/quickstart.yml).
Needs bash, Docker, git, uv, curl, openssl.

The README installs the RELEASED siftwise: the published server image and the CLI from PyPI.
By default the run tests THIS checkout instead, so a pull request's code is what runs: two
exact rewrites (``FROM_CHECKOUT``: the image is built, ``--build``, under its own name,
``CHECKOUT_IMAGE``; the CLI is installed from ``.``), each of which must match its blocks once.
``--published`` runs the blocks unchanged (after a release, to check what users get).

Only the setup a reader already has is added: a git identity, and uv's tool directory on PATH.
The server runs as its own compose project with its own volume (``ISOLATED``), so a run in a
development checkout never touches the ``siftwise`` project's containers or data (``down -v``
deletes the volume).

``--port N`` is for local runs where port 8000 is taken: the server publishes on N
(``SIFTWISE_PORT``), the CLI calls it (``SIFTWISE_SERVER``) and the blocks' ``localhost:8000``
becomes ``localhost:N``. CI never uses it. ``--bash``: on Windows, Git Bash's bash.exe
(``bash`` there may resolve to WSL's).
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCK = re.compile(r"^```sh quickstart\n(.*?)^```", re.MULTILINE | re.DOTALL)
EXPECTED_SELECTION = "pytest tests/test_cart.py"
# Override the compose file's project name and volume (the environment wins over both).
ISOLATED = {
    "COMPOSE_PROJECT_NAME": "siftwise-quickstart",
    "SIFTWISE_PG_VOLUME": "siftwise_quickstart_pgdata",
}
# Default runs: the README's published install -> this checkout's code.
FROM_CHECKOUT = {
    "docker compose up -d --wait": "docker compose up -d --build --wait",
    "uv tool install siftwise ": "uv tool install . ",
}
# The local build's name, so it never replaces the published image's tag (docker-compose.yml).
CHECKOUT_IMAGE = "siftwise-quickstart:checkout"

PRELUDE = """\
set -euo pipefail
export GIT_AUTHOR_NAME=quickstart GIT_AUTHOR_EMAIL=quickstart@example.com
export GIT_COMMITTER_NAME=quickstart GIT_COMMITTER_EMAIL=quickstart@example.com
tool_bin="$(uv tool dir --bin)"
if command -v cygpath > /dev/null; then tool_bin="$(cygpath -u "$tool_bin")"; fi
export PATH="$tool_bin:$PATH"
"""

CHECK = f"""\
selected="$(cat selected.sh)"
if [ "$selected" != {EXPECTED_SELECTION!r} ]; then
    echo "quickstart: FAILED: expected {EXPECTED_SELECTION!r}, selected: $selected" >&2
    exit 1
fi
echo "quickstart: selection OK: $selected"
"""


def blocks(readme: str) -> list[str]:
    found = BLOCK.findall(readme)
    if not found:
        raise SystemExit("run_quickstart: no ```sh quickstart blocks in README.md")
    return found


def from_checkout(found: list[str]) -> list[str]:
    """The blocks with ``FROM_CHECKOUT``'s rewrites; each must match exactly once."""
    rewritten = list(found)
    for old, new in FROM_CHECKOUT.items():
        if sum(block.count(old) for block in rewritten) != 1:
            raise SystemExit(f"run_quickstart: expected {old!r} once in the quickstart blocks")
        rewritten = [block.replace(old, new) for block in rewritten]
    return rewritten


def script(found: list[str], port: int | None) -> str:
    parts = [PRELUDE, "__total=$SECONDS\n"]
    for n, block in enumerate(found, 1):
        if port is not None:
            block = block.replace("localhost:8000", f"localhost:{port}")
        parts.append(
            f'echo "::group::quickstart block {n}"\n'
            f"cat <<'__QUICKSTART_BLOCK__'\n{block}__QUICKSTART_BLOCK__\n"
            f"__start=$SECONDS\n{block}"
            # On its own line, even after output without a final newline (curl's).
            'printf "\\n::endgroup::\\n"\n'
            f'echo "quickstart: block {n} took $((SECONDS - __start)) s"\n'
        )
    parts.append(CHECK)
    parts.append('echo "quickstart: all blocks took $((SECONDS - __total)) s"\n')
    return "".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--published",
        action="store_true",
        help="run the blocks as written: the published image and the CLI from PyPI",
    )
    parser.add_argument("--port", type=int, help="publish the server on this port, not 8000")
    parser.add_argument("--keep", action="store_true", help="leave the server running")
    parser.add_argument("--bash", default="bash", help="the bash to run the blocks with")
    args = parser.parse_args()

    found = blocks((ROOT / "README.md").read_text(encoding="utf-8"))
    env = {**os.environ, **ISOLATED}
    if not args.published:
        found = from_checkout(found)
        env["SIFTWISE_IMAGE"] = CHECKOUT_IMAGE
    if args.port is not None:
        env["SIFTWISE_PORT"] = str(args.port)
        env["SIFTWISE_SERVER"] = f"http://localhost:{args.port}"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
    ) as file:
        file.write(script(found, args.port))
    started = time.monotonic()
    returncode = 1
    try:
        returncode = subprocess.run([args.bash, file.name], cwd=ROOT, env=env).returncode
    finally:
        Path(file.name).unlink()
        compose = ["docker", "compose"]
        if returncode != 0:
            subprocess.run([*compose, "logs", "--tail", "200"], cwd=ROOT, env=env, check=False)
        if not args.keep:
            subprocess.run([*compose, "down", "-v"], cwd=ROOT, env=env, check=False)
    source = "published" if args.published else "this checkout"
    print(f"run_quickstart: {len(found)} blocks ({source}), "
          f"{time.monotonic() - started:.0f} s in all, exit {returncode}")  # fmt: skip
    sys.exit(returncode)


if __name__ == "__main__":
    main()
