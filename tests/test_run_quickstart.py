"""scripts/run_quickstart.py: it runs the README's quickstart blocks in one bash."""

from pathlib import Path

import pytest

from scripts import run_quickstart as rq

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def test_the_readme_has_the_quickstart_blocks_in_order() -> None:
    blocks = rq.blocks(README)

    assert len(blocks) == 4
    assert "docker compose up -d --wait" in blocks[0]  # the published image
    assert "uv tool install siftwise " in blocks[1]  # the CLI from PyPI
    assert "siftwise record junit.xml" in blocks[2]
    assert "siftwise select" in blocks[3] and "> selected.sh" in blocks[3]
    # What the README says the selection is, the run checks.
    assert f"# {rq.EXPECTED_SELECTION}" in blocks[3]


def test_the_script_runs_every_block_unchanged_then_checks_the_selection() -> None:
    blocks = rq.blocks(README)
    script = rq.script(blocks, port=None)

    assert script.startswith("set -euo pipefail\n")
    positions = [script.rindex(block) for block in blocks]  # each runs (after its echo)
    assert positions == sorted(positions)
    assert script.index(rq.CHECK) > positions[-1]


def test_a_local_port_rewrites_only_the_server_address() -> None:
    blocks = ["curl -fsS http://localhost:8000/healthz\n", "echo done\n"]

    script = rq.script(blocks, port=8001)

    assert "localhost:8001/healthz" in script and "localhost:8000" not in script
    assert "echo done\n" in script


def test_runs_are_isolated_from_the_development_stack() -> None:
    # `docker compose down -v` must never reach the `siftwise` project or its volume.
    assert rq.ISOLATED["COMPOSE_PROJECT_NAME"] != "siftwise"
    assert rq.ISOLATED["SIFTWISE_PG_VOLUME"] not in ("siftwise_pgdata", "sieve_pgdata")


def test_default_runs_use_this_checkout_not_the_published_release() -> None:
    blocks = rq.from_checkout(rq.blocks(README))

    assert "docker compose up -d --build --wait" in blocks[0]
    assert "uv tool install . " in blocks[1] and "uv tool install siftwise" not in blocks[1]
    # Nothing else changes.
    assert blocks[2:] == rq.blocks(README)[2:]
    # The local build gets its own name, never the published image's tag.
    assert not rq.CHECKOUT_IMAGE.startswith("ghcr.io/")


def test_a_rewrite_that_no_longer_matches_stops_the_run() -> None:
    blocks = ["docker compose up -d --build --wait\n", "uv tool install siftwise .\n"]

    with pytest.raises(SystemExit, match="docker compose up -d --wait"):
        rq.from_checkout(blocks)
