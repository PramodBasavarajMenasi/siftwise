<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img alt="siftwise" src="assets/logo-light.svg" width="370">
  </picture>
</p>

<p align="center">
  <strong>Run only the tests your change needs, safely.</strong><br>
  Self-hosted test selection for agent-driven CI.
</p>

<p align="center">
  <a href="https://github.com/PramodBasavarajMenasi/siftwise/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/PramodBasavarajMenasi/siftwise/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="Status: alpha" src="https://img.shields.io/badge/status-alpha-orange">
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#results-on-real-repos">Results</a> ·
  <a href="#roadmap">Roadmap</a>
</p>

<p align="center">
  <b>2,049 / 2,050</b> real failures caught on rdflib (the miss: a flaky test) · <b><code>/select</code> in ~0.6 s</b> on 18,000 tests · <b>self-hosted</b>, Apache-2.0
</p>

<p align="center"><sub>If siftwise looks useful, a ⭐ helps others find it.</sub></p>

> **Alpha, early development.** The server, CLI, test selection and GitHub Action work, and
> selection has been evaluated on two real repos. The MCP server and failure triage don't
> exist yet. APIs and the database schema may change.

## The problem

Coding agents multiply pull requests and CI runs. Running the full suite on every push wastes
time and money, and an agent can't tell whether a red test is its own failure, already broken
on main, or flaky. See Anthropic's [Agentic coding is straining CI](https://claude.com/blog/agentic-coding-is-straining-ci-heres-how-we-scaled-test-impact-analysis-at-anthropic)
and Linear's [AI coding has made CI a bottleneck](https://linear.app/now/ci-bottleneck-reworked).

## How it works

```mermaid
flowchart LR
    CI["CI run<br/>(JUnit XML)"] -->|POST /runs| S["siftwise server"]
    S --> DB[("Postgres<br/>per-test history")]
    PR["PR or agent branch"] -->|"siftwise select<br/>(git diff + import graph)"| S
    S -->|"test command + reasons"| T["pytest / go test / jest"]
```

<p align="center">
  <img alt="siftwise select on an rdflib commit: 325 test files import the changed modules, 17921 of 18020 known tests selected, and a pytest command" src="assets/demo.svg" width="900">
</p>

- **Record.** CI uploads JUnit XML with the commit, branch and changed files. siftwise keeps every
  result and rolls up per-test history: failures, average duration, a flaky score, broken on main.
- **Select.** `siftwise select` diffs your branch, builds a Go or Python import graph locally, and
  asks the server which tests the change can affect.
- **Fall back safely.** Unknown diff, changed build/CI/dependency files, no history, or a changed
  file no signal covers → the full suite. Missing a failure is worse than running extra tests.
- **Explain.** Every selected test lists its reasons, e.g. `imports changed module rdflib/term.py`
  or `co-change: failed in run 412 (3f2a9c1), which also changed src/cart.py`.

## Results on real repos

Replays of real CI history with no peeking at the future. Full details: [docs/eval-results.md](docs/eval-results.md).

