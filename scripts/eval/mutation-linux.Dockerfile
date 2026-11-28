# Linux runner for the mutation eval (scripts/eval/mutation.py), for suites that need Linux.
# Cirq's CI (ci-monitoring.yml) runs Python 3.12 on Ubuntu with apt-system-requirements.txt;
# on Windows its multiprocessing and subprocess tests hang under pytest-timeout. The Jest
# profiles (mobx) run here too, on Node 24, and the JVM profiles (moshi: mutation_jvm.py) on
# JDKs 17, 21 and 26 in /usr/lib/jvm, where Gradle auto-detects toolchains (moshi's
# records-tests wants 17; ktlint's build logic needs a 26 runtime).
#
# No COPY of this repo: it's mounted read-only at /repo, and the eval's state (clone,
# virtualenvs, results, uv cache) lives in a volume at /state so runs resume. See
# docs/eval-results.md#mutation-based-recall for the docker run command.
#
#   docker build -t siftwise-mutation-eval -f scripts/eval/mutation-linux.Dockerfile scripts/eval
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv
# Node 24 for the Jest profiles (mobx's CI: actions/setup-node with node-version 24).
FROM node:24-bookworm@sha256:9d266751ca3d7fdbe651c878ffd5ad0a42fed49a18c283509267c97f025b7496 AS node
FROM eclipse-temurin:21-jdk@sha256:8ad118b146aa92da815b576441acd6b42b38236797ad0b6edc8c200b85bb0734 AS jdk21
FROM eclipse-temurin:17-jdk@sha256:7a0b2bc7e1372bb05ee361fb79e8a9e822df47343b8716253b3bbaa75060653e AS jdk17
FROM eclipse-temurin:26-jdk@sha256:a45ef8c9f6304700665c5a5ac7250122e951831593bfa95859c295aea67f7d16 AS jdk26

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
COPY --from=jdk21 /opt/java/openjdk /usr/lib/jvm/jdk-21
COPY --from=jdk17 /opt/java/openjdk /usr/lib/jvm/jdk-17
COPY --from=jdk26 /opt/java/openjdk /usr/lib/jvm/jdk-26
ENV UV_CACHE_DIR=/state/uv-cache \
    UV_PROJECT_ENVIRONMENT=/state/siftwise-venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    JAVA_HOME=/usr/lib/jvm/jdk-21 \
    GRADLE_USER_HOME=/state/gradle-home
WORKDIR /repo
