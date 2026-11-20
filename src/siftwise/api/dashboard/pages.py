"""The dashboard's pages: server-rendered HTML, read-only, no JavaScript.

Templates are Jinja2 with autoescape forced on: test IDs, messages, branches, repo names and
causes are untrusted and are only ever inserted escaped (no ``|safe`` on data). The numbers
come from the API's own core functions (``data.py``).
"""

import hashlib
import math
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, Request
from jinja2 import Environment, PackageLoader, StrictUndefined
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, RedirectResponse, Response

from siftwise import __version__
from siftwise.api.dashboard import data
from siftwise.api.dashboard.auth import (
    CSRF_COOKIE,
    LOGIN_PATH,
    PREFIX,
    SESSION_COOKIE,
    cookie,
)
from siftwise.api.deps import get_app_settings
from siftwise.api.forwarded import public_origin
from siftwise.cli.main import CAUSE_LABELS
from siftwise.cli.repoconfig import SUGGESTED_FLAKY_DAYS, known_flaky_entry
from siftwise.config import Settings
from siftwise.core import ui_sessions
from siftwise.core.flaky import FlakyTest, flaky_tests
from siftwise.core.selector import matching_glob
from siftwise.core.shadow import VERDICT_ORDER
from siftwise.core.shadow import recall as shadow_recall
from siftwise.core.tokens import ALL_REPOS, Principal
from siftwise.core.triage import triage_ci_run
from siftwise.core.ui_sessions import Active
from siftwise.db import get_session

router = APIRouter(include_in_schema=False)

README = "https://github.com/PramodBasavarajMenasi/siftwise/blob/main/README.md"
# Windows: the last N days, counted back from NOW on every page (None = all history). The
# recall API and `siftwise report` count the same way; `siftwise flaky` ends at the newest run.
DAYS: tuple[int | None, ...] = (7, 30, 90, None)
DEFAULT_DAYS = 30
MAX_RUN_TESTS = 500  # tests listed on a run page
MAX_FLAKY_ROWS = 100  # flaky tests listed (and entries suggested); `siftwise flaky` has all
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
CSRF_LIFETIME = timedelta(hours=1)

Db = Annotated[Session, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]


# --- rendering ----------------------------------------------------------------------------


def _minutes(ms: float) -> str:
    minutes = ms / 60_000
    return f"{minutes:.1f} min" if minutes < 120 else f"{minutes / 60:.1f} h"


def _pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{100 * value:.{digits}f}%"


def _recall(value: float | None) -> str:
    """A recall, rounded DOWN: 2,043 of 2,044 is 99.9%, never "100.0%" with a miss."""
    return "n/a" if value is None else f"{math.floor(1000 * value) / 10:.1f}%"


def _num(value: int | float) -> str:
    return f"{value:,}"


def _when(value: datetime | None) -> str:
    return "" if value is None else value.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def _day(value: datetime | None) -> str:
    return "" if value is None else value.astimezone(UTC).strftime("%Y-%m-%d")


