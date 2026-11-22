"""The read-only dashboard (/ui): login, sessions, scope, escaping, headers and every page."""

import hashlib
import html
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from siftwise import __version__
from siftwise.api.dashboard import pages
from siftwise.api.dashboard.auth import CSP, LOGIN_MAX_BYTES, SECURITY_HEADERS, path_repo
from siftwise.api.main import create_app
from siftwise.config import Settings
from siftwise.core import tokens
from siftwise.core.models import Repo, UiSession
from siftwise.core.tokens import Scope
from siftwise.core.triage import triage_ci_run, triage_ci_runs
from siftwise.db import get_session
from tests.test_shadow import SHADOW, shadow_run
from tests.test_triage_db import OTHER, REPO, T0, TEST, F, P, record, sha

ADMIN = "admin-s3cret-token"
ELSEWHERE = "acme/secret-project"
BASE = "https://testserver"
EVIL = "<script>alert(1)</script>"


@pytest.fixture
def make_client(db_session: Session) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def make(base_url: str = BASE, **settings: Any) -> TestClient:
        options: dict[str, Any] = {"api_token": ADMIN, "dashboard": True, **settings}
        app = create_app(Settings(_env_file=None, **options))
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app, base_url=base_url, follow_redirects=False)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


def csrf_of(response: httpx2.Response) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert found, response.text
    return found.group(1)


def login(client: TestClient, token: str, **headers: str) -> httpx2.Response:
    form = client.get("/ui/login")
    assert form.status_code == 200, form.text
    response: httpx2.Response = client.post(
        "/ui/login", data={"token": token, "csrf": csrf_of(form)}, headers=headers
    )
    return response


def signed_in(client: TestClient, token: str = ADMIN) -> TestClient:
    response = login(client, token)
    assert response.status_code == 303, response.text
    return client


def set_cookie(response: httpx2.Response, name: str) -> str:
    return next(h for h in response.headers.get_list("set-cookie") if h.startswith(name + "="))


# --- on or off ----------------------------------------------------------------------------


def test_the_dashboard_is_off_unless_enabled(make_client: Callable[..., TestClient]) -> None:
    off = make_client(dashboard=False)
    for path in ("/ui/", "/ui/login", f"/ui/repos/{REPO}"):
        assert off.get(path).status_code == 404
        assert off.get(path, headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 404


# --- login, cookies, logout ---------------------------------------------------------------


def test_the_login_page(client: TestClient) -> None:
    response = client.get("/ui/login")
    assert response.status_code == 200
    field = re.search(r"<input id=\"token\"[^>]*>", response.text)
    assert field is not None
    assert 'type="password"' in field.group(0) and 'autocomplete="off"' in field.group(0)
    assert "value=" not in field.group(0)
    csrf = set_cookie(response, "siftwise_csrf").lower()
    assert all(flag in csrf for flag in ("httponly", "secure", "samesite=strict", "path=/ui"))


def test_login_with_the_admin_token_sets_a_strict_session_cookie(
    client: TestClient, db_session: Session
) -> None:
    response = login(client, ADMIN)
    assert response.status_code == 303 and response.headers["location"] == "/ui/"
    cookie = set_cookie(response, "siftwise_session")
    flags = cookie.lower()
    assert all(f in flags for f in ("httponly", "secure", "samesite=strict", "path=/ui",
                                    "max-age=14400"))  # fmt: skip
    value = cookie.split(";", 1)[0].split("=", 1)[1]
    assert ADMIN not in cookie and ADMIN not in response.text
    row = db_session.scalars(select(UiSession)).one()
    assert row.id_hash != value and len(row.id_hash) == 64  # only its hash is stored
    assert ADMIN not in (row.admin_digest or "") and row.api_token_id is None
    assert client.get("/ui/").status_code == 200


def test_a_wrong_token_is_refused_and_never_echoed(client: TestClient) -> None:
    response = login(client, "sw_not-a-real-token-value")
    assert response.status_code == 401
    assert "Token not accepted (unknown or revoked)." in response.text
    assert "sw_not-a-real-token-value" not in response.text
    assert "siftwise_session" not in response.headers.get("set-cookie", "")


def test_login_needs_the_csrf_token_and_a_same_origin(client: TestClient) -> None:
    form = client.get("/ui/login")
    no_csrf = client.post("/ui/login", data={"token": ADMIN})
    assert no_csrf.status_code == 403
    wrong = client.post("/ui/login", data={"token": ADMIN, "csrf": "forged"})
    assert wrong.status_code == 403
    cross = client.post("/ui/login", data={"token": ADMIN, "csrf": csrf_of(form)},
                        headers={"Origin": "https://evil.example"})  # fmt: skip
    assert cross.status_code == 403
    same = client.post("/ui/login", data={"token": ADMIN, "csrf": csrf_of(client.get(
        "/ui/login"))}, headers={"Origin": BASE})  # fmt: skip
    assert same.status_code == 303


def test_a_null_origin_is_refused_and_the_referrer_policy_keeps_origins_real(
    client: TestClient,
) -> None:
    # Found taking screenshots: with Referrer-Policy no-referrer, a real browser sends
    # `Origin: null` on the login form's own post, so every sign-in was refused.
    assert SECURITY_HEADERS["referrer-policy"] == "same-origin"
    page = client.get("/ui/login")
    assert '<meta name="referrer" content="same-origin">' in page.text
    null = client.post("/ui/login", data={"token": ADMIN, "csrf": csrf_of(page)},
                       headers={"Origin": "null"})  # fmt: skip
    assert null.status_code == 403


def test_the_login_body_is_small_and_a_form(client: TestClient) -> None:
    big = client.post("/ui/login", content=b"token=" + b"x" * LOGIN_MAX_BYTES,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})  # fmt: skip
    assert big.status_code == 413
    json = client.post("/ui/login", json={"token": ADMIN})
    assert json.status_code == 415


