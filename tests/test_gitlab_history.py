"""examples/gitlab/ci/history.py's download of the default branch's history, against a local
HTTP server standing in for the GitLab API: the endpoint, the job token, and the log lines."""

import importlib.util
import io
import threading
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
JOB_TOKEN = "glcbt-JOBSECRET-55555555"
EXPECTED_PATH = (
    "/api/v4/projects/4242/jobs/artifacts/main/download"
    "?job=siftwise-rollup&search_recent_successful_pipelines=true"
)


def load_history() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gitlab_example_history", ROOT / "examples" / "gitlab" / "ci" / "history.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


history = load_history()


def archive(files: dict[str, bytes]) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as out:
        for name, content in files.items():
            out.writestr(name, content)
    return data.getvalue()


class FakeGitLab:
    """Answers every GET with ``status``, ``body`` and ``headers``; records what it was sent."""

    def __init__(self) -> None:
        self.status, self.body, self.headers = 200, b"", dict[str, str]()
        self.requests: list[tuple[str, dict[str, str]]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fake.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                self.send_response(fake.status)
                for name, value in fake.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(fake.body)))
                self.end_headers()
                self.wfile.write(fake.body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gitlab_api() -> Iterator[FakeGitLab]:
    fake = FakeGitLab()
    yield fake
    fake.close()


@pytest.fixture
def env(gitlab_api: FakeGitLab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.chdir(tmp_path)
    return {
        "CI_JOB_TOKEN": JOB_TOKEN,
        "CI_API_V4_URL": f"{gitlab_api.url}/api/v4",
        "CI_PROJECT_ID": "4242",
        "CI_DEFAULT_BRANCH": "main",
    }


def test_the_endpoint_is_the_documented_one() -> None:
    assert history.artifacts_path("4242", "main") == (
        "/projects/4242/jobs/artifacts/main/download"
        "?job=siftwise-rollup&search_recent_successful_pipelines=true"
    )
    # A project path or a ref with a slash is one URL-encoded segment.
    assert history.artifacts_path("acme/shop", "release/1.0").startswith(
        "/projects/acme%2Fshop/jobs/artifacts/release%2F1.0/download?"
    )


def test_200_restores_the_history_files_only(
    gitlab_api: FakeGitLab, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    gitlab_api.body = archive({
        ".siftwise-history/runs.copy": b"runs",
        ".siftwise-history/tests.copy": b"tests",
        ".siftwise-ci/rollup.md": b"not ours",
        "../escape.copy": b"never",
    })  # fmt: skip

    assert history.fetch_from_default_branch(env) is True

    path, headers = gitlab_api.requests[0]
    assert path == EXPECTED_PATH
    assert headers["job-token"] == JOB_TOKEN  # the header, never the URL
    assert JOB_TOKEN not in path
    restored = sorted(p.name for p in Path(".siftwise-history").iterdir())
    assert restored == ["runs.copy", "tests.copy"]
    assert not Path(".siftwise-ci").exists() and not Path("../escape.copy").exists()
    out = capsys.readouterr().out
    assert f"GET {EXPECTED_PATH.removeprefix('/api/v4')}: HTTP 200" in out
    assert JOB_TOKEN not in out


@pytest.mark.parametrize(
    ("status", "body", "meaning"),
    [
        (404, b'{"message":"404 Not found"}', "no successful pipeline on main with a siftwise"),
        (404, b'{"message":"404 Project Not Found"}', "can't see the project"),
        (401, b'{"message":"401 Unauthorized"}', "the job token was refused"),
        (403, b'{"message":"403 Forbidden"}', "Job token permissions"),
        (500, b"<html>\n  oops\n</html>", "unexpected answer"),
    ],
)
def test_a_refusal_logs_the_endpoint_status_and_body(
    gitlab_api: FakeGitLab,
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    status: int,
    body: bytes,
    meaning: str,
) -> None:
    gitlab_api.status, gitlab_api.body = status, body

    assert history.fetch_from_default_branch(env) is False

    out = capsys.readouterr().out
    endpoint = EXPECTED_PATH.removeprefix("/api/v4")
    assert f"GET {endpoint}: HTTP {status}" in out
    assert " ".join(body.decode().split()) in out  # the body's start, on one line
    assert meaning in out
    assert JOB_TOKEN not in out
    assert not Path(".siftwise-history").exists()


def test_a_body_echoing_the_token_is_masked(
    gitlab_api: FakeGitLab, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    gitlab_api.status, gitlab_api.body = 403, f"bad token {JOB_TOKEN}".encode()

    assert history.fetch_from_default_branch(env) is False

    out = capsys.readouterr().out
    assert JOB_TOKEN not in out and "bad token [MASKED]" in out


def test_a_long_body_is_cut(
    gitlab_api: FakeGitLab, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    gitlab_api.status, gitlab_api.body = 404, b"x" * 5000

    history.fetch_from_default_branch(env)

    out = capsys.readouterr().out
    assert "x" * 200 in out and "x" * 201 not in out


def test_an_unreachable_api_or_a_bad_archive_starts_empty(
    gitlab_api: FakeGitLab, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    gitlab_api.body = b"<html>a sign-in page</html>"
    assert history.fetch_from_default_branch(env) is False
    assert "HTTP 200 but not a zip" in capsys.readouterr().out

    gitlab_api.body = archive({"something/else.txt": b""})
    assert history.fetch_from_default_branch(env) is False
    assert "the archive has no .siftwise-history/" in capsys.readouterr().out

    gitlab_api.close()
    assert history.fetch_from_default_branch(env) is False
    out = capsys.readouterr().out
    assert "failed:" in out and JOB_TOKEN not in out


def test_without_a_job_token_nothing_is_requested(
    gitlab_api: FakeGitLab, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    del env["CI_JOB_TOKEN"]

    assert history.fetch_from_default_branch(env) is False

    assert gitlab_api.requests == []
    assert "no CI_JOB_TOKEN" in capsys.readouterr().out


def test_a_redirect_elsewhere_drops_the_job_token(
    gitlab_api: FakeGitLab, env: dict[str, str]
) -> None:
    """GitLab.com may redirect the download to a CDN: the token stays with GitLab."""
    cdn = FakeGitLab()
    try:
        cdn.body = archive({".siftwise-history/runs.copy": b"runs"})
        gitlab_api.status, gitlab_api.headers = 302, {"Location": f"{cdn.url}/signed/archive.zip"}

        assert history.fetch_from_default_branch(env) is True

        assert gitlab_api.requests[0][1]["job-token"] == JOB_TOKEN
        assert cdn.requests[0][0] == "/signed/archive.zip"
        assert "job-token" not in cdn.requests[0][1]
    finally:
        cdn.close()
