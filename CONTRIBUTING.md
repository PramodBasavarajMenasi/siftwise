# Contributing to siftwise

Issues and pull requests are welcome. This page covers the dev setup, the checks CI runs,
migrations, and what a PR needs. For how the pieces fit, read
[ARCHITECTURE.md](ARCHITECTURE.md) first.

## Setup

You need Python 3.11+, [uv](https://docs.astral.sh/uv/), Docker and git. On Windows, run the
shell commands in Git Bash or WSL.

```sh
git clone https://github.com/PramodBasavarajMenasi/siftwise.git
cd siftwise
uv sync                                   # deps, the dev group and every extra
cp .env.example .env                      # then set SIFTWISE_API_TOKEN in it
# Postgres on :5432 plus a siftwise_test database: the quickstart file + the dev additions
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres
```

- **`docker-compose.yml` alone is the quickstart:** it doesn't publish Postgres and creates no
  test database. Development always adds `docker-compose.dev.yml`.
- **The test database:** the database tests run only when `SIFTWISE_TEST_DATABASE_URL` is set;
  without it they're skipped, not failed:

  ```sh
  export SIFTWISE_TEST_DATABASE_URL=postgresql+psycopg://siftwise:siftwise@localhost:5432/siftwise_test
  ```

- **The `siftwise_test` database** is created when the Postgres volume is first created
  (`docker/initdb`). The migration tests create and drop a scratch database, so the role needs
  `CREATEDB` (the compose role is a superuser).

## Run the server locally

```sh
uv run alembic upgrade head                        # or: uv run siftwise server migrate
uv run uvicorn siftwise.api.main:app --reload      # or: uv run siftwise server start
```

Both read `SIFTWISE_DATABASE_URL` and `SIFTWISE_API_TOKEN` from the environment or `.env`.
To run the whole stack in Docker instead (the API image too):

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Rebuild with `--build` after pulling: an old API image can't find newer migrations, fails
with `Can't locate revision ...` and restarts in a loop.

## Checks

CI ([ci.yml](.github/workflows/ci.yml)) runs these on Python 3.11 and 3.12; run them before
opening a PR:

```sh
uv run ruff check
uv run ruff format --check                # `uv run ruff format` fixes it
uv run mypy src tests scripts             # strict
uv run pytest                             # with SIFTWISE_TEST_DATABASE_URL set
```

**The package checks** (CI's `package` job) build the sdist and wheel, run `twine check`,
check the sdist's file list, and install the wheel in fresh venvs: the light install must
import none of the server's or MCP's dependencies, and `[server]` and `[mcp]` must work.

```sh
uv build && uv run --no-project python scripts/check_dist.py dist
```

Two more workflows run on PRs:

- [quickstart.yml](.github/workflows/quickstart.yml) runs the README Quickstart as written
  (below).
- [action-e2e.yml](.github/workflows/action-e2e.yml) runs the three GitHub Actions against a
  real server: verdicts, `fail-on`, shadow mode, `known_flaky` and re-runs.

## The quickstart script

`scripts/run_quickstart.py` takes the README's ```` ```sh quickstart ```` blocks in order,
runs them in one bash, and checks the selection the last block wrote. Change the Quickstart's
commands, and this script and `tests/test_run_quickstart.py` check them.

```sh
python scripts/run_quickstart.py              # CI's run
python scripts/run_quickstart.py --port 8001  # locally, when port 8000 is taken
```

- It always runs as its own compose project (`siftwise-quickstart`) with its own volume, and
  ends with `docker compose down -v`, so it never touches your dev stack's data.
- It installs the CLI with `uv tool install .`: run it with `UV_TOOL_DIR` and
  `UV_TOOL_BIN_DIR` pointing elsewhere if you don't want your installed `siftwise` replaced.
- On Windows, pass Git Bash: `--bash "C:/Program Files/Git/bin/bash.exe"` (plain `bash` may
  be WSL's).

## Migrations

Migrations are Alembic scripts in `src/siftwise/migrations/versions`, numbered `0001`,
`0002`, ... They ship in the wheel: `siftwise server migrate` and `siftwise server start` apply
them without a checkout, under a Postgres advisory lock so replicas starting together migrate
once. `alembic.ini` at the repo root points at the same scripts for development.

To add one:

```sh
uv run alembic revision --rev-id 0011 -m "what it changes"
```

- **Change the models too** (`src/siftwise/core/models.py`): `test_migrations_match_models`
  fails when the migrated schema and the models differ.
- **Write a downgrade.** The test suite migrates the test database down to base and back up.
- **Data migrations get a test** on a scratch database (see `tests/test_migrations.py`).
- **Big tables:** `test_results` has tens of millions of rows on a real repo. Avoid rewriting
  it; add columns with defaults (no rewrite in Postgres 11+), and say in the migration's
  docstring what it locks and for how long.
- **Add an upgrade note below** when operators must do something after it.

### Upgrade notes

- **0005:** run `POST /repos/{repo}/rollup` (or `siftwise rollup --repo ...`) once per repo,
  to fill `last_seen_*` and `file_path`.
- **0006** rewrites `test_results` in place: it needs free disk for a second, smaller copy
  while it runs. Run `VACUUM ANALYZE test_results` afterwards.
- **0007** builds a small index on failed results (seconds; it blocks writes to
  `test_results` meanwhile).
- **0008 and 0009** only add the shadow-mode tables and columns.
- **0010** adds `runs.known_flaky` and `runs.rerun` and rebuilds the small `runs` unique index.

## Evals

The selection and triage evals replay real CI history; see
[docs/eval-methodology.md](docs/eval-methodology.md). They need a backfilled repo and
`GITHUB_TOKEN` (in the environment or `.env`; never print or log it):

```sh
uv run python scripts/backfill.py --repo RDFLib/rdflib --workflow validate.yaml \
    --artifact-pattern '*-pytest-junit-xml' --max-runs 20 --batch
uv run python -m scripts.eval.run_eval --repo RDFLib/rdflib --language python --source github
```

Each repo's exact commands are in its "Reproducing" section of
[docs/eval-results.md](docs/eval-results.md).

## Conventions

- **Type hints everywhere** (mypy strict), no bare `except`, ruff's rules as configured.
- **A test for every bug fix.**
- **Configuration only through environment variables** (`SIFTWISE_*`, `GITHUB_TOKEN`).
- **The light CLI imports no server dependency** at module level: `tests/test_packaging.py`
  runs every command's `--help` with them blocked.
- **When unsure, the selector runs the full suite,** and the Action fails open. Changes that
  trade safety for savings need eval numbers.
- **Small, focused PRs**, with the why in the description.

## PR checklist

- [ ] `ruff check`, `ruff format --check`, `mypy src tests scripts` and `pytest` (with the test
      database) pass
- [ ] A test for the change, and one for every bug fixed
- [ ] A migration, if the models changed, with a downgrade (and an upgrade note if operators
      must act)
- [ ] README or docs updated for anything a user sees: commands, Action inputs, API fields
- [ ] Quickstart changed? `python scripts/run_quickstart.py` passes
- [ ] Packaging changed? `uv build && uv run --no-project python scripts/check_dist.py dist`
      passes
- [ ] No secrets, tokens or real test-history data in the diff