def test_plain_http_works_on_localhost_only(make_client: Callable[..., TestClient]) -> None:
    local = make_client("http://localhost:8000")
    response = login(local, ADMIN)
    assert response.status_code == 303
    assert "secure" not in set_cookie(response, "siftwise_session").lower()
    remote = make_client("http://siftwise.example.com")
    page = remote.get("/ui/login")
    assert "needs HTTPS" in page.text and 'name="token"' not in page.text
    assert remote.post("/ui/login", data={"token": ADMIN, "csrf": "x"}).status_code == 400


def test_behind_a_trusted_proxy_the_forwarded_origin_counts(
    make_client: Callable[..., TestClient],
) -> None:
    forwarded = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "dash.example.com"}
    proxied = make_client("http://10.0.0.5:8000", forwarded_allow_ips="*")
    form = proxied.get("/ui/login", headers=forwarded)
    assert 'name="token"' in form.text  # https as the browser sees it: allowed
    csrf = csrf_of(form)
    # The CSRF cookie is Secure (https as the browser sees it); this test client talks plain
    # http to the "proxy" and won't send it back, so it goes in by hand, as a browser would.
    sent = {**forwarded, "Cookie": f"siftwise_csrf={csrf}"}
    internal = proxied.post("/ui/login", data={"token": ADMIN, "csrf": csrf},
                            headers={**sent, "Origin": "http://10.0.0.5:8000"})  # fmt: skip
    assert internal.status_code == 403  # the internal origin isn't the page's
    public = proxied.post("/ui/login", data={"token": ADMIN, "csrf": csrf},
                          headers={**sent, "Origin": "https://dash.example.com"})  # fmt: skip
    assert public.status_code == 303
    assert "secure" in set_cookie(public, "siftwise_session").lower()
    # The same headers from a peer not in --forwarded-allow-ips are ignored: plain http.
    untrusted = make_client("http://10.0.0.5:8000")
    assert "needs HTTPS" in untrusted.get("/ui/login", headers=forwarded).text


def test_logout_ends_the_session(client: TestClient, db_session: Session) -> None:
    signed_in(client)
    page = client.get("/ui/")
    forged = client.post("/ui/logout", data={"csrf": "forged"})
    assert forged.status_code == 403 and client.get("/ui/").status_code == 200
    out = client.post("/ui/logout", data={"csrf": csrf_of(page)})
    assert out.status_code == 303 and out.headers["location"] == "/ui/login"
    assert 'siftwise_session=""' in set_cookie(out, "siftwise_session")
    assert db_session.scalar(select(func.count()).select_from(UiSession)) == 0
    assert client.get("/ui/").status_code == 303


def test_a_revoked_token_stops_working_on_the_next_request(
    client: TestClient, db_session: Session
) -> None:
    token_id, token = tokens.create_token(db_session, REPO, Scope.READ, "laptop")
    signed_in(client, token)
    assert client.get("/ui/").status_code == 200
    tokens.revoke_token(db_session, token_id)
    response = client.get("/ui/")
    assert response.status_code == 303 and response.headers["location"] == "/ui/login"
    assert db_session.scalar(select(func.count()).select_from(UiSession)) == 0
    assert login(client, token).status_code == 401


def test_a_changed_admin_token_ends_admin_sessions(
    make_client: Callable[..., TestClient],
) -> None:
    old = signed_in(make_client())
    session = old.cookies.get("siftwise_session")
    rotated = make_client(api_token="a-new-admin-token")
    rotated.cookies.set("siftwise_session", session or "", domain="testserver", path="/ui")
    assert rotated.get("/ui/").status_code == 303


def test_without_a_session_every_page_redirects(client: TestClient) -> None:
    for path in ("/ui/", f"/ui/repos/{REPO}", f"/ui/repos/{REPO}/-/runs",
                 f"/ui/repos/{REPO}/-/flaky", f"/ui/repos/{REPO}/-/runs/1/1"):  # fmt: skip
        response = client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/ui/login"
    assert client.post("/ui/logout").status_code == 303
    client.cookies.set("siftwise_session", "made-up", domain="testserver", path="/ui")
    assert client.get("/ui/").status_code == 303


def test_the_api_never_accepts_the_session_cookie(client: TestClient) -> None:
    signed_in(client)
    assert client.get(f"/repos/{REPO}/recall").status_code == 401


# --- scope --------------------------------------------------------------------------------


