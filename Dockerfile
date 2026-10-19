FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra server --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --extra server

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "siftwise", "server", "start", "--host", "0.0.0.0"]
