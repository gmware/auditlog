"""Best-effort unittest replay ordering for Django's single test job.

This module uses only Python 2.7 standard-library syntax. History stores test
identifiers and CI provenance, never test output or environment dumps.
"""
from __future__ import print_function

import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import tempfile
import threading
import unittest

MAX_BYTES = 8 * 1024 * 1024
MAX_TESTS = 25000
TEST_TIMEOUT_SECONDS = 900


class TestTimeoutError(Exception):
    """Raised in the test thread when one unittest exceeds its deadline."""


def _test_timeout_handler(signum, frame):
    raise TestTimeoutError('test exceeded %s second deadline' %
                           TEST_TIMEOUT_SECONDS)


def _git(*args):
    try:
        process = subprocess.Popen(['git'] + list(args), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        timer = threading.Timer(5, process.kill)
        timer.start()
        try:
            output, _ = process.communicate()
        finally:
            timer.cancel()
        if process.returncode:
            return None
        return output.decode('utf-8', 'replace').strip()
    except (OSError, AttributeError):
        return None


def _context():
    env = os.environ
    head_ref = (env.get('CI_REPLAY_HEAD_REF') or
                env.get('TRAVIS_PULL_REQUEST_BRANCH') or
                env.get('GITHUB_HEAD_REF') or env.get('CI_MERGE_REQUEST_SOURCE_BRANCH_NAME') or
                env.get('TRAVIS_BRANCH') or env.get('GITHUB_REF_NAME')
                or env.get('CI_COMMIT_REF_NAME') or '')
    branch = (env.get('CI_REPLAY_BRANCH') or env.get('TRAVIS_BRANCH')
              or env.get('GITHUB_BASE_REF') or env.get('CI_MERGE_REQUEST_TARGET_BRANCH_NAME')
              or env.get('GITHUB_REF_NAME') or env.get('CI_COMMIT_REF_NAME')
              or _git('rev-parse', '--abbrev-ref', 'HEAD') or '')
    ref = env.get('GITHUB_REF', '')
    pr = env.get('TRAVIS_PULL_REQUEST') or env.get('CI_MERGE_REQUEST_IID')
    if pr == 'false':
        pr = ''
    if not pr and ref.startswith('refs/pull/'):
        pr = ref.split('/')[2]
    base_ref = (env.get('CI_REPLAY_BASE_REF') or env.get('TRAVIS_BRANCH')
                or env.get('GITHUB_BASE_REF') or
                env.get('CI_MERGE_REQUEST_TARGET_BRANCH_NAME') or '')
    base_sha = env.get('CI_REPLAY_BASE_SHA') or ''
    if not base_sha and re.match(r'^[A-Za-z0-9._/-]{1,200}$', base_ref):
        base_sha = _git('rev-parse', 'refs/remotes/origin/' + base_ref)
    values = {
        'repository': env.get('TRAVIS_REPO_SLUG') or env.get('GITHUB_REPOSITORY')
                      or env.get('CI_PROJECT_PATH') or '',
        'branch': branch,
        'head_ref': head_ref,
        'base_ref': base_ref,
        'base_sha': base_sha,
        'pr': pr or '',
        'sha': env.get('TRAVIS_COMMIT') or env.get('GITHUB_SHA') or
               env.get('CI_COMMIT_SHA') or _git('rev-parse', 'HEAD') or '',
        'run': env.get('TRAVIS_BUILD_ID') or env.get('GITHUB_RUN_ID')
               or env.get('CI_PIPELINE_ID') or '',
        'run_attempt': env.get('CI_REPLAY_RUN_ATTEMPT') or
                       env.get('GITHUB_RUN_ATTEMPT') or env.get('CI_JOB_RETRY') or '',
        'job': env.get('TRAVIS_JOB_ID') or env.get('GITHUB_JOB')
               or env.get('CI_JOB_ID') or '',
        'lane': env.get('CI_REPLAY_LANE') or env.get('TOX_ENV') or 'default',
        'python': platform.python_version(),
    }
    return dict((key, str(value)[:400]) for key, value in values.items())


def _origin_key(context):
    payload = json.dumps(context, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class ReplayHistory(object):
    def __init__(self, directory=None):
        self.context = _context()
        scope_kind = 'pull-request' if self.context['pr'] else 'push'
        scope_id = self.context['pr'] or self.context['branch']
        scope = [self.context['repository'], scope_kind, scope_id,
                 self.context['branch'], self.context['head_ref'],
                 self.context['lane'], self.context['python']]
        digest = hashlib.sha256(json.dumps(scope).encode('utf-8')).hexdigest()[:32]
        self.directory = os.path.abspath(directory or
                                         os.environ.get('CI_REPLAY_CACHE_DIR', '.ci-replay-cache'))
        self.path = os.path.join(self.directory, digest + '.json')
        self.load_path = self.path
        if not self.context['pr'] and not os.path.exists(self.path):
            legacy_scope = [self.context[key] for key in
                            ('repository', 'branch', 'lane', 'python')]
            legacy_digest = hashlib.sha256(
                json.dumps(legacy_scope).encode('utf-8')).hexdigest()[:32]
            legacy_path = os.path.join(self.directory, legacy_digest + '.json')
            if os.path.exists(legacy_path):
                self.load_path = legacy_path
        self.state = {'version': 2, 'failed': [], 'pending': [], 'risk': {},
                      'contexts': [], 'origins': {}, 'origin_contexts': {},
                      'legacy_unattributed': []}
        if (os.path.islink(self.directory) or os.path.islink(self.path) or
                os.path.islink(self.load_path)):
            raise ValueError('Invalid replay history: symbolic link')
        if os.path.exists(self.load_path):
            self._load()
        self.previous_sha = (self.state['contexts'][-1].get('sha', '')
                             if self.state['contexts'] else '')
        self.state['contexts'] = (self.state['contexts'] + [self.context])[-12:]
        self.failed_this_run = set()
        self.passed_tests = set()

    def _valid_ids(self, value):
        return (isinstance(value, list) and len(value) <= MAX_TESTS and
                all(isinstance(item, type(u'')) and len(item) <= 2000
                    for item in value))

    def _load(self):
        try:
            if os.path.getsize(self.load_path) > MAX_BYTES:
                raise ValueError('oversized state')
            with open(self.load_path) as stream:
                value = json.load(stream)
            if (not isinstance(value, dict) or value.get('version') not in (1, 2) or
                    not self._valid_ids(value.get('failed')) or
                    not self._valid_ids(value.get('pending')) or
                    not isinstance(value.get('risk'), dict) or
                    len(value['risk']) > MAX_TESTS or
                    not self._valid_ids(list(value['risk'])) or
                    not all(isinstance(n, int) and 0 <= n <= 1000000
                            for n in value['risk'].values()) or
                    not isinstance(value.get('contexts'), list) or
                    len(value['contexts']) > 12 or
                    not all(isinstance(item, dict) and
                            isinstance(item.get('sha', ''), type(u''))
                            for item in value['contexts'])):
                raise ValueError('invalid schema')
            if value['version'] == 1:
                unresolved = list(set(value['failed']) | set(value['pending']))
                value = dict(value)
                value['version'] = 2
                value['origins'] = {}
                value['origin_contexts'] = {}
                value['legacy_unattributed'] = unresolved
            elif (not isinstance(value.get('origins'), dict) or
                  len(value['origins']) > MAX_TESTS or
                  not all(isinstance(identifier, type(u'')) and
                          self._valid_ids(keys) and
                          all(isinstance(key, type(u'')) and
                              key in value.get('origin_contexts', {}) for key in keys)
                          for identifier, keys in value['origins'].items()) or
                  not isinstance(value.get('origin_contexts'), dict) or
                  len(value['origin_contexts']) > MAX_TESTS * 4 or
                  not all(isinstance(key, type(u'')) and isinstance(context, dict)
                          for key, context in value['origin_contexts'].items()) or
                  not self._valid_ids(value.get('legacy_unattributed'))):
                raise ValueError('invalid provenance schema')
            self.state = value
        except (ValueError, TypeError, AttributeError, IOError, OSError) as exc:
            raise ValueError('Invalid replay history: ' + str(exc))

    def save(self):
        if (len(self.state['failed']) > MAX_TESTS or
                len(self.state['pending']) > MAX_TESTS or
                len(self.state['risk']) > MAX_TESTS or
                len(self.state['origins']) > MAX_TESTS or
                len(self.state['origin_contexts']) > MAX_TESTS * 4):
            raise ValueError('Replay history exceeds supported test count')
        if not os.path.isdir(self.directory):
            os.makedirs(self.directory)
        payload = json.dumps(self.state, sort_keys=True).encode('utf-8')
        if len(payload) > MAX_BYTES:
            raise ValueError('Replay history exceeds supported byte count')
        fd, temporary = tempfile.mkstemp(prefix='.checkpoint-', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _changed_files(self):
        if not re.match(r'^[0-9a-f]{40}$', self.previous_sha):
            return None
        output = _git('diff', '--no-ext-diff', '--name-only', self.previous_sha,
                      'HEAD', '--')
        return None if output is None else set(output.splitlines())

    def order(self, tests, pending_ids=None):
        failed, pending = set(self.state['failed']), set(self.state['pending'])
        risk = self.state['risk']
        changed = self._changed_files()

        def priority(test):
            identifier = test.id()
            if identifier in failed:
                return (0, 0)
            if identifier in pending:
                return (1, 0)
            if changed is None or changed:
                return (2, -risk.get(identifier, 0))
            return (3, -risk.get(identifier, 0))

        tests.sort(key=priority)
        recorded_ids = []
        seen = set()
        if pending_ids is None:
            pending_ids = [test.id() for test in tests]
        for identifier in list(pending_ids) + self.state['pending']:
            if identifier not in seen:
                recorded_ids.append(identifier)
                seen.add(identifier)
        self.state['pending'] = recorded_ids
        for test in tests:
            self._record_origin(test.id())
        self.save()
        return tests

    def priority_ids(self, tests):
        """Return candidates for the global replay-first tox phase."""
        identifiers = set(test.id() for test in tests)
        changed = self._changed_files()
        if changed is None or changed:
            return identifiers
        return (identifiers & (set(self.state['failed']) |
                               set(self.state['pending']) |
                               set(self.state['risk'])))

    def _record_origin(self, identifier):
        key = _origin_key(self.context)
        self.state['origin_contexts'][key] = self.context
        origins = self.state['origins'].setdefault(identifier, [])
        if key not in origins:
            origins.append(key)

    def _prune_origins(self):
        unresolved = set(self.state['failed']) | set(self.state['pending'])
        self.state['origins'] = dict(
            (identifier, keys) for identifier, keys in self.state['origins'].items()
            if identifier in unresolved)
        referenced = set(key for keys in self.state['origins'].values() for key in keys)
        self.state['origin_contexts'] = dict(
            (key, context) for key, context in self.state['origin_contexts'].items()
            if key in referenced)
        self.state['legacy_unattributed'] = [
            item for item in self.state['legacy_unattributed'] if item in unresolved]

    def error(self, test, pending=False):
        identifier = test.id()
        self._record_origin(identifier)
        first_error = identifier not in self.failed_this_run
        self.failed_this_run.add(identifier)
        if identifier not in self.state['failed']:
            self.state['failed'].append(identifier)
        if pending and identifier not in self.state['pending']:
            self.state['pending'].append(identifier)
        if first_error:
            self.state['risk'][identifier] = min(
                1000000, self.state['risk'].get(identifier, 0) + 1)
        self.save()

    def complete(self, test, passed=False):
        identifier = test.id()
        self.state['pending'] = [item for item in self.state['pending']
                                 if item != identifier]
        if passed and identifier not in self.failed_this_run:
            self.passed_tests.add(identifier)
            self.state['failed'] = [item for item in self.state['failed']
                                    if item != identifier]
            self.state['legacy_unattributed'] = [
                item for item in self.state['legacy_unattributed'] if item != identifier]
        self._prune_origins()
        self.save()


class ReplayTextTestResult(unittest.TextTestResult):
    history = None
    known_tests = ()

    def _fixture_targets(self, test):
        if isinstance(test, unittest.TestCase):
            return [test], False

        description = getattr(test, 'description', None) or test.id()
        match = re.match(r'^(setUp|tearDown)Class \((.+)\)$', description)
        if match:
            scope = match.group(2)
            targets = [item for item in self.known_tests
                       if scope in (item.__class__.__module__ + '.' +
                                    getattr(item.__class__, '__qualname__',
                                            item.__class__.__name__),
                                    item.__class__.__module__ + '.' +
                                    item.__class__.__name__)]
            return targets, True

        match = re.match(r'^(setUp|tearDown)Module \((.+)\)$', description)
        if match:
            scope = match.group(2)
            targets = [item for item in self.known_tests
                       if item.__class__.__module__ == scope]
            return targets, True

        return [], True

    def startTest(self, test):
        self._test_errors = set()
        if not hasattr(signal, 'SIGALRM') or not hasattr(signal, 'alarm'):
            raise RuntimeError('per-test timeout requires POSIX SIGALRM')
        previous_alarm = signal.alarm(0)
        previous_handler = signal.getsignal(signal.SIGALRM)
        try:
            signal.signal(signal.SIGALRM, _test_timeout_handler)
            signal.alarm(TEST_TIMEOUT_SECONDS)
        except Exception:
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_alarm:
                signal.alarm(previous_alarm)
            raise
        self._previous_alarm = (previous_handler, previous_alarm)
        unittest.TextTestResult.startTest(self, test)

    def stopTest(self, test):
        previous_alarm = getattr(self, '_previous_alarm', None)
        if previous_alarm is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_alarm[0])
            if previous_alarm[1]:
                signal.alarm(previous_alarm[1])
            self._previous_alarm = None
        unittest.TextTestResult.stopTest(self, test)

    def addError(self, test, err):
        if not hasattr(self, '_test_errors'):
            self._test_errors = set()
        targets, fixture_error = self._fixture_targets(test)
        if fixture_error:
            for target in targets:
                self._test_errors.add(target.id())
                self.history.error(target, pending=True)
        else:
            self._test_errors.add(test.id())
            self.history.error(test)
            self.history.complete(test)
        return unittest.TextTestResult.addError(self, test, err)

    def addFailure(self, test, err):
        if not hasattr(self, '_test_errors'):
            self._test_errors = set()
        self._test_errors.add(test.id())
        self.history.error(test)
        self.history.complete(test)
        return unittest.TextTestResult.addFailure(self, test, err)

    def addSuccess(self, test):
        passed = test.id() not in self._test_errors
        self.history.complete(test, passed=passed)
        return unittest.TextTestResult.addSuccess(self, test)

    def addSkip(self, test, reason):
        self.history.complete(test)
        return unittest.TextTestResult.addSkip(self, test, reason)

    def addExpectedFailure(self, test, err):
        self.history.complete(test)
        return unittest.TextTestResult.addExpectedFailure(self, test, err)

    def addUnexpectedSuccess(self, test):
        self.history.error(test)
        self.history.complete(test)
        return unittest.TextTestResult.addUnexpectedSuccess(self, test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            if not hasattr(self, '_test_errors'):
                self._test_errors = set()
            self._test_errors.add(test.id())
            self.history.error(test)
            self.history.complete(test)
        return unittest.TextTestResult.addSubTest(self, test, subtest, err)


class ReplayTextTestRunner(unittest.TextTestRunner):
    def __init__(self, *args, **kwargs):
        history = kwargs.pop('replay_history')
        known_tests = kwargs.pop('known_tests', ())
        kwargs['failfast'] = True
        kwargs['resultclass'] = type(
            'ReplayBoundTextTestResult', (ReplayTextTestResult,),
            {'history': history, 'known_tests': tuple(known_tests)})
        unittest.TextTestRunner.__init__(self, *args, **kwargs)


from django.test.runner import DiscoverRunner


class ReplayDiscoverRunner(DiscoverRunner):
    """Django runner retaining the standard discovery and DB lifecycle."""

    def run_suite(self, suite, **kwargs):
        history = ReplayHistory()
        tests = []

        def flatten(items):
            for item in items:
                if isinstance(item, unittest.TestSuite):
                    flatten(item)
                else:
                    tests.append(item)

        flatten(suite)
        priority_ids = history.priority_ids(tests)
        phase = os.environ.get('CI_REPLAY_PHASE', 'canonical')
        if phase not in ('priority', 'canonical'):
            raise ValueError('Invalid CI_REPLAY_PHASE: %s' % phase)
        unresolved_before = (set(history.state['failed']) |
                             set(history.state['pending']))
        phase_pending_ids = None
        if phase == 'priority':
            discovered_ids = set(test.id() for test in tests)
            missing = unresolved_before - discovered_ids
            if missing:
                raise RuntimeError(
                    'Unresolved replay tests are missing from discovery: %s' %
                    ', '.join(sorted(missing)))
            phase_pending_ids = [test.id() for test in tests
                                 if test.id() in priority_ids]
        ordered = history.order(tests, pending_ids=phase_pending_ids)
        if phase == 'priority':
            ordered = [test for test in ordered if test.id() in priority_ids]
        suite = unittest.TestSuite(ordered)
        runner = ReplayTextTestRunner(
            verbosity=self.verbosity, replay_history=history,
            known_tests=tests)
        result = runner.run(suite)
        if phase == 'priority':
            not_passed = unresolved_before - history.passed_tests
            if not_passed:
                raise RuntimeError(
                    'Unresolved replay tests did not pass: %s' %
                    ', '.join(sorted(not_passed)))
        return result
