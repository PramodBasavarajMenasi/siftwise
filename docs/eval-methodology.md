# Eval methodology

How siftwise's numbers are measured, where the measurements are biased, and what they do and
don't prove. The numbers themselves are in [eval-results.md](eval-results.md); the design
they test is in [ARCHITECTURE.md](../ARCHITECTURE.md).

## What we measure, and why in this order

1. **Recall of selection**: of the failures a full run found, how many would the selection
   have run? A missed failure is the costly error, so this comes first.
2. **Time skipped**: how much test time the selection would have saved. It only matters once
   recall holds.
3. **Triage accuracy**: whether verdicts (new regression, pre-existing, flaky, unknown) match
   what happened next. The costly error is a regression called flaky.

Three instruments measure these: replays of past CI history (selection), a triage eval on the
same history, and shadow mode on live CI.

## The data

- **Source:** past GitHub Actions runs that uploaded JUnit XML as artifacts, pulled with
  `scripts/backfill.py`: one stored run per artifact (matrix leg), with the PR's whole diff
  (base...head) or a push's `before...after`, looked up through the GitHub API.
- **Repos:** [ipfs/kubo](eval-results.md#ipfskubo) (Go),
  [RDFLib/rdflib](eval-results.md#rdflibrdflib) (Python, ~16 legs per run),
  [quantumlib/Cirq](eval-results.md#quantumlibcirq) (a Python monorepo, JUnit from main only)
  and [agent-substrate/substrate](eval-results.md#agent-substratesubstrate) (Go, a busy repo
  with real failures, 8 days of history). They were picked for having JUnit artifacts and
  real failures (`scripts/eval/find_candidates.py`, `deep_check.py`), not for flattering
  results.
- **Unknown diffs** (no PR found, a failed compare, 300+ files, other events) are stored as
  unknown and make the selector fall back to the full suite, as they would live.

## Replay evals (selection)

Code: `scripts/eval/run_eval.py`.

- **Targets.** A target is one CI run attempt, with its matrix legs grouped. PR mode replays
  the 10 newest PR runs plus every PR run with failures (`--failing`); `--replay-main` treats
  each main commit as a PR against the previous main commit; `--replay-prs` replays every PR
  merged in a window, for repos that only upload JUnit from main (Cirq: savings, no recall).
- **No peeking.** For each target, history is rebuilt from runs strictly before it, by run
  time. The eval loads raw results into memory once (`HistoryIndex`) and applies the
  selector's definitions per target; a test checks it matches the server's `load_history`.
- **Graphs at the target commit.** The Go (`go list -deps -test -json ./...`) and Python
  import graphs are built at each target commit, from a checkout (`--checkout`) or the
  commit's zipball (`--source github`), exactly as `siftwise select` would in CI.
- **Caught** means the selection would have run the failing test: full mode runs everything;
  a whole Go package or Python file runs all its tests, new ones included. The same
  `WouldRun` code is used by shadow mode.
- **Selective-only figures** count only runs where siftwise didn't fall back to the full
  suite: a fallback catches everything, so the all-runs figure flatters the selector.
- **"Without fallbacks"** is a counterfactual: what the selector's own signals catch with the
  full-suite fallbacks switched off.
- **Time skipped** is estimated from each test's average duration in history; a full-suite run
  counts as 0%.

### Biases and limits

- **Failures cluster.** rdflib's 2,050 failures come from 39 PRs, and one bad change can fail
  hundreds of tests; the per-run figures (140 of 141 selective) are the more honest
  denominator.
- **Flaky failures count as misses** in the replays: rdflib's one miss is a flaky remote
  test.
- **History is only as long as the backfill:** substrate has 8 days, so co-change and
  recently-failed signals have little to work with.
- **The eval peeks at file paths:** a test's latest reported path is used for every target
  (results don't store their own path). It only matters for tests that moved files.
- **Broken-on-main in the eval reads the last 20 main CI runs**, which can change a reason's
  text, never a selection.
- **Main replays are not real PRs:** consecutive main commits are smaller and cleaner than
  PRs; they measure savings more than recall.
- **Savings are repo-specific:** they follow the import structure (Cirq's packages vs rdflib's
  everything-imports-everything), so the numbers don't transfer between repos.

## Triage eval

Code: `scripts/eval/run_eval.py --triage`, labels in `scripts/eval/triage_labels.py`.
Results: [Triage eval](eval-results.md#triage-eval).

- **The production engine.** Every recorded run with failures is triaged by
  `core.triage.triage_run`, cut off at the run exactly as `POST /repos/{repo}/triage` is: only
  runs before it, never another leg of the same CI run attempt. The eval never re-implements
  triage.
- **Change links as live recording would have stored them.** Backfilled runs have no stored
  links, so the eval computes each run's links from its changed files and the import graphs at
  its commit and passes them through one optional callback (unused in production). A graph
  that can't be built leaves the change unknown, as `record` would.
- **Unit:** one failing test at one commit; Go subtests fold into a failing parent.

### Proxy labels

The labels may read the future; the verdicts never do. The first that applies wins:

1. **flaky:** the same commit and variant also passed, in another run;
2. **pre_existing:** main (same variant) was failing the test at its latest run before, in a
   streak of at least 2 main commits;
3. **regression:** (a) on a branch, main passed the test before, no run of the commit passed
   it, and a later commit on the branch passed it after a diff touching a file linked to the
   test; or (b) on main, the previous main run passed and the test then failed on at least 2
   main commits in a row, starting here.

Anything else is excluded with its reason, never guessed: a failure that never passed again,
no main history, a later pass after an unrelated change, a one-off main failure, an unknown fix
diff.

### Biases

- **Flaky and pre-existing labels share evidence with verdict rules 1 and 2.** A `flaky`
  verdict from a same-commit pass agreeing with a `flaky` label is by construction, not a
  prediction. Only medium-confidence `pre_existing` (called after one failing main run, then
  confirmed) and history-only `flaky` verdicts test prediction; the results report them apart.
- **Regression label (a) uses the selector's links on the fix diff,** the same mapping verdict
  rule 6 uses, so `new_regression` precision is partly measured with the engine's own links.
- **Fix diffs that merged `main` carry `main`'s changes** and can credit a fix to the wrong
  diff (9 of substrate's 34; the likely source of its 5 regressions called flaky).
- **Few episodes:** 12 on rdflib, 34 on substrate, one of them most of substrate's
  regressions. Treat percentages as counts.
- **rdflib has no re-runs:** every label there is a regression, so it measures regressions
  only.

## Shadow mode (live)

Shadow mode measures recall on live CI instead of replays: a sampled run runs the full suite
while siftwise stores what it would have selected; after the run is recorded, each failure is
checked against that selection (see [architecture](../ARCHITECTURE.md#shadow-comparison)).

- **Verdicts first:** failures triage calls flaky are counted apart, never as caught or
  missed; pre-existing and unknown failures count.
- **Two recall figures,** all runs and selective runs only, always reported together.
- **Time saved is measured** on the run's own durations, not estimated.
- **Not counted:** runs whose selection wasn't stored, with no upload, a commit mismatch, a
  failed comparison, or a selection never compared (cancelled); `siftwise report` lists them.
- **Every miss has a cause** from stored evidence (`new_test`, `flaky_history`,
  `config_gap`, `no_link`, `unknown`).

### Biases

- **Shadow runs are full runs:** history then has every test's result, while a repo that
  actually selects only records the selected tests. Signals that read history (co-change,
  recently failed) see more in shadow mode than they would live.
- **Recall depends on the triage verdicts** that decide what counts as flaky.
- **Failures are rare on healthy repos:** a week of shadow runs may see a handful, so report
  raw counts and intervals (0 misses in n failures bounds the miss rate below about 3/n at
  95%), never a bare percentage.
- **Sampling** by a hash of the run ID is uniform over runs, not over changes or failures.

## Dogfooding (live, on siftwise itself)

[dogfood.yml](../.github/workflows/dogfood.yml) makes every siftwise PR and push to `main` a
shadow run, with no hosted server:

- **What runs:** the job starts a siftwise server from its own checkout, with a random token
  made in the job, on the runner's Postgres. It loads the history, runs `actions/select` with
  `shadow: always`, the full test suite with JUnit, then `actions/record` and `actions/rollup`.
  It gates nothing and comments nothing: no `fail-on`, and every siftwise step fails open.
- **History:** the database moves between runs through the Actions cache. It is dumped and
  loaded by `examples/gitlab/ci/history.py`, which keeps the dump's migration revision. Only
  runs on `main` build `main`'s history. A PR run starts from `main`'s newest entry, and what it
  saves stays with the PR: GitHub scopes a PR's cache entries to its merge ref, so neither `main`
  nor other PRs can read them. Fork PRs need no secrets, so they run too.
- **What's measured:** each run uploads `dogfood-summary.json` (90 days, the most a public repo
  keeps). It holds the server's own shadow comparison for the run (caught, missed with causes,
  flaky, measured time), the failure verdicts, the would-be selection and which steps failed.
  `python -m scripts.dogfood_report report [--since DATE] [--by week]` downloads the
  summaries, keeps each workflow run's first attempt, and prints `siftwise report`'s text.
  The numbers are exactly what `/recall` adds up (a test checks this against the real server).
  `--save DIR` keeps copies past 90 days, and `--from DIR` reads them back.

### Limits

- **A small sample at first:** a handful of PRs a week, and few real failures. Report raw
  counts, as for any shadow mode.
- **Our own PRs only:** one Python repo, mostly changed by one person with coding agents. The
  numbers say how siftwise does on siftwise, not on other repos.
- **PR history isn't shared:** a PR sees `main`'s history plus its own earlier runs, never
  other PRs' (cache scope). A hosted server would see every run, so the co-change and
  recently-failed signals see a little less here.
- **History can reset:** if `main`'s cache entries are evicted (unused for 7 days, despite a
  twice-weekly keep-alive, or over the 10 GB repo limit), the history starts again and selection
  falls back to the full suite until it rebuilds. Those runs count as full runs, not as misses.
- **Fork summaries are untrusted:** fork code builds them. The report counts them apart, and
  `--exclude-forks` leaves them out.
- **Runs on `main` are serialised:** each starts from the previous run's database. A third run
  queued behind a second is cancelled by GitHub's concurrency rules, and its data is lost.

## What the numbers prove, and what they don't

They show that, on these four repos' past CI:

- the fallbacks and graphs would have run almost every failing test (the misses are explained:
  a flaky remote test, and e2e suites no import edge reaches until a declared rule covers them);
- savings depend on structure: large in a monorepo's independent packages, near zero where
  everything imports everything;
- triage's `new_regression` and `pre_existing` verdicts held up where they could be checked,
  and it called no labelled rdflib regression flaky.

They don't show:

- recall on repos with dynamic dependencies the graphs can't see (plugins loaded by name from
  outside the repo, data-driven tests), beyond what `[[depends]]` covers;
- behaviour on languages other than Python and Go (Jest selection is implemented but not
  evaluated; Java always runs in full);
- live recall: no shadow-mode numbers are published yet (dogfooding runs since 2026-10-26;
  numbers with v0.3);
- that flaky verdicts generalise: most are rule 1, which re-reads the label's own evidence.

## Reproducing

Each repo's section in [eval-results.md](eval-results.md) ends with its commands
([kubo](eval-results.md#reproducing), [rdflib](eval-results.md#reproducing-1),
[substrate](eval-results.md#reproducing-2), [triage](eval-results.md#reproducing-3)). They
need the history backfilled, `GITHUB_TOKEN` (environment or `.env`) and, for Go repos, Go.
