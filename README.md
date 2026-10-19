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

> **Alpha, early development.** The server, CLI, test selection, failure triage, GitHub Action
> and MCP server work. Selection and triage have been evaluated on real repos' history; shadow
> mode, which measures recall on live CI, is new. APIs and the database schema may change.

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

Replays of real CI history with no peeking at the future, with the current code except where
noted. Full details: [docs/eval-results.md](docs/eval-results.md).

| Repo | Failures caught | Test time skipped |
|---|---|---|
| [ipfs/kubo](https://github.com/ipfs/kubo) (Go, ~4,100 tests), measured with the week-2 code | 19 of 19 in failing PRs | 30.7% over 20 main commits replayed as PRs; 0% on recent PRs (all dependency/CI bumps) |
| [RDFLib/rdflib](https://github.com/RDFLib/rdflib) (Python, ~18,000 tests) | 2,049 of 2,050 in 39 failing PRs; 140 of 141 in selective runs. The miss is a flaky remote-service test (see below) | 10.4% on the 10 most recent PRs (was 0.4% before lint-only configs stopped forcing the full suite); 0.6% over 19 main commits |
| [quantumlib/Cirq](https://github.com/quantumlib/Cirq) (Python monorepo, 9 packages, ~40,000 tests) | not measurable: JUnit only from main, which never failed | 37.9% over 132 merged PRs; 79.8% for `cirq-google`-only PRs, 20.2% for `cirq-core` |
| [agent-substrate/substrate](https://github.com/agent-substrate/substrate) (Go, ~9,000 tests, 8 days of history) | 2,043 of 2,044 in 72 failing PRs; 38 of 39 in selective runs, with declared rules for its e2e and functional suites (26 of 38 without them, week-3 code) | 5.1% on PR runs and 13.9% over 40 main commits, with both rules (19.5% / 31.7% without rules, week-3 code) |

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
[triage](#triage) should label it rather than count it as a miss. Every rdflib change links to
it, so listing it in `known_flaky` makes it `unknown` ("re-run to tell"), not flaky; the
Action's `rerun-unknown` with `rerun-verdicts: unknown,new_regression` re-runs it and calls it
flaky when it passes ([Flaky tests](#flaky-tests)).

**Triage** (a verdict per failure, scored against what happened next; details in
[the triage eval](docs/eval-results.md#triage-eval)):

- **The worst error, a regression called flaky:** 0 on rdflib. On substrate, 5 of 406
  labelled regressions were called flaky, all on tests with earlier same-commit flakes; 4 are
  likely label noise from merge diffs.
- **The real prediction tests held up:** `new_regression` 319 of 319 labelled substrate
  failures (and 10 of 11 rdflib fix episodes with a known diff), and medium-confidence
  `pre_existing` 759 of 759, each called after a single failing main run.
- **Flaky precision is mostly by construction:** almost every `flaky` verdict is rule 1, a
  pass of the same commit seen before the failure, which is also the flaky label's evidence.
  Without re-runs, on a repo where every change links to every test, flaky tests look like
  regressions (rdflib's remote-service tests): that's what [`rerun-unknown`](#flaky-tests) is
  for.

**Performance** (rdflib history, before → after the October 2026 work; results identical
before and after, checked on real data; details in
[the eval results](docs/eval-results.md#performance)):

- Triage of a 22-variant CI run attempt (the PR comment's verdicts): 1,032 → 220 ms, and
  180 → 12 queries. The query count no longer grows with the number of variants.
- `/select`: 605 → 237 ms for the selection itself.
- Ingesting a 9,891-result upload: about 2.2 → 1.25 s.

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
`--defer-rollup`, then run `siftwise rollup --repo acme/shop` once after all legs. With
`--base`, `record` also stores what the change links to (its import graphs and declared rules),
which [triage](#triage) uses to tell whether a later failure was related to this run's change.

## Triage

`siftwise triage` reads a run's JUnit reports and its diff, and says for each failing test
whether the change broke it:

```sh
siftwise triage junit.xml --repo acme/shop --base origin/main --variant ubuntu-py3.12 \
    --ci-run-id 1234 --fail-on new_regression
```

```text
new_regression  high   tests.test_cart::test_total
    tests/test_cart.py tests changed src/cart.py
pre_existing    high   tests.test_db::test_query
    failing on main on ubuntu-py3.12 since 4f2a9c1 (3 main runs)
flaky           medium tests.test_net::test_fetch
    flaky 1 time in 90 days (1 unrelated fail-then-pass)
siftwise: 3 failing test(s): 1 new regression, 1 pre existing, 1 flaky, 0 unknown
```

The first rule that applies decides:

| # | Situation | Verdict | Confidence |
|---|---|---|---|
| 1 | The same commit and variant passed before (a re-run or a retry), or passed on one of this run's [re-runs](#flaky-tests) | **flaky** | high |
| 2 | Failing on main for this variant, at the last main run before this one | **pre-existing** (a `known_flaky` listing is only noted) | high for a streak of 2+ main runs or a matching message, else medium; at most medium if the change also touches the test |
| 3 | The diff is unknown | **unknown** | low |
| 3b | Listed in `known_flaky` | linked to the change: **unknown**, re-run to tell; else **flaky** | low; flaky: medium, high with strong flaky history (as rule 7) |
| 4 | No history before this run | **new regression** if the test's own file changed (medium), else **unknown** | |
| 5 | Linked to the change, but known flaky | **unknown**: re-run to tell | low |
| 6 | Linked to the change | **new regression** | high: the test's file or path mapping; medium: an import, a `[[depends]]` rule or a nested build file; low: a root build/CI file. One level lower without a passing main run to compare with |
| 7 | Not linked, with flaky evidence | **flaky** | high with 2+ events or a matching message, else medium |
| 8 | Not linked, no flaky evidence | **unknown** (infrastructure, environment, or a dependency no signal sees) | low |

- **Linked** means the selector's change-based signals: the test's file, path mapping, Go and
  Python imports, `[[depends]]` rules, build files. History signals (co-change, recently failed,
  always-run) don't link a failure to *this* change.
- **No peeking:** only runs before the triaged one count (by run time; with `--ci-run-id`, the
  recorded run sets the cut-off and none of that CI run's uploads count). Triaging an old run
  gives the answer it would have got live.
- **Per variant:** a failure is compared with its own matrix leg; other legs failing on main are
  only noted.
- **Flaky evidence**, in the stats window, comes in two kinds, counted apart: a fail and a pass on
  one commit and variant; or a failure on a run whose change didn't link to the test, then a
  pass on the branch's next run, whose change didn't either. The second kind needs both runs
  recorded with a known diff and change links (`siftwise record --base`, or the Action): runs
  without them (e.g. backfilled ones) never count as evidence.
- **Failure messages** are evidence only. Normalised (numbers, addresses, temp paths and
  timestamps removed), a match raises confidence; a mismatch is noted, never decisive.
- **`known_flaky`** (in [`.siftwise.toml`](#configuration)) comes from the base ref by default
  (`--known-flaky-source base|head`), so a change can't mark its own failures flaky; when the
  checkout's list differs, `siftwise triage` says the change applies after merge. The MCP server
  always uses the base's list.

`--json` prints the full response: per test, the verdict, confidence, reasons and evidence
(links, the main streak, flaky events by kind, history). The API is `POST /triage`, with
`/select`'s change fields plus `commit_sha`, `variant`, `ci_run_id`, `known_flaky` and
`failures: [{test_id, message, file_path}]` (up to 5,000). Exit codes: 0 ok, 1 API error,
2 usage error, 4 a failure got a `--fail-on` verdict (off by default). 500 failures on rdflib
take about 0.25 s, or 1 s with a cold cache (`scripts/bench_triage.py`). The GitHub Action
puts the verdicts in the PR comment ([Failure verdicts](#failure-verdicts)).

## Coding agents (MCP)

`siftwise mcp` is an MCP server (stdio) that gives a coding agent three tools. It keeps no
state: each call computes the change in the agent's checkout, with the same code as
`siftwise select`, and asks the siftwise server. It never records runs.

| Tool | The agent calls it | Returns |
|---|---|---|
| `select_tests` | before running tests after a change | `mode`, the command to run, the selected tests with a reason each |
| `triage_failure` | when tests fail, before trying to fix them | a verdict per failure (from JUnit paths or test IDs), with reasons and evidence |
| `explain` | when a selection or verdict surprises it | one test's recent results, flaky evidence, broken-on-main status, and why it would (not) be selected |

- **Uncommitted work counts.** The diff runs from the merge base of the base branch and HEAD
  to the working tree, untracked files included (`include_uncommitted`, on by default). When
  the tree is dirty, triage sends no commit, so "passed on this commit before" can't apply.
- **Fails open.** If the server is down, errors or no token is set, `select_tests` says to run
  the full suite and `triage_failure` says verdicts are unavailable.
- **Compact output.** Each result stays under 8,000 characters (long lists end with a
  `more_*` count). The API token never appears in a result.

**Install** the CLI with the `mcp` extra, from a siftwise checkout:

```sh
uv tool install ".[mcp]"     # siftwise lands in ~/.local/bin (Windows: %USERPROFILE%\.local\bin)
uv tool update-shell         # once, if that directory isn't on your PATH; then restart the agent
uv tool dir --bin            # prints the directory, for a full path in the configs below
```

`siftwise mcp` without the extra exits 2 with this install command. The agent starts the
server in its working directory: open the agent in the repository (any directory inside the
checkout works). It reads `SIFTWISE_SERVER` and `SIFTWISE_API_TOKEN`, and optionally
`SIFTWISE_REPO` (default: `owner/name` from the `origin` remote) and `SIFTWISE_BASE_REF`
(default: the branch `origin/HEAD` points to, else `main`). Keep the token out of files you
commit: use your user-level config, or environment-variable expansion where the client has it.

**Claude Code:**

```sh
claude mcp add siftwise --env SIFTWISE_SERVER=https://siftwise.example.com \
    --env SIFTWISE_API_TOKEN=your-token -- siftwise mcp
```

or a project `.mcp.json`, which expands `${VAR}` from your environment:

```json
{
  "mcpServers": {
    "siftwise": {
      "command": "siftwise",
      "args": ["mcp"],
      "env": {
        "SIFTWISE_SERVER": "https://siftwise.example.com",
        "SIFTWISE_API_TOKEN": "${SIFTWISE_API_TOKEN}"
      }
    }
  }
}
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.siftwise]
command = "siftwise"
args = ["mcp"]
env = { SIFTWISE_SERVER = "https://siftwise.example.com", SIFTWISE_API_TOKEN = "your-token" }
```

**Cursor** (`~/.cursor/mcp.json` for every project, or `.cursor/mcp.json` in one):

```json
{
  "mcpServers": {
    "siftwise": {
      "command": "siftwise",
      "args": ["mcp"],
      "env": {
        "SIFTWISE_SERVER": "https://siftwise.example.com",
        "SIFTWISE_API_TOKEN": "your-token"
      }
    }
  }
}
```

**If the agent can't find `siftwise`** (it doesn't see your shell's PATH, common on Windows),
give the full path from `uv tool dir --bin` as the command. In JSON, double the backslashes;
in TOML, use a single-quoted string:

```json
"command": "C:\\Users\\you\\.local\\bin\\siftwise.exe"
```

```toml
command = 'C:\Users\you\.local\bin\siftwise.exe'
```

Then tell the agent when to use it, e.g. in `AGENTS.md` or `CLAUDE.md`:

```markdown
Before running tests, call the siftwise `select_tests` tool and run the command it returns.
When tests fail, call `triage_failure` before fixing anything: fix `new_regression` failures,
re-run `flaky` ones, and leave `pre_existing` ones (already failing on main).
```

## GitHub Action

Three composite actions in this repo. They install the CLI from the action's own checkout
(siftwise isn't on PyPI yet), and they never block CI: if the server is unreachable, times out
or returns an error, `select` outputs `mode: full` and `record`/`rollup` log a warning. The
only exception is [`fail-on`](#fail-on), which you opt into.

| Action | When | What |
|---|---|---|
| `actions/select` | before tests | picks tests; outputs `mode`, `command`, `selected`, `total`, `reason`, `junit-dir` (and the [shadow](#shadow-mode) outputs); posts one sticky PR comment and a job summary |
| `actions/record` | after tests, `if: always()` | uploads JUnit with the run ID and attempt, the PR head commit (not the merge commit), branch, variant, start time, changed files and [`known_flaky`](#flaky-tests); optionally [re-runs unknown failures](#flaky-tests); in a single job, then [triages the failures](#failure-verdicts) and reports a [shadow run](#shadow-mode) |
| `actions/rollup` | once after a matrix | [triages the failures](#failure-verdicts) of every leg and reports a [shadow run](#shadow-mode), then updates test stats once for all legs (`POST /repos/{repo}/rollup`) |

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
`record` skips the upload, and there are no failure verdicts.

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

The rollup job needs no checkout: the server already has what each leg recorded.

### Failure verdicts

After the run's last upload, siftwise triages its failing tests
([verdicts](#triage)): in `actions/rollup` after a matrix, in `actions/record` in a single job
(its `triage` input: `auto`, `true` or `false`). The server works from what the uploads stored,
including the change links `record` computed, so the verdicts match `siftwise triage`.

The sticky PR comment gets a **Failures** section under the selection:

- a summary line, e.g. "2 new regressions, 1 unknown, 1 flaky, 1 pre-existing", counting each
  test once, under its worst verdict over the legs;
- a table of test, verdict, confidence, the legs and one reason: new regressions first, then
  unknown; flaky and pre-existing collapsed below. `all` means the test got that verdict on
  every leg recorded; a test that failed on some legs lists only those;
- at most 20 rows per table, then "+N more" with a link to the job summary, which has the full
  list (up to 500).

The comment is left alone if the PR got new commits since the run started (that run owns it),
and a new push's selection drops the old commit's failures. Fork PRs get the verdicts nowhere:
they have no siftwise token (the job summary says so). Outputs: `verdicts-available`, `counts`
(JSON, per verdict) and `report` (the summary's Markdown, as a file).

### fail-on

`fail-on` (on `rollup`, or `record` in a single job) fails the step when a failure gets one of
the listed verdicts, on any leg:

```yaml
      - uses: PramodBasavarajMenasi/siftwise/actions/rollup@main
        with:
          server-url: ${{ secrets.SIFTWISE_URL }}
          token: ${{ secrets.SIFTWISE_TOKEN }}
          fail-on: new_regression,unknown
```

- **It can only add a failure.** A leg whose tests failed is already red. To let siftwise
  decide, give the test steps `continue-on-error: true` and make the rollup job the required
  check.
- **`new_regression` alone lets `unknown` through,** and `unknown` is common (a new test, an
  unknown diff, an unrelated failure with no flaky history). Gate on `new_regression,unknown`.
- **With `fail-on` set, unavailable verdicts fail the step too** (fail closed):
  `siftwise verdicts unavailable (<reason>); failing because fail-on is set`. That covers a
  server that's down or erroring, a timeout, and nothing recorded for the attempt (a 404), so
  a real failure can't pass while siftwise is unreachable.
- **Except without a siftwise token:** fork PRs get no secrets, so the step warns
  `fail-on skipped: no siftwise token (fork PR?); this run is not gated` and passes. Keep
  another required check on fork PRs (the test legs themselves, without `continue-on-error`
  there). A missing `SIFTWISE_TOKEN` secret on your own repo looks the same: watch for that
  warning.

- **A PR can't excuse its own failures with `known_flaky`:** `record` applies the PR base
  commit's list (see [Flaky tests](#flaky-tests) and [Security and limits](#security-and-limits)).

Without `fail-on` (the default), siftwise never fails anything.

### Flaky tests

Two ways to keep known flakes from reading as regressions, both in `record`:

```yaml
      - if: always()
        uses: PramodBasavarajMenasi/siftwise/actions/record@main
        with:
          server-url: ${{ secrets.SIFTWISE_URL }}
          token: ${{ secrets.SIFTWISE_TOKEN }}
          rerun-unknown: 2         # re-run unknown failures up to twice
          rerun-prefix: uv run     # how pytest runs in your job (e.g. poetry run)
```

- **`known_flaky`** in [`.siftwise.toml`](#configuration): a failing listed test not linked to
  the change is flaky (medium); linked, it's unknown ("re-run to tell"). For a pull request,
  `record` applies the **base commit's** list (`known-flaky-source: base`, the default), so a PR
  can't mark its own failures flaky and get past `fail-on`; when the PR changes the list, the
  comment says "known_flaky changes in this PR apply after merge". `known-flaky-source: head`
  applies the PR's own list instead. A push always applies its own.
- **`rerun-unknown: N`** (0 to 3, default 0): after uploading, `record` triages its own leg,
  re-runs failures whose verdict is in `rerun-verdicts` (`unknown`, or
  `unknown,new_regression`) with pytest node IDs, and records each re-run. A test that passes
  on one is **flaky**, high ("passed on re-run 1 of this commit and variant"); one that fails
  again keeps its verdict, with a note. Tests still failing go round again, up to N times.
- **Limits:** at most `rerun-max-tests` tests (20) within `rerun-timeout-minutes` (10) for all
  rounds; a round cut off is discarded. pytest only for now; tests whose file isn't in the
  checkout, and other runners, aren't re-run. `rerun-prefix` is split like a shell would, and
  the command runs without a shell. Re-runs write their JUnit outside the checkout and leave no
  pytest cache.
- **Fail open:** no token, no verdicts, pytest missing, a timeout or a failed upload is a
  warning; the verdicts stay what they were. Re-runs never turn a red job green; the PR comment
  and `fail-on` use the verdicts after them. Outputs: `rerun-tests`, `rerun-passed`.
- **Where:** in a matrix each leg re-runs its own failures (it has the checkout and the
  environment), and `rollup` triages after every leg, re-runs included. In a single job,
  `record` re-runs, then triages. Selection is unchanged: known-flaky tests still run when
  selected.

### Shadow mode

How often would siftwise's selection have missed a real failure? Shadow mode measures it on
your own CI. On a sampled run the full suite runs while siftwise stores what it would have
selected; after the run is recorded, the server checks every failure against that selection:

```yaml
      - id: siftwise
        uses: PramodBasavarajMenasi/siftwise/actions/select@main
        with:
          server-url: ${{ secrets.SIFTWISE_URL }}
          token: ${{ secrets.SIFTWISE_TOKEN }}
          shadow: 10%          # or always; off by default
```

- **`shadow`:** `off` (default), `always`, or a percentage of CI runs. Sampling hashes the run
  ID, so every matrix leg and every re-run attempt of a run agrees. An invalid value is a
  warning and turns shadow mode off.
- **A shadow run always runs the full suite.** `select` outputs `mode: full` and an empty
  `command` before it calls the server, so your workflow's full-suite branch runs whatever
  happens. Shadow mode never fails or shortens a run. Shadow runs record their results to
  history as usual.
- **Matrix legs** can set `variant` on `select` too (the same value as `record`'s), so each leg's
  selection is stored for that leg. Without it one selection covers every leg.
- **Where verdicts are computed** (`rollup` after a matrix, `record` in a single job), the
  failures section of the PR comment and the job summary get a line like "**Shadow run:**
  would have run 120 of 692 tests (70% time saved) and caught 3 of 4 failures (1 flaky not
  counted)", then the missed tests with their cause (at most 20). The time saved is measured
  on the run's own durations. Failures that triage calls flaky (including `known_flaky` and
  re-run passes) are counted separately, never as misses; pre-existing and unknown ones count.
- **Outputs:** `select` has `shadow`, `shadow-mode` (what siftwise would have done),
  `shadow-selected` and `shadow-total`; `rollup` and `record` have `shadow`, `shadow-mode`,
  `shadow-caught` and `shadow-missed`.
- **Not counted, with a warning:** a selection the server couldn't store (the warning gives
  the reason: `incomplete_selection`, `commit_conflict` or `storage_error`), siftwise
  unreachable at select time, or a comparison that failed. No siftwise token (fork PRs): no
  shadow run.

Example from a real PR: on a one-line change, siftwise would have run 197 of 900 tests (56% time
saved), caught the pinned failing test, and reported the one it missed with its cause: new test.

![siftwise shadow-run PR comment](docs/images/shadow-comment-2.png)

Recall over time comes from `GET /repos/{repo}/recall?days=30`, or:

```sh
siftwise report --repo acme/shop --days 30
# Recall 98.7% (148 of 150) over 40 shadow runs; selective runs only: 96.0% (48 of 50) over 25
```

The first figure counts every shadow run, including those where siftwise would have run the
full suite anyway (a fallback catches everything), so the report always shows it with the
selective-only figure. Below that come the misses by verdict and by cause, the measured time
saved, the runs not counted (by reason: no upload, commit mismatch, compare error, cancelled)
and the misses, newest first. `--json` prints the API response. `--by week` prints recall,
caught, missed and time saved per week instead (Monday to Monday UTC, empty weeks included),
again with the selective-only recall next to the all-runs one:

```sh
siftwise report --repo acme/shop --days 30 --by week
```

```text
Recall 98.7% (148 of 150) over 40 shadow runs; selective runs only: 96.0% (48 of 50) over 25

Week of     Runs  Recall (all)        Selective  Recall (selective)  Caught  Missed  Time saved
2026-09-28    11  100.0% (41 of 41)           7  100.0% (13 of 13)       41       0         38%
2026-10-05    12  98.0% (49 of 50)            8  95.0% (19 of 20)        49       1         41%
2026-10-12     0  n/a (no failures)           0  n/a (no failures)        0       0           -
2026-10-19    17  98.3% (58 of 59)           10  94.1% (16 of 17)        58       1         44%
```

Each miss gets a cause, from evidence the server already has (`unknown` when it doesn't say):

| Cause | Evidence |
|---|---|
| `new_test` | no result for the test before the run |
| `flaky_history` | flaky before, but not called flaky this time |
| `config_gap` | a non-code file (data, fixture, golden file) changed in the test's own directory, which no import graph follows |
| `no_link` | the change is known and nothing in it links to the test (the report lists what changed) |
| `unknown` | the run's change links weren't recorded, or the test was linked but not selected (the report warns: possibly a selector bug) |

A miss that was linked to the change but not selected (`linked_not_selected` in the API)
makes the report print a warning: the selector may not have known the test when it selected,
which can be a selector bug.

For config gaps only, the report suggests a `.siftwise.toml` rule. It is shown for you to
review; siftwise never applies it:

```toml
[[depends]]
tests = "tests/test_db.py"
on = ["tests/data/**"]
```

`.github/workflows/action-e2e.yml` runs all three actions against a real server on every push
and PR of this repo, including a deliberately failing test's verdict, `fail-on`, a shadow
run, `known_flaky` from the checkout and from the PR base, and `rerun-unknown` on a test that
fails once.


## Features

- ✅ JUnit XML ingest: pytest, Jest (jest-junit), Go (go-junit-report), Maven/Gradle
- ✅ Per-test history: failures, durations, flaky score, broken on main (per matrix variant)
- ✅ Backfill from GitHub Actions artifacts, with whole-PR diffs
- ✅ `siftwise select` CLI and `POST /select` API with a reason for every test
- ✅ Go import graph (`go list`) and static Python import graph
- ✅ Safe full-suite fallbacks, `.siftwise.toml` for dependencies no graph can see
- ✅ Self-hosted: one FastAPI service and Postgres, stateless workers
- ✅ GitHub Action (select, record, rollup) with a sticky PR comment; fails open
- ✅ Failure triage (`siftwise triage`, `POST /triage`): new regression vs pre-existing on main
  vs flaky, with evidence; evaluated on rdflib and substrate history
- ✅ Failure verdicts in the Action's PR comment and job summary, and an opt-in `fail-on` gate
- ✅ MCP server for coding agents (`siftwise mcp`): `select_tests`, `triage_failure`, `explain`
- ✅ Shadow mode: sampled CI runs run the full suite and report what the selection would have
  caught or missed, with a cause for every miss; `siftwise report` (`GET /repos/{repo}/recall`)
  for recall, by week too, and `.siftwise.toml` rules to consider for config gaps
- ✅ Flaky handling: `known_flaky` in `.siftwise.toml` (a PR's base list, so a PR can't excuse
  its own failures) and `rerun-unknown` in the Action (pytest re-runs; a pass makes it flaky)

## How it compares

From each project's public docs as of October 2026; corrections welcome.

| | Open source | Self-hosted | Languages | How it selects | Agent/MCP support |
|---|---|---|---|---|---|
| **siftwise** | Yes (Apache-2.0) | Yes | Python, Go, JS/TS | file mapping, Go/Python import graphs, failure history | MCP server (select, triage, explain) |
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
generated = ["build/reports/**"]          # more files your test runs write (see below)
known_flaky = ["tests/test_net.py"]       # triage: unlinked failures are flaky (see below)

[[depends]]                               # dependencies no import graph can see
tests = "test/cli/**"                     # test file paths, or <Go package dir>/<TestName>
on = ["**/*.go", "!**/*_test.go"]         # ! excludes
```

All use the same globs: `**` spans directories, while `*` and `?` stay within one path
segment (`tests/*` matches `tests/test_a.py`, not `tests/unit/test_b.py`). Go test IDs contain
their package's import path, so match them with `**::TestName` or `<import path>::*`.

Files that test runs write never count as changed code, even untracked or committed by
mistake: `junit*.xml`, `coverage.xml`, `.coverage`, `.coverage.*`, `htmlcov/`, `*.log`,
`.pytest_cache/` and `__pycache__/`, at any depth. Like docs, they never select a test or force
the full suite. `generated` adds to that list.

`known_flaky` is for [triage](#triage) only (selection still runs listed tests): a listed
test's failure is flaky unless the change links to it. CI applies a pull request's **base**
list ([Flaky tests](#flaky-tests)).

| Variable | Used by | Default |
|---|---|---|
| `SIFTWISE_API_TOKEN` | server (required: if unset, every endpoint but `/healthz` returns 503) and clients | none |
| `SIFTWISE_DATABASE_URL` | server | `postgresql+psycopg://siftwise:siftwise@localhost:5432/siftwise` |
| `SIFTWISE_STATS_WINDOW_DAYS` | server: per-test stats window (0 = all history) | `90` |
| `SIFTWISE_MAX_UPLOAD_BYTES` | server: request body limit | 50 MB |
| `GITHUB_TOKEN` | `scripts/backfill.py` | none |

## Security and limits

- **One token per server, with full access.** `SIFTWISE_API_TOKEN` is a single shared secret.
  Every client (CI, the CLI, the MCP server) uses the same token, and it can read and write the
  history of **every repo** on the server. There are no per-repo, read-only or per-user
  tokens. Run a separate server (or database) for repos whose owners shouldn't see each
  other's test history, and rotate the token by changing it on the server and in each secret.
- **Keep the token out of fork PRs.** Use `pull_request`, never `pull_request_target` (see
  [GitHub Action](#github-action)): a fork's code must never run with the token.
- **What the server stores:** test IDs, outcomes, durations, failure messages (truncated to 4
  KB), changed file paths, branch names and commit SHAs. Failure messages can contain whatever
  your tests print, so treat the database as you treat CI logs.
- **Public PR comments** show test IDs, verdicts and siftwise's own reasons, never raw failure
  messages.
- **Protect your workflows.** The base-branch known_flaky rule assumes the workflow itself is
  protected. A PR that edits .github/workflows/ can change any gate, so protect workflows with
  CODEOWNERS or a ruleset.
- **Stats cover the window only.** Per-test stats use the last `SIFTWISE_STATS_WINDOW_DAYS`
  (90) days before the repo's newest run. A test not seen within the window loses its stats row
  on the next rollup; its raw results stay, and `GET /tests/{id}/history` shows them with
  `stats: null`.

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
run `VACUUM ANALYZE test_results` afterwards. Migration 0007 builds a small index on failed
results (seconds; it blocks writes to `test_results` meanwhile). Migrations 0008 and 0009 only add the
shadow-mode tables and columns. Migration 0010 adds `runs.known_flaky` and `runs.rerun` and
rebuilds the small `runs` unique index. Replay the selector
on a backfilled repo with `uv run python -m scripts.eval.run_eval --repo owner/name --checkout <clone>`.

## Roadmap

- **Done:** recording and history, test selection with import graphs and safe fallbacks, the
  GitHub Action, failure triage with verdicts in the PR comment and `fail-on`, the MCP server,
  the triage eval, [shadow mode](#shadow-mode) with `siftwise report` (by week too), miss
  causes, and [flaky handling](#flaky-tests) (`known_flaky`, `rerun-unknown`).
- **Next:** performance (batched triage and rollup queries), packaging (a light CLI, a Docker
  image) and dogfooding: shadow mode on siftwise and another repo, for real recall data.
- **Then:** a `v0.1.0` release on PyPI, docs and launch.

## Contributing

Issues and pull requests are welcome. Keep changes small, add a test for every bug fix, and run
ruff, mypy and pytest before opening a PR.

## License

Apache-2.0
