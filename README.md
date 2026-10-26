<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/PramodBasavarajMenasi/siftwise/main/assets/logo-dark.svg">
    <img alt="siftwise" src="https://raw.githubusercontent.com/PramodBasavarajMenasi/siftwise/main/assets/logo-light.svg" width="370">
  </picture>
</p>

<p align="center">
  <strong>Run only the tests your change needs, safely.</strong><br>
  Self-hosted test selection and failure triage for agent-driven CI.
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="https://github.com/PramodBasavarajMenasi/siftwise/blob/main/ARCHITECTURE.md">Architecture</a> ·
  <a href="https://github.com/PramodBasavarajMenasi/siftwise/blob/main/docs/eval-results.md">Eval results</a> ·
  <a href="https://github.com/PramodBasavarajMenasi/siftwise/blob/main/docs/eval-methodology.md">Eval methodology</a> ·
  <a href="https://github.com/PramodBasavarajMenasi/siftwise/blob/main/CHANGELOG.md">Changelog</a> ·
  <a href="https://github.com/PramodBasavarajMenasi/siftwise/blob/main/CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <a href="https://github.com/PramodBasavarajMenasi/siftwise/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/PramodBasavarajMenasi/siftwise/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="Status: alpha" src="https://img.shields.io/badge/status-alpha-orange">
</p>

<p align="center">
  <b>2,049 / 2,050</b> real failures caught replaying rdflib's CI (the miss: a flaky test) · <b><code>/select</code> in ~0.4 s</b> on 18,000 tests · <b>self-hosted</b>, Apache-2.0
</p>

