# siftwise on GitLab CI: a self-contained example

A small Python project (`shop/`, four tests) with a `.gitlab-ci.yml` that uses the siftwise
GitLab template. To be pushed on its own to a new GitLab project. Each job runs its own siftwise
server and Postgres (a `services:` container), with a random token made in the job, so you
need no hosted server and no siftwise secret. The only CI/CD variable is
`SIFTWISE_GITLAB_TOKEN`, for the merge request note.

**A real setup runs one persistent siftwise server** (see the main README), which keeps
every pipeline's results: the default branch's runs are there when a merge request pipeline
asks what to run, with nothing to move around. This example has no server between
pipelines, so it does two things a real setup doesn't:

- **History moves between the jobs of one pipeline** as artifacts (`.siftwise-history/`,
  written by `ci/history.py dump`, loaded by `ci/history.py restore`). Nothing moves between
  pipelines: a `main` pipeline starts empty and records its own run.
- **A merge request pipeline seeds its history from the MR's base commit.** Its first job,
  `siftwise-base`, checks out `CI_MERGE_REQUEST_DIFF_BASE_SHA` in a worktree, runs the full
  suite there and records it as a `main` run of that commit (`ci/history.py record-base`,
  with plain `siftwise record` and its own run ID: `siftwise gitlab record` would use this
  pipeline's ID, which the MR's own upload in the `test` job needs). So `siftwise-select` and the triage in `siftwise-rollup` have main's
  results to compare with. It costs one full test run per merge request pipeline.

What we learned on gitlab.com: an earlier version read the default branch's history from its
latest `siftwise-rollup` job's artifacts with `CI_JOB_TOKEN`
(`GET /projects/:id/jobs/artifacts/main/download?job=siftwise-rollup&search_recent_successful_pipelines=true`). On a private Free
project (2026-10-26) that request got `404 {"message":"404 Project Not Found"}` from a merge
request pipeline, although the artifacts existed and the docs list the artifact downloads among
the job token's [allowed endpoints](https://docs.gitlab.com/ci/jobs/ci_job_token/). We
haven't verified why, so the example no longer depends on it.

## Run it on gitlab.com

1. **siftwise with `siftwise gitlab`.** Until a release that has it is on PyPI, build a wheel
   from the siftwise checkout and ship it with the example:

   ```sh
   uv build --wheel                       # in the siftwise checkout: dist/siftwise-<version>-py3-none-any.whl
   cp -r examples/gitlab ~/siftwise-gitlab-example
   mkdir ~/siftwise-gitlab-example/wheels
   cp dist/siftwise-*-py3-none-any.whl ~/siftwise-gitlab-example/wheels/
   ```

   Then, in `.gitlab-ci.yml`'s `.example-server` variables, point both specs at it (the
   comment there shows how):
   `SIFTWISE_PIP_SPEC: "./wheels/siftwise-<version>-py3-none-any.whl"` and
   `EXAMPLE_SERVER_PIP_SPEC: "./wheels/siftwise-<version>-py3-none-any.whl[server]"`.

2. **A new project:** on gitlab.com, create a blank private project (no README), then push the
   folder as its `main` branch:

   ```sh
   cd ~/siftwise-gitlab-example
   git init -b main && git add . && git commit -m "siftwise GitLab example"
   git remote add origin git@gitlab.com:<you>/<project>.git
   git push -u origin main
   ```

   This first pipeline has no history: `siftwise-select` runs the full suite, `test` passes
   and records the run, `siftwise-rollup` passes. Every `main` pipeline is like this one,
   since nothing carries over between pipelines.

