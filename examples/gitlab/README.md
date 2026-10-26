# siftwise on GitLab CI: a self-contained example

A small Python project (`shop/`, four tests) with a `.gitlab-ci.yml` that uses the siftwise
GitLab template. To be pushed on its own to a new GitLab project. Each job runs its own siftwise
server and Postgres (a `services:` container), with a random token made in the job, so you
need no hosted server and no siftwise secret. The only CI/CD variable is
`SIFTWISE_GITLAB_TOKEN`, for the merge request note.

History moves between jobs as artifacts (`.siftwise-history/`, written by `ci/history.py`). A
merge request pipeline starts from the latest successful pipeline of the default branch: it
reads the `siftwise-rollup` job's artifacts with `CI_JOB_TOKEN`. So the main pipeline records
history, and merge requests read it without writing it. A real project runs one siftwise
server instead.

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

   This first pipeline records history: `siftwise-select` runs the full suite (no history
   yet), `test` passes, `siftwise-rollup` passes. Check that `siftwise-rollup`'s artifacts
   have `.siftwise-history/`. **Wait until this pipeline has passed before step 4:** a
   pipeline that starts earlier finds no successful `main` pipeline to read history from and
   starts empty (see [The history log line](#the-history-log-line)).

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
   - **`siftwise-select`** is selective: `tests/test_cart.py` only, because it imports the
     changed `shop.cart`. `test_prices.py` is skipped. The MR gets a siftwise note with the
     selection.
   - **`test`** runs `tests/test_cart.py`; `test_bulk_discount` fails.
   - **`siftwise-rollup`** adds a Failures section to the note:
     `tests.test_cart::test_bulk_discount`, **new regression**, because the change links to it
     through the import. It then fails the job, because of
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

## The history log line

Every job's log has a `history:` line from `ci/history.py restore`. In a merge request
pipeline it reads `main`'s history with the job's own `CI_JOB_TOKEN`, from
`GET /projects/:id/jobs/artifacts/main/download?job=siftwise-rollup&search_recent_successful_pipelines=true`
([Download job artifacts by reference name](https://docs.gitlab.com/api/job_artifacts/#download-job-artifacts-by-reference-name)).
The job token can call this endpoint for its own project on every tier, Free included: the
job artifacts download endpoints are on its list of
[allowed endpoints](https://docs.gitlab.com/ci/jobs/ci_job_token/), and a project's job
token allowlist always includes the project itself. No other token is needed.

- `HTTP 200, N bytes: the default branch's history`: it worked.
- `HTTP 404 '{"message":"404 Not found"}'`: GitLab found no successful `main` pipeline whose
  `siftwise-rollup` job kept artifacts. Usually `main`'s pipeline hadn't finished when this
  pipeline started. Expiry is unlikely: GitLab keeps the most recent successful pipeline's
  artifacts of each branch whatever their `expire_in`, unless **Keep artifacts from most
  recent successful jobs** is turned off
  ([docs](https://docs.gitlab.com/ci/jobs/job_artifacts/#keep-artifacts-from-most-recent-successful-jobs)). Let
  `main`'s pipeline pass, then run the merge request's pipeline again (**Run pipeline** on
  its Pipelines tab; retrying one job isn't enough, since each job restores on its own).
- `HTTP 404 '{"message":"404 Project Not Found"}'`: the token's user can't see the project.
- `HTTP 401`: the job token was refused. It is valid only while its job runs.
- `HTTP 403`: the job token was refused for this project (Settings > CI/CD > Job token
  permissions).

The token is sent in the `JOB-TOKEN` header, never in the URL, and never logged.

## Clean up

Revoke the token, or let it expire, and delete the test project.