def test_a_one_repo_token_sees_only_its_repo(client: TestClient, db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P}, ci_run_id="1")
    record(db_session, commit=2, hour=2, tests={TEST: P}, ci_run_id="2")
    db_session.execute(
        text("UPDATE repos SET name = :n WHERE name = :o"),
        {"n": ELSEWHERE, "o": REPO},
    )
    record(db_session, commit=3, hour=3, tests={TEST: P}, ci_run_id="3")  # REPO again
    _, token = tokens.create_token(db_session, REPO, Scope.READ)
    signed_in(client, token)
    index = client.get("/ui/").text
    assert REPO in index and ELSEWHERE not in index
    hidden = client.get(f"/ui/repos/{ELSEWHERE}")
    missing = client.get("/ui/repos/acme/no-such-repo")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.text == missing.text and ELSEWHERE not in hidden.text
    for page in ("runs", "flaky", "runs/1/1"):
        assert client.get(f"/ui/repos/{ELSEWHERE}/-/{page}").status_code == 404
    assert client.get(f"/ui/repos/{REPO}").status_code == 200


def test_an_all_repos_token_lists_every_repo(client: TestClient, db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P}, ci_run_id="1")
    _, token = tokens.create_token(db_session, "*", Scope.READ)
    signed_in(client, token)
    assert REPO in client.get("/ui/").text


def test_the_repo_of_a_path() -> None:
    assert path_repo("/ui/repos/acme/shop") == "acme/shop"
    assert path_repo("/ui/repos/acme/shop/-/runs/12/1") == "acme/shop"
    assert path_repo("/ui/repos/a/b/c/-/flaky") == "a/b/c"
    assert path_repo("/ui/") is None


# --- pages: empty -------------------------------------------------------------------------


def test_pages_with_no_data_say_what_is_missing(client: TestClient, db_session: Session) -> None:
    _, token = tokens.create_token(db_session, REPO, Scope.READ)
    signed_in(client, token)
    index = client.get("/ui/")
    assert index.status_code == 200 and "no runs yet" in index.text
    overview = client.get(f"/ui/repos/{REPO}")
    assert overview.status_code == 200 and "No runs recorded for" in overview.text
    assert "#github-action" in overview.text
    assert "No runs recorded" in client.get(f"/ui/repos/{REPO}/-/runs").text
    assert "No runs recorded" in client.get(f"/ui/repos/{REPO}/-/flaky").text
    assert client.get(f"/ui/repos/{REPO}/-/runs/9/1").status_code == 404


def test_a_repo_without_shadow_runs_says_how_to_get_them(
    client: TestClient, db_session: Session
) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P}, ci_run_id="1")
    signed_in(client)
    overview = client.get(f"/ui/repos/{REPO}").text
    assert "No shadow runs in the last 30 days" in overview and "shadow: 10%" in overview
    assert "#shadow-mode" in overview
    # The run is from 2026-10-01: older than 30 days back from now, so the window is empty and
    # the page says older runs exist, with links to wider windows.
    flaky = client.get(f"/ui/repos/{REPO}/-/flaky").text
    assert "Nothing in the last 30 days, but 1 older CI runs exist" in flaky
    assert "?days=all" in flaky
    every = client.get(f"/ui/repos/{REPO}/-/flaky", params={"days": "all"}).text
    assert "No flaky tests in all recorded history" in every and "#flaky-tests" in every
    index = client.get("/ui/")
    assert "no repos" not in index.text.lower()


def test_an_admin_with_no_repos(client: TestClient) -> None:
    signed_in(client)
    assert "No repos yet" in client.get("/ui/").text


# --- pages: real data ---------------------------------------------------------------------


def _text(html: str) -> str:
    """The page's text: tags dropped (stats put their numbers in <strong>)."""
    return re.sub(r"<[^>]+>", "", html)


def _hours_ago(hours: float) -> float:
    """record()'s ``hour`` (hours after T0) for a run that started ``hours`` ago."""
    return (datetime.now(UTC) - T0).total_seconds() / 3600 - hours


def test_the_repos_page_shows_totals_the_window_and_how_long_ago(
    client: TestClient, db_session: Session
) -> None:
    # Found in a browser: "CI runs, 30 days = 0" alone for a repo with lots of history.
    record(db_session, commit=1, hour=1, tests={TEST: P}, ci_run_id="1")  # 2026-10-01
    record(db_session, commit=1, hour=1.5, tests={TEST: P}, ci_run_id="1", variant="mac")
    record(db_session, commit=2, hour=2, tests={TEST: P}, ci_run_id="2")
    signed_in(client)
    page = client.get("/ui/").text
    row = re.search(rf"{REPO}</a>.*?</tr>", page, re.S)
    assert row is not None
    cells = re.findall(r'<td class="n">(.*?)</td>', row.group(0))
    # CI runs, in the window, shadow runs in the window, selections recorded (none in it)
    assert cells == ["2", "0", "0", ""]
    assert re.search(r"> \d+ days ago<", row.group(0).replace("·", ""))  # the newest run, relative
    assert "1 repo · 2 CI runs recorded · 0 in the last 30 days · newest run" in _text(page)
    assert "1 of 1 repo has no CI runs in the last 30 days, only older ones" in page
    assert 'href="/ui/?days=90"' in page and 'href="/ui/?days=all"' in page

    record(db_session, commit=3, hour=_hours_ago(2), tests={TEST: P}, ci_run_id="3")
    page = client.get("/ui/").text
    assert "3 CI runs recorded · 1 in the last 30 days · newest run 2 hours ago" in _text(page)
    assert "only older ones" not in page
    every = client.get("/ui/", params={"days": "all"}).text
    assert "CI runs, last" not in every and 'aria-current="true">all' in every


