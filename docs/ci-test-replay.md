# CI test replay

The repository's existing `coverage run --source src/auditlog src/runtests.py`
command now uses Django discovery through `ReplayDiscoverRunner`. Each tox job
still runs its complete test suite and stops after its first failure. History
reorders work; it does not select a subset of tests.

Travis runs the tox environments as separate matrix jobs. Its existing
`matrix.fast_finish` setting does not cancel required sibling jobs when one job
fails, so other queued or running Python/Django combinations may continue.
The replay runner contains failure within its own job; cross-job cancellation
is not provided by this Travis configuration.

The runner stores failed test IDs, interrupted test IDs, bounded failure
counts, and the last 12 run contexts in `.ci-replay-cache`. Context includes
repository, branch, pull request, commit, run, job, tox lane, and Python
version. The cache is scoped by repository, branch, lane, and Python version.
Assertions, exception text, logs, and environment dumps are not stored.

Previously failed tests run first, followed by tests left pending by an
interrupted job. A source change or unavailable Git ancestry conservatively
marks the entire suite as potentially affected; all remaining tests still run
with historically riskier tests first. No source-to-test dependency map is
assumed. A previous failure is cleared only by a normal successful test; skips,
expected failures, setup errors, and teardown errors do not clear it.

Checkpoint writes use a flushed temporary file and atomic rename before tests
start and after each result. A checkpoint error fails the job. The cache is
best-effort state: eviction, upload failures, concurrent jobs, or runner loss
can lose replay history. It is not a durable failure ledger and cannot promise
perfect replay. The existing Travis and tox matrix remains responsible for
job scheduling.

Focused adapter checks run with `python -m unittest discover -s ci_replay_tests`.
