# Selector and triage evaluation

Replays of siftwise's test selector against real CI history, to find out how much test time
selection saves and whether it would have missed real failures. The rdflib, Cirq and
substrate numbers are from the current code (re-run on 2026-10-18); kubo's were measured with
the week-2 code (its history isn't in the current database):

- [ipfs/kubo](#ipfskubo): a Go project with about 4,100 known tests.
- [RDFLib/rdflib](#rdflibrdflib): a Python project with about 18,000 known tests. Each CI run
  uploads one JUnit report per matrix leg (operating system × Python version), about 16 legs
  per run.
- [quantumlib/Cirq](#quantumlibcirq): a Python monorepo of nine packages, about 30,000–42,000
  known tests. Savings per package touched; no recall (no PR test results).
- [agent-substrate/substrate](#agent-substratesubstrate): a busy Go repo (one module, ~2,000
  packages) with real failures: recall, with and without a declared rule for its e2e suites.

And of triage's verdicts, on rdflib and substrate: [Triage eval](#triage-eval). Server and
eval speed-ups, with before/after numbers: [Performance](#performance). Live numbers from
siftwise's own CI, in shadow mode: [siftwise on itself](#siftwise-on-itself).

## ipfs/kubo

Measured with the week-2 code: kubo's history isn't in the current database, so these numbers
weren't re-run.

### Summary

- **Every real failure was caught in every round:** 19 of 19 in the final round. In each case the
  run fell back to the full suite. Not one failing run went selective, so recall on a selective
  run with failures is still unmeasured.
- **The Go import graph closed the dependents gap.** Counting only the selector's own signals, with
  the full-suite fallbacks switched off, the failing PRs went from **1 of 9** failures caught
  (path mapping alone) to **9 of 10**. The one remaining miss was protected by a `go.mod`
  fallback in the same change, and co-change caught it when the PR was re-pushed.
- **Savings on kubo are modest:** an estimated **30.7% of test time** across 20 main commits
  replayed as PRs.
  - 13 of the 20 commits ran the full suite, almost all because of dependency or CI file changes.
  - Of the 7 selective commits, 6 were docs-only and skipped everything. The only selective code
    change skipped 14% of test time.
- **One kubo package dominates test time.** `test/cli` runs the built `ipfs` binary. It holds 48%
  of kubo's tests but about 85% of their runtime (11.6 of 13.7 minutes, counting top-level
  tests). Its test harness imports 61 kubo packages, so most code changes reach it through the
  import graph. That's correct, but it means code changes save only 10–20% of time, not 90%.

### Method

Code: `scripts/eval/run_eval.py`. `scripts/eval_kubo.py` is a kubo preset for it.

- **No peeking.** For each target run, the selector's history is rebuilt from raw results stored
  strictly before that run, ordered by run time.
- **What runs.** A failure counts as caught if the selection would have run it. In full mode
  everything runs. A Go package that is run whole runs all its tests, including new ones. A
  `-run` filter on a top-level test also runs that test's subtests.
- **"Without fallbacks"** is a counterfactual. It asks which failures the selector's own signals
  would have caught if the full-suite fallbacks were switched off. The signals are path mapping,
  the Go import graph, declared dependencies, co-change and recently failed. Runs where nothing
  maps on its own are counted separately: there, only a fallback protects the run.
- **Time skipped** is estimated from each test's average duration in history. A full-suite run
  counts as 0%.
- **Import graph.** The Go import graph is computed with `go list -deps -test -json ./...` at
  each target commit.
- **Two modes:**
  - **PR mode** replays the 10 most recent PR runs, plus every PR run that had failures (`--failing`).
  - **`--replay-main`** treats each main-branch run as a PR. Its diff is the previous main commit
    compared with this one, fetched from GitHub.
- **Data:** about 100 backfilled GitHub Actions runs, from 2026-07-29 to 2026-10-01. The first
  backfill stored 107 runs and 432K results. After the backfill was changed to record whole-PR
  diffs and re-run, the final data set had 65 PR runs and 21 main runs, all with known diffs.

### Round 1: path mapping only

Baseline selector: path mapping, co-change, recently failed and always-run. Backfill recorded
each run's diff as head commit versus first parent.

**10 most recent PR runs:** 7 full, 3 selective. 6 of the full runs were for build or CI files,
and 1 for a file that mapped to no tests. The selective runs skipped 99.6% of tests on average.
All 9 failures were caught.

**PR runs with failures:** 6 runs and 22 failures. All 6 ran the full suite, so all 22 failures
were caught.

| Run | What changed | Failed tests | Without fallbacks | Signal that would catch it |
|---|---|---|---|---|
| a41db4a | `go.mod`, `go.sum` (dependabot) | 9 routing tests in `test/cli` | nothing maps; only the fallback protects it | build-file fallback |
| 6badb4a | `config/import.go`, `core/commands/dag/import.go` | 4 × `test/cli::TestCidCommands…` | selects `./config` (260 tests), **misses 4** | dependency on the binary: `test/cli` links both packages |
| ef7060f | `plugin/plugins/wasmipld/wasmipld.go` | the same 4 tests | nothing maps; only the fallback protects it | same |
| 9446212 | `core/core_test.go`, `go.mod`, `go.sum` | `test/cli::TestBackupBootstrapPeers` | selects `./core` (13 tests), **misses 1** | same |
| 5931539 | the same change, re-pushed | the same test | **caught** (15 tests) | co-change: it failed in the previous push |
| d0857a8 | 40 files | 3 × `ondemandpin::…` (new tests) | **misses 3** | none: the package isn't in the recorded diff (a backfill bug, see round 2) |

Without fallbacks, path mapping caught **1 of 9** failures in the runs where it applied. The other
13 failures had only a fallback protecting them.

### Round 2: whole-PR diffs, changed Go packages run whole

Two changes went into this round:
- Backfill now diffs the PR base against the head for `pull_request` runs, and `before...after`
  for pushes.
- A Go package with changed `.go` files runs without a `-run` filter, so new tests in it execute.

| | Round 1 (head vs first parent) | Round 2 (whole PR or push) |
|---|---|---|
| **10 most recent PR runs** | | |
| Full / selective | 7 / 3 | 10 / 0 |
| Full because of build or CI files | 6 | 10 |
| Full because a file mapped to no tests | 1 | 0 |
| Failures caught | 9 / 9 | 9 / 9 |
| **PR runs with failures and a known diff** | 6 runs, 22 / 22 caught | 3 runs, 11 / 11 caught |
| Caught without fallbacks | 1 / 9 | 1 / 2 |

The numbers got less favourable, and that's correct. The old 99.6% came from release-branch runs
whose recorded diff was a single commit (`version.go`). Their real PR diff has 75 to 76 files,
including CI workflows.

The number of failing runs dropped from 6 to 3 for two reasons:
- GitHub hadn't linked the `TestCidCommands` runs to a PR, so their diffs became unknown. The
  selector runs the full suite for an unknown diff, but the eval skips such runs.
- The `ondemandpin` run was no longer among the 100 newest runs on GitHub.

The `-run` change had no effect on this sample, because no failing run went selective.

### Round 3: Go import graph, declared dependencies, PR lookup fallback

Three changes went into this round:
- **PR lookup fallback:** backfill finds a run's PR from the repo's PR list by head branch,
  including closed PRs. Unknown PR diffs went from 18 to 0.
- **Import graph:** `siftwise select` runs `go list` and sends `affected_packages`. Every package
  whose test build imports a changed package is selected and run whole.
- **Declared dependencies:** `.siftwise.toml` `[[depends]]`. kubo's rule
  ([scripts/eval/kubo.siftwise.toml](../scripts/eval/kubo.siftwise.toml)) makes `test/cli` depend on
  every non-test `.go` file.

#### PR mode

| | Round 2 | Round 3 |
|---|---|---|
| 10 most recent PR runs | 10 full | 10 full (all for build or CI file changes) |
| Failing PR runs with a known diff | 3 runs, 11 failures, all caught | 5 runs, 19 failures, all caught |
| **Caught without fallbacks** | **1 of 2** | **9 of 10** |

| Run | What changed | Failed tests | Without fallbacks (import graph + `.siftwise.toml`) |
|---|---|---|---|
| a41db4a | `go.mod`, `go.sum` | 9 routing tests | nothing maps; only the fallback protects it |
| 6badb4a | 11 files incl. `config/import.go`, `core/commands/dag/import.go`, `go.mod` | 4 × `TestCidCommands…` | 4,020 tests, **catches 4 / 4** (go imports) |
| ef7060f | 9 files incl. `plugin/loader/loader.go`, `go.mod` | the same 4 tests | 3,385 tests, **catches 4 / 4** (go imports) |
| 5931539 | `core/core_test.go`, `go.mod`, `go.sum` | `TestBackupBootstrapPeers` | 15 tests, **catches 1 / 1** (co-change) |
| 9446212 | the same change, first push | the same test | 13 tests, **misses 1** |

#### The import-graph improvement

Counting only the selector's own signals, failures caught went from **1 of 9** with path mapping
alone (round 1) to **9 of 10** (round 3).

- **The import graph caught the `TestCidCommands` failures.** In round 1 they were the main
  example of the dependents gap. `test/cli`'s test build imports 61 kubo packages through its
  harness, so a change to `core/commands/dag` or the plugin loader reaches it through imports.
- **The declared rule added almost nothing for kubo.** With and without `kubo.siftwise.toml`, PR mode
  caught the same failures: 9 of 10. The rule only made selections slightly larger: 4,020 vs
  4,018 tests on 6badb4a, and 3,385 vs 3,237 on ef7060f. In the main replay it applied to one
  run (ceafeba): 1,865 → 2,013 tests and 19% → 14% of time skipped. That moved the overall
  estimate from 31.0% to 30.7%. It stays useful as a safety net for effects that pass through
  the binary.

#### Main replay (`--replay-main`, 20 main commits replayed as PRs)

| | |
|---|---|
| Full / selective | 13 (65%) / 7 (35%) |
| Full because of build or CI files | 9 (8 bumped `go.mod`/`go.sum`; 1 changed only a workflow file) |
| Full because a changed file mapped to no known tests | 3 (`fuse/*_test.go`, `test/sharness/t0025-datastores.sh`, `test/dependencies/pollEndpoint/main.go`) |
| Full because the diff was empty | 1 |
| Skipped in selective runs | 93.0% of tests, 87.7% of time on average |
| **Estimated time skipped across all runs** | **30.7%** (full runs count as 0%) |
| Signals used by selective runs | path mapping 1, go imports 1, declared 1 |
| Failures | none: main had no failing runs in this window |

- **Docs-only commits:** 6 of the 7 selective runs had no affected tests and skipped 100%.
- **The one code change** (ceafeba) selected 2,013 of 4,133 tests. That skipped 51% of tests but
  only 14% of time, because the selection included `test/cli`.
- **Nested build files:** a later change scoped nested build files such as
  `docs/examples/kubo-as-a-library/go.mod` to their own directory. The split stayed 13 / 7,
  because every commit that bumped the nested `go.mod` also bumped the root one.

### Misses

Every counterfactual miss across the rounds, and the signal that catches it:

| Failure | Change | Status now | Signal |
|---|---|---|---|
| 4 × `test/cli::TestCidCommands…` (6badb4a, ef7060f) | `config`, `core/commands/dag`, plugin loader | **caught** | Go import graph (round 3) |
| `test/cli::TestBackupBootstrapPeers` (9446212) | `core/core_test.go` + `go.mod` | **missed** without fallbacks | The `go.mod` build-file fallback in the same change already runs the full suite. The only code change is a test file in `./core`, which `test/cli` doesn't import, and the declared rule excludes `*_test.go`. Co-change caught it on the re-push (5931539). |
| 3 × `ondemandpin::…` (d0857a8) | 40 files, multi-commit PR | fixed in the data | A backfill bug, not a selector gap: the package was added earlier in the same PR, and only the head commit's diff was recorded. Whole-PR diffs (round 2) fix it. |
| 9 routing tests in `test/cli` (a41db4a) | `go.mod`, `go.sum` only | caught | The build-file fallback. No selector signal applies to a dependency-only bump, and the full suite is the right answer. |

### Findings

1. **Fallbacks did all the protecting.** Every failing run fell back to the full suite: the
   conservative rules work, but they also keep savings low. On kubo, 8 of the 13 full-suite runs in
   the main replay came from `go.mod`/`go.sum` bumps.
2. **Path mapping alone isn't safe for Go.** It caught 1 of 9 failures in round 1, because the
   failures were in packages that depend on the change. With the import graph it's 9 of 10, and
   the remaining miss was covered by a fallback.
3. **Binary-level suites dominate.** `test/cli` is about 85% of kubo's test time and is reachable
   from most packages. Realistic savings on kubo come from docs, version-only and narrow unit
   changes.
4. **Backfill diffs must match what `siftwise select` sends.** With head-vs-first-parent diffs, PRs
   looked smaller than they were. That inflated the savings (99.6%) and created false misses
   (`ondemandpin`).
5. **Co-change only helps after a first miss.** It caught `TestBackupBootstrapPeers` on the
   re-push, not the first time, so it can't be the safety net.
6. **Not measured:**
   - recall on a selective run that has failures (none occurred);
   - recall in the main replay (main had no failures in the window).

   The rdflib evaluation is meant to cover both.

### Reproducing

This needs a kubo clone, Go, a siftwise server with kubo backfilled, and `GITHUB_TOKEN` (for
`--replay-main`).

```sh
uv run python scripts/eval_kubo.py --failing --siftwise-toml scripts/eval/kubo.siftwise.toml \
    --kubo-checkout ../kubo --go go
uv run python scripts/eval_kubo.py --replay-main --siftwise-toml scripts/eval/kubo.siftwise.toml \
    --kubo-checkout ../kubo --go go
```

GitHub compares and import graphs are cached in `.eval-cache/`.

## RDFLib/rdflib

### Summary

- **Final: 2,049 of 2,050 failures caught** in the 39 failing PR runs, 140 of 141 in the 18
  runs that went selective, plus 1 of 1 in the main replay. The one miss is
  `test_service_with_bound_solutions`, a remote-service test that also failed in 2 earlier
  unrelated runs: flaky, for week-4 flaky detection to handle. It failed in a PR that only
  changed `.pre-commit-config.yaml`, which no longer forces the full suite. Round 1 (path
  mapping only) caught 1 of 7 in selective runs.
- **What closed the gaps:** a static Python import graph from the CLI (including parent
  packages, conftests and module names in strings), whole-file runs for changed or new test
  files, doctest modules, and running changed modules under `--doctest-modules` when the repo
  imports source modules. See [Round 2](#round-2-python-import-graph-whole-test-files-doctests).
- **But rdflib saves almost nothing.** Its package `__init__.py` and plugin registry connect
  nearly every module to nearly every test file, so a selective run still runs about 99% of
  test time. Estimated time skipped across all runs fell from 10–19% (round 1, with misses)
  to 0.2–0.6%. Since lint and format-only configs stopped forcing the full suite, two
  pre-commit-only PRs skip everything: on the 10 most recent PRs that's **0.4% → 10.4%**.
- **`/select` takes 0.3–1.4 s**, down from 79–124 s, after the history and collection-error
  fixes below.

### Data and method

- 190 CI runs (20 on main) from 2026-07-12 to 2026-10-07: 2,959 uploads (one per matrix leg),
  29.2M results, 40,145 distinct test IDs, about 18,000 of them known at any one time.
- Same method as for kubo, with two differences:
  - **Python import graph.** In round 1, selection used path mapping, co-change and recently
    failed, and a static import graph at each commit only explained misses. In round 2 the
    graph is computed at each target commit and passed to the selector, as `siftwise select`
    does.
  - **History is loaded once.** The eval reads the repo's raw results into memory once (about
    100 s) and rebuilds each target's history from that. PR mode took 7.7 minutes for 49
    targets, and the main replay 2.3 minutes.
- No `.siftwise.toml` (no declared dependencies or always-run patterns).

### Fixes made before these numbers

A first attempt showed that `/select` took 79–124 s on rdflib, and that every code change went
full. Three fixes came first:

| Problem | Fix |
|---|---|
| Building history scanned raw results on every call: 73 s for known tests (about 8M rows: 50 CI runs × 16 legs × about 9,900 results) and 36 s for co-change failures | Known tests are read from `test_stats.last_seen_run_id`, which the rollup keeps. Failures are read through a partial index on failed results (2.4 MB for 13,329 rows). `load_history` never reads passing raw results; a test checks this with `EXPLAIN ANALYZE`. |
| pytest collection errors (`pytest::rdflib.plugins.serializers.n3`, a module that failed to import) had no runner, and co-change selected them in almost every code change, forcing the full suite | They map to the module's file (`rdflib/plugins/serializers/n3.py`), which pytest runs whole. If no file can be derived, they're left out of the command instead of forcing the full suite. |
| The per-upload rollup sometimes chose a nested loop that re-read the upload's results for every result in the window (about 40 s per upload on 120 uploads × 150 tests) | The upload's test list is materialized once. |

`POST /select`, timed over HTTP on the same data:

| Change | Before | After | Result after |
|---|---|---|---|
| Source: `rdflib/plugins/sparql/parser.py` | 78.9 s, full (collection error) | 1.36 s (first call) | selective, 347 tests |
| Test: `test/test_sparql/test_agg_distinct.py` | 94.1 s, full (collection error) | 0.33 s | selective, 14 tests |
| Docs: `docs/index.md` | 123.7 s | 0.34 s | selective, 1 test |

After the fix, loading history takes 0.11–0.85 s and selecting about 0.1 s. Every history query
takes 0.05 s or less; the rest is Python building 18,000 known-test entries. The "before" times
were measured while an eval was loading the same database, so they are inflated.

### Round 1: PR mode (path mapping only)

| | 10 most recent PR runs | 39 PR runs with failures |
|---|---|---|
| Full / selective | 8 / 2 | 35 / 4 |
| Full: changed file maps to no known tests | 3 | 21 |
| Full: build or CI file changed | 4 | 14 |
| Full: selected test has no runner | 1 (a doctest, see findings) | 0 |
| Time skipped in selective runs (average) | 96.7% | 99.0% |
| **Estimated time skipped, all runs** | **19.3%** | **10.2%** |
| Failures caught | none to catch | 2,044 / 2,050 |
| Caught in selective runs | — | **1 / 7** |
| **Caught without fallbacks** (full runs) | — | **1,160 / 1,954**, plus 89 failures that only a fallback protected |

**Selective runs with failures**

| Run | Change | Selected | Failures | Missed, and the signal that would catch it |
|---|---|---|---|---|
| 7d6a498 `undef-enum` | `rdflib/plugins/shared/jsonld/context.py` | 381 | 1 | `test_sparql.test_service::test_service`: flaky / recurring. It also failed in an earlier unrelated run, and it calls a remote SPARQL service. |
| 1e924cc `fix/uri-control-character-validation` | `rdflib/term.py`, `test/test_literal/test_term.py` | 8 | 4 | All 4: Python imports. `test_aggregate_graphs`, `test_namespacemanager` and two `test_store_sparqlstore` tests import `rdflib/term.py`, directly or through `sparqlstore.py`. |
| 81c247a `history` | 13 files, mostly docs | 26 | 1 | none: caught |
| 9839265 `history` | the same change, re-pushed | 25 | 1 | `test_service::test_service_with_values`: Python imports (through `rdflib/store.py`); probably also flaky, like `test_service`. |

**Without fallbacks** (what the selector's own signals catch in the runs that went full):

| Signal that would catch the miss | Failures | Where most of them come from |
|---|---|---|
| Python imports | 685 | `upgrade-mypy` (343 failures, 41 files changed) and `fix/dataset-length-count` (311 failures from a 4-file change that selects 21 tests) |
| New test (not in history yet) | 106 | `complete-uv` 51, `owlrl` 21 + 13 + 1, `upgrade-mypy-v3` 12, `longturtle-default` 8 |
| Flaky / recurring | 1 | |
| Python imports, but no graph for that test | 1 | |
| None: looks unrelated to the change | 1 | |

### Round 1: main replay (`--replay-main`, 19 main commits replayed as PRs)

| | |
|---|---|
| Full / selective | 17 (89%) / 2 (11%) |
| Full: changed file maps to no known tests | 9 |
| Full: build or CI file changed | 7 |
| Full: selected test has no runner (a doctest) | 1 |
| Time skipped in selective runs (average) | 94.0% |
| **Estimated time skipped, all runs** | **9.9%** |
| Failures | 1 in 1 run, caught by the full-suite fallback. Without fallbacks, nothing maps: `rdflib/plugins/serializers/nt.py` has no test named after it. |

### Round 1 findings

Items 1–3 are addressed in round 2.


1. **Python needs an import graph, as Go did.** Path mapping by file name misses most of
   rdflib's dependencies. It forces the full suite for 33 of the 60 full runs here, and when it
   does select, it missed 5 of 7 failures. Imports are the signal for 685 of the 794
   counterfactual misses and 5 of the 6 real ones. Like `go list` for kubo, the CLI could
   compute the importers of each changed module, using `src/siftwise/cli/pygraph.py` as a
   starting point. That should both catch these misses and turn many full runs selective.
2. **Tests new in the PR are never run by a selective Python run.** Commands name only known
   pytest node IDs, so a test added in the change isn't run, even when its file changed (106
   counterfactual misses). Running changed test files whole, as changed Go packages already
   are, would cover the ones in changed files.
3. **Doctests have no runner.** IDs such as `rdflib.container::rdflib.container.Container`
   (classname = a source module) can't be mapped to a command, so selecting one forces the full
   suite (1 recent PR, 1 main commit). They could map to `pytest rdflib/container.py`, like
   collection errors now do.
4. **Flaky tests are now measurable.** `test_service` failed in a selective run and in an
   earlier unrelated run, and it depends on a remote service. The week-4 flaky signal would
   label it rather than count it as a miss.
5. **Matrix repos multiply history size.** About 16 uploads per CI run made every raw-results
   query 16× bigger than on kubo. `/select` now avoids raw results entirely. The batch rollup
   still reads the whole 90-day window: about 16 minutes and up to 15 GB of Postgres temp
   files for 29M results.

### Round 2: Python import graph, whole test files, doctests

**Changes**

- **Python import graph.** `siftwise select` builds a static import graph of the checkout with
  `ast` (`siftwise.cli.pygraph`), like `go list` for Go. It includes parent packages'
  `__init__.py` and the `conftest.py` files pytest loads. It sends every test file that imports
  a changed module, directly or transitively, as `affected_files`. The selector runs those
  files whole, with the reason `imports changed module X`, and a changed module imported by a
  known test file no longer forces the full suite. If the graph can't be built, nothing is
  sent and the fallbacks apply.
- **Module names in strings count as imports.** rdflib registers its parsers and serializers
  by module path (`register("nt", Serializer, "rdflib.plugins.serializers.nt", ...)`), so the
  first version of the graph didn't connect them to the tests that use them. A string literal
  that is exactly the dotted name of a module in the repo is now an import edge.
- **Changed or added test files run whole** (`pytest test/test_x.py`), so tests new in them
  run. The CLI also sends deleted paths, so a deleted test file is never named in a command.
- **Doctests** (`rdflib.container::rdflib.container.Container`) run as `pytest
  --doctest-modules rdflib/container.py`. If no file can be derived, they're left out of the
  command instead of forcing the full suite.
- **Changed modules run under `--doctest-modules`** when history shows the repo's pytest
  imports source modules (doctests, or collection errors in non-test modules). This catches a
  new module that fails to import. pytest exits 5 for a module with no doctests, so that
  command accepts exit 5; import errors (exit 2) and failing doctests still fail.

**Before and after**

| | Round 1 (path mapping) | Graph, first version | + module names in strings | + new modules' collection errors | **+ lint-only configs (final)** |
|---|---|---|---|---|---|
| **39 PR runs with failures** | | | | | |
| Full / selective | 35 / 4 | 24 / 15 | 22 / 17 | 22 / 17 | **21 / 18** |
| Failures caught | 2,044 / 2,050 | 1,991 / 2,050 | 2,037 / 2,050 | 2,050 / 2,050 | **2,049 / 2,050** |
| **Caught in selective runs** | 1 / 7 | 58 / 117 | 127 / 140 | 140 / 140 | **140 / 141** |
| Caught without fallbacks (full runs) | 1,160 / 1,954 | 1,873 / 1,931 | 1,864 / 1,908 | 1,880 / 1,908 | **1,880 / 1,908** |
| Estimated time skipped, all runs | 10.2% | 5.0% | 0.2% | 0.2% | **2.8%** |
| **10 most recent PR runs** | | | | | |
| Full / selective | 8 / 2 | 6 / 4 | 5 / 5 | 5 / 5 | **4 / 6** |
| Estimated time skipped, all runs | 19.3% | 0.3% | 0.4% | 0.4% | **10.4%** |
| **Main replay (19 commits)** | | | | | |
| Full / selective | 17 / 2 | 8 / 11 | 7 / 12 | 7 / 12 | **7 / 12** |
| Failures caught (in selective runs) | 1 / 1 (by the fallback) | 1 / 1 (1 / 1) | 1 / 1 (1 / 1) | 1 / 1 (1 / 1) | **1 / 1 (1 / 1)** |
| Estimated time skipped, all runs | 9.9% | 25.8% | 0.6% | 0.6% | **0.6%** |

What each step fixed:

- **Round 1 → first graph:** catches the 5 import misses after `rdflib/term.py` and
  `rdflib/store.py` changes.
- **First graph → module names in strings:** catches the 58 failures of `string-n-serialization`
  (round-trip, serializer and W3C N-Triples/N-Quads tests after an `nt`/`nquads` serializer
  change), which reach the serializers only through `rdflib/plugin.py`. It also selects
  `test_service`, the recurring network test, after a JSON-LD context change, which it reaches
  through the same registry.
- **→ new modules' collection errors:** catches the 13 failures of an `owlrl` run that added
  the new package `rdflib/plugins/inference/`, whose modules failed to import
  (`pytest::rdflib.plugins.inference.closure`, ...).
- **→ lint-only configs (final):** `.pre-commit-config.yaml` and other lint/format-only configs
  no longer force the full suite. Two `pre-commit-ci-update-config` PRs that changed only that
  file now run one recently failed test (run 287) or nothing (run 999). Run 999 had one
  failure, now missed: `test_service_with_bound_solutions`, a remote-service test that also failed in 2 earlier unrelated runs. It's flaky, not caused by the change; week-4 flaky
  detection should label it rather than count it as a miss.

**Remaining full-suite reasons** (7 failing PR runs): files that no known test reaches. These
are `examples/datasets.py` and new modules that nothing imports yet (`rdflib/plugins/serializers/longturtle.py`, a new `rdflib/inference/`
package). Without fallbacks, 28 failures would be missed: all are collection errors of modules the
run didn't change, in runs that went full anyway.

**Findings**

1. **One miss in selective runs, a flaky one; almost no savings on code changes.** The miss
   is a remote-service test that fails intermittently (see above). `rdflib/__init__.py`
   imports most of the library, and `rdflib/plugin.py` names every plugin. So a change to
   almost any module reaches 325 of 326 test files: selective runs skip about 1% of test
   time. That's correct for this codebase: statically, nearly every test can reach nearly
   every module. The 25.8% of the first graph version came from missing the plugin edges, and
   it missed 58 failures.
2. **File-level selection can't separate rdflib's tests.** Savings here would need a finer
   signal, such as per-test coverage (which test executes which module), not a better import
   graph.
3. **The safety mechanisms are general.** Import graph with parent packages and conftests,
   module-name strings, whole test files, doctest modules and deleted files apply to any
   Python repo. A repo with less central `__init__` imports should see real savings; that
   needs a second Python repo to measure.
4. **Costs.** Building the graph takes about 5 s for rdflib's 538 files, and finding affected
   files is instant. Eval runs: PR mode 8 minutes, main replay 2 minutes.

### Reproducing

This needs an rdflib clone, a siftwise server with rdflib backfilled and rolled up (`POST
/repos/RDFLib/rdflib/rollup`), and `GITHUB_TOKEN` for `--replay-main`.

```sh
uv run python -m scripts.eval.run_eval --repo RDFLib/rdflib --failing --checkout ../rdflib
uv run python -m scripts.eval.run_eval --repo RDFLib/rdflib --replay-main --checkout ../rdflib
```

## quantumlib/Cirq

### Summary

- **The monorepo case: savings depend on which package a PR touches.** Over 134 merged PRs,
  93 went selective and the estimated time skipped was **37.3%** (full runs count as 0%).
  PRs that changed only `cirq-google` skipped 80% of test time; PRs in `cirq-core`, which every
  other package imports, skipped 19%.
- **Re-run with the current code (2026-10-18):** the first run's window extended past the end
  of the recorded history; this re-run uses the 132 PRs merged by 7 October (the 90 days from
  2026-07-09 to 2026-10-07): **37.9%** skipped, 91 selective; 79.8% for `cirq-google`-only PRs
  (21), 20.2% for `cirq-core`-only PRs (46).
- **Recall can't be measured here.** Cirq uploads JUnit only from a push-to-main monitoring
  workflow: there are no PR test results, and main had no failing run in the window. These
  numbers measure selection and savings, not safety.
- **Two monorepo bugs were found and fixed first.** Before the fixes, every Cirq code PR fell
  back to the full suite, and the first "fixed" run claimed 59.8% saved with a nearly empty import
  graph, which would have been unsafe. See [Bugs found](#bugs-found-by-this-eval).

### Data and method

- 132 main CI runs (`ci-monitoring.yml`, one `pytest-ubuntu-report` artifact each) from
  2026-07-14 to 2026-10-07: 3.1M results, about 23,000 per run. 70,850 distinct test IDs, because
  parametrized IDs change from run to run; about 30,000–42,000 are known at a time.
- Nine packages (`cirq-core`, `cirq-google`, `cirq-ionq`, `cirq-aqt`, `cirq-pasqal`,
  `cirq-web`, ...), each with its own `setup.py`, tested by one `pytest` run from the repo root.
- **`--replay-prs`:** every PR merged in the last 90 days (134), whether or not CI recorded it.
  The diff is the PR's base...head from the GitHub compare API, as `siftwise select` would send;
  history is cut at the PR head commit's time (no peeking). The Python import graph is built at
  each PR head from the commit's zipball (`--source github`, no git checkout).
- Test time comes from the main runs' average durations.

### Results

| | |
|---|---|
| Merged PRs | 134 |
| Full / selective | 41 (31%) / 93 (69%) |
| Full: a changed file maps to no known tests | 19 |
| Full: build or CI file changed | 20 |
| Full: unknown diff / no history yet | 1 / 1 |
| Time skipped in selective runs (average) | 53.8% |
| **Estimated time skipped, all PRs** | **37.3%** |
| Signal that selected the most tests, per PR | Python imports 68, path mapping 12, nothing affected 13, full suite 41 |
| Failures | none to catch (no PR results; main never failed in the window) |

**By the area a PR changed:**

| Python changed in | PRs | Full / selective | Time skipped | Main signal |
|---|---|---|---|---|
| `cirq-core` only | 50 | 9 / 41 | 18.6% | Python imports |
| no Python file (docs, CI, notebooks) | 33 | 20 / 13 | 39.4% | none affected, or a build-file fallback |
| `cirq-google` only | 22 | 3 / 19 | 80.4% | Python imports |
| several packages | 18 | 5 / 13 | 17.0% | Python imports |
| outside the packages (`dev_tools`, `docs`) | 5 | 3 / 2 | 40.0% | path mapping |
| `cirq-web` only | 3 | 1 / 2 | 66.0% | path mapping |
| `cirq-pasqal` only | 3 | 0 / 3 | 99.8% | Python imports |

`cirq-core` behaves like rdflib: `cirq/__init__.py` imports most of the library, so a change
to almost any core module reaches most test files (477 for `cirq/ops/raw_types.py`). The other packages depend on
`cirq-core` but not on each other, so their changes stay inside their own tests: a
`cirq-google` engine change reaches 88 test files, a `cirq-ionq` service change 13.

### Bugs found by this eval

| Bug | Effect on Cirq | Fix |
|---|---|---|
| pytest classnames from a monorepo root start with the package directory (`cirq-google.cirq_google.ops.lzs_reset_test`), and `cirq-google` isn't a Python identifier, so the selector gave those tests no runner and no file | every code PR fell back to the full suite ("changed file maps to no known tests") | directories above the test module may contain `-`; the module and class parts must still be Python names |
| The import graph named modules only by their path from the repo root (`cirq-core.cirq.ops.raw_types`), so `import cirq` resolved to nothing: 213 edges for 1,210 files | the replay looked like **59.8% saved**, but with path mapping alone: a change to `raw_types.py` reached 0 test files. Unsafe | each directory with `pyproject.toml`, `setup.py` or `setup.cfg` is a package root; modules are also named from there (and its `src/`); an ambiguous name links to every match. Now ~5,150 edges; `raw_types.py` reaches 477 test files |

The 59.8% run is kept only as this example: it's what selection without a working import graph
would have claimed.

### Limits

- No recall: no PR results and no failures on main. A selective PR that broke a test would go
  unnoticed by this eval.
- History is cut at the PR head commit's committer time, which a rebase can move.
- Test-ID churn from parametrization makes "known tests" vary between runs.

## agent-substrate/substrate

### Summary

- **Recall on a busy Go repo:** of 2,044 failures in 72 failing PR runs, 2,032 were caught;
  but only **26 of 38 in the runs that went selective**. Main commits replayed as PRs: 1,308 of
  1,308, and 33 of 33 in selective runs.
- **The misses are binary-level e2e suites and flaky tests.** `internal/e2e/suites/*` deploy
  the whole system and reach it through no Go import. With one declared rule (as for kubo),
  selective runs catch **36 of 39**, and savings drop: **5.8%** of test time on PR runs and 15.1%
  on main commits, against 19.5% and 31.7% without it. A second rule for `functionaltest`
  suites brings it to **38 of 39** (5.1% of PR test time skipped). With both rules, the 40
  main commits replayed as PRs: 1,308 of 1,308 caught (33 of 33 in selective runs), **13.9%**
  of test time skipped.
- **Not an independent-packages monorepo:** one Go module with about 2,000 packages, and PRs
  often touch several. Savings come from narrow changes, not from package boundaries.
- **Only 8 days of history.** Its artifacts expire after about two weeks; the backfill got
  2026-10-04 to 2026-10-11.

### Data and method

- 798 CI runs of `pr-workflow.yaml` (1,674 uploads: variants `unit`, `e2e-envoy`,
  `e2e-agentgateway`, `e2e-envoy-preview`), 6.4M results, 18,614 test IDs; 7,000–14,000 known
  tests at a time.
- **PR mode:** the 10 most recent PR runs and every PR run with failures (72; 80 targets in all).
  **Main replay:** the 40 most recent main commits, each diffed against the previous one.
- The Go import graph is `go list -deps -test -json ./...` at each target commit, from its
  zipball (`--source github`, Go 1.27.2, `GOOS=linux`). All 120 targets had a graph.
- Two configurations: no `.siftwise.toml`, and
  [scripts/eval/substrate.siftwise.toml](../scripts/eval/substrate.siftwise.toml), which makes
  `internal/e2e/**` depend on every non-test `.go` file.

### Results

| | No declared rule | **With the e2e rule** |
|---|---|---|
| **PR mode (80 runs)** | | |
| Full / selective | 61 / 19 | 60 / 20 |
| Failures caught | 2,032 / 2,044 | **2,041 / 2,044** |
| Caught in selective runs | 26 / 38 | **36 / 39** |
| Caught without fallbacks | 1,804 / 2,004 | 1,823 / 2,003 |
| Estimated time skipped, all runs | 19.5% | 5.8% |
| Time skipped in selective runs (average) | 82.3% | 23.2% |
| **Main replay (40 commits)** | | |
| Full / selective | 24 / 16 | 23 / 17 |
| Failures caught (in selective runs) | 1,308 / 1,308 (33 / 33) | 1,308 / 1,308 (33 / 33) |
| Estimated time skipped, all runs | 31.7% | 15.1% |

The 10 most recent PR runs all went full: 4 for build or CI files, 6 for a file that maps to no
known tests (Python benchmarking scripts and other non-Go files). Full runs in general: 36–37
for an unmapped file, 24 for build or CI files.

**Misses in selective runs:**

| Failure | Change | Without the rule | With the rule |
|---|---|---|---|
| `e2e/suites/egressmitm::TestActorEgressMITMTrust` (3 runs) | `internal/atunnel`, `internal/ateomnet/dns`, benchmarking fakes | missed; failed in earlier unrelated runs too, calls external hosts: flaky | caught (runs on every Go change) |
| `e2e/suites/egresscredinject::TestActorEgressCredentialInjection` (2 runs) | `internal/atunnel`, `cmd/atecontroller` | missed; recurring, calls `httpbin.org`: flaky | caught |
| `e2e/suites/demo::TestMultipleDurableDirLifecycle` | `cmd/atenet/...`, `internal/atunnel` | missed: the suite drives the deployed system | caught |
| `controlapi/functionaltest::TestResumeActor_AteletTransport...` (2) | `cmd/atecontroller` | missed: drives ateapi and atelet, no import edge | **missed**; caught by the `functionaltest` rule (below) |
| `internal/credbundle::TestLoaderConcurrentHandshakes` | `internal/ateomnet` | missed: imports nothing changed; a concurrency test, probably flaky | **missed** |

**Adding a `functionaltest` rule** (a second `[[depends]]`: `**/functionaltest/**` on every
non-test Go file, now in the same config), re-run on the same 80 PR targets with today's code:

| | e2e rule only | e2e + functionaltest rules |
|---|---|---|
| Caught in selective runs | 36 / 39 | **38 / 39** |
| Failures caught | 2,041 / 2,044 | **2,043 / 2,044** |
| Estimated time skipped, all runs | 6.4% | 5.1% |
| Time skipped in selective runs (average) | 23.2% | 18.7% |
| Full / selective | 60 / 20 | 60 / 20 |

It catches both `TestResumeActor_AteletTransportErrorLeavesActorResuming` failures (the test
and its `caller_deadline` subtest) at `79a94f1`, a change to `cmd/atecontroller/main.go` and
`workersync`. The one miss left is `internal/credbundle::TestLoaderConcurrentHandshakes`
(`dd7d5a3`), a concurrency test with no link to the change. The e2e-only figure is 6.4% here
against 5.8% in the first run: the selector changed in between, the targets didn't.

### Findings

1. **Go import graphs aren't enough when tests drive deployed binaries.** As with kubo's
   `test/cli`, e2e suites need a declared dependency. With it they run on almost every change,
   which costs most of the savings: e2e is a large share of substrate's test time.
2. **Flaky e2e tests dominate the remaining risk.** Most misses without the rule failed in
   earlier unrelated runs. Week-4 flaky detection should label them instead of counting them
   as misses.
3. **Fallbacks protect, as before.** Every miss was in a selective run; every full run caught
   everything.

### Limits

- 8 days of history: co-change and recently-failed signals see little.
- Both rules are coarse (any non-test Go file): they run e2e and functional suites on almost
  every Go change.
- The eval's Go graphs come from zipballs on Windows with `GOOS=linux`. The first attempt
  shared one unpack directory between two evals and corrupted 22 graphs; all were rebuilt. The
  7 targets that had run without a graph gave the same result with one (all were full-suite
  runs for build or unmapped files).

### Reproducing

Needs Go, a siftwise server with the repo backfilled, and `GITHUB_TOKEN`. No git checkout:
`--source github` fetches each commit's zipball.

```sh
uv run python scripts/backfill.py --repo quantumlib/Cirq --workflow ci-monitoring.yml \
    --artifact-pattern 'pytest-*-report' --batch --workers 4
uv run python -m scripts.eval.run_eval --repo quantumlib/Cirq --replay-prs --source github \
    --language python --resume cirq.jsonl

uv run python scripts/backfill.py --repo agent-substrate/substrate --workflow pr-workflow.yaml \
    --artifact-pattern 'test-results-*' --max-runs 3000 --batch --workers 8
uv run python -m scripts.eval.run_eval --repo agent-substrate/substrate --language go \
    --go-module github.com/agent-substrate/substrate --source github --goos linux \
    --siftwise-toml scripts/eval/substrate.siftwise.toml --failing --resume substrate-pr.jsonl
uv run python -m scripts.eval.run_eval ... --replay-main --last 40 --resume substrate-main.jsonl
```

## Triage eval

How accurate are triage's verdicts (`new_regression`, `pre_existing`, `flaky`, `unknown`) on
real history? Every recorded run with failures was triaged as of just before it, then scored
against proxy labels taken from what happened afterwards.

### Summary

- **When triage says `new_regression`, it held up.** On rdflib, **10 of 11 fix episodes with a
  known diff** got `new_regression`; the 11th is a known-flaky remote test where triage said
  `unknown` and the label is probably wrong. On substrate, all 319 `new_regression` verdicts on
  labelled failures were regressions.
- **`pre_existing` is the strongest result:** 2,092 of 2,092 on substrate, including 759 called
  at medium confidence after a *single* failing main run and confirmed by the main runs after it.
- **Flaky detection mostly re-reads re-runs.** 678 of substrate's 683 `flaky` verdicts come
  from a pass of the same commit before the failure, the same fact the flaky label uses, so
  that agreement isn't an independent test. Only 8 `flaky` verdicts came from history alone; the
  5 that could be labelled were labelled regressions.
- **Regressions called flaky (the worst error): 0 on rdflib, 5 on substrate**, and the 5 look
  like label noise: each test had same-commit flakes on record, and the 4 on branches were
  "fixed" by diffs of 100+ files that had merged `main` (see the substrate section).
- **Unknown when unsure:** 12.6% of substrate's failures and 14% of rdflib's got `unknown`,
  mostly runs whose diff was never recorded (rule 3), as designed.
- **Blind spot:** on a repo where every change links to every test (rdflib), flaky tests look
  like regressions unless CI re-runs them. rdflib's `test_service*` tests, which call a remote
  SPARQL service, were called `new_regression` 12 times out of 13.

### Method

Code: `scripts/eval/run_eval.py --triage`, labels in `scripts/eval/triage_labels.py`.

- **No peeking.** Verdicts come from the production engine (`core.triage.triage_run`), cut off
  at each run exactly as `POST /repos/{repo}/triage` is: only runs before it, never another leg
  of the same CI run attempt.
- **Change links as a live recording would have stored them.** Backfilled runs have no
  `runs.affected`, so the eval computes each run's links from its stored changed files and the
  import graphs at its commit (Go `go list`, Python's static graph), through `triage_run`'s
  `run_links` argument: one optional callback, used for runs stored without links, `None` in
  production. A graph that can't be built leaves the change unknown, as `record` would.
- **Unit:** one failing test at one commit (Go subtests folded into a failing parent).
- **Labels** (the first that applies wins):
  1. **flaky:** the same commit and variant also passed, in another run;
  2. **pre_existing:** main (same variant) was failing the test at its latest run before, in a
     streak of at least 2 main commits;
  3. **regression:** (a) on a branch, main passed the test before, no run of the commit passed
     it, and a later commit on the branch passed it after a diff touching a file linked to the
     test (a GitHub compare, the selector's links); or (b) on main, the previous main run passed
     and the test then failed on at least 2 main commits in a row, starting here.
- **Excluded, never guessed:** a failure that never passed again on its branch, no main
  history of the test, a later pass after a change unrelated to the test, a one-off failure on
  main (calling it flaky would reuse the engine's own fail-then-pass rule), an unknown fix diff.

### RDFLib/rdflib (regression checks only)

**10 of 11 fix episodes with a known diff got `new_regression`.** An episode is a branch and
the commit that fixed its failures; the 11th, and a 12th whose diff was never recorded, are
below. rdflib has no re-runs of a commit and almost nothing failing on main, so every label is
a regression: this section measures regressions only, with no flaky or pre-existing precision.

| Distinct (commit, test) failures | |
|---|---|
| Failures | 2,315 (41 commits, 1,105 tests) |
| Labelled (all regression) | 1,312 (57%), in 12 fix episodes |
| → `new_regression` | 1,048 (80%): 656 high confidence, 392 medium |
| → `unknown` | 264 (20%): one PR (`undef-enum`) whose diff was never recorded (rule 3) |
| → `flaky` (regressions called flaky) | **0** |
| `unknown` over all failures | 323 of 2,315 (14%): 264 unknown diff, 58 new tests with no history |

Per matrix leg (about 15 per commit), the same run counts 13,329 failures: 10,568 labelled,
6,977 `new_regression`, 3,591 `unknown`, 0 `flaky`; 2,761 excluded (never passed again on its
branch 2,190, no main history 546, later pass after an unrelated change 24, one-off main
failure 1). Verdicts agree across legs for 2,312 of the 2,315 failures.

Three caveats make the regression numbers weaker evidence than they look:

1. **The label barely separates fixes from other changes on rdflib.** The "fixed by a linked
   change" check said linked 10,568 times and unrelated 24 times: rdflib's `__init__.py` and
   plugin registry link nearly every module to nearly every test (why selection saves ~1%
   there). On rdflib the label effectively means "failed, then passed later on its branch".
2. **12 fix episodes, not 1,048 data points.** Two PRs hold 75% of the labelled regressions:
   `upgrade-mypy-v3` (666 tests) and `fix/dataset-length-count` (311).
3. **A known flaky test was called a regression.** The `test_service*` tests call a remote
   SPARQL service; triage said `new_regression` for 12 of their 13 failures and the label said
   regression for 6. Neither can see flakiness: there are no same-commit re-runs, and the
   fail-then-pass rule needs a change that doesn't link to the test, which almost never
   happens on rdflib.

### agent-substrate/substrate

The headline repo for flaky and pre-existing: 202 re-runs of a commit and 50 failing main runs.
3,620 failures in 153 runs (140 commits; 419 tests; one (commit, test) failed in two legs), 8
days of history; 3,176 labelled (87.7%).

| Label ↓ / verdict → | `new_regression` | `flaky` | `pre_existing` | `unknown` |
|---|---|---|---|---|
| regression (406) | **319** | **5** | 0 | 82 |
| flaky (678) | 0 | **678** | 0 | 0 |
| pre_existing (2,092) | 0 | 0 | **2,092** | 0 |

| | |
|---|---|
| Precision of `new_regression` | 319 / 319 (high 177/177, medium 141/141) |
| Precision of `pre_existing` | 2,092 / 2,092 (high 1,333/1,333, **medium 759/759**) |
| Precision of `flaky` | 678 / 683, but see below |
| Regressions called flaky | **5** (below) |
| Regressions found (recall) | 319 / 406 (78.6%): 62 `unknown` not linked to the change, 20 `unknown` linked but known flaky ("re-run to tell"), 5 `flaky` |
| `unknown` over all failures | 456 / 3,620 (12.6%): 347 in 22 runs with an unknown diff |
| Fix check | linked 402, unrelated 11 (rdflib: 24 of 10,592) |
| Excluded | 444: never passed again 360, no main history 68, unrelated fix 11, one-off main failure 4, fix diff unknown 1 |

- **`flaky` precision is mostly by construction.** 678 of the 683 `flaky` verdicts are rule 1
  (the same commit and variant passed before), and all 678 flaky labels are that same earlier
  pass: no flaky label needed a pass the engine couldn't see. Only 8 `flaky` verdicts came from
  history alone (past same-commit flakes, or an unrelated fail-then-pass); 3 were unlabelable
  and the other 5 are the regressions called flaky.
- **`pre_existing` at medium confidence is the real test of prediction:** the verdict was made
  after one failing main run, and the label needed the streak to continue on later main runs.
- **Regressions as `unknown` are triage refusing to guess.** The 62 "not linked" include
  `atepg::TestAcquireLease_*`, broken by a PR that no import edge or declared rule connects to
  them (the selection eval's no-fallback counterfactual misses them too).
- **The 5 regressions called flaky are probably label noise.** 9 of the 34 fix diffs have 100+
  files, because the branch merged `main` before the fix: the diff then carries `main`'s
  changes, and they can touch the test's own package. 38 labels rest on such diffs, including
  all 4 branch-side regressions called flaky; each of those tests had same-commit flakes on
  record before the run. The 5th is on main (`combinedvolumes::TestCombinedVolumes`, failing on
  2 main commits), with 2 same-commit flakes before it. rdflib's 17 fix diffs all have 20 files
  or fewer. Only 1 label anywhere rests on a low-strength link alone (a root build or CI file).

### Examples

1. **A regression, called correctly.** rdflib `fix/dataset-length-count` changed
   `rdflib/graph.py`; 311 tests failed. Triage: `new_regression`, "imports changed module
   rdflib/graph.py". A later commit on the branch (`ecf2e75`) fixed them.
2. **Pre-existing, predicted from one main run.** substrate run 5046 (main, `e2e-agentgateway`):
   the `TestActorEgress*` e2e tests had failed on main once, at `ac41c06`. Triage:
   `pre_existing`, medium. They kept failing on main for 9 commits.
3. **Unknown instead of a guess.** substrate `aramase/c/objectstore_proto_rename`: 332 labelled
   regressions; 272 `new_regression`, and 60 `unknown` "not linked to this change" for tests
   (`atepg::TestAcquireLease_*`) the diff reaches through no signal.
4. **A "regression called flaky" that probably isn't.** substrate
   `atelet::TestSandboxAssetPrewarmDownloads` failed on three unrelated PRs (`pmem-dax`,
   `atunnel-egress-h2`, `atunnel-strip-stale-header`). Triage: `flaky`, high, "flaky 5 times in
   90 days (3 same-commit, 2 unrelated fail-then-pass)". Each PR's later passing commit came
   after merging `main`: diffs of 220–266 files, including `cmd/atelet` itself, the test's own
   package. So the label called each a regression fixed by a linked change. Counted as errors
   here; the test's history says flaky.
5. **The blind spot.** rdflib `test_service_with_bound_solutions` calls a remote SPARQL
   service. On a PR that only bumped the pre-commit config, triage said `unknown` (not linked)
   and the label said regression: this is the 11th episode above. Elsewhere its `test_service*`
   siblings got `new_regression`, because rdflib's changes link to everything and it never
   re-runs a commit (a Known issue: a `known_flaky` list or re-run detection would fix it).

### Limits

- **The labels are proxies.** Label (a) uses the selector's links on a later diff, the same
  mapping verdict rule 6 uses, so `new_regression` precision is partly measured with the
  engine's own links; on rdflib that check is nearly always "linked".
- **Fix diffs that merged `main` carry `main`'s changes,** so label (a) can credit a fix to
  them (9 of substrate's 34 fix diffs; none of rdflib's).
- **Flaky and pre-existing labels share evidence with rules 1 and 2;** only the parts noted
  above (medium-confidence `pre_existing`, history-only `flaky`) test prediction.
- **substrate has 8 days of history:** little for the week-4 fail-then-pass rule to find.
- **Episodes are few:** 12 on rdflib, 34 on substrate (one of them is 332 of the 406
  substrate regressions).

### Reproducing

Needs the history backfilled, `GITHUB_TOKEN` (from the environment or `.env`), and Go for
substrate. A network outage stops the eval (it never records "unavailable" for one);
`--resume` continues.

```sh
uv run python -m scripts.eval.run_eval --repo RDFLib/rdflib --language python \
    --source github --triage --resume rdflib-triage.jsonl
uv run python -m scripts.eval.run_eval --repo agent-substrate/substrate --language go \
    --go-module github.com/agent-substrate/substrate --source github --goos linux \
    --siftwise-toml scripts/eval/substrate.siftwise.toml --triage --resume substrate-triage.jsonl
```

rdflib took 47 minutes; substrate about 3½ hours, mostly building Go import graphs (about 3
minutes per commit on Windows).

## Risk order: time to first failure

How much sooner does a selective run show its first failure when the commands list the
riskiest tests first (`order: risk`, [ARCHITECTURE](../ARCHITECTURE.md#risk-order)) instead of
by path and test ID (`order: default`)? And would ordering by risk per unit of time be better?
Measured 2026-11-04 on the same PR replays as above (`--failing`), for every selective run that
caught a failure. Every order selects exactly the same tests: the eval checks it on every run,
and the recall is unchanged (rdflib 2,049 of 2,050, 140 of 141 in selective runs; substrate
2,043 of 2,044, 38 of 39).

### Results

Share of the selected run that comes before the first failure shows (lower is better): the
median over the runs, with the mean in brackets. Earlier / later / same counts each run
against the order named.

| | Order | By test count | By measured time | vs default (count; time) | vs risk (count; time) |
|---|---|---|---|---|---|
| RDFLib/rdflib, 17 runs | default | 13.0% (26.0%) | 37.3% (42.5%) | | |
| | **risk** | **1.2%** (12.8%) | **1.4%** (12.1%) | 10/4/3; 10/4/3 | |
| | risk-per-time | 4.5% (28.3%) | 0.3% (27.2%) | 9/5/3; 10/4/3 | 3/11/3; 3/11/3 |
| | risk-per-time, tiered | 4.5% (28.4%) | 0.5% (27.4%) | 9/5/3; 10/4/3 | 3/11/3; 3/11/3 |
| agent-substrate/substrate, 19 runs | default | 74.8% (66.7%) | 42.0% (49.3%) | | |
| | **risk** | **23.3%** (30.3%) | 25.6% (34.4%) | 16/2/1; 12/5/2 | |
| | risk-per-time | 56.6% (52.2%) | 14.9% (27.4%) | 12/7/0; 12/7/0 | 5/13/1; 10/8/1 |
| | risk-per-time, tiered | 50.1% (51.1%) | 33.5% (45.1%) | 14/4/1; 9/9/1 | 2/13/4; 7/8/4 |

**Risk order** (the default):
- **rdflib:** in 6 of the 14 timed runs, the first failure came after 238 to 359 s of a 388 to
  550 s run in the default order, and within 25 s in risk order. In 7 of the 14 it came
  within the first 5 s of the risk-ordered run. Selective runs select about 93% of the suite,
  so the order matters most here.
- **It's not always earlier.** In 4 rdflib runs risk order reached the failure later. In the
  worst it moved from test 775 to test 4,422 (22 s to 79 s of 136 s). The likely reason is
  that the failing test ranked below tests the change reaches more directly, but the eval
  doesn't record each failure's link to confirm it. Three runs selected 22 tests with no
  recorded durations: the same either way.
- **substrate, by time:** risk order was slower in 5 runs, although it reached the failure
  earlier by count in 4 of them. Packages under a declared `[[depends]]` rule (the e2e suites)
  rank high and run long, so a slow risky package can come before a quick failing one. In one
  run the first failure moved from 114 s to 1,292 s of a 1,293 s run.

### With import depths (#48)

Measured 2026-11-10 with the eval sending each import's depth, as `siftwise select` does
since #48 (`run_eval`'s default now; `--no-import-depths` gives the numbers above). Depth 1
(a test file imports the changed code itself) ranks as a direct link, deeper as indirect; it
changes the order only. Same runs, same selections (checked on every run) and the same
default order. Three versions: before #48, #48 as first built (depths for every graph, Go
packages included), and as shipped (depths for test files only: Python, Jest, Java).

| | Order | By test count | By measured time | vs default (count; time) |
|---|---|---|---|---|
| RDFLib/rdflib, 17 runs | risk, before #48 | 1.2% (12.8%) | 1.4% (12.1%) | 10/4/3; 10/4/3 |
| | risk, depths for every graph | 1.2% (11.3%) | 1.4% (11.2%) | 10/4/3; 10/4/3 |
| | **risk, depths for test files (shipped)** | **1.2%** (11.3%) | **1.4%** (11.2%) | 10/4/3; 10/4/3 |
| agent-substrate/substrate, 19 runs | risk, before #48 | 23.3% (30.3%) | 25.6% (34.4%) | 16/2/1; 12/5/2 |
| | risk, depths for every graph | 34.4% (39.0%) | 25.6% (34.9%) | 16/2/1; 12/5/2 |
| | **risk, depths for test files (shipped)** | **23.3%** (30.3%) | **25.6%** (34.4%) | 16/2/1; 12/5/2 |

**What went wrong with Go packages** (every graph, the first build): run for run against
risk order without depths, substrate's first failure came earlier in 1 run, later in 6 and
the same in 12 (by count and by time alike). Taken failure by failure in the 6 that got
worse:
- **Every one of the 32 packages that moved ahead of the first failure** was a Go package
  importing the changed package itself (depth 1), promoted from indirect (3) to direct (6):
  5 to 14 such packages per run.
- **In 5 of the 6, the first failure is an e2e suite** (`internal/e2e/suites/...`) that only
  the declared `[[depends]]` rule selects, with no import depth: it kept its indirect link
  and a score of 5.8 to 6.0 (3 plus a recent failure), while the importers went from 3 to 5.7
  (3, plus 2 if new) to 6 to 8.7. It moved from position 6, 9, 7 and 7 to 13, 14, 18 and 14,
  and from 2 to 3 in the fifth.
- **In the 6th** the failing package (`cmd/atelet`) was already direct (its own file
  changed); one depth-1 importer with more history moved one place ahead of it (2 -> 3).
- **Depth didn't tell Go packages apart:** in the 6 runs no selected package had depth 2 or
  more, and over the 19 runs 83% of the import-linked packages are at depth 1 (59% to 100%
  per run). rdflib's test files are the opposite: 16% at depth 1 (0% to 23% per run), 84%
  deeper.

So the "many Go packages import it directly" cause holds, with the precise mechanism: depth
1 is the common case in a package-level graph, so it ranked nearly every importer above the
tests a `[[depends]]` rule selects, which #48 never meant to reorder.

**The rule shipped:** Go packages are never promoted by depth (they stay indirect, as before
#48); test files' depth 1 still makes a direct link. The reason isn't these scores: a Go
edge is package to package (a package imports another for any of its symbols, and its tests
run whole), while a test file importing a module is a narrower statement about what that
test uses. A smaller bump for depth 1 (between 3 and 6) was considered and not taken: it
would still lift most Go importers over rule-selected tests with the same history (a new
importer at 4.5 + 2 = 6.5 against the failing suites' 5.8 to 6.0), so it softens the cause
instead of removing it. With the rule, substrate is exactly as before #48 (all 19 runs
identical), and rdflib keeps its small gain (all 17 runs identical to depths for every
graph: it has no Go). The Jest and Java depths stay on with no replay to measure them: no
Jest replay exists, and dropwizard's and fixture-monkey's handful of runs with a first
failure weren't re-measured.

- **How it was run:** rdflib from the cached Python graphs (49 minutes for the whole replay,
  13 for the 17 runs again with the rule). substrate's cached Go graphs predate the direct
  edges depth needs, so the graphs of the 19 runs measured here were rebuilt with `go list`
  (`--commits ... --jobs 4`, 41 minutes; 4 minutes again with the rule); the other runs only
  matter for recall, which depth can't change. A first pass on the old graphs (no depths)
  reproduced the published substrate numbers exactly.

**Risk per unit of time (evaluated, not adopted).** This is the standard cost-aware order: each
unit of work (a pytest argument, a Go package, a Jest file) by its best score divided by its
estimated duration. The duration is the sum of its tests' mean durations, with an unknown one
counted as the selection's median and a floor of 100 ms. The "tiered" variant does this only
within each link tier, so a direct link still comes before an indirect one. The constants were
set before the runs and not tuned to them.
- **It isn't clearly better by time on both repos.** On substrate, its median time is lower
  (14.9% vs 25.6%), but run for run it beats risk order in only 10 of 19 runs and loses 8. The
  swings are large both ways: one run went from 909 s to 4 s, another from 278 s to 1,239 s. On
  rdflib it loses to risk order in 11 of 17 runs, and its mean more than doubles (27.2% vs
  12.1%). In 3 runs the failing test went to the very end (test 10,761 of 10,772): a cheap,
  low-risk unit outranks an expensive, high-risk one once the cost divides the score.
- **It's clearly worse by count on both repos:** 4.5% vs 1.2% and 56.6% vs 23.3%.
- **The tiered variant is worse than risk order on both repos.** On rdflib it differs from
  the untiered one in two runs.
- **So the default stays `risk`, and risk-per-time isn't offered** as an option: nothing in
  these runs suggests when someone should pick it. Both variants stay in the eval
  (`risk.Order.RISK_PER_TIME*`, eval only; the API, CLI, Action, GitLab and MCP reject them),
  so they can be measured again on new data.

### Method and limits

- **Execution model** (`scripts/eval/ordering.py`): commands run one after another. pytest
  arguments and Jest files run in the order given, one Go package per command. JUnit doesn't
  record a file's definition order, so tests within one argument or package are taken by test
  ID, the same in every order. A Go subtest counts as its parent (only a Go subtest: see the
  correction below). Real Jest and pytest-xdist
  runs parallelise and reorder, so these are upper bounds on the gain for them.
- **Durations:** what each first failure costs is the evaluated run's own measured durations
  (mean over its variants), summed serially. Runs with no recorded durations are left out of
  the time figures. What the orders *estimate* (the tie-break, the risk-per-time cost) is each
  test's mean over the 50 runs (uploads) before the evaluated one; production uses
  `test_stats`' window mean. Using these estimates moved rdflib's risk order slightly from an
  earlier run that broke ties by test ID only (by count 2.5% then, 1.8% after).
- **Correction (with the time-budget work, 2026-11-04):** the execution model folded *any*
  test with a `/` in its ID into a "parent", as it should only do for Go subtests. rdflib's W3C
  suite tests are parametrized by URLs (`test_entry[http://...]`), so thousands of them lost
  their time and their place in the count. Fixed, with a test; the rdflib rows above are
  re-measured (risk by count 1.8% → 1.2%, by time 1.6% → 1.4%; default by count 8.7% →
  13.0%, by time 41.3% → 37.3%). The conclusions are unchanged. substrate (Go) was not
  affected.
- **Score inputs as of each run** (`HistoryIndex._risk_stats`, no peeking): the failure rate,
  last failure and flaky share over the 90-day window before the run. A run counts when the
  test appears in it, and a failure is any failed attempt (production reads `test_stats`, which
  uses final outcomes and leaves out skips).
- **"First failure"** is any failing test of the run that the selection runs, whatever its
  verdict: the eval has no triage verdict per failure in this mode.
- **Small samples:** 17 and 19 runs, from two repos.

### Reproducing

As the PR-mode commands above. Every order's time to first failure is in each run's line and
in the summary (`time to first failure, N selective runs that caught one ...`).

```sh
uv run python -m scripts.eval.run_eval --repo RDFLib/rdflib --language python --source github \
    --failing --resume rdflib-risk.jsonl
uv run python -m scripts.eval.run_eval --repo agent-substrate/substrate --language go \
    --go-module github.com/agent-substrate/substrate --source github --goos linux \
    --siftwise-toml scripts/eval/substrate.siftwise.toml --failing --resume substrate-risk.jsonl
```

Add `--no-import-depths` for the numbers before #48. Go graphs cached before #48 have no
direct edges (no depths): delete a commit's `.eval-cache/gograph-<sha>.json` to rebuild it.

## Time budget: recall within a budget

What does a time budget (`--budget`, [ARCHITECTURE](../ARCHITECTURE.md#time-budget)) still
catch? Measured 2026-11-04 on the same PR replays (`--failing`): every run with failures and
measured durations, under budgets of 10%, 25% and 50% of that run's own full measured time
(every test it ran, Go subtests inside their parents). The budget packs the run's selection in
risk order; when siftwise would have run the full suite (an unknown diff, a build file, ...),
it packs the full suite's known tests (a "budget-limited full suite"). The estimates are each
test's mean duration as of the run (no peeking); the recall and times are the run's own
results.

### Results

"Runs whose failure is still caught" is the CI question: did the budget-limited run still go
red? A run with several failures counts once. "Failures caught" counts every failing test, so a few runs
with hundreds of failures dominate it; it's also shown without them. Time to first failure is
over the runs whose budget caught one, as a share of the run's full time.

| | Budget | Runs whose failure is still caught | Failures caught | Without the biggest runs | First failure (median) | Measured over budget |
|---|---|---|---|---|---|---|
| RDFLib/rdflib, 31 runs | none | 30 of 31 | 1,903 of 1,904 | | | |
| | 10% | 22 of 31 | 1,240 (65.1%) | 127 of 246 (52%) | 5.0 s, 1.5% of the run | 1 run (at most 3.9×) |
| | 25% | 24 of 31 | 1,620 (85.1%) | 144 of 246 (59%) | 9.4 s, 2.3% | 5 runs (at most 2.2×) |
| | 50% | 28 of 31 | 1,825 (95.9%) | 176 of 246 (72%) | 11.6 s, 3.4% | 7 runs (at most 1.7×) |
| agent-substrate/substrate, 72 runs | none | 71 of 72 | 2,043 of 2,044 | | | |
| | 10% | 34 of 72 | 109 (5.3%) | 78 of 234 (33%) | 75 s, 4.6% | 31 runs (at most 3.2×) |
| | 25% | 43 of 72 | 138 (6.8%) | 107 of 234 (46%) | 175 s, 15.1% | 27 runs (at most 1.3×) |
| | 50% | 54 of 72 | 200 (9.8%) | 154 of 234 (66%) | 199 s, 17.5% | 25 runs (at most 1.4×) |

"The biggest runs" are those with 100 or more failures: 5 rdflib runs (1,658 of its 1,904
failures) and 3 substrate runs (1,810 of 2,044).

- **Small budgets miss a lot.** At 10%, 9 of 31 rdflib runs and 38 of 72 substrate runs that
  failed with the full selection would have passed. Even at 50%, 3 rdflib runs and 17
  substrate runs would have passed. A budget-limited run is not a check of the change, and
  siftwise says so on every surface; these numbers are why.
- **rdflib does better.** When a budget catches a failure, it shows within seconds (median
  5 to 12 s of a run of minutes). Its five biggest runs (311 to 343 failures each, all
  full-suite fallbacks) are caught almost whole from 25% on; at 10% two of them lose most of
  their failures (119 of 333, 68 of 311 caught).
- **substrate does badly by failures.** Its three biggest runs (about 603 failures each) are
  barely touched: the budget catches 31 of their 1,810 failures at 10% and 25%, 46 at 50%. By
  runs it does better (about half of the runs' failures are still caught at 10%, three
  quarters at 50%), but
  its first failures come late (median 75 to 199 s). Most of its failing runs (52 of 72) are
  full-suite fallbacks, which a budget can only pack by history (every test ranked
  history-only), and its three biggest runs are among them.
- **The estimates are means, so a run can overrun.** rdflib's budget-limited runs measured a
  median 0.75 to 0.79 of their budget: the estimates run high (in one run,
  `test/test_sparql/test_service.py` was estimated at 56 s and measured 10 s). substrate's sit at 0.95 to 0.98 and overran in 25 to 31 of 72 runs, by
  at most 1.4× at 25% and 50%. The big overruns at 10% (3.9× and 3.2×) are the "riskiest unit
  always runs" rule: a single unit already over the budget.

### Method and limits

- **Targets:** the PR runs with failures from the replays above (39 rdflib, 72 substrate),
  minus those without measured durations (8 rdflib runs). The budget is a
  share of each run's own full measured time, so budgets differ per run.
- **Estimates as of the run** (`run_eval.with_durations(skipped_as_zero=True)`): each known
  test's mean over every variant of the latest 50 CI runs before the run (the runs that make a
  test known), 0 for a test that only skipped there, as production counts a test whose
  `test_stats.runs` is 0. Production's means come from `test_stats` over the 90-day window.
- **Execution model:** as for time to first failure above (serial, `scripts/eval/ordering.py`).
  "Measured" is the run's own durations of the tests the budget-limited commands run.
- **What's caught** is what the budget-limited selection would run (`WouldRun`), any verdict;
  flaky failures count as failures here, as in the rest of the selection eval.
- **Not tuned:** the 90th percentile for unknown durations and the three budget shares were
  set before the runs. Three problems found in the first runs were fixed, not tuned: packing
  stopped at the first unit that didn't fit (one big rank-2 unit left a 50% budget running
  about 1% of an rdflib run), so it now skips that unit and goes on; known tests without a
  duration (about 6,100 on rdflib: tests that only skipped, or ran in none of the last 50
  uploads, which are 2 or 3 rdflib CI runs) each counted at 30 ms, so a skipped-only test now
  counts 0 and the eval estimates from the latest 50 CI runs; and the execution-model bug
  above made measured times look several times below the estimates.
- **Small samples:** 31 and 72 runs from two repos; substrate's figures by failures rest on
  three runs.

### Reproducing

The PR-mode commands above: each run's line lists its budgets (`budget 10% (...): ...`), and
the summary ends with `time budgets (...)`.

## Java: dropwizard/dropwizard (Maven)

Measured 2026-11-06 for Java selection ([ARCHITECTURE](../ARCHITECTURE.md#java-maven-and-gradle)):
the first replay of Java history. dropwizard is a Maven build of 42 modules with a wrapper;
its CI (`maven.yml`) runs Ubuntu and Windows legs on three JDKs and uploads Surefire reports.

**Finding a repo.** A code search for workflows uploading Surefire or Gradle reports
(`scripts.eval.find_candidates`, Java and Kotlin) found 15 repos with live artifacts and
enough PRs and tests; dropwizard has the most failing runs among the Maven ones (Gradle:
naver/fixture-monkey and apple/servicetalk are left for later).

**Data.** 2,144 uploads (each CI run's six legs) from the last 400 `maven.yml` runs, backfilled
with `scripts/backfill.py`. The first pass lost all 781 Windows uploads: Surefire writes a
`<testcase name="">` for a JUnit 5 `@Nested` class skipped as a whole (Docker tests on
Windows), and the parser rejected the whole file. Fixed (recorded as the class's `(class)`
result) and re-backfilled; 0 errors.

**Replay** (`run_eval --language java --source github --failing`): every PR run with
failures plus the 10 newest, each with the build, test classes and graph read at its commit
(no peeking).

| | |
|---|---|
| Targets | 148 PR runs on 58 branches; 114 are Renovate dependency bumps |
| Full suite | 131: 129 for a changed build file (every bump, plus module `pom.xml` edits), 1 for a changed file no test reaches, 1 for a selected test with no runner (a class no test file is named after) |
| Selective | 17 runs from 7 branches; median 91% of the estimated test time skipped (26% to 93%) |
| Failures caught | 74 of 74 (34 runs with failures); in selective runs 7 of 7 |
| Without fallbacks | the full-suite runs whose change some signal reaches would have caught 21 of 21; 46 more failures had only a fallback |
| Time skipped over all runs | 10.0% over the 148 targets (full runs count as 0%; 10.6% over the 140 runs with failures) |

**Module build files scoped** (#54, re-run 2026-11-08 on the same 148 targets): a change to
a module's own `pom.xml` now runs the test classes of that module and of every module that
depends on it, instead of the full suite.

| | before | after |
|---|---|---|
| Full suite | 131 (129 build file) | 126: 122 build file, 2 a changed file no test reaches, 2 a selected test with no runner |
| Selective | 17 runs from 7 branches | 22 runs from 11 branches; median 90.6% of the estimated time skipped (26% to 93%) |
| Failures caught | 74 of 74; selective 7 of 7 | 74 of 74; selective 8 of 8 |
| Time skipped over all runs (148 targets) | 10.0% | 11.9% (12.6% over the 140 runs with failures) |

- **7 runs left the build file fallback**: 5 became selective (`dropwizard-e2e`,
  `dropwizard-servlets`, twice `dropwizard-testing`, and `dropwizard-e2e` with
  `dropwizard-request-logging`), skipping 38% to 88% of the estimated time; one of them caught
  its failure (`AssetServletTest`, in the scoped module). The other 2 still run everything,
  for other fallbacks: a changed main class no test reaches (with a `dropwizard-benchmarks`
  POM edit) and a selected class no test file is named after.
- **The 122 that stay full** are what the rule keeps on purpose: 87 change the BOM
  (`dropwizard-dependencies/pom.xml`: a version bump can reach every module), 24 a workflow,
  6 the root `pom.xml`, 4 `dropwizard-example/pom.xml` (it has `<dependencyManagement>`), 1
  `dropwizard-parent`. Most Renovate PRs bump the BOM, so they still get no savings.
- **Scopes on this build** (read at a recent commit): 33 of 41 module POMs can be scoped; the
  scope is the module alone for 7 (`dropwizard-json-logging`...), up to 36 modules for
  `dropwizard-util`. The first version flagged every module: `dropwizard-bom` lists every
  module in `<dependencyManagement>` and `dropwizard-parent` imports it, which made every
  child a "dependent". Managed versions aren't dependencies (the BOM doesn't change when a
  module's POM does), so they no longer count; a change to the BOM itself stays full.

- **Recall in selective runs rests on one PR**: the 7 failures are 10 runs of
  `limit-json-logged-exceptions`. It's a real check (a failing test in a class with no
  history, run because it's new, and failures reached through the graph), not a broad one.
- **The savings are capped by the fallbacks.** Most dropwizard PRs are dependency bumps that
  change a `pom.xml`, and a `pom.xml` at any depth runs the full suite. Scoping a module's
  POM change to that module and its dependents would help here; it isn't built.
- **Time to first failure** (7 selective runs that caught one, all from that PR): risk order
  reached it after a median 9.7% of the tests and 1.3% of the time, against 100% for the
  default order (the failing class sorts last by path there).
- **Under a time budget** (34 runs with failures; 27 are packed full suites): runs whose
  failure is still caught 21, 23 and 27 of 34 at 10%, 25% and 50% (34 with no budget);
  failures caught 73%, 76% and 88%.
- **Graph checked by hand** for a change to `EventJsonLayout.java` (that PR): the three test
  classes it selects all use it, directly or through `EventJsonLayoutBaseFactory` (an import,
  and a same-package reference with no import); a text search finds no other test naming it.
  Layouts discovered through `ServiceLoader` (`META-INF/services`) are what the graph can't
  see: no failure in this history came from that.
- **Performance** on the snapshot (927 Java files, 438 test files): reading the build 0.8 s,
  the graph about 2 s warm (19 s the first time after unpacking, from the cold file cache).

**An eval bug the replay found:** a test class with no history that the selection runs (by
`-Dtest`) was scored as a miss, because `WouldRun` only credited tests by ID. It now credits
every test of a class run whole, in the eval and in shadow mode (`java_classes`, migration
0012); the first replay's one "miss" was that.

### Reproducing

```sh
uv run python scripts/backfill.py --repo dropwizard/dropwizard --workflow maven.yml     --artifact-pattern "test-reports-*" --max-runs 400 --batch --workers 6
uv run python -m scripts.eval.run_eval --repo dropwizard/dropwizard --language java     --source github --failing --resume dropwizard-java.jsonl
```

The #54 re-run took 47 minutes (the commits' sources downloaded again: the cached Java
snapshots are `javagraph-v2-<sha>.json` now, with the module dependencies).

## Java: naver/fixture-monkey (Gradle)

Measured 2026-11-08: the first replay of a Gradle build. fixture-monkey is a multi-project
Gradle build (28 projects: Java, plus Kotlin projects siftwise doesn't analyse) testing with
JUnit 5; its `Run tests` workflow uploads `build/test-results` as `test-results`.

**Finding a repo.** A code search for workflows uploading Gradle test results
(`scripts.eval.find_candidates --language Java`, three `build/test-results` queries; 191 repos)
left two with live artifacts, enough PRs and tests: naver/fixture-monkey and tamboui/tamboui.
tamboui's settings include projects through a computed list and `projectDir` (siftwise can't
read that build) and none of its failed runs had test failures. Of the Gradle repos the
earlier Java search found, apple/servicetalk keeps artifacts for 9 runs (1 with test
failures) and gorpipe/gor is a third Scala. fixture-monkey it is, with thin failure data:
of its newest 150 test runs, 116 have artifacts and 4 of those failed.

**Data.** 116 uploads (one per run) from `build.yml`, backfilled in 3 minutes; 11,763 tests.

**Replay** (`run_eval --language java --source github --failing`, 6 minutes): the 10 newest
PR runs plus every other PR run with failures.

| | |
|---|---|
| Targets | 11 PR runs on 11 branches |
| Full suite | 5, all for a changed build file: `gradle/libs.versions.toml` twice (the version catalog), a workflow, and two module `build.gradle.kts` (not scoped: the build uses typesafe project accessors, `projects.x`, which siftwise doesn't read, so no build file is scoped) |
| Selective | 6 runs; median 57% of the tests and 13% of the estimated time skipped (one run 66%) |
| Failures caught | 3 of 3 (2 runs with failures); in selective runs 1 of 1 |
| Time skipped over all runs | 12.0% (full runs count as 0%) |

- **Recall rests on 3 failures**, 2 of them in a full-suite run (a catalog bump): it says the
  selector didn't miss these, not much more.
- **Time to first failure** (the 1 selective run that caught one): risk order reached it at
  test 392 of 1,888 (4.2 s), the default order at test 393 (90.2 s): 1.6% of the time against
  33.7%. By count the same; by time the risk order runs the quick classes first.
- **Commands**: one Gradle command per selection (`./gradlew :fixture-monkey:test --tests
  ...` with one `:project:test --tests ...` group per project), as #54 builds them.
- **The selective runs skip 57% of the tests but 13% of the time**: the classes they keep
  carry most of the run's measured time.

### Reproducing

```sh
uv run python scripts/backfill.py --repo naver/fixture-monkey --workflow build.yml     --artifact-pattern test-results --max-runs 300 --batch --workers 6
uv run python -m scripts.eval.run_eval --repo naver/fixture-monkey --language java     --source github --failing --resume fixture-monkey-java.jsonl
```

## JS/TS import graph: performance and validation

Measured 2026-11-05 for the JS/TS import graph (`cli/jsgraph.py`,
[ARCHITECTURE](../ARCHITECTURE.md#the-jsts-import-graph)).

**No replay.** A replay needs an open-source Jest repo whose GitHub Actions upload JUnit with
each test's file (jest-junit's `addFileAttribute`): without file paths siftwise has no runner
for Jest tests and always runs the full suite, so there's nothing to measure. A code search
for workflows using jest-junit (`scripts.eval.find_candidates`, TypeScript and JavaScript)
found 8 repos with live JUnit artifacts and enough PRs and tests; the 5 checked
(starwards/starwards, tomquist/hm2mqtt, dorny/test-reporter, ejjays/phantom, druxt/druxt.js)
report no `file` attribute, and most had no failing runs to measure recall on. So there are
no recall or savings numbers for JS/TS.

**Performance** on jestjs/jest's main branch (downloaded as a zipball, 2026-11-05): 3,497
files, 2,272 JS/TS (5.7 MB), 1,362 Jest test files. Building the graph took 1.9 s (Windows
laptop; the tokenizer itself 1.3 s), 0.6 s again in the same process (the content-hash
cache), and about 6 MB of memory; finding the tests one changed file affects took 5 ms. Of
4,441 import specifiers, 1,927 resolved as relative paths, 915 through workspace packages
and aliases, 1,561 were external packages and 38 relative ones aren't in the checkout (test
fixtures that import missing files on purpose). 40 files are dynamic, and the graph noted 5
`jest.config.js`/`.ts` files it can't read.

**Validation by hand** on the same checkout, for 8 recent jestjs/jest commits (their changed
files from the GitHub API, the graph as of the snapshot, not of each commit):

| Commit | Changed (source) | Test files selected by import | More, reaching a dynamic import | Fallback |
|---|---|---|---|---|
| 4685157 | jest-mock/src/index.ts | 388 | 82 | no |
| 0dbdc23 | jest-source-map/src/SourceMapCache.ts | 43 | 430 | no |
| 121c2ae | an e2e fixture, e2e/runJest.ts, eslint.config.mjs, ... | 370 | 100 | yes: the e2e fixture and the ESLint config no import reaches |
| 981920c | scripts/buildUtils.mjs | 0 | 470 | yes: build tooling no test imports |
| 74bd550 | jest-each/src/table/interpolation.ts | 46 | 424 | no |
| c94f28d | jest-resolve/src/resolver.ts | 321 | 149 | no |
| cda97a3 | jest-runtime/src/internals/EsmLoader.ts | 32 | 438 | no |
| ea7258e | jest-config (3 files), jest-types/src/Config.ts | 468 | 12 | no |

- **Chains checked by hand**, file by file, for 5 sampled tests (e.g. a jest-circus test ->
  jest-circus/src/index.ts -> jest-each's index -> bind.ts -> table/array.ts ->
  table/interpolation.ts): every link is a real import, including `@jest/source-map`, which
  resolves through the package's `exports` (`./build/index.js`) back to `src/index.ts`.
- **Possible misses:** the test files that mention a changed file's name without being
  selected were, read by hand, about other files of the same name. The e2e fixture in
  121c2ae is run by `e2e/__tests__/nativeTypescriptStrip.test.ts` spawning Jest on its
  directory: no import reaches it, so the selector keeps its full-suite fallback.
- **The cost of the dynamic rule:** Jest's own runtime and config loading `require()` paths
  it computes, and most of its tests reach that code, so 12 to 470 test files run on top of
  the import graph's for every change. That's the safe answer for a test runner's own
  source; most application repos have far fewer dynamic imports, but that isn't measured.

## siftwise on itself

Measured 2026-11-10 from the dogfood workflow (`.github/workflows/dogfood.yml`), which runs
siftwise's own full suite on every PR and push to main as a shadow run: it stores what
siftwise would have selected, runs everything, and compares
([ARCHITECTURE](../ARCHITECTURE.md#shadow-mode)). The numbers are
`python -m scripts.dogfood_report report --since 2026-10-26 --exclude-forks`, over every
run's summary artifact.

| | |
|---|---|
| Workflow runs | 36, from 2026-10-26 to 2026-11-09: 17 PR runs, 19 pushes to main; 2 of the PR runs are from forks (#62, #63; first-time contributors, so they ran once approved) |
| Shadow runs counted | 32: the first 2 runs stored no selection (no history yet), and the 2 fork runs are left out (`--exclude-forks`) because code from the fork wrote their summaries; each fork change still counts once, as its push to main |
| Selective | 9 of 32; the other 23 would have run the full suite: 20 for a changed build or config file (`Dockerfile` and `docker-compose.yml` 6 runs each, workflows, and the test fixtures' own `build.gradle.kts`, `tsconfig.json`, `jest.config.ts`), 3 for a changed file no test reaches (e.g. `glama.json`) |
| Tests selected in the selective runs | 0 to 1,455 of 1,189 to 1,641 known tests (median 39% of them) |
| Time saved, measured on the full runs | 14.7% of 23.0 minutes over the 32 runs |
| Failures | none: every run's suite passed, so recall can't be measured yet |

- **Small and correlated:** a merged PR usually counts twice, once as its PR run and once as
  the push to main of the same change, so the 9 selective runs are about 6 distinct changes.
  One of them changed no tested code (0 tests selected, 100% saved).
- **Weekly:** 6, 20 and 6 counted runs in the weeks of 2026-10-26, 11-02 and 11-09; time saved
  21%, 7% and 34%.
- **Fixture build files count as build files:** a change to `tests/fixtures/java_gradle/
  build.gradle.kts` or the JS fixtures' `tsconfig.json` ran siftwise's own full suite,
  though they are test data, not siftwise's build. A `[[depends]]` rule can't narrow a build
  file (build files are checked first); scoping test fixtures is left for later.
- **No recall yet.** A dogfood failure would show up here as caught or missed; until there is
  one, the recall numbers are the replays' above.

## Performance

Measured on 2026-10-19 on a Windows laptop, Postgres 16 in Docker, on the rdflib and substrate
histories above. Every change gives identical results; each was checked on real data before
and after, and the tests compare the new code with the old.

| Path | Before | After | Identical results, checked by |
|---|---|---|---|
| Triage of rdflib's widest CI run attempt (22 variants; the Action's `rollup`, `POST /repos/{repo}/triage`) | 1,032 ms, 180 queries | 220 ms, 12 queries | full `triage_ci_run` output for all 41 rdflib attempts with failures (13,329 triaged failures) and all 765 multi-variant substrate attempts, byte for byte |
| Shadow-run comparison, 1 / 5 / 20 variants (synthetic) | 27 / 75 / 255 queries | 27 / 27 / 27 | the shadow tests |
| `select_tests` (the selection inside `/select`), rdflib, a one-file change | 605 ms | 237 ms | the full selections for the 40 newest diffs of each repo, plus 3 edge cases |
| `select_tests`, rdflib, a docs-only change | 315 ms | 210 ms | same |
| `select_tests`, substrate | 128 ms | 99 ms | same |
| Ingest of a 9,891-result rdflib upload (`POST /runs`, rollup deferred) | ~2.2 s | ~1.25 s | the stored rows, row for row |
| Eval Go graphs, 12 substrate commits (`run_eval --jobs 4` vs serial) | — | about 1.8× faster (estimated, see below) | the graph cache files and the eval's results, byte for byte |

- **Triage:** every variant of an attempt is loaded in one batch (`load_histories_many`): the
  same fixed queries read what all variants need, then each variant keeps its own history (its
  cut-off, its own attempt left out, its window). Before, each variant cost about 10 queries.
  A one-variant `/triage` is unchanged: 500 rdflib failures in 0.22-0.24 s. The "before" figure
  is the old one-load-per-variant pattern timed the same way.
- **Selection:** each known test is classified once instead of twice (the time estimate
  reuses it), and two hot character loops became regular expressions. `load_history` (about
  0.18 s on rdflib) is unchanged.
- **Ingest:** only test IDs not seen before are inserted into `tests` (inserting all 9,891 cost
  0.5 s even when none was new), and the results go in one `INSERT ... SELECT FROM unnest`.
  That insert itself takes about 75 ms; most of the remaining time is the two foreign keys'
  checks, one per row each (about 0.67 s), which aren't changed.
- **`run_eval --jobs N`:** Go import graphs are built N at a time, each in its own unpack
  directory, with atomic cache writes. On 12 substrate commits `--jobs 4` built every graph in
  1,006 s. The serial run took 3,949 s, but its first four commits ran `go list` with a cold
  Go module cache (5.5-12 minutes each against about 1.5 later), and the parallel run came
  second. From the serial run's eight warm commits (152 s each), a warm serial run of 12 would
  take about 1,830 s: **about 1.8× with a warm Go module cache** (estimated from the serial
  run's warm commits; CPU-bound on a laptop: four parallel `go list` runs took about 4.5
  minutes each instead of 1.5).
- **Not changed:** the repo rollup (rdflib 119 s, substrate 19 s). Limiting it to tests with
  results in the stats window saves nothing on these histories (every test is active) and would
  cost 12-17 s to find the active tests; stale tests cost about 30 ms per batch of them.

**Measuring note:** on Windows with Docker Desktop, connect to `127.0.0.1`, not `localhost`.
Through `localhost`, every query whose parameters exceed about 8 KB stalls about 45 ms (two
1,100-integer arrays: 48 ms against 5 ms), which hides batching gains. All numbers above are
through `127.0.0.1`.