def test_a_recall_with_a_miss_never_shows_100_percent() -> None:
    # Found in a screenshot: 2,043 of 2,044 caught read "100.0%" next to "1 missed".
    assert pages._recall(2043 / 2044) == "99.9%"
    assert pages._recall(1.0) == "100.0%" and pages._recall(0.5) == "50.0%"
    assert pages._recall(None) == "n/a"


def test_relative_times() -> None:
    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    cases = {timedelta(seconds=30): "just now", timedelta(minutes=1, seconds=40): "2 minutes ago",
             timedelta(minutes=89): "89 minutes ago", timedelta(hours=2): "2 hours ago",
             timedelta(hours=35): "35 hours ago", timedelta(hours=36): "1 day ago",
             timedelta(days=39, hours=23): "39 days ago"}  # fmt: skip
    for back, expected in cases.items():
        assert pages._ago(now - back, now) == expected, back
    assert pages._ago(None) == ""


def test_an_empty_window_with_older_shadow_runs_says_so(
    client: TestClient, db_session: Session
) -> None:
    _shadow_data(client, db_session)
    db_session.execute(text("UPDATE ci_selections SET created_at = now() - interval '40 days'"))
    signed_in(client)
    page = client.get(f"/ui/repos/{REPO}").text
    assert "Nothing in the last 30 days, but 1 older shadow runs exist (the newest 40 days" in page
    assert f'href="/ui/repos/{REPO}?days=90"' in page and "?days=all" in page
    every = client.get(f"/ui/repos/{REPO}", params={"days": "all"}).text
    assert "(1 of 2)" in every and "recall from shadow runs, in all recorded history" in every


def _shadow_data(client: TestClient, db_session: Session) -> None:
    """A selective shadow run that catches TEST and misses OTHER (the API's own path)."""
    from siftwise.core.history import recompute_repo_stats
    from siftwise.core.models import Repo

    record(db_session, commit=1, hour=1, tests={TEST: P, OTHER: P}, ci_run_id="100")
    repo_id = db_session.scalars(select(Repo.id).where(Repo.name == REPO)).one()
    recompute_repo_stats(db_session, repo_id, 90)
    auth = {"Authorization": f"Bearer {ADMIN}"}
    selected = client.post("/select", headers=auth, json={
        "repo": REPO, "changed_files": ["src/cart.py"],
        "shadow": {"ci_run_id": SHADOW, "run_attempt": 1, "commit_sha": sha(50)},
    })  # fmt: skip
    assert selected.status_code == 200, selected.text
    shadow_run(db_session, {TEST: F, OTHER: F})
    compared = client.post(f"/repos/{REPO}/shadow", json={"ci_run_id": SHADOW}, headers=auth)
    assert compared.status_code == 200, compared.text


def test_the_overview_shows_recall_weeks_and_misses(
    client: TestClient, db_session: Session
) -> None:
    _shadow_data(client, db_session)
    signed_in(client)
    page = client.get(f"/ui/repos/{REPO}").text
    assert "50.0%" in page and "(1 of 2)" in page  # recall with its counts
    assert "Selective only" in page and "By week" in page
    assert "Time saved: <strong>50.0%</strong>" in page
    # Found in a screenshot: the sentences after it ran together ("runs).Full-suite").
    assert re.search(r"full-suite runs\)\.\s+Full-suite selections", page), page
    assert "no link" in page and OTHER in page  # the miss and its cause
    assert f"/ui/repos/{REPO}/-/runs/{SHADOW}/1" in page
    seven = client.get(f"/ui/repos/{REPO}", params={"days": 7}).text
    assert 'aria-current="true">7d' in seven


def test_the_runs_page_counts_failing_tests_and_marks_shadow_runs(
    client: TestClient, db_session: Session
) -> None:
    _shadow_data(client, db_session)
    signed_in(client)
    page = client.get(f"/ui/repos/{REPO}/-/runs").text
    row = re.search(rf"{SHADOW}</span> #1.*?</tr>", page, re.S)
    assert row is not None
    assert "shadow" in row.group(0) and "would run 1 of 2" in row.group(0)
    cells = re.findall(r'<td class="n">(.*?)</td>', row.group(0))
    assert cells == ["1", "2"]  # variants, failing tests
    assert ">view</a>" in row.group(0)  # verdicts aren't computed for the list
    assert "100 #1" in _text(page)  # the history run is listed too


def test_the_run_page_lists_verdicts_and_the_shadow_comparison(
    client: TestClient, db_session: Session
) -> None:
    _shadow_data(client, db_session)
    signed_in(client)
    page = client.get(f"/ui/repos/{REPO}/-/runs/{SHADOW}/1").text
    assert TEST in page and OTHER in page
    assert "Shadow run" in page and "1 of 2" in page  # would have run 1 of 2 tests
    assert "Missed test" in page


