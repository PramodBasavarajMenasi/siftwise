"""The image's HEALTHCHECK command, as the Dockerfile has it: healthy only on HTTP 200 from
/healthz (a plain-socket GET, cheap enough under QEMU emulation for the arm64 image)."""

import json
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def healthcheck_code(port: int) -> str:
    """The python -c code of the Dockerfile's HEALTHCHECK, aimed at ``port``."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    found = re.search(r"^HEALTHCHECK .*? CMD (\[.*\])$", text, re.M)
    assert found is not None, "no HEALTHCHECK ... CMD [...] in the Dockerfile"
    argv = json.loads(found.group(1))
    assert argv[:2] == ["python", "-c"], argv
    code = str(argv[2])
    assert "8000" in code and "/healthz" in code
    return code.replace("8000", str(port))


@pytest.fixture
def server() -> Iterator[tuple[int, dict[str, int]]]:
    """A server answering /healthz with the status in ``answer["status"]``, 404 elsewhere."""
    answer = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(answer["status"] if self.path == "/healthz" else 404)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:  # quiet
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1], answer
    httpd.shutdown()
    httpd.server_close()


def run(code: str) -> int:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=30).returncode


def test_the_healthcheck_passes_on_200_from_healthz(server: tuple[int, dict[str, int]]) -> None:
    port, _ = server
    assert run(healthcheck_code(port)) == 0


@pytest.mark.parametrize("status", [404, 500, 503])
def test_the_healthcheck_fails_on_any_other_status(
    server: tuple[int, dict[str, int]], status: int
) -> None:
    port, answer = server
    answer["status"] = status
    assert run(healthcheck_code(port)) != 0


def test_the_healthcheck_fails_when_nothing_listens() -> None:
    with HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as probe:
        free = probe.server_address[1]  # closed again before the check runs
    assert run(healthcheck_code(free)) != 0