def _ago(value: datetime | None, now: datetime | None = None) -> str:
    """How long ago, roughly: "just now", "5 minutes ago", "3 hours ago", "39 days ago"."""
    if value is None:
        return ""
    minutes = ((now or datetime.now(UTC)) - value).total_seconds() / 60
    if minutes < 1.5:
        return "just now"
    if minutes < 90:
        n, unit = round(minutes), "minute"
    elif minutes < 36 * 60:
        n, unit = round(minutes / 60), "hour"
    else:
        n, unit = int(minutes // (60 * 24)), "day"
    return f"{n} {unit}{'' if n == 1 else 's'} ago"


def _window(days: int | None) -> str:
    return "all recorded history" if days is None else f"the last {days} days"


def _with_days(url: str, days: int | None) -> str:
    return f"{url}?days={'all' if days is None else days}"


def _repo_url(repo: str, page: str = "", **params: Any) -> str:
    """A repo page's URL (repo names have "/": each part is quoted, the slashes kept)."""
    url = f"{PREFIX}/repos/{quote(repo, safe='/')}" + (f"/-/{page}" if page else "")
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    return f"{url}?{query}" if query else url


@dataclass(frozen=True)
class Stylesheet:
    name: str  # siftwise-<content hash>.css: any change to the CSS is a new URL
    css: bytes


def _css_path() -> Traversable:
    return resources.files("siftwise.api.dashboard").joinpath("static/siftwise.css")


@lru_cache(maxsize=4)
def _stylesheet_at(mtime_ns: int, size: int) -> Stylesheet:
    css = _css_path().read_bytes()
    return Stylesheet(f"siftwise-{hashlib.sha256(css).hexdigest()[:16]}.css", css)


def stylesheet_file() -> Stylesheet:
    """The stylesheet and its name. Named by a hash of its content, not the release version,
    so browsers never keep an old copy after the CSS changes (it is cached as immutable). Read
    again when the file changes on disk (a development checkout)."""
    path = _css_path()
    try:
        stat = Path(str(path)).stat()
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:  # not a plain file (a zip import): it can't change
        key = (0, 0)
    return _stylesheet_at(*key)


def _short(text: str, limit: int = 72) -> str:
    """A long test ID shortened in the middle, keeping its start and its end (the test's
    name): the page puts the full value in a title attribute."""
    if len(text) <= limit:
        return text
    head = limit // 3
    return f"{text[:head]}…{text[-(limit - head - 1) :]}"


def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("siftwise.api.dashboard", "templates"),
        autoescape=True,  # every template, whatever its name
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(minutes=_minutes, pct=_pct, recall=_recall, num=_num, when=_when,
                       day=_day, ago=_ago, window=_window, short=_short)  # fmt: skip
    env.globals.update(repo_url=_repo_url, with_days=_with_days, days_options=DAYS,
                       version=__version__, stylesheet=lambda: stylesheet_file().name,
                       readme=README, prefix=PREFIX, cause_labels=CAUSE_LABELS,
                       verdicts=VERDICT_ORDER)  # fmt: skip
    return env


ENV = _environment()


def render(template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    html = ENV.get_template(template).render(**context)
    return HTMLResponse(html, status_code=status_code)


def _page(request: Request, template: str, status_code: int = 200, **context: Any
          ) -> HTMLResponse:  # fmt: skip
    active: Active = request.state.ui_session
    return render(template, status_code, principal=active.principal, csrf=active.csrf,
                  **context)  # fmt: skip


def not_found(active: Active) -> HTMLResponse:
    """The same answer for a repo that doesn't exist and one the token can't see."""
    return render("message.html", 404, principal=active.principal, csrf=active.csrf,
                  title="Not found", crumbs=[], repo=None,
                  message="No such page, or this token can't see it.")  # fmt: skip


def _days(request: Request) -> int | None:
    """The window asked for: 7, 30 or 90 days, or None for "all"; else the default."""
    raw = request.query_params.get("days", "")
    if raw == "all":
        return None
    return int(raw) if raw.isdigit() and int(raw) in DAYS else DEFAULT_DAYS


def _since(days: int | None, now: datetime) -> datetime | None:
    return None if days is None else now - timedelta(days=days)


def _days_back_to(oldest: datetime | None, now: datetime) -> int:
    """Whole days from ``oldest`` to now, rounded up: a window that reaches it."""
    if oldest is None:
        return DEFAULT_DAYS
    return max(1, -(-(now - oldest) // timedelta(days=1)) + 1)


def _principal(request: Request) -> Principal:
    principal: Principal = request.state.principal
    return principal


# --- origin, cookies, CSRF ----------------------------------------------------------------


def _origin_ok(request: Request) -> bool:
    """The form came from this dashboard's own public origin (behind a trusted proxy: the
    forwarded one). No Origin header (older browsers): the CSRF token alone decides."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    scheme, host = public_origin(request.scope)
    return origin.strip().lower() == f"{scheme}://{host}"


def _transport(request: Request) -> tuple[bool, bool]:
    """``(allowed, secure)``: HTTPS (Secure cookies), or plain HTTP only on localhost."""
    scheme, host = public_origin(request.scope)
    hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    if scheme == "https":
        return True, True
    return hostname in LOCAL_HOSTS, False


def _set_cookie(response: Response, name: str, value: str, max_age: timedelta, secure: bool
                ) -> None:  # fmt: skip
    response.set_cookie(name, value, max_age=int(max_age.total_seconds()), path=PREFIX,
                        secure=secure, httponly=True, samesite="strict")  # fmt: skip


def _login_page(request: Request, status_code: int = 200, error: str | None = None
                ) -> HTMLResponse:  # fmt: skip
    allowed, secure = _transport(request)
    csrf = secrets.token_urlsafe(32)
    response = render("login.html", status_code, error=error, csrf=csrf, allowed=allowed,
                      principal=None, crumbs=[], repo=None, title="Sign in")  # fmt: skip
    if allowed:
        _set_cookie(response, CSRF_COOKIE, csrf, CSRF_LIFETIME, secure)
    return response


@router.get(LOGIN_PATH)
def login_form(request: Request) -> HTMLResponse:
    return _login_page(request)


@router.post(LOGIN_PATH)
async def login(request: Request, settings: AppSettings) -> Response:
    allowed, secure = _transport(request)
    if not allowed:
        return _login_page(request, 400)
    try:
        fields = parse_qs((await request.body()).decode("utf-8"), max_num_fields=4)
    except (UnicodeDecodeError, ValueError):
        return _login_page(request, 400, "The form could not be read.")
    sent = fields.get("csrf", [""])[0]
    expected = cookie(request.scope, CSRF_COOKIE)
    if not (_origin_ok(request) and sent and expected
            and secrets.compare_digest(sent.encode(), expected.encode())):  # fmt: skip
        return _login_page(request, 403, "The form expired. Sign in again.")
    token = fields.get("token", [""])[0]
    made = await run_in_threadpool(_login_sync, request, token, settings.api_token)
    if made is None:
        # Never echoed: the field comes back empty, and nothing says which part was wrong.
        return _login_page(request, 401, "Token not accepted (unknown or revoked).")
    response = RedirectResponse(f"{PREFIX}/", status_code=303)
    _set_cookie(response, SESSION_COOKIE, made.session_id, ui_sessions.LIFETIME, secure)
    response.delete_cookie(CSRF_COOKIE, path=PREFIX)
    return response


def _login_sync(request: Request, token: str, admin: str | None) -> ui_sessions.Login | None:
    from siftwise.api.deps import app_sessions

    with app_sessions(request.app)() as session:
        return ui_sessions.login(session, token, admin)


@router.post(f"{PREFIX}/logout")
async def logout(request: Request) -> Response:
    active: Active = request.state.ui_session
    try:
        fields = parse_qs((await request.body()).decode("utf-8"), max_num_fields=4)
    except (UnicodeDecodeError, ValueError):
        fields = {}
    sent = fields.get("csrf", [""])[0]
    if not (_origin_ok(request) and sent
            and secrets.compare_digest(sent.encode(), active.csrf.encode())):  # fmt: skip
        message = "The form expired. Reload the page and try again."
        return _page(request, "message.html", 403, title="Not signed out", crumbs=[],
                     repo=None, message=message)  # fmt: skip
    session_id = cookie(request.scope, SESSION_COOKIE)
    await run_in_threadpool(_logout_sync, request, session_id)
    response = RedirectResponse(LOGIN_PATH, status_code=303)
    response.delete_cookie(SESSION_COOKIE, path=PREFIX)
    return response


def _logout_sync(request: Request, session_id: str) -> None:
    from siftwise.api.deps import app_sessions

    with app_sessions(request.app)() as session:
        ui_sessions.logout(session, session_id)


@router.get(f"{PREFIX}/static/{{name}}")
def stylesheet(name: str) -> Response:
    sheet = stylesheet_file()
    if name != sheet.name:  # an old hash, or anything else
        return Response("not found", status_code=404, media_type="text/plain")
    return Response(sheet.css, media_type="text/css",
                    headers={"cache-control": "private, max-age=31536000, immutable"})  # fmt: skip


# --- pages --------------------------------------------------------------------------------


@router.get(PREFIX)
def ui_root() -> RedirectResponse:
    return RedirectResponse(f"{PREFIX}/", status_code=303)


@router.get(f"{PREFIX}/")
def index(request: Request, session: Db) -> HTMLResponse:
    principal = _principal(request)
    days = _days(request)
    repos = data.repos_for(session, principal, _since(days, datetime.now(UTC)))
    return _page(request, "index.html", title="Repos", crumbs=[], repo=None, repos=repos,
                 all_repos=principal.repo == ALL_REPOS, days=days)  # fmt: skip


def _repo_crumbs(repo: str) -> list[tuple[str, str]]:
    return [("Repos", f"{PREFIX}/"), (repo, _repo_url(repo))]


@router.get(f"{PREFIX}/repos/{{repo:path}}/-/runs/{{ci_run_id}}/{{attempt:int}}")
def run_page(request: Request, repo: str, ci_run_id: str, attempt: int, session: Db,
             settings: AppSettings) -> HTMLResponse:  # fmt: skip
    meta = data.attempt_meta(session, repo, ci_run_id, attempt)
    if meta is None:
        return not_found(request.state.ui_session)
    variants = triage_ci_run(session, repo, ci_run_id, attempt, settings.stats_window_days) or []
    by_test: dict[str, list[tuple[str | None, Any]]] = {}
    for v in variants:
        for result in v.report.results if v.report else ():
            by_test.setdefault(result.test_id, []).append((v.variant, result))
    rank = {name: i for i, name in enumerate(VERDICT_ORDER)}
    conf = {"high": 0, "medium": 1, "low": 2}
    rows = []
    for test_id, results in by_test.items():
        results.sort(key=lambda r: (rank.get(r[1].verdict.value, 9),
                                    conf.get(r[1].confidence.value, 9), r[0] or ""))  # fmt: skip
        worst = results[0][1]
        rows.append({
            "test_id": test_id, "verdict": worst.verdict.value,
            "confidence": worst.confidence.value,
            "variants": [v for v, r in results if r.verdict == worst.verdict],
            "reason": worst.reasons[0] if worst.reasons else "",
        })  # fmt: skip
    rows.sort(key=lambda r: (rank.get(r["verdict"], 9), conf.get(r["confidence"], 9),
                             r["test_id"]))  # fmt: skip
    counts = Counter(r["verdict"] for r in rows)
    shadow = data.shadow_attempt(session, repo, ci_run_id, attempt)
    crumbs = [*_repo_crumbs(repo), ("Runs", _repo_url(repo, "runs")),
              (f"{ci_run_id} attempt {attempt}", "")]  # fmt: skip
    return _page(request, "run.html", title=f"Run {ci_run_id}", crumbs=crumbs, repo=repo,
                 tab="runs", meta=meta, rows=rows[:MAX_RUN_TESTS],
                 more=max(0, len(rows) - MAX_RUN_TESTS), counts=counts,
                 variant_names=[v.variant for v in variants], shadow=shadow,
                 multi=len(variants) > 1)  # fmt: skip


@router.get(f"{PREFIX}/repos/{{repo:path}}/-/runs")
def runs_page(request: Request, repo: str, session: Db, settings: AppSettings) -> HTMLResponse:
    runs = data.recent_runs(session, repo)
    recorded = data.repo_activity(session, repo, None)
    return _page(request, "runs.html", title=f"{repo} runs",
                 crumbs=[*_repo_crumbs(repo), ("Runs", "")], repo=repo, tab="runs",
                 runs=runs or [], known=runs is not None, limit=data.RECENT_RUNS,
                 recorded=recorded)  # fmt: skip


@dataclass(frozen=True)
class FlakyRow:
    test: FlakyTest
    known_flaky: str | None  # the known_flaky glob that lists it


@router.get(f"{PREFIX}/repos/{{repo:path}}/-/flaky")
def flaky_page(request: Request, repo: str, session: Db) -> HTMLResponse:
    days = _days(request)
    now = datetime.now(UTC)
    recorded = data.repo_activity(session, repo, _since(days, now))
    span = days if days is not None else _days_back_to(recorded.oldest_run_at, now)
    report = flaky_tests(session, repo, span, end=now)
    rows: list[FlakyRow] = []
    until = datetime.now(UTC).date() + timedelta(days=SUGGESTED_FLAKY_DAYS)
    if report is not None:
        for t in report.tests:
            match = matching_glob(t.test_id, t.file_path, report.main_known_flaky)
            rows.append(FlakyRow(t, match))
    more = max(0, len(rows) - MAX_FLAKY_ROWS)
    rows = rows[:MAX_FLAKY_ROWS]
    suggested = [known_flaky_entry(r.test.test_id, until) for r in rows
                 if r.known_flaky is None]  # fmt: skip
    return _page(request, "flaky.html", title=f"{repo} flaky tests",
                 crumbs=[*_repo_crumbs(repo), ("Flaky tests", "")], repo=repo, tab="flaky",
                 days=days, span=span, report=report, rows=rows, more=more,
                 suggested=suggested, until=until, recorded=recorded)  # fmt: skip


@router.get(f"{PREFIX}/repos/{{repo:path}}")
def overview(request: Request, repo: str, session: Db) -> HTMLResponse:
    days = _days(request)
    now = datetime.now(UTC)
    recorded = data.repo_activity(session, repo, _since(days, now))
    span = days if days is not None else _days_back_to(recorded.oldest_shadow_at, now)
    report = shadow_recall(session, repo, span, now, by_week=True)
    crumbs = [*_repo_crumbs(repo)[:1], (repo, "")]
    return _page(request, "overview.html", title=repo, crumbs=crumbs, repo=repo, tab="overview",
                 days=days, report=report, recorded=recorded)  # fmt: skip