def test_the_flaky_page_lists_flaky_tests_and_suggests_entries(
    client: TestClient, db_session: Session
) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: [F, P]}, ci_run_id="1")
    record(db_session, commit=1, hour=2, tests={TEST: P, OTHER: [F, P]}, ci_run_id="2",
           known_flaky=[OTHER])  # fmt: skip
    signed_in(client)
    page = client.get(f"/ui/repos/{REPO}/-/flaky", params={"days": "all"}).text
    assert TEST in page and OTHER in page
    assert "listed" in page  # OTHER is in the newest main run's known_flaky
    assert f'{{ test = "{TEST}", until = "' in page.replace("&#34;", '"')
    assert f'"{OTHER}"' not in page.split("Suggested known_flaky")[-1].replace("&#34;", '"')


# --- escaping and headers -----------------------------------------------------------------


def test_untrusted_names_are_escaped_everywhere(client: TestClient, db_session: Session) -> None:
    evil_test = f"tests.test_x::test_{EVIL}"
    branch = '"><img src=x onerror=alert(1)>'
    record(db_session, commit=1, hour=1, tests={evil_test: F}, ci_run_id="7", branch=branch,
           variant='<b>leg</b>')  # fmt: skip
    record(db_session, commit=1, hour=2, tests={evil_test: P}, ci_run_id="8", branch=branch,
           variant='<b>leg</b>')  # fmt: skip
    signed_in(client)
    for path in (f"/ui/repos/{REPO}/-/runs", f"/ui/repos/{REPO}/-/runs/7/1",
                 f"/ui/repos/{REPO}/-/flaky", "/ui/"):  # fmt: skip
        body = client.get(path).text
        assert EVIL not in body and "<img src=x" not in body and "<b>leg" not in body, path
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in client.get(
        f"/ui/repos/{REPO}/-/runs/7/1").text  # fmt: skip


@pytest.mark.parametrize(
    "path", ["/ui/login", "/ui/", f"/ui/repos/{REPO}", f"/ui/repos/{REPO}/-/runs",
             f"/ui/repos/{REPO}/-/flaky", "/ui/repos/acme/nope"],
)  # fmt: skip
def test_every_page_has_the_security_headers(client: TestClient, path: str) -> None:
    if path != "/ui/login":
        signed_in(client)
    response = client.get(path)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value, name
    assert response.headers["cache-control"] == "no-store"
    assert "script-src" not in CSP and "default-src 'none'" in CSP
    assert "<script" not in response.text and " style=" not in response.text


def test_the_stylesheet(client: TestClient) -> None:
    name = pages.stylesheet_file().name
    css = client.get(f"/ui/static/{name}")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert name == f"siftwise-{hashlib.sha256(css.content).hexdigest()[:16]}.css"
    assert "immutable" in css.headers["cache-control"]
    assert f'href="/ui/static/{name}"' in client.get("/ui/login").text
    # The release-version name of before is gone: it kept old CSS cached within a version.
    assert client.get(f"/ui/static/siftwise-{__version__}.css").status_code == 404
    assert "tabular-nums" in css.text and "#4fc79e" in css.text
    # Found in a screenshot: the page container was also called .wrap, the class of wrapping
    # cells, so those cells and spans got the container's side padding and sat off their
    # column. .wrap may only set how text wraps.
    rules = re.findall(r"([^{}]*)\{([^}]*)\}", css.text)
    wrap = [body for selector, body in rules if re.search(r"\.wrap(?![\w-])", selector)]
    assert wrap and all(re.fullmatch(r"\s*((white-space|overflow-wrap|min-width):[^;]+;\s*)+",
                                     body) for body in wrap), wrap  # fmt: skip
    assert client.get("/ui/static/other.css").status_code == 404


def test_the_stylesheet_name_changes_with_its_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Found in a browser: named by release version, a CSS change within a version kept the
    # old file cached. The name is a hash of the content, so any change is a new URL.
    (tmp_path / "static").mkdir()
    css = tmp_path / "static" / "siftwise.css"
    css.write_text("a { color: red; }")
    monkeypatch.setattr(pages, "_css_path", lambda: css)
    first = pages.stylesheet_file()
    assert first.css == b"a { color: red; }"
    css.write_text("a { color: green; }")
    second = pages.stylesheet_file()
    assert second.name != first.name and second.css == b"a { color: green; }"
    assert pages.stylesheet_file() == second  # unchanged file: the cached one


def test_long_values_are_cut_with_the_full_value_in_a_title(
    client: TestClient, db_session: Session
) -> None:
    long_test = "tests.test_" + "x" * 80 + "::test_the_end"
    branch = "feature/" + "b" * 60
    record(db_session, commit=1, hour=1, tests={long_test: F}, ci_run_id="7", branch=branch)
    record(db_session, commit=2, hour=2, tests={TEST: P}, ci_run_id="8")  # on main
    signed_in(client)
    runs = client.get(f"/ui/repos/{REPO}/-/runs").text
    assert f'<span class="mono trunc" title="{branch}">' in runs  # cut by CSS, title in full
    assert "main main" not in re.sub(r"<[^>]+>", " ", runs).replace("  ", " ")
    assert '<span class="tag">main</span>' in runs
    run = client.get(f"/ui/repos/{REPO}/-/runs/7/1").text
    shown = pages._short(long_test)
    assert len(shown) == 72 and shown.endswith("::test_the_end") and "…" in shown
    assert f'<span class="mono" title="{long_test}">{shown}</span>' in run
    assert pages._short("short::test") == "short::test"