3. **The note's token.** On GitLab.com Free, create a fine-grained personal access token
   (avatar > Edit profile > Access > Personal access tokens > Generate token > Fine-grained
   token). Use a dedicated bot account if you have one, else your own for this test. Give it:
   - access to this project only;
   - **Work Item**: Create, Read, Update;
   - **Merge Request**: Read;
   - **User**: Read;
   - an expiry date a few days out.

   On Premium or Ultimate, a project access token (Reporter, `api` scope) works too.

   Add it under Settings > CI/CD > Variables as `SIFTWISE_GITLAB_TOKEN`. Set **Visibility** to
   Masked and hidden. Clear **Protect variable** (the MR branch below isn't protected) and
   **Expand variable reference**.

4. **A merge request with a regression:**

   ```sh
   git checkout -b break-discount
   sed -i 's|cents \* 9 // 10|cents * 8 // 10|' shop/cart.py   # 20% off instead of 10%
   git commit -am "Bigger bulk discount" && git push -u origin break-discount
   ```

   Open a merge request from `break-discount`. Its pipeline:
   - **`siftwise-base`** runs the four tests at the MR's base commit (all pass) and records
     them as a `main` run. Its log ends with `history: recording <sha> (main) as a main run`,
     `siftwise: run 1 stored (4 results)` and `history: saved ... (1 runs, 1 on main, 4 tests)`.
   - **`siftwise-select`** logs `history: restored ... 1 runs, 1 on main, 4 tests`, and is
     selective: `tests/test_cart.py` only, because it imports the
     changed `shop.cart`. `test_prices.py` is skipped. The MR gets a siftwise note with the
     selection.
   - **`test`** runs `tests/test_cart.py`; `test_bulk_discount` fails.
   - **`siftwise-rollup`** adds a Failures section to the note:
     `tests.test_cart::test_bulk_discount`, **new regression** (high), because `main` passed it
     at the base commit and the change links to it (`tests/test_cart.py` maps to the changed
     `shop/cart.py`). It then fails the job, because of
     `SIFTWISE_FAIL_ON: new_regression,unknown`.

## What to check

- **The note:**
  - one siftwise note, edited in place on each push;
  - the selection, then the failures;
  - test IDs and reasons in code formatting.
- **The logs:** each job's log ends with `siftwise: ...` lines. Search them for the token's
  value: it must not appear (siftwise redacts it; GitLab masks it too).
- **The dotenv:** `siftwise-select`'s artifacts have `.siftwise-ci/select.env`, which holds
  `SIFTWISE_MODE=selective` and `SIFTWISE_COMMAND_FILE=.siftwise-ci/command.sh`. The `test`
  job's log shows it running that command.
- **The full list:** `siftwise-rollup`'s artifacts have `.siftwise-ci/failures.md`.
- **Fail-open:**
  - Remove `SIFTWISE_GITLAB_TOKEN` and push again: no note, with a warning, and the rest
    unchanged.
  - Set `SIFTWISE_SERVER` to `https://nowhere.invalid` as a CI/CD variable (it overrides the
    YAML's): the test job runs the full suite, with warnings. `siftwise-rollup` fails,
    because with `fail-on` set, unavailable verdicts fail closed. Delete the variable
    afterwards.

## The history log lines

Every job's log has `history:` lines from `ci/history.py`:

- `history: no .siftwise-history/ from an earlier job of this pipeline: starting empty`: the
  first job of a `main` pipeline, and `siftwise-base`. Anywhere else in a merge request
  pipeline it means `siftwise-base` failed (it's allowed to fail, so the later jobs run the
  full suite): see its log.
- `history: restored N tables from .siftwise-history/ (this pipeline's artifacts): R runs, M
  on main, T tests`: what the job starts from. In a merge request pipeline, the one run on
  `main` is the base commit's, from `siftwise-base`; `test` adds the MR's own run.
- `history: not in .siftwise-history/, left empty: ...`: tables the earlier job didn't save
  (an artifact from an older siftwise).
- `history: saved N tables to .siftwise-history/ (...)`: what the job passes on.

No job uses `CI_JOB_TOKEN` or any other GitLab token for the history, and `ci/history.py`
never prints a token: each job's siftwise token is random, made in the job and never printed.

## Clean up

Revoke the token, or let it expire, and delete the test project.
