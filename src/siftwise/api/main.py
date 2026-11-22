"""FastAPI application entry point."""

from fastapi import FastAPI

from siftwise import __version__
from siftwise.api.auth import BearerAuthMiddleware
from siftwise.api.deps import SessionTokenStore, app_sessions
from siftwise.api.forwarded import ForwardedMiddleware
from siftwise.api.limits import BodySizeLimitMiddleware
from siftwise.api.routes import ci, explain, flaky, history, repos, runs, select, shadow, triage
from siftwise.config import Settings, get_settings
from siftwise.db import sessionmaker_for


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="siftwise", version=__version__)
    app.state.settings = settings  # read by routes through siftwise.api.deps.get_app_settings
    # Every request's session (db.get_session) and the middleware's token lookups use this
    # app's database, the one in its settings: never the process-wide SIFTWISE_DATABASE_URL.
    app.state.sessionmaker = sessionmaker_for(settings.database_url)
    # Starlette runs the last-added middleware first: forwarded headers, then auth (which
    # rejects before any body is read), then the dashboard's session check, then the limit.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_upload_bytes)
    if settings.dashboard:
        from siftwise.api.dashboard.auth import DashboardAuthMiddleware

        app.add_middleware(
            DashboardAuthMiddleware, admin_token=settings.api_token, sessions=app_sessions(app)
        )
    app.add_middleware(
        BearerAuthMiddleware,
        token=settings.api_token,
        tokens=SessionTokenStore(app_sessions(app)),
    )
    app.add_middleware(ForwardedMiddleware, trusted_hosts=settings.forwarded_allow_ips)
    app.include_router(runs.router)
    app.include_router(history.router)
    app.include_router(select.router)
    app.include_router(triage.router)
    app.include_router(explain.router)
    app.include_router(repos.router)
    app.include_router(shadow.router)
    app.include_router(flaky.router)
    app.include_router(ci.router)
    if settings.dashboard:
        from siftwise.api.dashboard import pages

        app.include_router(pages.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # The version (from 0.4): clients use it to pick /repos/{repo}/outcome over the
        # older /repos/{repo}/shadow. An older server reports none.
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