def test_the_top_bar_names_the_token_not_the_repo(client: TestClient, db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P}, ci_run_id="1")
    _, token = tokens.create_token(db_session, REPO, Scope.READ, "dashboard-check")
    signed_in(client, token)
    page = client.get(f"/ui/repos/{REPO}").text
    bar = page.split('<header class="top">')[1].split("</header>")[0]
    assert "dashboard-check · read" in bar and REPO not in bar.replace(f'title="{REPO}"', "")
    assert 'class="quiet">Sign out' in bar
    assert f"siftwise v{__version__}</div></footer>" in page
    # The window switcher sits in the tab row, on the repos page too.
    for path in (f"/ui/repos/{REPO}", "/ui/"):
        row = client.get(path).text.split('<div class="tabrow')[1].split("</div>")[0]
        assert 'aria-label="Window"' in row and 'aria-current="true">30d' in row, path
    # The repos page has no lone "Repos" tab (its title says it); the row keeps its rule.
    index = client.get("/ui/").text
    row = index.split('<div class="tabrow')[1].split("</div>")[0]
    assert 'class="tabs"' not in row and ">Repos</a>" not in row
    assert row.startswith('">')  # not "bare": the rule under the switcher stays


def test_the_repos_page_lists_the_newest_run_first(client: TestClient, db_session: Session) -> None:
    # record() writes to REPO: each repo is recorded there, then renamed.
    for name, hour in (("acme/old", 1), ("acme/zzz-new", 5), ("acme/aaa-middle", 3)):
        record(db_session, commit=hour, hour=hour, tests={TEST: P}, ci_run_id=str(hour))
        db_session.execute(text("UPDATE repos SET name = :n WHERE name = :r"),
                           {"n": name, "r": REPO})  # fmt: skip
    db_session.add(Repo(name="acme/no-runs"))
    db_session.commit()
    signed_in(client)
    page = client.get("/ui/", params={"days": "all"}).text
    names = re.findall(r'href="/ui/repos/(acme/[^"/]+)"', page)
    assert list(dict.fromkeys(names)) == ["acme/zzz-new", "acme/aaa-middle", "acme/old",
                                          "acme/no-runs"]  # fmt: skip


def test_the_login_page_has_a_primary_button(client: TestClient) -> None:
    page = client.get("/ui/login").text
    assert '<button type="submit" class="primary">Sign in</button>' in page


# --- the batched triage the runs page uses -------------------------------------------------


def test_triage_of_many_attempts_equals_each_one_alone(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P, OTHER: P}, ci_run_id="1")
    record(db_session, commit=2, hour=2, tests={TEST: F, OTHER: P}, ci_run_id="2")
    record(db_session, commit=3, hour=3, tests={TEST: F, OTHER: F}, ci_run_id="3",
           variant="mac")  # fmt: skip
    record(db_session, commit=3, hour=3.5, tests={TEST: P, OTHER: F}, ci_run_id="3")
    attempts = [("2", 1), ("3", 1), ("1", 1), ("404", 1)]
    together = triage_ci_runs(db_session, REPO, attempts)
    assert together is not None
    for ci, attempt in attempts:
        alone = triage_ci_run(db_session, REPO, ci, attempt)
        assert together[(ci, attempt)] == alone, ci
    assert triage_ci_runs(db_session, "acme/unknown", attempts) is None


def test_the_flaky_page_lists_at_most_max_rows(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pages, "MAX_FLAKY_ROWS", 1)
    record(db_session, commit=1, hour=1, tests={TEST: [F, P], OTHER: [F, P]}, ci_run_id="1")
    signed_in(client)
    page = client.get(f"/ui/repos/{REPO}/-/flaky", params={"days": "all"}).text
    assert "2 tests, most flaky first" in page
    # "all": a window back to the oldest run (2026-10-01), which the command gets in days
    assert "+1 more not shown" in page
    assert re.search(rf"siftwise flaky --repo {REPO} --days [0-9]+</code>", page), page
    suggested = page.split("Suggested known_flaky")[-1].replace("&#34;", '"')
    assert suggested.count("{ test = ") == 1  # entries only for the listed tests


# --- what siftwise did in a run (#85) -----------------------------------------------------

EVIL_BRANCH = 'x"><script>alert(1)</script>'


def _ci_run(client: TestClient, db_session: Session, *, ci: str = "ci-7",
            branch: str = "feature/x") -> None:  # fmt: skip
    """A CI run whose selection the select step recorded (ci), its upload, its outcome."""
    from siftwise.core.history import recompute_repo_stats
    from siftwise.core.models import Repo

    record(db_session, commit=1, hour=1, tests={TEST: P, OTHER: P}, ci_run_id="100")
    repo_id = db_session.scalars(select(Repo.id).where(Repo.name == REPO)).one()
    recompute_repo_stats(db_session, repo_id, 90)
    auth = {"Authorization": f"Bearer {ADMIN}"}
    selected = client.post("/select", headers=auth, json={
        "repo": REPO, "changed_files": ["src/cart.py"],
        "ci": {"provider": "github", "ci_run_id": ci, "run_attempt": 1, "commit_sha": sha(50),
               "branch": branch, "is_main": False, "pr_number": 9, "event": "pull_request",
               "client_version": "0.4.0"},
    })  # fmt: skip
    assert selected.json()["recorded"] is True, selected.text
    record(db_session, commit=50, hour=6, branch=branch, variant=None, ci_run_id=ci,
           tests={TEST: F, OTHER: F})  # fmt: skip
    outcome = client.post(f"/repos/{REPO}/outcome", json={"ci_run_id": ci}, headers=auth)
    assert outcome.status_code == 200, outcome.text


