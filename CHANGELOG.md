# Changelog

All notable changes to siftwise. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (before 1.0, a minor version may break things).

## [Unreleased]

### Added

- `siftwise token list --active` (only tokens not revoked) and `--json` (a JSON array with
  the table's fields: `id`, `name`, `repo` (`*` for every repo), `scope`, `created`,
  `last_used`, `revoked`, times in ISO 8601 UTC, `null` when empty; never the token or its
  hash) ([#90](https://github.com/PramodBasavarajMenasi/siftwise/issues/90)). The flags
  combine; without them the table is unchanged.

- The GitHub Action is tested on macOS and Windows runners as well as Linux
  ([#79](https://github.com/PramodBasavarajMenasi/siftwise/issues/79)): `action-os.yml` runs
  `actions/select`, `record` and `rollup` from the checkout on `ubuntu-latest`,
  `macos-latest` and `windows-latest` against a server and Postgres in the job, on a sample
  repo made in the job (a full run, then a selective run after a change; outputs, the
  selected command, verdicts and `fail-on`'s exit code checked). It runs when the actions,
  the CLI code they run, `pyproject.toml` or `uv.lock` change, weekly, and on demand. The README
  lists the tested runners and the Windows requirement (Git Bash, which GitHub's Windows
  runners have).
- `SIFTWISE_HEAD_SHA` (advanced): the commit `actions/select`, `record` and `rollup` test,
  for workflows that test another commit than the event's. A full 40-character SHA of a
  commit in the checkout, else the step fails (no fallback); a notice names both commits.

- The server image is published for `linux/arm64` as well as `linux/amd64`
  ([#76](https://github.com/PramodBasavarajMenasi/siftwise/issues/76)): one multi-arch image
  per tag on GHCR, same tags as before, built with buildx (arm64 under QEMU on the amd64
  runner). Tested under QEMU emulation, not on arm64 hardware: the arm64 image starts,
  migrates an empty database to the newest migration and answers `/healthz` (about 22 s
  under emulation; amd64 4 s). Pull requests that touch the image now build both platforms
  without pushing (`image` job in quickstart.yml). The image's health check now sends its
  `GET /healthz` over a plain socket instead of importing `urllib` (same endpoint, interval,
  timeout and retries): under emulation the import alone outran the 3 s timeout.
- CI health and older outcomes ([#85](https://github.com/PramodBasavarajMenasi/siftwise/issues/85),
  part 3):
  - The dashboard's *CI* page: the overview's CI numbers per UTC day or week (Monday 00:00
    UTC), empty periods listed, for all runs, main or pull requests; the slowest tests by mean
    duration ("mean over the last N runs", only tests that ran in the window, no
    percentiles). `GET /repos/{repo}/ci` takes `by=day|week` for the same periods. Every
    number comes from `core/ci_stats.py`, with PR 2's rules (counts next to shares, rounded
    down, the too-few-runs flag, no recall from selective runs).
  - `siftwise outcomes backfill --repo X [--dry-run]` (`POST /repos/{repo}/outcomes/backfill`,
    a write token): stores outcomes for CI runs uploaded without one (older clients, a failed
    outcome step), labelled "verdicts computed later" (plus "no selection recorded" when the
    run has none) on the run page, the runs list and the CI page. No peeking: each uses only
    what a live outcome call could have read (for runs CI uploaded itself, only runs uploaded
    by the attempt's last upload). Resumable and idempotent; batches capped at 100 attempts
    and 60 s; a live outcome always wins (an advisory lock per attempt).
  - `docker-compose.dev.yml` passes `SIFTWISE_DASHBOARD` through (default off).
- The CI numbers ([#85](https://github.com/PramodBasavarajMenasi/siftwise/issues/85), part 2):
  `GET /repos/{repo}/ci?days=30&branch=all|main|prs` (read scope), the overview's CI section
  (main and pull requests apart) and new lines in `siftwise report` show, for a window:
  selections recorded for X of Y CI runs; selective vs full-suite runs, full-suite runs by
  reason code; the median share of tests selected; test time saved (estimated: per leg, the
  median measured time of the same variant's full-suite runs in the 30 days before it, at
  least 3, minus the leg's measured test time; not /select's summed per-test estimate, which
  charges every matrix leg every known test and overstated the saving: rdflib 413 s
  estimated for a 231 s leg; sums of test durations, not wall-clock CI time; negative when a
  leg took longer, never clipped; "based on N of M selective runs (K legs left out)" for legs
  with no baseline yet or no measured upload; the run page shows the same baseline); main = the uploader's `is_main` (the repo's default
  branch, whatever its name);
  shadow runs apart (what they would have saved, measured, never added to the time saved);
  and failures in selective runs (in the selection or not, and their rank). One module
  (`core/ci_stats.py`) computes them for all three. No recall is claimed from selective runs:
  it comes only from shadow runs. README: [How these numbers work](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/README.md#how-these-numbers-work).
- Every CI selection is recorded, and the dashboard shows what siftwise did in each run
  ([#85](https://github.com/PramodBasavarajMenasi/siftwise/issues/85), part 1):
  - The Action's and the GitLab template's select step send their CI run with `/select`
    (`ci`: provider, run ID, attempt, variant, commit, branch, PR number, event, client
    version). With a write token the server records the selection under that run: mode, the
    full-suite reason as a code (`changed_files_unknown`, `no_net_changes`, `build_file`,
    `no_history`, `java_build`, `fixture_unreferenced`, `unmapped_file`, `no_runner`) with
    the files or tests behind it (the reason text is unchanged), the selected tests in risk
    order, the estimates, the budget, the order, the changed-file count and the five
    highest-risk tests. A read token still selects, with one warning that nothing was
    recorded. Laptop and MCP calls are never recorded. About 4 bytes per selected test:
    a selective row's median is 78 KB on rdflib (18,219 tests), 6.3 KB on substrate, 1.4 KB
    on dropwizard; a full one under 1 KB.
  - `POST /repos/{repo}/outcome` (write), called by the rollup step (or record in a single
    job) after the attempt's last upload, triages the attempt once and stores each leg's
    outcome: failures by verdict, which of them the recorded selection ran and at what rank,
    and the `fail-on` verdicts. `POST /repos/{repo}/shadow` does the same for older clients.
    A 0.4 client uses `/outcome` when `/healthz` reports the server's version (new), else
    `/shadow`.
  - Uploads carry `client_version`; every upload stores its measured test time
    (`runs.tests_run`, `runs.measured_ms`).
  - Dashboard: a run's page says what siftwise did (selected X of Y, or why the full suite
    ran; estimated and measured test time, test time saved (estimated); budget; order; the
    highest-risk tests) next to each failure's verdict, whether it was in the selection and its
    rank, or why no selection was recorded (a client older than 0.4, a select step that didn't
    reach the server, the repo's first run). The runs list shows each run's selection; every
    page says how many CI runs in its window have one.

- A read-only dashboard ([#75](https://github.com/PramodBasavarajMenasi/siftwise/issues/75)),
  off unless `SIFTWISE_DASHBOARD=1`: server-rendered pages at `/ui` (no JavaScript) for each
  repo a token can see: shadow-mode recall (all runs and selective runs, by week, misses by
  cause, suggested rules), recent CI runs with their failing tests and each run's verdicts
  (and its shadow comparison), and flaky tests with `known_flaky` entries to paste. Windows
  count back from now (7, 30 or 90 days, or all history) on every page. Sign in by
  pasting any API token (a read token is enough): a server-side session of 4 hours in an
  HttpOnly, SameSite=Strict cookie on `/ui`; a revoked token, or a changed admin token, ends it
  at the next request. Every page escapes test names, branches and repo names, and is served
  with a strict Content-Security-Policy (no scripts), `frame-ancestors 'none'` and
  `Cache-Control: no-store`; login and logout are CSRF-protected. New dependency in the server
  extra: Jinja2. README: [Dashboard](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/README.md#dashboard).
- `siftwise server start --forwarded-allow-ips IPS` (or `SIFTWISE_FORWARDED_ALLOW_IPS`,
  default `127.0.0.1`): the proxies whose `X-Forwarded-Proto`, `-For` and `-Host` the server
  believes. The server now reads these headers itself (`X-Forwarded-Host` too, which uvicorn
  ignores), with the same trust rule uvicorn applied, and starts uvicorn with its own copy off.

- Mutation-based recall eval ([#74](https://github.com/PramodBasavarajMenasi/siftwise/issues/74)):
  `python -m scripts.eval.mutation` plants mutants on the lines a merged PR changed and checks
  whether the selection siftwise would have made runs a test that catches each (kill rule: fails
  with the mutant, fails again on a re-run, passes without it; 95% Wilson intervals). pytest
  repos replay CI history; Jest repos (`scripts/eval/mutation_jest.py`) seed one full run at
  each PR's base. A Linux runner image (`scripts/eval/mutation-linux.Dockerfile`, Python 3.12
  and Node 24). Results: rdflib 26 of 26, Cirq 63 of 63 (13 of 13 in narrow selections), mobx
  40 of 40 (6 of 6); docs/eval-results.md#mutation-based-recall.

- Flaky-test tracking ([#72](https://github.com/PramodBasavarajMenasi/siftwise/issues/72)):
  - `siftwise flaky --repo acme/shop [--days 30] [--min-runs N] [--until DATE] [--json]`
    lists tests that failed and passed on the same commit and variant (retries, re-runs),
    marks those `known_flaky` already lists (from the checkout's `.siftwise.toml`, else the
    list the newest main run applied), and prints entries to paste for the rest. It changes
    nothing and needs only a read token: new route `GET /repos/{repo}/flaky` (read scope).
  - `known_flaky` entries can expire: `{ test = "tests/test_db.py::test_pool", until =
    "2026-12-31" }` next to plain globs (which work as before). An entry applies through its
    date, judged by the UTC date the CI run started (a local triage or an agent: today), and
    after that the PR comment and GitLab note say "known_flaky entry expired: <test> (until
    <date>)". A PR still runs with its base commit's list, so it can't add or extend an
    entry for itself.
  - Migration 0015 adds `runs.known_flaky_expired`. An older server rejects that field: the
    run is then recorded without it (with a warning).

- Test fixtures ([#73](https://github.com/PramodBasavarajMenasi/siftwise/issues/73)):
  `fixtures = ["tests/fixtures/**"]` in `.siftwise.toml` marks test data. A changed file
  there (a sample project's `pom.xml`, `build.gradle.kts`, `tsconfig.json`, `go.mod`, ...)
  no longer forces the full suite: the test files that name the fixture run (the CLI reads
  them, and Python helpers they import), plus `[[depends]]` rules on it. If nothing
  references it, the full suite runs, naming the file. Files your own build includes
  (Maven/Gradle modules, `go.work` uses, JS workspace packages) keep today's behaviour.
  Shadow mode has a new miss cause, `fixture`. Migration 0014 adds
  `shadow_selections.fixture_files` and the cause. siftwise's own `.siftwise.toml` lists
  `tests/fixtures/**`: of the dogfood runs from 2026-10-26 to 2026-11-09, the two of #50
  (fixture build files only) would now run 32 of the 40 test files instead of everything;
  the two of #53 still run the full suite (`.github/workflows/ci.yml` changed too).

- Per-repo API tokens with `read` and `write` scopes
  ([#71](https://github.com/PramodBasavarajMenasi/siftwise/issues/71)):
  - `siftwise token create --repo acme/shop --scope read|write [--name ci]` prints a new
    token (`sw_...`) once; `siftwise token list` shows ids, names, repos, scopes and when
    each was created, last used (to 10 minutes) and revoked, never the tokens;
    `siftwise token revoke <id>`. They use the database directly (`SIFTWISE_DATABASE_URL`,
    the server extra), like `siftwise server migrate`: there is no API to make tokens.
  - `read`: select, triage, explain, recall, test history and run lookups. `write`: also
    uploads, rollups, shadow compares and storing a shadow run's selection (`/select` with
    `shadow`). A token is for one repo or `*` (`--all-repos`, which also works on Windows,
    where Click expands a `*` argument into file names even when it is quoted).
  - Auth is still checked before the request body is read: unknown or revoked tokens get
    401, a read token on a write call or a token for another repo get 403 (the same whether
    that repo exists or not). A route not listed in the scope table needs `write`.
  - `SIFTWISE_API_TOKEN` keeps working as an admin token (every repo, write), so existing
    setups don't change. The server can now run without it, on per-repo tokens alone; with
    neither it still answers 503.
  - Migration 0013 adds `api_tokens` (only each token's SHA-256 is stored).
  - Clients are unchanged: they send a bearer token as before. The docs now recommend a
    `write` token for CI and a `read` token for coding agents (docs/mcp-clients.md).

### Upgrading

- **Migrations in this release: 0013 to 0018. Back up the database first.**
  `siftwise server start` (or `siftwise server migrate`) applies them: 0013 `api_tokens`,
  0014 fixture files and the fixture miss cause, 0015 `runs.known_flaky_expired`, 0016
  `ui_sessions`, 0017 the CI selection tables (below), 0018 `attempt_outcomes.source` (below).
- **Migration 0018** adds `attempt_outcomes.source` (`ci` for every existing row, `backfill`
  for the outcomes backfill's). A catalog change plus a check of that small table: 0.1 s on a copy of substrate's history (6.4M results, 798 outcomes, median of 3); `test_results` is not touched and no index is built, so uploads are never blocked; outcome writes wait for those 0.1 s (the table is locked while the column is added). Then, to count older runs' failures in the CI
  numbers, run `siftwise outcomes backfill --repo X` once per repo (`--dry-run` first).
- **0017.** Migration 0017 renames the shadow-mode
  tables to `ci_selections`, `run_outcomes` and `run_failures` (with their constraints,
  indexes and sequence), adds columns with defaults, moves the measured test time of shadow
  runs to `runs` and adds `attempt_outcomes`. It doesn't rewrite `test_results`. Recall and
  `siftwise report` give the same numbers before and after (checked on a replay of 112
  substrate shadow runs). Its downgrade keeps all shadow data but drops what 0016 can't hold:
  selections of non-shadow runs and outcomes without a shadow selection.
- **Update the server before the clients, or together.** A 0.4 client against an older server
  works as before: `/select` is sent again without `ci` (the summary says the server doesn't
  record selections), uploads drop `client_version`, and the outcome goes to `/shadow`.
  Recording needs the select step's token to be a write token (it is for most setups: the
  same secret as record).
- **Migration 0016** adds `ui_sessions` (dashboard logins: the SHA-256 of each session ID, the
  token it was made with, and its expiry). A new table: nothing is rewritten, and it stays
  empty unless the dashboard is turned on. `siftwise server start` applies it (with 0013 to
  0015 if they're new to you).
- **The dashboard is off by default.** To use it, set `SIFTWISE_DASHBOARD=1` and serve it over
  HTTPS (plain HTTP only on localhost); behind a TLS proxy, also set `--forwarded-allow-ips`
  to the proxy's address ([Dashboard](https://github.com/PramodBasavarajMenasi/siftwise/blob/main/README.md#dashboard)).

### Fixed

- The Action writes `$GITHUB_OUTPUT`, `$GITHUB_ENV`, the job summary and the failures report
  with LF line endings on every OS. On Windows, Python's text mode wrote CRLF, which could
  leave a `\r` on an output value such as `command`.
- `create_app(settings)` now serves every request from `settings.database_url`: the routes'
  sessions and the auth middleware's token lookups used the process-wide
  `SIFTWISE_DATABASE_URL` instead (`siftwise server start` builds both from the same
  environment, so servers weren't affected; an app built with another URL was).
- `siftwise token revoke` deletes the token's dashboard sessions at once. They were already
  refused on their next request (every `/ui` request checks the token), but their rows stayed
  until then or until they expired.
- Go: preserve workspace vendoring when listing packages by checking for
  `vendor/modules.txt` next to the active `go.work`
  ([#78](https://github.com/PramodBasavarajMenasi/siftwise/issues/78)).
- Go: `go list` ran with Go's own default when GOFLAGS had no `-mod` setting, and that default
  only became read-only in Go 1.16, so on Go 1.15 or older listing packages could write go.mod
  and go.sum. `-mod=readonly` is passed now, unless the module is vendored (Go's default there
  is `-mod=vendor`)
  ([#67](https://github.com/PramodBasavarajMenasi/siftwise/issues/67)).
- Shadow mode: shadow selections stopped being stored after 10 on a long-lived database
  connection, so shadow recall was undercounted. After psycopg prepared the insert and Postgres
  switched to a generic plan, the `ON CONFLICT` target's bound `''` (in `coalesce(variant,
  '')`) no longer matched the unique index: every later `/select` with `shadow` answered
  `shadow_stored: false` (`storage_error`) on that API worker until it restarted. The `''` is
  now part of the SQL.

## [0.3.0] - 2026-11-10

Risk order and time budgets for selected tests, Jest and Java (Maven and Gradle) selection,
import depth in the risk order, and module build files scoped for Java. Two database
migrations (0011, 0012): see Upgrading.

### Upgrading

- **Migrate the server.** `siftwise server start` runs the migrations before it serves
  (unless `--no-migrate`); otherwise run `siftwise server migrate`. New since 0.2.0: 0011
  (`shadow_selections.budget`, for time budgets in shadow mode) and 0012
  (`shadow_selections.java_classes`, Java classes run whole). Both only add columns.
- **Update the server and the clients together** (CLI, GitHub Action, GitLab template, MCP).
  `/select` takes new fields: `js_graph`, `java`, `go_modules`, `import_depths` and
  `budget_seconds`. A server older than the client rejects them; the clients then:
  - send the request again without `js_graph`, `java`, `go_modules` or `import_depths`, so
    selection works as on 0.2.0 (Jest and Java tests run in full, every import ranks as
    indirect, Go commands name packages as before);
  - for `budget_seconds`, fall back to the full suite with a warning (Action, GitLab, MCP) or
    exit 1 (`siftwise select --budget`), since a budget can't be dropped silently.
- An older client still works with a 0.3.0 server: every new field is optional, and new
  response fields (`score`, `rank`, `rank_reason`, `budget`) are additions.

### Added

- Risk order ([#42](https://github.com/PramodBasavarajMenasi/siftwise/issues/42)): every
  selected test gets a risk score, rank and short reason. By default, the commands run the
  riskiest tests first, so likely failures show up early. The score is a fixed formula, with no
  learning (ARCHITECTURE.md, "Risk order"):
  - how directly the change reaches the test: direct, indirect or history only;
  - how recently it failed (half-life 7 days) and its failure rate, with flaky failures
    discounted;
  - +2 for a new test.

  Ties go to the shorter test, then the test ID. It never changes which tests are selected.
  - pytest arguments, Go packages (one command each) and Jest files come in risk order. Jest
    may still reorder files across workers.
  - `/select` returns `score`, `rank` and `rank_reason` per test, and the request takes
    `order` (`risk` or `default`). Older clients work unchanged.
  - The PR comment and GitLab note name the three highest-risk tests, and MCP `select_tests`
    lists tests riskiest first.
  - To keep the old order: `siftwise select --order default`, the Action's `order: default`,
    `SIFTWISE_ORDER: default` on GitLab, or the MCP `order` argument.
- Eval: time to first failure, in the default and in risk order, for selective PR runs that
  caught a failure (`scripts/eval/ordering.py`; results in docs/eval-results.md). A
  cost-aware variant was also evaluated: risk per unit of estimated time, with or without
  link tiers. It wasn't clearly better by time on both repos and was worse by test count, so
  it isn't offered.
- Dogfood: each failure's risk rank goes into the run's summary, and
  `scripts/dogfood_report.py report` shows where failures ranked.
- Java selection for Maven and Gradle ([#45](https://github.com/PramodBasavarajMenasi/siftwise/issues/45)):
  Java tests used to always run the full suite. `siftwise select` (and the Action, GitLab
  and MCP) now reads the build (Maven modules, Gradle includes; no JVM runs) and runs test
  classes per module: `./mvnw -B -pl <module> -am test -Dtest=...` (`verify -Dit.test=...` for
  integration tests), `./gradlew :<module>:test --tests ...`, in risk order and within a
  budget.
  - A changed class selects its tests by name (`FooTest`, `FooTests`, `TestFoo`, `FooIT`...)
    and every test class that uses it, through a static dependency graph (imports, static and
    on-demand imports, same-package names, fully-qualified names, `Class.forName` literals)
    across modules, with "uses changed class X (via Y)" reasons.
  - Safe where it can't see: build files, wrappers, `gradle/`, `.mvn/` and `lombok.config`,
    main resources, Kotlin/Groovy/Scala sources, modules with custom source directories or
    code-generating annotation processors, and builds it can't read run the full suite. Test
    resources run their module's tests. Tests that use a generated class run for any Java
    change. Reflection (Spring scanning, `ServiceLoader`) needs `[[depends]]` rules.
  - `/select` takes an optional `java`; a client resends without it on an older server's
    422. `--no-java-graph` turns the graph off. Triage links Java tests too.
- JS/TS import graph ([#44](https://github.com/PramodBasavarajMenasi/siftwise/issues/44)):
  `siftwise select` (and the Action, GitLab and MCP, which use it) selects Jest test files
  that import a changed file, at any depth, with no Node.js: a tokenizer that skips comments,
  strings, templates, regexes and JSX; resolution through relative paths (extensions, index
  files, TS ESM `.js` → `.ts`), tsconfig/jsconfig `paths` and `baseUrl` with `extends`,
  npm/yarn/pnpm/lerna workspace packages, and a JSON Jest `moduleNameMapper`. Reasons name
  the file the test imports on the way. A changed JSON or other asset a test imports selects
  it too.
  - Safe where it can't see: a test that reaches a `require()`/`import()` of a computed path
    runs for every JS change but never covers one, a change no static import reaches still
    falls back, and a `jest.config.js`/`.ts` (unreadable without Node) is named in the
    reasons. `jsconfig*.json`, `.swcrc`, `swc.config.*`, `.babelrc.*`, `lerna.json` and
    `.yarnrc.yml` now force the full suite like tsconfig and Babel configs.
  - A Jest test file new in the change runs first, by its path, instead of forcing the full
    suite, when the path Jest expects can be told from known Jest tests (the same, a parent
    or a matching project-relative directory); else, and for a new file outside the Jest
    test patterns, the full suite runs as before.
  - `--no-js-imports` turns it off. `/select` takes an optional `js_graph`; a client gets
    an older server's 422 on it and sends the request again without it.
  - Measured on jestjs/jest (2,272 JS/TS files): 1.9 s to build, about 6 MB. No open-source
    Jest repo with replayable JUnit was found (see docs/eval-results.md).
- Time budget ([#43](https://github.com/PramodBasavarajMenasi/siftwise/issues/43)): run the
  highest-risk selected tests whose estimated time fits a target. Off by default; without a budget
  nothing changes. `siftwise select --budget 5m` (s, m or h), the Action's `budget` input
  (outputs `budget-limited`, `budget-left-out`), `SIFTWISE_BUDGET` on GitLab, the MCP `budget`
  argument, `budget_seconds` in `/select`.
  - The budget is an estimated target, not a hard cap: every surface shows the estimated time
    against the target, and runs can take longer (estimates are mean durations).
  - Whole units (pytest arguments, Go packages, Jest files) are taken in risk order while
    their estimate fits; one that doesn't is skipped. Estimates are each test's mean
    duration, an unknown one counts as the 90th percentile of the known ones. The riskiest
    unit always runs.
  - The response says the run is budget-limited (`budget_limited`, `budget`): tests left out,
    their estimated time, their share of the risk score. The PR comment and GitLab note show
    **budget-limited (estimated 4m 50s of a 5m target): N tests left out**; the CLI prints it
    on stderr.
  - A full-suite fallback under a budget packs the full suite's known tests by risk, as a
    "budget-limited full suite". Without history or durations the budget isn't applied.
  - Shadow mode stores what the budget left out: such misses get the cause `budget`, and
    `/recall` and `siftwise report` show budget-limited runs and their misses apart.
  - Migration 0011: `shadow_selections.budget` (nullable) and the `budget` miss cause.
  - A server older than this release rejects `budget_seconds` (422): the Action, GitLab and
    MCP then run the full suite with a warning; `siftwise select --budget` exits 1.
  - Eval: recall and time to first failure under budgets of 10%, 25% and 50% of each run's
    measured time, on rdflib and substrate (docs/eval-results.md).

- Java selection follow-ups ([#54](https://github.com/PramodBasavarajMenasi/siftwise/issues/54)):
  - **Module build files are scoped.** A change to a module's own `pom.xml` or
    `build.gradle[.kts]` runs the tests of that module and of every module that depends on
    it, transitively (the build model now reads Maven artifactId references and Gradle
    `project(':x')`), instead of the full suite. Root build files, settings, version
    catalogs, wrappers, parent/aggregator/BOM POMs, Gradle build files that configure other
    projects, and builds whose dependencies siftwise can't read (`${...}` artifactIds,
    typesafe `projects.x` accessors) still run everything. On dropwizard's 148 PR runs: 5
    more selective (22), 74 of 74 failures still caught, time skipped 10.0% -> 11.9%.
  - **One Maven or Gradle command** for every selected module: `mvn -B -pl a,b -am test
    -Dtest=...` and `gradle :a:test --tests A :b:test --tests B` (checked with real Maven
    and Gradle). A Maven module whose selected class has a fully qualified name another
    module also has keeps its own command: one `-Dtest` list would run the copy too. Maven
    runs one invocation in reactor order.
  - **A Gradle replay**: naver/fixture-monkey (docs/eval-results.md), 3 of 3 failures caught.

- JS/TS dynamic imports with a static directory are bounded
  ([#51](https://github.com/PramodBasavarajMenasi/siftwise/issues/51)): templates and
  concatenations like ``import(`./locales/${lang}.json`)`` or `require('./plugins/' + name)`,
  `import.meta.glob` (Vite) and `require.context` (webpack) become imports of every file
  below that directory that they can match (relative or through tsconfig `paths`), so their
  tests run only when one of those files changes, not for every JS change. Anything siftwise
  can't bound stays as before (no static directory, just `./` or `../`, a bare package,
  outside the checkout, more than 500 matching files). On jestjs/jest, 2 of 40 such files
  became bounded.

- Import depth in the risk order ([#48](https://github.com/PramodBasavarajMenasi/siftwise/issues/48)):
  a test whose own file (Python, Jest, Java) imports changed code now ranks as a direct link
  (6), above tests that reach it only through other files (3). The CLI sends an optional
  `import_depths` in `/select`: the shortest depth per test file and per Go package (Python
  counts a `conftest.py` as one hop; Go uses `go list`'s `Imports`, `TestImports` and
  `XTestImports`). Go packages are never promoted, whatever their depth: Go's graph is per
  package, and most dependents import the changed package themselves, so promoting them
  ranked nearly every importer above the tests a `[[depends]]` rule selects. Order only: the
  selected tests and `--order default` don't change. An older server gets the request again
  without it, and an older client's imports stay indirect.
  Measured on the risk-order replays (docs/eval-results.md, "With import depths"): rdflib (17
  runs) has the same medians and slightly lower means (by test count 12.8% -> 11.3%);
  substrate (19 runs) is unchanged. Promoting Go packages too, as first built, made
  substrate worse by test count (median 23.3% -> 34.4%; 6 runs later, 1 earlier). The eval
  (`run_eval`) sends depths too now; `--no-import-depths` reproduces the earlier replays.

- v0.2 follow-ups ([#46](https://github.com/PramodBasavarajMenasi/siftwise/issues/46)):
  - **Jest without `addFileAttribute`:** jest-junit's default report names no test file, so
    Jest tests couldn't be selected by file or re-run. A suite named by its test file's path
    (jest-junit's `usePathForSuiteName`) now gives the file, and otherwise
    `record` finds the one test file in the checkout whose string literals make up the
    suite's test names and adds it to the report it uploads; re-runs do the same. Two files
    that could both be it, or none, leave the suite without a file, with a warning. The
    search reads the checkout's Jest test files once per command, within 20 seconds (about
    3 s cold for jestjs/jest's 1,362 test files on Linux); past that, the remaining suites get
    no file, with one warning.
  - **JUnit 5 display names in re-runs:** Gradle names tests by their `@DisplayName` (and a
    parameterized invocation by its name alone, `[1] 1`), and so does Surefire's phrased
    reporter, so `rerun-unknown` couldn't re-run them. The method is now read from the class's
    source (`@DisplayName`, `@ParameterizedTest`/`@RepeatedTest` name templates, nested
    classes, phrased class names); a name more than one method may carry isn't re-run, with
    the reason.
  - **Several Go modules:** `go.work` workspaces and `go.mod` files below the root. `go list`
    runs in each module and the graphs merge (a local `replace` or a workspace module links
    tests across modules); `/select` takes an optional `go_modules`, so a nested module's
    package runs as `go test -C <dir> ./pkg` (from the root it failed with "setup failed"),
    and an older server gets the request without it. Go re-runs run in the package's module.
  - **GitLab Tests tab:** `.siftwise-record` now declares `artifacts:reports:junit:
    .siftwise-ci/junit/*.xml`, the directory siftwise's pytest commands write to: a test job
    that only extended it showed no tests in the pipeline's Tests tab. The README says where
    other runners' reports must go.

### Changed

- The light install (`pip install siftwise`) now includes pydantic, because typer 0.29+
  requires it (18 MB installed, was 9 MB). siftwise's own code doesn't import it; typer
  0.29+ does, to parse command options. CI's package check fails if siftwise's modules
  import pydantic or any server module.

### Fixed

- Java: when two modules have a test class with the same fully qualified name, a change to
  one module's test resources ran only the other module's copy (each class was kept under
  one module). Each module's own classes run now
  ([#54](https://github.com/PramodBasavarajMenasi/siftwise/issues/54)).
- JS/TS: `import('./a/' + x)` was read as a literal import of `./a/`, and `import.meta.glob`
  and `require.context` were ignored, so the files they load had no edges and changing one
  didn't select the tests that load it. They are bounded imports now, or dynamic when they
  can't be bounded ([#51](https://github.com/PramodBasavarajMenasi/siftwise/issues/51)).

- `go list` (selection's Go graph) ran with the user's GOFLAGS, so `-mod=mod` (from the
  environment or `go env -w`) could rewrite go.mod or go.sum. It runs with `-mod=readonly`
  now; `-mod=vendor` is kept ([#52](https://github.com/PramodBasavarajMenasi/siftwise/issues/52)).
- A test of a Go module below the repo root was selected with `go test ./dir/pkg` (or its
  import path) run from the root, which Go can't build; its changed files mapped only when
  the module's path mirrored its directory, and `go list` never saw the module, so its
  dependents were missed. See "Several Go modules" above.
- A Surefire report with a class-level result (`<testcase name="" classname="...">`, written
  for a JUnit 5 `@Nested` class skipped or failing as a whole) made `POST /runs` reject the
  whole upload (400). It's recorded as the class's `(class)` result now: every Windows leg of
  dropwizard's CI (781 uploads) was rejected for it.
- A deleted Jest test file was still selected by path, so `jest <deleted file>` failed
  with "No tests found". Its tests are dropped now, as deleted Python test files are.
- The eval header showed `(python)` for `--language java`
  ([#61](https://github.com/PramodBasavarajMenasi/siftwise/issues/61)). It names the language
  that runs now: go, java or python.

## [0.2.0] - 2026-10-27

MCP for more coding agents, GitLab CI support, and `rerun-unknown` beyond pytest. `siftwise
mcp` was tested on Windows with Claude Code, GitHub Copilot in VS Code, Cursor, Codex CLI and
Gemini CLI: `select_tests`, `triage_failure` and `explain` worked in each, and fail-open with
the server down in Cursor ([per-client setup](docs/mcp-clients.md)). No database migration
since 0.1.0.

### Added

- GitLab CI: a template, `gitlab/siftwise.gitlab-ci.yml`, with the hidden jobs
  `.siftwise-select`, `.siftwise-record` and `.siftwise-rollup`, around new
  `siftwise gitlab select|record|rollup` commands. They run the same steps as the GitHub
  Action:
  - selection, with the outputs passed to later jobs as a dotenv report (the command in a
    file);
  - uploads, recording a merge request's source commit, never a merged results commit;
  - verdicts and shadow runs in one sticky merge request note, through the Notes API;
  - `fail-on`, `known_flaky` from the merge request's base commit, and `rerun-unknown`.

  The template pins its siftwise version (`siftwise==0.2.0`). `examples/gitlab` is a project
  to try it on gitlab.com, with the server running inside the pipeline. History stays within a
  pipeline, and a merge request pipeline first records its base commit's full test run as a
  main run.
- Action: `rerun-unknown` re-runs Go, Jest, Maven and Gradle failures, not only pytest's. Each
  failing test gets its own runner, chosen from its ID, its file and the checkout:
  - Go, per package: `-run '^(TestA|TestB)$'`, through gotestsum when it's on PATH, else
    `go test -json` converted to JUnit by siftwise;
  - Jest, per file: `-t` with the anchored full name, and jest-junit reports;
  - Maven (`-Dtest=Class#m1+m2`) and Gradle (`--tests Class.method`), with `./mvnw` and
    `./gradlew` when present.

  `rerun-prefix` applies to pytest and Jest (e.g. `npx`). A test whose runner can't be
  determined isn't re-run, with a warning. The limits and fail-open behaviour are unchanged,
  and a round's reports go up in one upload. Maven and Gradle re-runs leave the CI run's
  `TEST-*.xml` reports as they were: they're backed up before the re-run and put back after
  it, also when it fails or times out.
- MCP: the `repo_path` tool argument and `SIFTWISE_REPO_PATH`, for clients that start the
  server outside the repository. The repository is the first of:
  - `repo_path`;
  - `SIFTWISE_REPO_PATH`;
  - `CLAUDE_PROJECT_DIR` (set by Claude Code);
  - the client's workspace folders over MCP roots (asked only when the client supports roots);
  - the working directory.

  Outside a git repository, every tool says so and how to fix it, instead of
  "set SIFTWISE_REPO". Relative `junit_paths` are read from the repository root.
- MCP: the server reports siftwise's version to clients (it was empty).
- `scripts/mcp_smoke.py`: checks `siftwise mcp` over stdio like a client, against a real
  server, including the fail-open cases.
- Live dogfooding: `.github/workflows/dogfood.yml` runs every siftwise PR and push to main as a
  shadow run.
  - A siftwise server runs inside the job, with a random per-job token, and the history lives
    in the Actions cache. A PR's cache entries stay with the PR.
  - It gates nothing and posts no comment.
  - Each run uploads a summary artifact. `python -m scripts.dogfood_report report` turns the
    summaries into recall, time saved and misses with causes, in total or per week.

### Changed

- MCP: tool schemas work across clients. Every parameter has a description, and optional
  parameters are plain JSON types instead of `anyOf` with null, which Codex's schema converter
  reads as a string ([openai/codex#1975](https://github.com/openai/codex/pull/1975)). Clients
  that send null still work.
- MCP: API calls give up after 30 s (was 60 s), inside Codex's 60 s default tool timeout, so
  the agent gets the fail-open answer instead of a client error.
- Selection: editor and coding-agent settings (`.vscode/`, `.cursor/`, `.idea/`, `.mcp.json`,
  `.codex/`, `.gemini/`, at any depth) are treated as generated files by default, never as
  changed code. An agent's MCP config written mid-change no longer forces the full suite.

### Fixed

- `examples/gitlab/ci/history.py` (also used by the dogfood workflow) now stores the dump's
  migration revision and restores into that schema before migrating on, so a dump from an older
  siftwise loads. Before, it migrated to the newest schema first, and a binary dump with fewer
  columns failed to load. A dump from a newer siftwise is now skipped, and the job starts
  empty.
- MCP: a folder the client sets (`CLAUDE_PROJECT_DIR`, or a root) that isn't in a git
  repository, but holds exactly one directly inside, now means that repository, with a
  `repository_note` in the result. Otherwise the next option is tried instead of failing.
  `repo_path` and `SIFTWISE_REPO_PATH`, which the user sets, still fail when they're not in a
  repository. When nothing works, the error lists the git repositories found one level down.
- MCP: `explain` returned `file_path: null` for pytest tests whose JUnit names no file (e.g.
  `tests.test_cart::test_bulk_discount`). The file now comes from the test ID, through the
  selector's mapping, when it exists in the checkout.

### Security

- Action: the PR comment and job summary show untrusted text (test IDs, file paths in reasons,
  variants, error messages) as code spans, as the GitLab note does. Before, a file name in a
  pull request could add a link, an `@` mention (`pkgs/@scope/...`) or an autolink (`www.`
  names) to the comment, and the selection reason was escaped for table pipes only. Test IDs
  look the same; reasons and variants are now shown as code.
- GitLab:
  - tokens come from environment variables only, and are redacted from every log line, file
    and note;
  - test IDs and reasons appear in the note as code spans, so they can't add links, mentions,
    HTML or quick actions, and no note line can start a quick action;
  - siftwise only edits a note written by its own token's user;
  - forks' pipelines get no tokens, and `fail-on` skips them with a warning;
  - plain-http server URLs outside the job warn.

### Known limits

- The GitLab support is tested with mocks and one example project on gitlab.com (Free);
  Self-Managed instances and Premium features are untested.
- `rerun-unknown` doesn't re-run Go modules below the repo root or multi-module Go
  (`go.work`). It needs the re-run's report to name tests as the CI run did (custom jest-junit
  templates and JUnit 5 display names don't match).
- MCP is tested on Windows only. The Windsurf and Cline configs follow their docs but are
  untested, as are the Codex and Gemini Code Assist VS Code extensions.
- No live recall numbers yet: dogfooding runs since 2026-10-26, and its numbers come with v0.3.
- The server image is linux/amd64 only.

## [0.1.0] - 2026-10-20

The first release: a self-hosted server, a CLI, three GitHub Actions and an MCP server for
test selection and failure triage in CI. Alpha: APIs and the database schema may change before
1.0; migrations upgrade existing databases.

### Recording and history

- `POST /runs` takes JUnit XML from pytest, Jest (jest-junit), Go (go-junit-report,
  gotestsum) and Maven/Gradle, with the commit, branch, changed files and matrix variant.
  Uploads are idempotent per CI run attempt and variant.
- Per-test history over a 90-day window (`SIFTWISE_STATS_WINDOW_DAYS`): runs, failures,
  flaky score, broken on main per variant, average duration.
- `scripts/backfill.py` seeds history from past GitHub Actions runs' JUnit artifacts.

### Test selection

- `siftwise select` diffs the branch and asks the server which tests the change can affect,
  each with a reason, and prints a ready-to-run command (pytest node IDs or files,
  `go test`, `jest`).
- Signals: file-to-test mapping (Python, Go, JS/TS), Go and Python import graphs built in the
  checkout, `[[depends]]` rules and `always_run` globs in `.siftwise.toml`, co-change from
  past failures, recently failed and broken-on-main tests.
- Conservative by design: the full suite runs when the diff is unknown or empty, a build or CI
  file changed, there's no history, a changed file isn't covered by any signal, or a selected
  test has no known runner. Docs, lint-only configs and generated files never force it.

### GitHub Action

- `actions/select`, `actions/record` and `actions/rollup` (composite actions in this repo): a
  sticky PR comment and job summary, matrix support (one rollup after all legs), and
  **fail open**: a siftwise outage never blocks CI.

### Failure triage

- A verdict per failing test: `new_regression`, `pre_existing` (failing on main),
  `flaky` or `unknown`, with a confidence, reasons and evidence, using only history from
  before the run.
- `siftwise triage`, `POST /triage`, and `POST /repos/{repo}/triage` for a recorded CI run
  attempt (all its variants at once). The PR comment lists the failures with their verdicts.
- Opt-in `fail-on` (CLI and Action) fails on chosen verdicts; with it set, unavailable
  verdicts fail closed, except on fork PRs without a token.

### MCP server for coding agents

- `siftwise mcp` (the `siftwise[mcp]` extra): `select_tests`, `triage_failure` and `explain`
  over stdio, for Claude Code, Codex, Cursor and other MCP clients. Uncommitted work counts;
  fails open; the API token never appears in a result.

### Shadow mode and miss causes

- `shadow` on `actions/select` (`always` or a percentage of runs): the full suite runs while
  siftwise stores what it would have selected, then every failure is checked against it.
- Every miss gets a cause: `new_test`, `flaky_history`, `config_gap` (with a suggested
  `[[depends]]` rule), `no_link` or `unknown`.
- `GET /repos/{repo}/recall` and `siftwise report` (`--by week`): recall over all and over
  selective runs, always together, with measured time saved.

### Flaky handling

- `known_flaky` globs in `.siftwise.toml`. For a pull request, the base commit's list
  applies, so a PR can't mark its own failures flaky.
- Same-commit passes (retries, re-run attempts) make a failure flaky with high confidence.
- `rerun-unknown` in `actions/record` re-runs unknown pytest failures (up to 3 rounds, 20
  tests, 10 minutes) and records each re-run.

### Packaging

- `pip install siftwise`: the light CLI and the Action's glue (typer, httpx, defusedxml).
- `pip install 'siftwise[server]'`: the API server, `siftwise server start` and
  `siftwise server migrate`, with the migrations in the package (applied under a Postgres
  advisory lock, so replicas starting together migrate once).
- `pip install 'siftwise[mcp]'`: the MCP server.
- The server image `ghcr.io/pramodbasavarajmenasi/siftwise:0.1.0` (linux/amd64, non-root,
  health check) and a Docker Compose quickstart.

### Known limits

- One API token per server, with read and write access to every repo on it; no per-repo or
  read-only tokens.
- Merge-queue (`merge_group`), scheduled and manual runs always run the full suite (no diff
  base).
- Java tests are recorded but always run in full; Jest selection needs file paths in the
  JUnit and isn't evaluated on a real repo; `rerun-unknown` re-runs pytest only.
- The GitHub Action is tested on Linux runners only.
- Savings depend on the codebase's structure: large across a monorepo's independent packages,
  near zero where everything imports everything (rdflib: 0.6% over 19 main commits).
- The week-4 flaky rule (an unrelated failure that passes on the next run) needs runs recorded
  with change links, which backfilled history doesn't have.
- No live recall numbers are published yet: the evals replay past CI history of four repos
  ([eval results](docs/eval-results.md)).
- The server image is linux/amd64 only.
- Renamed or moved tests start with no history.

[Unreleased]: https://github.com/PramodBasavarajMenasi/siftwise/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/PramodBasavarajMenasi/siftwise/releases/tag/v0.3.0
[0.2.0]: https://github.com/PramodBasavarajMenasi/siftwise/releases/tag/v0.2.0
[0.1.0]: https://github.com/PramodBasavarajMenasi/siftwise/releases/tag/v0.1.0
