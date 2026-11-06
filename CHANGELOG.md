# Changelog

All notable changes to siftwise. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (before 1.0, a minor version may break things).

## [Unreleased]

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

- Import depth in the risk order ([#48](https://github.com/PramodBasavarajMenasi/siftwise/issues/48)):
  a test whose own file imports changed code now ranks as a direct link (6), above tests that
  reach it only through other files (3). The CLI sends an optional `import_depths` in
  `/select`: the shortest depth per Python, Jest and Java test file and per Go package
  (Python counts a `conftest.py` as one hop; Go uses `go list`'s `Imports`, `TestImports` and
  `XTestImports`). Order only: the selected tests and `--order default` don't change. An
  older server gets the request again without it, and an older client's imports stay
  indirect.

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

### Fixed

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

[Unreleased]: https://github.com/PramodBasavarajMenasi/siftwise/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/PramodBasavarajMenasi/siftwise/releases/tag/v0.2.0
[0.1.0]: https://github.com/PramodBasavarajMenasi/siftwise/releases/tag/v0.1.0