| Repo | Failures caught | Test time skipped |
|---|---|---|
| [ipfs/kubo](https://github.com/ipfs/kubo) (Go, ~4,100 tests) | 19 of 19 in failing PRs | 30.7% over 20 main commits replayed as PRs; 0% on recent PRs (all dependency/CI bumps) |
| [RDFLib/rdflib](https://github.com/RDFLib/rdflib) (Python, ~18,000 tests) | 2,049 of 2,050 in 39 failing PRs; 140 of 141 in selective runs. The miss is a flaky remote-service test (see below) | 10.4% on the 10 most recent PRs (was 0.4% before lint-only configs stopped forcing the full suite); 0.6% over 19 main commits |
| [quantumlib/Cirq](https://github.com/quantumlib/Cirq) (Python monorepo, 9 packages, ~40,000 tests) | not measurable: JUnit only from main, which never failed | 37.3% over 134 merged PRs; 80% for `cirq-google`-only PRs, 19% for `cirq-core` |
| [agent-substrate/substrate](https://github.com/agent-substrate/substrate) (Go, ~9,000 tests, 8 days of history) | 2,041 of 2,044 in 72 failing PRs; 36 of 39 in selective runs, with a declared rule for its e2e suites (26 of 38 without) | 5.8% on PR runs and 15.1% over 40 main commits with the rule (19.5% / 31.7% without) |

In the Cirq monorepo, savings follow package boundaries: a change to one integration package
skips most of the suite, a change to the core package that everything imports skips little.
substrate's misses are end-to-end suites that drive a deployed system (no import edge reaches
them) and flaky tests; a declared dependency catches the e2e ones at the cost of most of the
savings.

rdflib's code changes save almost nothing: its `__init__.py` and plugin registry connect nearly
every module to nearly every test file, so safe file-level selection still runs ~99% of the
suite. The 10.4% comes from PRs that only touch lint configs. Bigger savings need codebases with
independent parts (monorepos), or per-test coverage. The one miss, `test_service_with_bound_solutions`,
calls a remote SPARQL service and also failed in 2 earlier unrelated runs: it's flaky, and
flaky-test detection (planned) should label it rather than count it as a miss.

## Quickstart

Start the server with Postgres. The API token is a shared secret you choose:

```sh
git clone https://github.com/PramodBasavarajMenasi/siftwise.git
cd siftwise
export SIFTWISE_API_TOKEN=$(openssl rand -hex 32)
docker compose up -d --build
curl http://localhost:8000/healthz        # {"status":"ok"}
```

Seed history from a repo's past GitHub Actions runs (here rdflib, which uploads one JUnit
artifact per matrix leg). `GITHUB_TOKEN` needs `actions:read` and `contents:read`:

```sh
export GITHUB_TOKEN=...
uv run python scripts/backfill.py --repo RDFLib/rdflib --workflow validate.yaml \
    --artifact-pattern '*-pytest-junit-xml' --max-runs 20 --batch
```

Install the CLI and ask for a test command in a checkout of that repo:

```sh
uv tool install .                          # puts `siftwise` on your PATH
cd .. && git clone https://github.com/RDFLib/rdflib.git && cd rdflib
siftwise select --repo RDFLib/rdflib --base HEAD~1
```

The command goes to stdout (so `eval "$(siftwise select ...)"` works) and a summary to stderr:

```text
siftwise: python imports: 325 test file(s) import changed modules
siftwise: selective (11448 of 11516 known tests): 11448 of 11516 known tests selected
pytest test/data/suites/trix/test_trix.py test/jsonld/test_api.py ...
```

`--json` prints the full response with per-test reasons. Exit codes: 0 ok, 1 API error, 2 usage
error, 3 full suite needed but no command known. In CI, check out with `fetch-depth: 0` so the
`base...head` diff works; if it fails, the full suite runs.

### Record results from CI

On GitHub, use the [GitHub Action](#github-action). Elsewhere, upload JUnit XML after each test
run with the CLI:

```sh
siftwise record junit.xml --repo acme/shop --branch feature-x --ci-run-id 1234 \
    --base origin/main --started-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Uploads are idempotent per `(repo, ci_run_id, run_attempt, variant)`: a re-upload returns 200
and stores nothing new. For a matrix, give each leg a `--variant` (e.g. `ubuntu-py3.12`) and
`--defer-rollup`, then run `siftwise rollup --repo acme/shop` once after all legs.

## GitHub Action

Three composite actions in this repo. They install the CLI from the action's own checkout
(siftwise isn't on PyPI yet), and they never block CI: if the server is unreachable, times out
or returns an error, `select` outputs `mode: full` and `record`/`rollup` log a warning.

| Action | When | What |
|---|---|---|
| `actions/select` | before tests | picks tests; outputs `mode`, `command`, `selected`, `total`, `reason`, `junit-dir`; posts one sticky PR comment and a job summary |
| `actions/record` | after tests, `if: always()` | uploads JUnit with the run ID and attempt, the PR head commit (not the merge commit), branch, variant, start time and changed files |
| `actions/rollup` | once after a matrix | updates test stats once for all legs (`POST /repos/{repo}/rollup`) |

**Setup:**
- **Secrets:** add `SIFTWISE_URL` (your server) and `SIFTWISE_TOKEN` (its `SIFTWISE_API_TOKEN`).
- **Permissions:** `contents: read`, `actions: read` (the run's start time) and
  `pull-requests: write` (the comment).
- **Full history:** check out with `fetch-depth: 0`. `select` fails with a clear message on a
  shallow clone, the only way it fails.

The examples use `@main` until a `v0` tag exists.

**Never use this action with `pull_request_target`.** That event runs with secrets on code
from the PR, so a fork could read your siftwise token. Use `pull_request`: on fork PRs, secrets
are empty, so `select` runs the full suite and skips the comment (the job summary says why),
and `record` skips the upload.

### Single job

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: read
  actions: read
  pull-requests: write
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt   # your install step

      - id: siftwise
        uses: PramodBasavarajMenasi/siftwise/actions/select@main
        with:
          server-url: ${{ secrets.SIFTWISE_URL }}
          token: ${{ secrets.SIFTWISE_TOKEN }}

      - name: Tests
        env:
          MODE: ${{ steps.siftwise.outputs.mode }}
          COMMAND: ${{ steps.siftwise.outputs.command }}
          JUNIT_DIR: ${{ steps.siftwise.outputs.junit-dir }}
        run: |
          if [ "$MODE" = selective ]; then
            # Shell-quoted by siftwise; each pytest command writes its own report to JUNIT_DIR.
            if [ -n "$COMMAND" ]; then eval "$COMMAND"; else echo "No tests affected."; fi
          else
            pytest --junitxml="$JUNIT_DIR/full.xml"   # your normal full suite
          fi

      - if: always()
        uses: PramodBasavarajMenasi/siftwise/actions/record@main
        with:
          server-url: ${{ secrets.SIFTWISE_URL }}
          token: ${{ secrets.SIFTWISE_TOKEN }}
```

Pass the command through `env:` as above, never as `${{ }}` inside `run:`, so the shell sees
it once, quoted. `record` uploads `**/junit*.xml` plus everything in the select step's
`junit-dir` (its `junit` input takes other globs). For Go, write JUnit with
`gotestsum --junitfile` or go-junit-report.

### Matrix

Each leg records its own `variant`. Each upload then skips the per-upload stats rollup, which
costs about as much as a full rollup on a large repo, and one `rollup` job runs after every leg:

```yaml
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        python: ["3.11", "3.12"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      # ... set up Python and install, as above ...
      - id: siftwise
        uses: PramodBasavarajMenasi/siftwise/actions/select@main
        with:
          server-url: ${{ secrets.SIFTWISE_URL }}
          token: ${{ secrets.SIFTWISE_TOKEN }}
          comment: ${{ strategy.job-index == 0 }}   # one leg comments, or they race
      # ... the Tests step, as above ...
      - if: always()
        uses: PramodBasavarajMenasi/siftwise/actions/record@main
        with:
          server-url: ${{ secrets.SIFTWISE_URL }}
          token: ${{ secrets.SIFTWISE_TOKEN }}
          variant: ${{ matrix.os }}-py${{ matrix.python }}

  siftwise-rollup:
    needs: test
    if: always()
    runs-on: ubuntu-latest
    steps:
      - uses: PramodBasavarajMenasi/siftwise/actions/rollup@main
        with:
          server-url: ${{ secrets.SIFTWISE_URL }}
          token: ${{ secrets.SIFTWISE_TOKEN }}
```

`.github/workflows/action-e2e.yml` runs all three actions against a real server on every push
and PR of this repo.


## Features

- ✅ JUnit XML ingest: pytest, Jest (jest-junit), Go (go-junit-report), Maven/Gradle
- ✅ Per-test history: failures, durations, flaky score, broken on main (per matrix variant)
- ✅ Backfill from GitHub Actions artifacts, with whole-PR diffs
- ✅ `siftwise select` CLI and `POST /select` API with a reason for every test
- ✅ Go import graph (`go list`) and static Python import graph
- ✅ Safe full-suite fallbacks, `.siftwise.toml` for dependencies no graph can see
- ✅ Self-hosted: one FastAPI service and Postgres, stateless workers
- ✅ GitHub Action (select, record, rollup) with a sticky PR comment; fails open
- 🔜 MCP server, so coding agents can ask which tests to run and why a test failed
- 🔜 Failure triage: new regression vs pre-existing on main vs flaky

## How it compares

From each project's public docs as of October 2026; corrections welcome.

| | Open source | Self-hosted | Languages | How it selects | Agent/MCP support |
|---|---|---|---|---|---|
| **siftwise** | Yes (Apache-2.0) | Yes | Python, Go, JS/TS | file mapping, Go/Python import graphs, failure history | Planned |
| [CloudBees Smart Tests](https://docs.cloudbees.com/docs/cloudbees-smart-tests/latest/features/predictive-test-selection) | No | Not stated | Many (runner integrations incl. Python, Go, Java, JS) | LLM analysis of changes + test history | Not documented |
| [Datadog Test Impact Analysis](https://docs.datadoghq.com/tests/test_impact_analysis/) | No | No (Datadog SaaS) | JS/TS, Java, Python, Go, Swift and more | per-test code coverage | Not documented |
| [Develocity Predictive Test Selection](https://docs.develocity.ai/current/using-develocity/predictive-test-selection/) | No | Yes (Develocity server) | Gradle and Maven builds | predictive model from past builds | Not documented |
| [pytest-testmon](https://pypi.org/project/pytest-testmon/) | Yes (MIT) | Local plugin, no server | Python (pytest) | per-test code coverage | No |

Coverage-based tools see dynamic dependencies that static graphs miss, but they need instrumented
runs. siftwise uses no instrumentation; when its graphs can't see a dependency, the fallbacks and
`.siftwise.toml` cover it.

## Configuration

`.siftwise.toml` at the repo root, read by `siftwise select`:

```toml
always_run = ["tests/smoke/**", "tests.test_health::*"]  # test file paths or test IDs

[[depends]]                               # dependencies no import graph can see
tests = "test/cli/**"                     # test file paths, or <Go package dir>/<TestName>
on = ["**/*.go", "!**/*_test.go"]         # ! excludes
```

Both use the same globs: `**` spans directories, while `*` and `?` stay within one path
segment (`tests/*` matches `tests/test_a.py`, not `tests/unit/test_b.py`). Go test IDs contain
their package's import path, so match them with `**::TestName` or `<import path>::*`.

| Variable | Used by | Default |
|---|---|---|
| `SIFTWISE_API_TOKEN` | server (required: if unset, every endpoint but `/healthz` returns 503) and clients | none |
| `SIFTWISE_DATABASE_URL` | server | `postgresql+psycopg://siftwise:siftwise@localhost:5432/siftwise` |
| `SIFTWISE_STATS_WINDOW_DAYS` | server: per-test stats window (0 = all history) | `90` |
| `SIFTWISE_MAX_UPLOAD_BYTES` | server: request body limit | 50 MB |
| `GITHUB_TOKEN` | `scripts/backfill.py` | none |

## Supported languages

| Language | Selection | Commands |
|---|---|---|
| **Python** (pytest) | `foo.py` → `test_foo.py`; test files importing a changed module (static graph incl. parent packages, conftests, plugin strings); changed test files | `pytest` node IDs, whole test files, `pytest --doctest-modules` for doctests |
| **Go** | changed package and every package whose tests import it (`go list -deps -test`) | `go test ./pkg` (whole) or `-run '^(TestA\|TestB)$'` |
| **JS/TS** (Jest) | `foo.ts` → `foo.test.*`, `foo.spec.*`, `__tests__/foo.*` (needs file paths in the JUnit) | `jest <files>` |
| **Java** (Maven/Gradle) | recorded with full history; selection falls back to the full suite | — |

## Development

```sh
uv sync                                   # install deps, incl. the dev group
docker compose up -d postgres             # Postgres on :5432, plus a siftwise_test database
cp .env.example .env
uv run alembic upgrade head
uv run uvicorn siftwise.api.main:app --reload

uv run ruff check && uv run ruff format --check
uv run mypy src tests scripts
SIFTWISE_TEST_DATABASE_URL=postgresql+psycopg://siftwise:siftwise@localhost:5432/siftwise_test uv run pytest
```

Database tests are skipped when `SIFTWISE_TEST_DATABASE_URL` is unset. After upgrading an existing
database past migration 0005, run `POST /repos/{repo}/rollup` once per repo. Migration 0006
rewrites `test_results` in place (it needs free disk for a second, smaller copy while it runs);
run `VACUUM ANALYZE test_results` afterwards. Replay the selector
on a backfilled repo with `uv run python -m scripts.eval.run_eval --repo owner/name --checkout <clone>`.

## Roadmap

- **Next:** a monorepo eval and a `v0` tag for the GitHub Action.
- **Then:** flaky and pre-existing failure triage, and an MCP server for coding agents.
- **Later:** a recall harness, docs and launch.

## Contributing

Issues and pull requests are welcome. Keep changes small, add a test for every bug fix, and run
ruff, mypy and pytest before opening a PR.

## License

Apache-2.0
