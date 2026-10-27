# Changelog

All notable changes to siftwise. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (before 1.0, a minor version may break things).

## [Unreleased]

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