def test_the_run_page_shows_what_siftwise_selected(client: TestClient, db_session: Session) -> None:
    _ci_run(client, db_session)
    signed_in(client)
    page = client.get(f"/ui/repos/{REPO}/-/runs/ci-7/1").text
    text_ = _text(page)
    assert "What siftwise did" in page
    assert "selective: 1 of 2 tests in history" in text_
    # No summed per-test estimate: the leg's own full-suite baseline (none yet here).
    assert "measured" in text_ and "for the full suite" not in text_
    assert "no baseline yet (3 full-suite runs of this variant in the 30 days before)" in text_
    assert "Order" in page and ">risk<" in page.replace("<dd>", ">").replace("</dd>", "<")
    assert "siftwise 0.4.0" in text_ and "#9" in text_
    assert "Highest-risk tests" in page
    # Each failure: in the selection with its rank, or run outside it.
    verdicts = page.split("<h2>Verdicts</h2>")[1]
    row = re.search(rf'title="{re.escape(TEST)}".*?</tr>', verdicts, re.S)
    other = re.search(rf'title="{re.escape(OTHER)}".*?</tr>', verdicts, re.S)
    assert row is not None and other is not None
    assert ">selected<" in row.group(0) and 'data-label="Rank">1<' in row.group(0)
    assert "not selected" in other.group(0)
    # The runs list says it too, and how many listed runs have one.
    runs = _text(client.get(f"/ui/repos/{REPO}/-/runs").text)
    assert "selective · 1 of 2" in runs and "selections recorded for 1 of these 2" in runs


def test_a_run_with_no_selection_says_why(client: TestClient, db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P}, ci_run_id="first")
    record(db_session, commit=2, hour=2, tests={TEST: F}, ci_run_id="old-client")
    signed_in(client)
    first = html.unescape(_text(client.get(f"/ui/repos/{REPO}/-/runs/first/1").text))
    assert "this was the repo's first CI run" in first
    # One reason where the upload says which client it was; the list only where it doesn't.
    cases = [
        ((None, None), "uploaded by a siftwise client older than 0.4"),
        (("0.4.0", "github"), "the select step didn't reach the server"),
        (("0.4.0", "gitlab"), "the select step didn't reach the server"),
        (("0.4.0", "cli"), "uploaded with siftwise record or backfill"),
        (("0.4.0", "backfill"), "uploaded with siftwise record or backfill"),
        (("0.4.0", None), "the upload doesn't say why. It can be"),
    ]
    for (version, kind), expected in cases:
        db_session.execute(
            text("UPDATE runs SET client_version = :v, client = :c WHERE ci_run_id = 'old-client'"),
            {"v": version, "c": kind},
        )
        page = html.unescape(_text(client.get(f"/ui/repos/{REPO}/-/runs/old-client/1").text))
        assert expected in page, (version, kind)
        assert not any(e in page for _, e in cases if e != expected), (version, kind)
    # Every page says how much of the window has selections.
    overview = _text(client.get(f"/ui/repos/{REPO}", params={"days": "all"}).text)
    assert "Selections recorded for 0 of 2 CI runs in all recorded history" in overview


def test_a_branch_from_ci_is_escaped(client: TestClient, db_session: Session) -> None:
    _ci_run(client, db_session, branch=EVIL_BRANCH)
    signed_in(client)
    for path in (f"/ui/repos/{REPO}/-/runs", f"/ui/repos/{REPO}/-/runs/ci-7/1"):
        page = client.get(path).text
        assert "<script>" not in page and "&lt;script&gt;" in page, path


def test_a_selection_without_estimates_and_a_full_shadow_one_read_plainly(
    client: TestClient, db_session: Session
) -> None:
    # Found in the screenshots: "estimated 0.0 min of 0.0 min" for a history without
    # durations, and "would run 9,167 of 9,167" for a shadow run's full-suite selection.
    _ci_run(client, db_session)
    db_session.execute(text(
        "UPDATE ci_selections SET estimated_ms_total = 0, estimated_ms_would_run = 0, "
        "shadow = true, mode = 'full'"
    ))  # fmt: skip
    signed_in(client)
    page = _text(client.get(f"/ui/repos/{REPO}/-/runs/ci-7/1").text)
    # A shadow row (its outcome is now a shadow comparison): the measured saving is below.
    assert "measures what it would have saved" in page and "0.0 min of" not in page
    assert "test time saved" not in page
    runs = _text(client.get(f"/ui/repos/{REPO}/-/runs").text)
    assert "shadow · would run the full suite" in runs


