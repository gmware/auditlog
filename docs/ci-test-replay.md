# CI test replay

Travis invokes `python ci_tox_matrix.py` through GNU `timeout` from one Python
3.6 job. The outer command has a 40-minute deadline, sends SIGINT at expiry,
and allows 30 seconds for the supervisor's bounded process-group cleanup before
SIGKILL. Each unittest also has a 15-minute SIGALRM deadline; a timeout is
recorded as an ordinary test error, triggers unittest fail-fast, and leaves the
test unresolved in replay history. The supervisor
runs all 15 tox environments in a priority phase, with at most five tox process
groups active at once. Only after every priority phase succeeds does it start
the canonical phase across the tox environments. Each environment's Django
`ReplayDiscoverRunner` selects its failed, interrupted, and historically risky
tests for the priority phase. If Git ancestry is unavailable or any source file
changed since that environment's last recorded run, its whole discovered suite
is conservatively treated as affected and runs in the priority phase. The
canonical phase still runs the whole suite. Thus, when the whole suite is
classified as affected, those tests run again in the canonical phase.

History persistence is best-effort. Travis can restore pull-request caches, but
cache misses, eviction, failed uploads, runner loss, and overlapping builds
writing the same scope can lose or overwrite history. The replay runner handles
a missing snapshot by running the complete affected-test candidate set before
the canonical suite; the provider cache is not a durable failure ledger.

The supervisor stops starting environments on the first failure, terminates
active tox process groups with bounded TERM/KILL cleanup, and never starts the
canonical phase after a priority failure. A failure in the canonical phase
also stops active siblings and prevents coverage aggregation. Each tox
environment gets a distinct PostgreSQL test database, SQLite test database
path, and coverage data file. Coverage is combined and uploaded only after both
phases pass. PyPI deployment remains a separate final Travis stage gated on the
stable branch and `py36-django-20`; that stage runs only after the supervisor
job succeeds.

The runner stores failed test IDs, interrupted test IDs, bounded failure
counts, a rolling list of the last 12 general run contexts, and origin
contexts referenced by unresolved tests in `.ci-replay-cache`. Pull-request
cache keys have an explicit pull-request scope and include repository, PR
number, base branch, head ref, tox lane, and Python version. Push keys have a
separate push scope keyed by repository and branch, so a push cannot share or
clear a pull request's history. Each unresolved test separately references every
origin context that observed its failure or interruption. Those origin records
remain after they leave the rolling list. If the supported history-size limit is
reached, checkpointing fails rather than dropping unresolved origins. An origin records
repository, PR number, head and base refs, base SHA when available, commit SHA,
run ID, run attempt when the provider exposes it, job, tox lane, and Python
version. Missing provider fields stay empty. Contexts referenced by unresolved
tests are retained even after they leave the bounded overall context list.
Legacy v1 cache entries on non-PR branch builds retain their failed/pending IDs
and are marked as having unattributed historical provenance; a new run is not
represented as the origin of that old failure. PR builds do not import legacy
cache paths because the old key lacked PR identity and could mix separate PRs.
Assertions, exception text, logs, and environment dumps are not stored.

Previously failed tests run first, followed by tests left pending by an
interrupted job. A source change or unavailable Git ancestry conservatively
marks the entire suite as potentially affected; all remaining tests still run
with historically riskier tests first. No source-to-test dependency map is
assumed. A previous failure is cleared only by a normal successful test; skips,
expected failures, setup errors, and teardown errors do not clear it.
The priority phase fails before canonical execution if an unresolved ID is
missing from discovery or does not receive a clean pass. It marks only tests
selected for that phase as pending, so ordinary tests that have not started are
not misreported as interrupted.

Checkpoint writes use a flushed temporary file and atomic rename before tests
start and after each result. A checkpoint error fails the job. This is
best-effort replay state and cannot promise perfect replay. A cache miss or
migrated valid legacy v1 cache causes the full candidate suite to run;
malformed cache data fails the job rather than discarding unresolved history.
Replay never omits tests.

Focused adapter checks run once in the `py36-django-20` tox environment, during
the canonical phase and only after the supervisor has completed the global
priority phase. They run before that lane's canonical app suite, so their
failure also stops active canonical tox siblings. Other tox environments run
only the replay-selected app tests in the priority phase. The Travis CLI is not
part of local validation; regression checks assert that the base test job
invokes `ci_tox_matrix.py`, that all tox lanes route through `ci_tox_test.py`
and `ReplayDiscoverRunner`, and that deployment remains in the final stage.
Travis remains the authority for compiled config.

Coverage files use `.coverage.<tox-env>`. The supervisor combines them after
every tox environment succeeds in both phases; a failed or cancelled run leaves
the separate partial files intact and does not publish an aggregate.
