# Linux runner for the mutation eval (scripts/eval/mutation.py), for suites that need Linux.
# Cirq's CI (ci-monitoring.yml) runs Python 3.12 on Ubuntu with apt-system-requirements.txt;
# on Windows its multiprocessing and subprocess tests hang under pytest-timeout. The Jest
# profiles (mobx) run here too, on Node 24.
#
# No COPY of this repo: it's mounted read-only at /repo, and the eval's state (clone,
# virtualenvs, results, uv cache) lives in a volume at /state so runs resume. See
# docs/eval-results.md#mutation-based-recall for the docker run command.
#
#   docker build -t siftwise-mutation-eval -f scripts/eval/mutation-linux.Dockerfile scripts/eval
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv
# Node 24 for the Jest profiles (mobx's CI: actions/setup-node with node-version 24).
FROM node:24-bookworm@sha256:9d266751ca3d7fdbe651c878ffd5ad0a42fed49a18c283509267c97f025b7496 AS node

FROM python:3.12-bookworm
# Cirq's apt-system-requirements.txt (contrib's LaTeX and PDF tests), plus git for the clone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ghostscript latexmk lmodern poppler-utils texlive-latex-extra \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=node /usr/local/bin/node /usr/local/bin/
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
ENV UV_CACHE_DIR=/state/uv-cache \
    UV_PROJECT_ENVIRONMENT=/state/siftwise-venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /repo
