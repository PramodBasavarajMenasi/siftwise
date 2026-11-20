"""FastAPI application entry point."""

from fastapi import FastAPI

from siftwise import __version__
from siftwise.api.auth import BearerAuthMiddleware
from siftwise.api.deps import SessionTokenStore, app_sessions
from siftwise.api.forwarded import ForwardedMiddleware
from siftwise.api.limits import BodySizeLimitMiddleware
from siftwise.api.routes import explain, flaky, history, repos, runs, select, shadow, triage
from siftwise.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="siftwise", version=__version__)
    app.state.settings = settings  # read by routes through siftwise.api.deps.get_app_settings
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
    if settings.dashboard:
        from siftwise.api.dashboard import pages

        app.include_router(pages.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
