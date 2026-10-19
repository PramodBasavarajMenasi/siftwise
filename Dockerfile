# syntax=docker/dockerfile:1
# The siftwise API server: `siftwise server start` (migrations, then uvicorn on :8000) as a
# non-root user. Bases are pinned by digest; Dependabot proposes updates weekly. The build and
# runtime stages must use the same python image (the venv links to its interpreter).

FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

# --- build: the server extra into /app/.venv, siftwise installed as a wheel (no source tree) ---
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Dependencies first: their layer is reused until uv.lock changes.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra server --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra server --no-editable

# --- runtime: the same base, the venv only (no uv, no build tools, no source) ---
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="siftwise" \
      org.opencontainers.image.description="siftwise API server: test impact analysis for CI" \
      org.opencontainers.image.source="https://github.com/PramodBasavarajMenasi/siftwise" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
RUN useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin siftwise
# Root-owned and read-only to the server: it writes nothing to disk.
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001:10001
EXPOSE 8000
# No curl in slim images. The start period covers the migrations.
HEALTHCHECK --interval=10s --timeout=3s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
ENTRYPOINT ["siftwise", "server"]
CMD ["start", "--host", "0.0.0.0"]