def test_the_run_page_labels_what_each_count_and_time_measures(
    client: TestClient, db_session: Session
) -> None:
    # Found in a screenshot: "637 of 7,487" above and "169 of 2,144" in the shadow table for
    # one variant, and "not estimated" next to a time saved, with nothing saying why.
    _shadow_data(client, db_session)
    db_session.execute(text("UPDATE ci_selections SET estimated_ms_total = 0, "
                            "estimated_ms_would_run = 0"))  # fmt: skip
    signed_in(client)
    page = client.get(f"/ui/repos/{REPO}/-/runs/{SHADOW}/1").text
    text_ = html.unescape(_text(page))
    assert "would have run 1 of 2 tests in history" in text_
    assert "Would have run, of the tests that ran here" in text_
    assert "Time saved (measured)" in text_
    assert (
        "measures what it would have saved" in text_
        and "estimated" not in text_.split("Time budget")[0].split("Test time")[1]
    )
    assert ">Reason<" not in page  # a selective selection: the reason repeats it


def test_a_full_selection_shows_its_reason(client: TestClient, db_session: Session) -> None:
    _ci_run(client, db_session)
    db_session.execute(text(
        "UPDATE ci_selections SET mode = 'full', reason = 'build/config file changed: go.mod', "
        "reason_code = 'build_file', reason_items = '{go.mod}'"
    ))  # fmt: skip
    signed_in(client)
    page = client.get(f"/ui/repos/{REPO}/-/runs/ci-7/1").text
    assert ">Reason<" in page and "build/config file changed: go.mod" in page
    assert "Caused by:" in page


def test_failures_are_listed_by_their_rank_and_stack_on_a_phone(
    client: TestClient, db_session: Session
) -> None:
    _ci_run(client, db_session)
    # Rank OTHER first, TEST second: the reverse of their names' order.
    db_session.execute(text(
        "UPDATE ci_selections SET selected_keys = ARRAY(SELECT id FROM tests "
        "WHERE test_id IN (:a, :b) ORDER BY test_id DESC)"
    ), {"a": TEST, "b": OTHER})  # fmt: skip
    signed_in(client)
    page = client.get(f"/ui/repos/{REPO}/-/runs/ci-7/1").text
    verdicts = page.split("<h2>Verdicts</h2>")[1]
    assert verdicts.index(f'title="{OTHER}"') < verdicts.index(f'title="{TEST}"')
    # Below 640 px the rows stack: every fact keeps a label, the full name can wrap.
    assert '<table class="stack">' in verdicts
    for label in ("Verdict", "Confidence", "In the selection", "Rank", "Why"):
        assert f'data-label="{label}"' in verdicts, label
    assert "tests.<wbr>" not in verdicts and "tests.test_db::<wbr>test_query" in verdicts


def test_the_full_test_name_is_escaped_before_it_can_wrap() -> None:
    assert str(pages._breakable('a/<b>::c"')) == "a/<wbr>&lt;b&gt;::<wbr>c&#34;"


def test_go_package_reasons_are_grouped_for_display_only() -> None:
    from siftwise.api.dashboard.data import grouped_reasons

    pkg = "in Go package github.com/a/dns, which has changed"
    reasons = [f"{pkg} dns/parse.go", "imports changed package x", f"{pkg} dns/relay.go",
               f"{pkg} dns/parse.go"]  # fmt: skip
    assert grouped_reasons(reasons) == [
        f"{pkg} dns/parse.go, dns/relay.go", "imports changed package x"]  # fmt: skip


def test_links_say_where_they_go(client: TestClient, db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P}, ci_run_id="first")
    record(db_session, commit=2, hour=2, tests={TEST: F}, ci_run_id="old")
    signed_in(client)
    old = client.get(f"/ui/repos/{REPO}/-/runs/old/1").text
    assert ">How to upgrade</a>" in old and "CHANGELOG.md#upgrading" in old
    first = client.get(f"/ui/repos/{REPO}/-/runs/first/1").text
    assert ">How selections are recorded</a>" in first
    empty = client.get("/ui/repos/acme/none/-/runs").text
    assert "GitHub Action</a>" not in empty


def test_the_ci_section_never_shows_0_for_a_window_without_selections(
    client: TestClient, db_session: Session
) -> None:
    record(db_session, commit=1, hour=_hours_ago(5), tests={TEST: P}, ci_run_id="1")
    signed_in(client)
    text_ = html.unescape(_text(client.get(f"/ui/repos/{REPO}").text))
    assert "No recorded selections in this window" in text_
    assert "Selective runs" not in text_ and "0 min" not in text_
    # Main has a selection, pull requests don't: that column says so instead of 0%.
    _ci_run(client, db_session)  # a pull request run with a selection
    db_session.execute(text("UPDATE runs SET is_main = true WHERE ci_run_id = 'ci-7'"))
    record(db_session, commit=3, hour=_hours_ago(4), branch="pr", tests={TEST: P},
           ci_run_id="pr-1")  # fmt: skip
    page = client.get(f"/ui/repos/{REPO}", params={"days": "all"}).text
    row = re.search(r">Selective runs</th>.*?</tr>", page, re.S)
    assert row is not None
    cells = re.findall(r'data-label="([^"]+)">(.*?)</td>', row.group(0), re.S)
    assert [label for label, _ in cells] == ["Main", "Pull requests and other branches", "All"]
    assert "100% (1 of 1)" in cells[0][1]
    assert "no recorded selections in this window" in cells[1][1] and "%" not in cells[1][1]