siftwise is a self-hosted service for teams whose CI is flooded with pull requests, many of
them now written by coding agents. It records every CI test result, picks the tests a change
can affect (running the full suite whenever it isn't sure), and tells people and agents
whether each failure is a new regression, already broken on main, or flaky. What sets it
apart: it runs on your own Postgres with no test instrumentation; it **fails open**, so a
siftwise outage never blocks CI; it ships an **MCP server**, so coding agents ask which tests
to run and why a test failed; and its **shadow mode** measures, on your own CI, how often a
selection would have missed a real failure before you rely on it. Why now: Anthropic reports
a 25x increase in CI jobs over six months as agents sped up writing code
([Agentic coding is straining CI](https://claude.com/blog/agentic-coding-is-straining-ci-heres-how-we-scaled-test-impact-analysis-at-anthropic)),
and Linear found that as development accelerates, CI becomes a bottleneck, because every PR
still has to pass through it
([AI coding has made CI a bottleneck](https://linear.app/now/ci-bottleneck-reworked)).

> **Status: alpha** (`0.1.0`). Evaluated on four real repos' CI history. APIs and the
> database schema may change before 1.0.

<p align="center">
  <img alt="siftwise select on an rdflib commit: 325 test files import the changed modules, 17921 of 18020 known tests selected, and a pytest command" src="https://raw.githubusercontent.com/PramodBasavarajMenasi/siftwise/main/assets/demo.svg" width="900">
</p>

The sticky PR comment on a [shadow run](#shadow-mode): what siftwise would have run, each
failure with its verdict, and the failure the selection would have missed, with its cause.

<p align="center">
  <img alt="siftwise PR comment: would have run 197 of 900 tests, two failures with verdicts, and one missed test with its cause, new test" src="https://raw.githubusercontent.com/PramodBasavarajMenasi/siftwise/main/docs/images/shadow-comment-2.png" width="700">
</p>

## How it works

```mermaid
flowchart LR
    A["CI run, PR or<br/>agent branch"] --> B["siftwise select<br/>(git diff + import graph)"]
    B --> C[("siftwise server<br/>Postgres per-test history")]
    C --> D["test command<br/>+ reasons"]
    D --> E["run tests"]
    E --> F["record JUnit"]
    F --> C
    F --> G["rollup: triage verdicts<br/>+ shadow compare"]
    G --> H["PR comment"]
```

Full details: [ARCHITECTURE.md](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/ARCHITECTURE.md).

- **Record.** CI uploads JUnit XML with the commit, branch, changed files and what the change
  links to. The server keeps every result and rolls up per-test history.
- **Select.** The CLI diffs your branch and builds Go or Python import graphs locally; the
  server returns the tests the change can affect, each with a reason, or the full suite when
  the diff, a build file or the history leaves any doubt.
- **Triage.** Each failure gets a verdict (new regression, pre-existing on main, flaky,
  unknown) with a confidence and its evidence, using only history from before the run.
- **Measure.** Shadow runs run the full suite and report what the selection would have
  caught or missed, and why.

## Quickstart

From zero to a first selection in about 5 minutes, on a small example project. You need
Docker, git and [uv](https://docs.astral.sh/uv/). The commands are bash: on Windows, use Git
Bash or WSL. CI runs the blocks below on every change
([quickstart.yml](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/.github/workflows/quickstart.yml)), with the server image and the CLI built
from the checkout.

```sh
git clone https://github.com/PramodBasavarajMenasi/siftwise.git
cd siftwise
```

**1. Start the server** with Postgres, from the published image
(`ghcr.io/pramodbasavarajmenasi/siftwise:0.1.0`). The API token is a shared secret you choose;
it goes in `.env`, which `docker compose` reads:

```sh quickstart
[ -f .env ] || echo "SIFTWISE_API_TOKEN=$(openssl rand -hex 32)" > .env
docker compose up -d --wait             # pulls the image, waits until both services are healthy
curl -fsS http://localhost:8000/healthz # {"status":"ok"}
```

To run the server from your checkout instead (when you change it), add `--build`.

**2. Install the CLI** (the light install, without the server's dependencies) and give it the
token:

```sh quickstart
uv tool install siftwise                # or: pip install siftwise
export SIFTWISE_API_TOKEN=$(sed -n 's/^SIFTWISE_API_TOKEN=//p' .env)
```

If `siftwise` isn't found afterwards, run `uv tool update-shell` once and open a new shell.

**3. Record a first run.** Copy [the example project](https://github.com/PramodBasavarajMenasi/siftwise/tree/main/examples/quickstart) into a git repo of
its own, run its tests with a JUnit report, and upload the report:

```sh quickstart
demo="$(mktemp -d)/shop" && cp -r examples/quickstart "$demo" && cd "$demo"
git init -q && git add -A && git commit -qm "A shop"
uv run pytest -q --junitxml=junit.xml
siftwise record junit.xml --repo demo/shop --branch main --main
```

**4. Change a file and ask what to run.** `shop/cart.py` changes; only the cart's tests import
it:

```sh quickstart
echo "# reviewed the bulk discount" >> shop/cart.py && git commit -qam "Touch the cart"
siftwise select --repo demo/shop --base HEAD~1 > selected.sh
cat selected.sh                         # pytest tests/test_cart.py
uv run sh selected.sh
```

The command goes to stdout (so `eval "$(siftwise select ...)"` works, or a file as here) and a
summary to stderr:

```text
siftwise: python imports: 1 test file(s) import changed modules
siftwise: selective (2 of 4 known tests): 2 of 4 known tests selected
```

A change to `shop/prices.py` selects both test files instead (the cart imports prices).
`--json` prints the full response with a reason per test. Exit codes: 0 ok, 1 API error, 2
usage error, 3 full suite needed but no command known. `docker compose down` stops the server
(`down -v` also deletes its data).

**On your own project:** record each CI run's JUnit report (the [GitHub Action](#github-action)
does it for you, or `siftwise record` below), then call `siftwise select` (or the Action's
select step) on a change. In CI, check out with `fetch-depth: 0` so the `base...head` diff works;
if it fails, the full suite runs.

## Install

| Install | What you get | Size |
|---|---|---|
| `pip install siftwise` | the CLI: `select`, `record`, `triage`, `report`, `rollup`, and the GitHub Action's steps | 9 MB |
| `pip install 'siftwise[server]'` | plus the API server: `siftwise server start` and `migrate` (FastAPI, SQLAlchemy, Postgres driver) | 50 MB |
| `pip install 'siftwise[mcp]'` | plus `siftwise mcp`, for [coding agents](#coding-agents-mcp) | 49 MB |

For a command-line tool, `uv tool install siftwise` (or `pipx install siftwise`) gives it its
own environment; add the extras the same way (`uv tool install 'siftwise[mcp]'`). From a
checkout: `uv tool install .` (or `".[server]"`, `".[mcp]"`). A command whose extra is missing
exits 2 and prints the install command. `siftwise --version` prints the version.

### Run the server

With Docker, from the published image:

```sh
docker run -d -p 8000:8000 -e SIFTWISE_API_TOKEN=... \
    -e SIFTWISE_DATABASE_URL=postgresql+psycopg://user:password@db-host:5432/siftwise \
    ghcr.io/pramodbasavarajmenasi/siftwise:0.1.0
```

To build the image from a checkout instead: `docker build -t siftwise .`, then run `siftwise`
in place of the image name.

- **The image** (about 80 MB to pull, 350 MB unpacked) runs `siftwise server start` as a
  non-root user: it applies the database migrations, then serves on port 8000. Migrations take
  a Postgres lock, so several replicas can start at once. A Docker health check calls
  `/healthz`.
- **Without Docker:** `pip install 'siftwise[server]'`, then `siftwise server start --host
  0.0.0.0` (`siftwise server migrate` applies the migrations alone).
- **Reaching it from CI:** the quickstart's `docker-compose.yml` publishes the API on
  `127.0.0.1` only (port `SIFTWISE_PORT`, default 8000) and keeps Postgres inside Docker. Put
  the server behind a reverse proxy with TLS: the token travels in a request header.
- **Upgrading:** change the image tag to the new version (the quickstart's
  `docker-compose.yml` names it; a newer checkout already has it), then `docker compose up -d`:
  the new image migrates the database when it starts. Never go back to an older image: once
  the database has a migration that image doesn't know, its `siftwise server start` fails with
  `Can't locate revision ...` and the container restarts in a loop. If you build from a
  checkout (`--build`), rebuild after every pull for the same reason. A few migrations need a
  step after them: see [the migration notes](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/CONTRIBUTING.md#upgrade-notes).

### Seed history from past CI runs

Backfill a repo's past GitHub Actions runs (here rdflib, which uploads one JUnit artifact per
matrix leg) instead of waiting for new ones. `GITHUB_TOKEN` needs `actions:read` and
`contents:read`:

```sh
export GITHUB_TOKEN=...
uv run python scripts/backfill.py --repo RDFLib/rdflib --workflow validate.yaml \
    --artifact-pattern '*-pytest-junit-xml' --max-runs 20 --batch
```

### Record results from other CI systems

On GitHub, use the [GitHub Action](#github-action); on GitLab, the [CI template](#gitlab-ci).
Elsewhere, upload JUnit XML after each test run:

```sh
siftwise record junit.xml --repo acme/shop --branch feature-x --ci-run-id 1234 \
    --base origin/main --started-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Uploads are idempotent per `(repo, ci_run_id, run_attempt, variant)`: a re-upload stores
nothing new. For a matrix, give each leg a `--variant` (e.g. `ubuntu-py3.12`) and
`--defer-rollup`, then run `siftwise rollup --repo acme/shop` once after all legs. With
`--base`, `record` also stores what the change links to, which triage needs to tell whether a
later failure was related to this run's change.

### Triage from the CLI

`siftwise triage` reads a run's JUnit reports and its diff, and gives each failing test a
verdict:

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

The rules, in order, are in [the architecture doc](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/ARCHITECTURE.md#triage-verdicts).
Only runs before the triaged one count, so triaging an old run gives the answer it would have
got live. `--json` prints the verdicts with their evidence. Exit codes: 0 ok, 1 API error, 2
usage error, 4 a failure got a `--fail-on` verdict (off by default). 500 failures on rdflib
take about 0.25 s.

## GitHub Action

Three composite actions in this repo. They install the light CLI from the action's own
checkout and never block CI: if the server is unreachable, times out or returns an error,
`select` outputs `mode: full` and `record`/`rollup` log a warning. The only exception is
[`fail-on`](#fail-on), which you opt into.

| Action | When | What |
|---|---|---|
| `actions/select` | before tests | picks tests; outputs `mode`, `command`, `selected`, `total`, `reason`, `junit-dir` (and the [shadow](#shadow-mode) outputs); posts one sticky PR comment and a job summary |
| `actions/record` | after tests, `if: always()` | uploads JUnit with the run ID and attempt, the PR head commit (not the merge commit), branch, variant, start time, changed files and [`known_flaky`](#flaky-tests); optionally [re-runs unknown failures](#flaky-tests); in a single job, then [triages the failures](#failure-verdicts) and reports a [shadow run](#shadow-mode) |
| `actions/rollup` | once after a matrix | [triages the failures](#failure-verdicts) of every leg and reports a [shadow run](#shadow-mode), then updates test stats once for all legs |

**Setup:**
- **Secrets:** `SIFTWISE_URL` (your server) and `SIFTWISE_TOKEN` (its `SIFTWISE_API_TOKEN`).
- **Permissions:** `contents: read`, `actions: read` (the run's start time) and
  `pull-requests: write` (the comment).
- **Full history:** check out with `fetch-depth: 0`. `select` fails with a clear message on a
  shallow clone, the only way it fails.
- **Version:** the examples use `@main`; pin a release tag (`@v0.1.0`) or a commit SHA for
  reproducible CI. The actions install the CLI from their own checkout, so the CLI's version
  always matches the tag.

**Never use these actions with `pull_request_target`.** That event runs with secrets on code
from the PR, so a fork could read your siftwise token. Use `pull_request`: on fork PRs, secrets
are empty, so `select` runs the full suite and skips the comment, `record` skips the upload,
and there are no failure verdicts.

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
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v7
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

Each leg records its own `variant` and skips the per-upload stats rollup; one `rollup` job runs
after every leg:

```yaml
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        python: ["3.11", "3.12"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v7
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

After the run's last upload (`rollup` after a matrix, `record` in a single job: its `triage`
input, `auto`, `true` or `false`), siftwise triages the failing tests from what the uploads
stored, including the change links `record` computed. The sticky PR comment gets a
**Failures** section under the selection:

- a summary line, e.g. "2 new regressions, 1 unknown, 1 flaky, 1 pre-existing", counting each
  test once, under its worst verdict over the legs;
- a table of test, verdict, confidence, legs and one reason: new regressions first, then
  unknown; flaky and pre-existing collapsed below. `all` means every recorded leg got that
  verdict;
- at most 20 rows per table, then "+N more"; the job summary has the full list (up to 500).

The comment is left alone if the PR got new commits since the run started, and a new push's
selection drops the old commit's failures. Raw failure messages are never shown (the comment
is public). Outputs: `verdicts-available`, `counts` (JSON, per verdict) and `report` (the
summary's Markdown, as a file). How each verdict is decided:
[triage verdicts](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/ARCHITECTURE.md#triage-verdicts).

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
- **Gate on `new_regression,unknown`.** `new_regression` alone lets `unknown` through, and
  `unknown` is common (a new test, an unknown diff, an unrelated failure with no flaky history).
- **With `fail-on` set, unavailable verdicts fail the step too** (fail closed): a server
  that's down or erroring, a timeout, or nothing recorded for the attempt. A real failure
  can't pass while siftwise is unreachable.
- **Except without a siftwise token:** fork PRs get no secrets, so the step warns
  `fail-on skipped: no siftwise token (fork PR?); this run is not gated` and passes. Keep
  another required check on fork PRs. A missing `SIFTWISE_TOKEN` secret on your own repo looks
  the same: watch for that warning.
- **A PR can't excuse its own failures with `known_flaky`:** `record` applies the PR base
  commit's list ([Flaky tests](#flaky-tests)).

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
          rerun-prefix: uv run     # how pytest or Jest runs in your job (poetry run, npx)
```

- **`known_flaky`** in [`.siftwise.toml`](#configuration): a failing listed test not linked to
  the change is flaky (medium); linked, it's unknown ("re-run to tell"). For a pull request,
  `record` applies the **base commit's** list (`known-flaky-source: base`, the default), so a PR
  can't mark its own failures flaky; when the PR changes the list, the comment says
  "known_flaky changes in this PR apply after merge". `known-flaky-source: head` applies the
  PR's own list instead. A push always applies its own.
- **`rerun-unknown: N`** (0 to 3, default 0): after uploading, `record` triages its own leg,
  re-runs failures whose verdict is in `rerun-verdicts` (`unknown`, or
  `unknown,new_regression`) with the test's own runner, and records each re-run (all of a
  round's reports in one upload). A test that passes on one is **flaky**, high; one that fails
  again keeps its verdict, with a note.
- **Runners**, chosen per failing test from its ID and file and the checkout:

  | Runner | When | Re-run command | Report |
  |---|---|---|---|
  | pytest | a test ID that maps to a file in the checkout | `[prefix] pytest -p no:cacheprovider <node IDs>` | `--junitxml` in the runner's temp dir |
  | Go | an import path in the root `go.mod`'s module | per package: `gotestsum ... -- -count=1 -run '^(TestA\|TestB)$' <package>`, or `go test -json` with the same flags when gotestsum isn't on PATH | gotestsum's JUnit, or `go test -json` converted by siftwise |
  | Jest | a JS/TS test file (the JUnit must name it: jest-junit's `addFileAttribute`) under a `package.json` or `jest.config.*` with Jest | per file, from that package: `[prefix] jest --ci --runTestsByPath <file> -t '^(<full name>\|...)$'` | jest-junit (it must be installed, as for the CI run) |
  | Maven | a Java class and method, `pom.xml` at the root | `mvn -B -Dtest=Class#m1+m2,Other#m3 test` (`./mvnw` when present) | the `target/surefire-reports` files the re-run wrote, copied out (then put back as they were) |
  | Gradle | a Java class and method, `build.gradle[.kts]` or `settings.gradle[.kts]` at the root | `gradle test --continue --tests Class.method ...` (`./gradlew` when present) | the `build/test-results` files the re-run wrote, copied out (then put back as they were) |

  Go subtests re-run through their top-level test; Java parameter lists and `[n]` indexes are
  dropped (all of a method's cases run). Names that reach a regex are escaped; Java class and
  method names must be plain identifiers, else the test isn't re-run. A test whose runner can't
  be told isn't re-run, with a warning.
- **`rerun-prefix`** goes in front of the pytest and Jest commands, split like a shell would,
  e.g. `uv run`, `poetry run`, `npx`, `yarn`. Go, Maven and Gradle never get it (a prefix for
  one language would break another in a mixed repo): siftwise uses gotestsum if it's on PATH
  (`go install gotest.tools/gotestsum@latest` before `record`), else `go test -json`, and the
  repo's `./mvnw` or `./gradlew` if present, else `mvn` or `gradle`.
- **Limits:** at most `rerun-max-tests` tests (20) within `rerun-timeout-minutes` (10) for all
  rounds and runners. Tests whose file isn't in the checkout aren't re-run. Every command runs
  without a shell. A re-run's report must name the test as the CI run did: with custom
  jest-junit name templates (or settings only the test step had), the re-run may not match,
  and the test keeps its verdict.
- **Maven and Gradle reports:** both write a re-run's reports where the CI run's are (Surefire
  can't be pointed elsewhere from the command line, and Gradle empties its results directory,
  leaving only the re-run classes'). So `record` backs up every `TEST-*.xml` under
  `target/surefire-reports` and `build/test-results` before a re-run, copies the re-run's out,
  and puts the CI run's back afterwards, also when the re-run fails or times out (a file it
  can't put back is a warning). Other output isn't restored: Surefire's `.txt` summaries,
  Gradle's HTML report (`build/reports/tests`) and binary results, compiled classes. A Gradle
  daemon can keep writing after a timeout. A build that puts its reports elsewhere (its own
  `reportsDirectory`) gets no re-runs: no report is found, and the re-runs stop with a warning.
- **Fail open:** any problem is a warning and the verdicts stay what they were. Re-runs never
  turn a red job green; the PR comment and `fail-on` use the verdicts after them. Outputs:
  `rerun-tests`, `rerun-passed`.

### Shadow mode

How often would siftwise's selection have missed a real failure? On a sampled run the full
suite runs while siftwise stores what it would have selected; after the run is recorded, the
server checks every failure against that selection:

```yaml
      - id: siftwise
        uses: PramodBasavarajMenasi/siftwise/actions/select@main
        with:
          server-url: ${{ secrets.SIFTWISE_URL }}
          token: ${{ secrets.SIFTWISE_TOKEN }}
          shadow: 10%          # or always; off by default
```

- **Sampling:** `off` (default), `always`, or a percentage of CI runs, by a hash of the run ID,
  so every matrix leg and re-run attempt agrees. Matrix legs can set `variant` on `select` (the
  same value as `record`'s) to store a selection per leg.
- **A shadow run always runs the full suite** and records its results as usual; shadow mode
  never fails or shortens a run.
- **The report:** where verdicts are computed, the PR comment and job summary get a line like
  "**Shadow run:** would have run 197 of 900 tests (56% time saved) and caught 1 of 2
  failures", then each missed test with its cause (see the screenshot above). Time saved is
  measured on the run's own durations. Failures triage calls flaky are counted apart, never as
  misses.
- **Not counted, with a warning:** a selection the server couldn't store, siftwise unreachable
  at select time, or a comparison that failed.

Recall over time comes from `GET /repos/{repo}/recall?days=30`, or `siftwise report`
(example output):

```sh
siftwise report --repo acme/shop --days 30
# Recall 98.7% (148 of 150) over 40 shadow runs; selective runs only: 96.0% (48 of 50) over 25
```

The first figure counts every shadow run, including those where siftwise would have run the
full suite anyway, so the report always shows the selective-only figure next to it. Below come
the misses by verdict and cause, the measured time saved, the runs not counted, and each miss.
`--by week` prints the same per week:

```text
Week of     Runs  Recall (all)        Selective  Recall (selective)  Caught  Missed  Time saved
2026-09-28    11  100.0% (41 of 41)           7  100.0% (13 of 13)       41       0         38%
2026-10-05    12  98.0% (49 of 50)            8  95.0% (19 of 20)        49       1         41%
```

Each miss gets a cause, from evidence the server already has:

| Cause | Evidence |
|---|---|
| `new_test` | no result for the test before the run |
| `flaky_history` | flaky before, but not called flaky this time |
| `config_gap` | a non-code file (data, fixture, golden file) changed in the test's own directory, which no import graph follows; the report suggests a `[[depends]]` rule for you to review |
| `no_link` | the change is known and nothing in it links to the test (the report lists what changed) |
| `unknown` | the run's change links weren't recorded, or the test was linked but not selected (the report warns: possibly a selector bug) |

[action-e2e.yml](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/.github/workflows/action-e2e.yml) runs all three actions against a real server
on every push and PR of this repo: verdicts, `fail-on`, a shadow run with a miss,
`known_flaky` and `rerun-unknown`.

## GitLab CI

A CI template, [`gitlab/siftwise.gitlab-ci.yml`](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/gitlab/siftwise.gitlab-ci.yml),
with three hidden jobs around `siftwise gitlab select|record|rollup`. They run the same steps
as the GitHub Action, with the same rules: they fail open, use the merge request's base for
`known_flaky`, support shadow mode and `rerun-unknown`, and fail only through `fail-on`.

| Your job | `extends:` | What it does |
|---|---|---|
| `siftwise-select` | `.siftwise-select` (stage `.pre`) | picks the tests; passes `SIFTWISE_MODE`, `SIFTWISE_COMMAND_FILE`, `SIFTWISE_SELECTED`, `SIFTWISE_TOTAL`, `SIFTWISE_REASON` and `SIFTWISE_JUNIT_DIR` (plus the shadow outputs) to later jobs as a dotenv report; writes the selection into one sticky merge request note |
| your test jobs | `.siftwise-record` | its `after_script` uploads the job's JUnit (`**/junit*.xml` and `.siftwise-ci/junit/`) with the pipeline ID, the merge request's source commit (never a merged results commit), the branch, the variant, the changed files and the base commit's `known_flaky`; optionally re-runs unknown failures |
| `siftwise-rollup` | `.siftwise-rollup` (stage `.post`, `when: always`) | triages every test job's failures into the note, applies `fail-on`, reports a shadow run, then updates the test stats once |

Each job writes its reports under `.siftwise-ci/` (artifacts, kept 1 week): `select.md`,
`failures.md`, and the dotenv files. GitLab has no job summary, so these files are where to
look.

**Setup:**
1. **Include the template by a release tag and its hash**, never a branch. The tag fixes what
   runs in your pipelines with your tokens; GitLab rejects the file if it changes
   ([`include:integrity`](https://docs.gitlab.com/ci/yaml/#includeintegrity), GitLab 17.9+). Get the hash
   with `curl -sL <url> | openssl dgst -sha256 -binary | openssl base64 -A`. Each template
   installs its own release (`pip install siftwise==<version>`). Or copy the file into your
   repository and `include: local:` it.
2. **CI/CD variables** (Settings > CI/CD > Variables): `SIFTWISE_SERVER` (your server, https),
   `SIFTWISE_TOKEN` (its `SIFTWISE_API_TOKEN`; Masked and hidden) and, for the note,
   `SIFTWISE_GITLAB_TOKEN` (Masked and hidden). Choosing between protected and not:
   [protected or unprotected](#protected-or-unprotected-variables).
3. **Full history:** the select and test jobs set `GIT_DEPTH: "0"`; new GitLab projects clone
   20 commits. On a shallow clone, `select` fails with that fix and the pipeline runs the full
   suite (the select job has `allow_failure: true`).
4. **Images:** the select job uses `python:3.12-bookworm`, which has git. For Go import graphs,
   give it an image with Go and Python 3.11+. The record step runs in your test job's image,
   which needs Python 3.11+ (`python3 -m venv`).

```yaml
include:
  - remote: https://raw.githubusercontent.com/PramodBasavarajMenasi/siftwise/vX.Y.Z/gitlab/siftwise.gitlab-ci.yml
    integrity: sha256-<base64 SHA-256 of that file>

workflow:  # merge request pipelines for open MRs, branch pipelines otherwise
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH && $CI_OPEN_MERGE_REQUESTS
      when: never
    - if: $CI_COMMIT_BRANCH

siftwise-select:
  extends: .siftwise-select

test:
  extends: .siftwise-record
  image: python:3.12
  script:
    - pip install -r requirements.txt
    - |
      if [ "$SIFTWISE_MODE" = selective ]; then
        # Shell-quoted by siftwise; each pytest command writes its report to .siftwise-ci/junit.
        if [ -s "$SIFTWISE_COMMAND_FILE" ]; then sh "$SIFTWISE_COMMAND_FILE"; else echo "No tests affected."; fi
      else
        pytest --junitxml=.siftwise-ci/junit/full.xml   # your full suite
      fi
  artifacts:
    reports:
      junit: .siftwise-ci/junit/*.xml

siftwise-rollup:
  extends: .siftwise-rollup
  variables:
    SIFTWISE_FAIL_ON: new_regression,unknown   # optional: see fail-on
```

If `siftwise-select` failed or didn't run, `SIFTWISE_MODE` is empty and the test job runs the
full suite. Test jobs that use `needs:` must list `siftwise-select` to get its variables. If
your test job has its own `after_script` or artifact `paths`, add
`!reference [.siftwise-record, after_script]` and `.siftwise-ci/` to them.

**Matrix:** give each leg a `SIFTWISE_VARIANT`. The template's record step defers the stats
and the verdicts to `siftwise-rollup`, which runs once after every leg:

```yaml
test:
  extends: .siftwise-record
  image: python:$PY
  parallel:
    matrix:
      - PY: ["3.11", "3.12"]
  variables:
    SIFTWISE_VARIANT: py$PY
  script:
    - ...   # as above
```

**Inputs** are the GitHub Action's, as variables: `SIFTWISE_REPO`, `SIFTWISE_BASE_REF`,
`SIFTWISE_SHADOW`, `SIFTWISE_COMMENT`, `SIFTWISE_VARIANT`, `SIFTWISE_JUNIT_GLOB`,
`SIFTWISE_FAIL_ON`, `SIFTWISE_KNOWN_FLAKY_SOURCE`, the `SIFTWISE_RERUN_*` ones. The rollup's
outputs are `SIFTWISE_VERDICTS_AVAILABLE`, `SIFTWISE_COUNT_<VERDICT>`, `SIFTWISE_REPORT` and
the shadow ones.

**Try it on gitlab.com** with [examples/gitlab](https://github.com/PramodBasavarajMenasi/siftwise/tree/main/examples/gitlab): a small project whose
pipeline runs its own siftwise server and Postgres inside each job, with a random token, so
the only variable it needs is `SIFTWISE_GITLAB_TOKEN`.

### The GitLab token

The note goes through the [Notes API](https://docs.gitlab.com/api/notes/). Give it the least
that works:

- **Premium or Ultimate, or Self-Managed:** a [project access token](https://docs.gitlab.com/user/project/settings/project_access_tokens/)
  with the **Reporter** role, the **`api`** scope (`read_api` can't write notes) and an expiry
  date. Its bot user writes the note. Planner is the lowest role that can comment, but not when
  merge requests are set to "Only Project Members" ([permissions](https://docs.gitlab.com/user/permissions/)).
- **GitLab.com Free** (no project access tokens): a dedicated user (a bot account, a project
  member with the Reporter role) with a [fine-grained personal access token](https://docs.gitlab.com/auth/tokens/fine_grained_access_tokens/)
  limited to this project: **Work Item** create, read and update (merge request notes are part
  of it), **Merge Request** read, and **User** read (siftwise looks up its own user so it only
  edits its own note). Set an expiry date. A classic token with the `api` scope also works, but
  it can do anything its user can, everywhere.
- **`CI_JOB_TOKEN` can't do it:** its allowlist has only `GET` endpoints for merge requests
  and notes ([job token](https://docs.gitlab.com/ci/jobs/ci_job_token/)), so it can read notes
  but not write them.

The role should stay below Maintainer: Maintainers can edit other users' comments. A token
without access just warns (`merge request comment failed: HTTP 403`), and the pipeline goes on.

### Protected or unprotected variables

- **Protected** variables reach only pipelines on protected branches and tags. A merge request
  from an unprotected branch (most of them) gets no token: siftwise runs the full suite, posts
  no note and records nothing. With `fail-on` set, `siftwise-rollup` then **fails closed**: only
  forks skip the gate, so don't combine protected variables and `fail-on`. GitLab 18.1+ can pass
  protected variables to merge request pipelines, but only when both the source and target
  branches are protected
  ([details](https://docs.gitlab.com/ci/pipelines/merge_request_pipelines/#control-access-to-protected-variables-and-runners)).
- **Unprotected** (masked and hidden) variables reach every branch pipeline. Anyone who can
  push a branch (the Developer role) can change `.gitlab-ci.yml` on it and read both tokens.
  Masking hides a value in logs, not from the job's code.
- **Recommended:** unprotected, masked and hidden, when everyone with the Developer role may read
  and write siftwise history. The siftwise token reads and writes **every repo on its server**:
  run a separate siftwise server per trust boundary, so that a leaked token exposes only one
  team's repos. Otherwise use protected variables: siftwise then helps only protected branches.

### Forks

By default a fork's merge request pipeline runs **in the fork, without this project's
variables**: the full suite, no note, no upload, and `fail-on` skipped with a warning (as for
fork PRs on GitHub). Keep another required check on them. **Never run untrusted forks'
pipelines in the parent project** (Run pipeline on the merge request's Pipelines tab): that
runs the fork's code with this project's variables, so it could read both tokens. siftwise
warns when it sees one. Like GitHub's `pull_request_target`, only do it for code you've read.

### GitLab security

- **Tokens come from the environment only.** No `--token` option exists, and siftwise redacts
  every value of `SIFTWISE_TOKEN`, `SIFTWISE_API_TOKEN`, `SIFTWISE_GITLAB_TOKEN` and
  `CI_JOB_TOKEN` from its log lines, dotenv files, reports and the note, even when an error
  message echoes one. Never add `set -x` or `CI_DEBUG_TRACE` to these jobs.
- **Untrusted text is inert in the note.** Test IDs, file paths, reasons and variant names come
  from the merge request's code, so siftwise writes them as code spans, which GitLab shows
  literally: no links, images, HTML, `@` mentions, references or placeholders. No line can
  start with `/`, so none can run a quick action. Raw failure messages are never shown.
- **A merge request can't excuse its own failures:** `record` applies the `known_flaky` list
  of the MR's base commit.
- **Plain http warns:** `SIFTWISE_SERVER` with `http://` and a host other than `localhost` or a
  one-word service name (the token travels in a header) gives a warning.
- **Protect `.gitlab-ci.yml`:** a merge request that edits it can change any gate. Protect the
  default branch and, with Premium or Ultimate, require code owner approval for the file
  ([CODEOWNERS](https://docs.gitlab.com/user/project/codeowners/)).

**Limits:**
- **Retried jobs:** a retried job keeps its pipeline ID, so only the pipeline's first upload of
  each leg is stored (`record` warns). Run a new pipeline to record again.
- **No diff base:** merge trains, scheduled, manual (web), API and trigger pipelines run the
  full suite.
- **`after_script` limits:** it runs with a 5-minute limit by default (`RUNNER_AFTER_SCRIPT_TIMEOUT`),
  which caps `rerun-unknown`, and, depending on the executor, without software the `script`
  section installed. Re-runs need the test dependencies inside the project directory (a
  virtualenv there, `node_modules`), or call `siftwise gitlab record` at the end of `script`.
- **Tested only with mocks:** the GitLab support is tested against GitLab's documented variables
  and API, with mocks, not yet on a real GitLab instance.

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
  to the working tree, untracked files included. When the tree is dirty, triage sends no
  commit, so "passed on this commit before" can't apply.
- **Fails open.** If the server is down, errors or no token is set, `select_tests` says to run
  the full suite and `triage_failure` says verdicts are unavailable.
- **Compact output.** Each result stays under 8,000 characters. The API token never appears
  in a result.

**Install** the CLI with the `mcp` extra ([Install](#install)):

```sh
uv tool install 'siftwise[mcp]'  # siftwise lands in ~/.local/bin (Windows: %USERPROFILE%\.local\bin)
uv tool update-shell             # once, if that directory isn't on your PATH; then restart the agent
uv tool dir --bin                # prints the directory, for a full path in the configs below
```

The server reads `SIFTWISE_SERVER` and `SIFTWISE_API_TOKEN`, and optionally `SIFTWISE_REPO`
(default: `owner/name` from the `origin` remote), `SIFTWISE_BASE_REF` (default: the branch
`origin/HEAD` points to, else `main`) and `SIFTWISE_REPO_PATH`. Keep the token out of files
you commit: use your user-level config, or environment-variable expansion where the client has
it.

**Which repository:** the first that applies of the tool's `repo_path` argument (an absolute
path the agent can pass), `SIFTWISE_REPO_PATH`, `CLAUDE_PROJECT_DIR` (set by Claude Code), the
client's workspace folders if it supports MCP roots, and the server's working directory.
A folder the client sets (`CLAUDE_PROJECT_DIR`, a root) that isn't a repository but holds
exactly one directly inside means that one, and the result says so. Outside a git repository,
every tool says so and how to fix it, listing the repositories one level down.

Tested on Windows with Claude Code, GitHub Copilot in VS Code, Cursor, Codex CLI and Gemini
CLI. Windsurf and Cline configs follow their docs but are untested. Setup for each, with
snippets: [MCP clients](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/docs/mcp-clients.md).

| Client | Config | How it finds the repository |
|---|---|---|
| Claude Code (tested) | `claude mcp add`, or `.mcp.json` | `CLAUDE_PROJECT_DIR`, which Claude Code sets |
| GitHub Copilot in VS Code (tested) | `.vscode/mcp.json` | MCP roots, else the workspace folder it starts in |
| Cursor (tested) | `.cursor/mcp.json` or `~/.cursor/mcp.json` | MCP roots: no path to configure |
| Codex CLI (tested) | `~/.codex/config.toml` | the working directory: set `cwd` |
| Gemini CLI (tested) | `.gemini/settings.json` | the working directory: set `cwd` |
| Windsurf (untested) | `mcp_config.json` | `SIFTWISE_REPO_PATH`, or the `repo_path` argument |
| Cline (untested) | `cline_mcp_settings.json` | `SIFTWISE_REPO_PATH`, or the `repo_path` argument |

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

**Codex CLI** (`~/.codex/config.toml`):

```toml
[mcp_servers.siftwise]
command = "siftwise"
args = ["mcp"]
cwd = '/path/to/your/repo'   # the repository: Codex starts the server there
env = { SIFTWISE_SERVER = "https://siftwise.example.com", SIFTWISE_API_TOKEN = "your-token" }
```

If Codex says the model isn't supported with a ChatGPT account, pick another model with
`/model`.

**Cursor** (`~/.cursor/mcp.json` for every project, or `.cursor/mcp.json` in one): the same
JSON as `.mcp.json` above, with the token value written in. No repository path: Cursor sends
its workspace folders as MCP roots.

**Gemini CLI** (`.gemini/settings.json`): the same JSON with `"cwd": "/path/to/your/repo"`
added. Put the token in the server's `env` block (as above): Gemini CLI strips inherited
variables whose names contain `TOKEN`, so one set only in your shell never reaches siftwise.

**GitHub Copilot in VS Code** (`.vscode/mcp.json`): servers go under `"servers"` with
`"type": "stdio"`; see [MCP clients](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/docs/mcp-clients.md#github-copilot-in-vs-code)
for a config that prompts for the token.

**If the agent can't find `siftwise`** (it doesn't see your shell's PATH, common on Windows),
give the full path from `uv tool dir --bin` as the command. In JSON, double the backslashes
(`"C:\\Users\\you\\.local\\bin\\siftwise.exe"`); in TOML, use a single-quoted string.

Then tell the agent when to use it, e.g. in `AGENTS.md` or `CLAUDE.md`:

```markdown
Before running tests, call the siftwise `select_tests` tool and run the command it returns.
When tests fail, call `triage_failure` before fixing anything: fix `new_regression` failures,
re-run `flaky` ones, and leave `pre_existing` ones (already failing on main).
```

## Configuration

`.siftwise.toml` at the root of the repo being tested, read by the CLI, the Action and the
MCP server:

```toml
always_run = ["tests/smoke/**", "tests.test_health::*"]  # test file paths or test IDs
generated = ["build/reports/**"]          # more files your test runs write (see below)
known_flaky = ["tests/test_net.py"]       # triage: unlinked failures are flaky

[[depends]]                               # dependencies no import graph can see
tests = "test/cli/**"                     # test file paths, or <Go package dir>/<TestName>
on = ["**/*.go", "!**/*_test.go"]         # ! excludes
```

- **Globs:** `**` spans directories, while `*` and `?` stay within one path segment
  (`tests/*` matches `tests/test_a.py`, not `tests/unit/test_b.py`). Go test IDs contain their
  package's import path, so match them with `**::TestName` or `<import path>::*`.
- **Generated files** never count as changed code, even untracked or committed by mistake:
  `junit*.xml`, `coverage.xml`, `.coverage`, `.coverage.*`, `htmlcov/`, `*.log`,
  `.pytest_cache/` and `__pycache__/`, plus editor and coding-agent settings: `.vscode/`,
  `.cursor/`, `.idea/`, `.mcp.json`, `.codex/` and `.gemini/`, all at any depth. `generated`
  adds to that list.
- **`known_flaky`** is for triage only (selection still runs listed tests); CI applies a pull
  request's base list ([Flaky tests](#flaky-tests)).

| Variable | Used by | Default |
|---|---|---|
| `SIFTWISE_API_TOKEN` | server (required: if unset, every endpoint but `/healthz` returns 503) and clients | none |
| `SIFTWISE_SERVER` | clients: the CLI's `--server`, MCP, the Action's steps | `http://localhost:8000` |
| `SIFTWISE_REPO_PATH` | MCP: the repository, when the client starts the server elsewhere ([which repository](#coding-agents-mcp)) | none |
| `SIFTWISE_DATABASE_URL` | server | `postgresql+psycopg://siftwise:siftwise@localhost:5432/siftwise` |
| `SIFTWISE_STATS_WINDOW_DAYS` | server: per-test stats window (0 = all history) | `90` |
| `SIFTWISE_MAX_UPLOAD_BYTES` | server: request body limit | 50 MB |
| `GITHUB_TOKEN` | `scripts/backfill.py` | none |

**Supported languages:**

| Language | Selection | Commands |
|---|---|---|
| **Python** (pytest) | `foo.py` → `test_foo.py`; test files importing a changed module (static graph incl. parent packages, conftests, plugin strings); changed test files | `pytest` node IDs, whole test files, `pytest --doctest-modules` for doctests |
| **Go** | changed package and every package whose tests import it (`go list -deps -test`) | `go test ./pkg` (whole) or `-run '^(TestA\|TestB)$'` |
| **JS/TS** (Jest) | `foo.ts` → `foo.test.*`, `foo.spec.*`, `__tests__/foo.*` (needs file paths in the JUnit) | `jest <files>` |
| **Java** (Maven/Gradle) | recorded with full history; selection falls back to the full suite | — |

## Results on real repos

Replays of real CI history with no peeking at the future, with the current code except where
noted. How they measure, and what they don't prove: [eval methodology](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/docs/eval-methodology.md);
full numbers: [eval results](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/docs/eval-results.md).

| Repo | Failures caught | Test time skipped |
|---|---|---|
| [ipfs/kubo](https://github.com/ipfs/kubo) (Go, ~4,100 tests), measured with the week-2 code | 19 of 19 in failing PRs | 30.7% over 20 main commits replayed as PRs; 0% on recent PRs (all dependency/CI bumps) |
| [RDFLib/rdflib](https://github.com/RDFLib/rdflib) (Python, ~18,000 tests) | 2,049 of 2,050 in 39 failing PRs; 140 of 141 in selective runs. The miss is a flaky remote-service test | 10.4% on the 10 most recent PRs; 0.6% over 19 main commits |
| [quantumlib/Cirq](https://github.com/quantumlib/Cirq) (Python monorepo, 9 packages, ~40,000 tests) | not measurable: JUnit only from main, which never failed | 37.9% over 132 merged PRs; 79.8% for `cirq-google`-only PRs, 20.2% for `cirq-core` |
| [agent-substrate/substrate](https://github.com/agent-substrate/substrate) (Go, ~9,000 tests, 8 days of history) | 2,043 of 2,044 in 72 failing PRs; 38 of 39 in selective runs, with declared rules for its e2e and functional suites (26 of 38 without them, week-3 code) | 5.1% on PR runs and 13.9% over 40 main commits, with both rules (19.5% / 31.7% without rules, week-3 code) |

- **Savings follow package boundaries.** In the Cirq monorepo, a change to one integration
  package skips most of the suite; a change to the core package everything imports skips
  little. rdflib's `__init__.py` and plugin registry connect nearly every module to nearly
  every test, so safe file-level selection still runs ~99% of its suite (its 10.4% comes from
  lint-only PRs). Bigger savings need codebases with independent parts, or per-test coverage.
- **The misses:** substrate's are end-to-end suites that drive a deployed system (no import
  edge reaches them; a declared dependency catches them at the cost of most of the savings)
  and flaky tests. rdflib's one miss, `test_service_with_bound_solutions`, calls a remote
  SPARQL service and had failed in unrelated runs before: it's flaky, which is what
  [`rerun-unknown`](#flaky-tests) is for.

**Triage** (a verdict per failure, scored against what happened next;
[details](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/docs/eval-results.md#triage-eval)):

- **The worst error, a regression called flaky:** 0 on rdflib. On substrate, 5 of 406
  labelled regressions, all on tests with earlier same-commit flakes; 4 are likely label noise
  from merge diffs.
- **The prediction tests held up:** `new_regression` 319 of 319 labelled substrate failures
  (and 10 of 11 rdflib fix episodes with a known diff), and medium-confidence `pre_existing`
  759 of 759, each called after a single failing main run.
- **Flaky precision is mostly by construction:** almost every `flaky` verdict is a pass of the
  same commit seen before the failure, which is also the flaky label's evidence.

**Performance** (rdflib history; results identical before and after;
[details](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/docs/eval-results.md#performance)): triage of a 22-variant CI run 1,032 → 220 ms
(180 → 12 queries, no longer growing with variants); the selection inside `/select` 605 → 237
ms; ingesting a 9,891-result upload about 2.2 → 1.25 s.

### How it compares

From each project's public docs as of October 2026; corrections welcome.

| | Open source | Self-hosted | Languages | How it selects | Agent/MCP support |
|---|---|---|---|---|---|
| **siftwise** | Yes (Apache-2.0) | Yes | Python, Go, JS/TS | file mapping, Go/Python import graphs, failure history | MCP server (select, triage, explain) |
| [CloudBees Smart Tests](https://docs.cloudbees.com/docs/cloudbees-smart-tests/latest/features/predictive-test-selection) | No | Not stated | Many (runner integrations incl. Python, Go, Java, JS) | LLM analysis of changes + test history | Not documented |
| [Datadog Test Impact Analysis](https://docs.datadoghq.com/tests/test_impact_analysis/) | No | No (Datadog SaaS) | JS/TS, Java, Python, Go, Swift and more | per-test code coverage | Not documented |
| [Develocity Predictive Test Selection](https://docs.develocity.ai/current/using-develocity/predictive-test-selection/) | No | Yes (Develocity server) | Gradle and Maven builds | predictive model from past builds | Not documented |
| [pytest-testmon](https://pypi.org/project/pytest-testmon/) | Yes (MIT) | Local plugin, no server | Python (pytest) | per-test code coverage | No |

Coverage-based tools see dynamic dependencies that static graphs miss, but they need
instrumented runs. siftwise uses no instrumentation; when its graphs can't see a dependency,
the fallbacks and `.siftwise.toml` cover it.

## Security and limits

Found a vulnerability? Report it privately, as [SECURITY.md](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/SECURITY.md) describes, not in a
public issue.

- **One token per server, with full access.** `SIFTWISE_API_TOKEN` is a single shared secret:
  every client (CI, the CLI, the MCP server) uses it, and it can read and write the history of
  **every repo** on the server. There are no per-repo, read-only or per-user tokens. Run a
  separate server for repos whose owners shouldn't see each other's history, and rotate the
  token by changing it on the server and in each secret.
- **Keep the token out of fork PRs.** Use `pull_request`, never `pull_request_target`: a
  fork's code must never run with the token. On GitLab, never run untrusted forks' merge
  request pipelines in the parent project ([Forks](#forks)).
- **Protect your workflows.** The base-branch `known_flaky` rule assumes the workflow itself
  is protected. A PR that edits `.github/workflows/` (or `.gitlab-ci.yml`) can change any gate,
  so protect workflows with CODEOWNERS or a ruleset (on GitLab: protected branches, and code
  owner approval with Premium or Ultimate). GitLab's token choices:
  [protected or unprotected variables](#protected-or-unprotected-variables).
- **What the server stores:** test IDs, outcomes, durations, failure messages (truncated to 4
  KB), changed file paths, branch names and commit SHAs. Failure messages can contain whatever
  your tests print, so treat the database as you treat CI logs. Public PR comments show test
  IDs, verdicts and siftwise's own reasons, never raw failure messages.
- **Stats cover the window only.** Per-test stats use the last `SIFTWISE_STATS_WINDOW_DAYS`
  (90) days before the repo's newest run; older raw results stay, but no longer feed stats.
- **Limits to know:** merge-queue (`merge_group`), scheduled and manual runs always run the
  full suite (no diff base); the Action is tested on Linux runners only, and the GitLab
  template with mocks only (not yet on a real GitLab instance); Java tests are
  recorded but always run in full (`rerun-unknown` re-runs pytest, Go, Jest, Maven and Gradle
  tests).

## Roadmap

- **Done:** recording and history, test selection with import graphs and safe fallbacks, the
  GitHub Action, the GitLab CI template (unreleased), failure triage with verdicts in the PR comment and `fail-on`, the MCP server,
  the triage eval, [shadow mode](#shadow-mode) with `siftwise report`, miss causes,
  [flaky handling](#flaky-tests) (`known_flaky`, `rerun-unknown`), performance (batched
  triage, faster select and ingest), packaging (a light CLI, a server image, the quickstart)
  and the `v0.1.0` release on PyPI and GHCR ([changelog](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/CHANGELOG.md)).
- **Next:** a demo and the launch write-up; an arm64 server image.
- **After launch:** live dogfooding: shadow mode on siftwise's own CI with a hosted server,
  with published live recall numbers.

## Contributing

Issues and pull requests are welcome: see [CONTRIBUTING.md](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/CONTRIBUTING.md) for the dev
setup, the checks and the PR checklist, and [ARCHITECTURE.md](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/ARCHITECTURE.md) for
how the pieces fit. Everyone taking part follows the [Code of Conduct](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/CODE_OF_CONDUCT.md).

## License

Apache-2.0
