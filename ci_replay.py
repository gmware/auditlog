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
import subprocess
import tempfile
import threading
import unittest

MAX_BYTES = 8 * 1024 * 1024
MAX_TESTS = 25000


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
    branch = (env.get('CI_REPLAY_BRANCH') or env.get('TRAVIS_PULL_REQUEST_BRANCH')
              or env.get('TRAVIS_BRANCH') or env.get('GITHUB_HEAD_REF')
              or env.get('GITHUB_REF_NAME') or env.get('CI_COMMIT_REF_NAME')
              or _git('rev-parse', '--abbrev-ref', 'HEAD') or '')
    ref = env.get('GITHUB_REF', '')
    pr = env.get('TRAVIS_PULL_REQUEST') or env.get('CI_MERGE_REQUEST_IID')
    if not pr and ref.startswith('refs/pull/'):
        pr = ref.split('/')[2]
    values = {
        'repository': env.get('TRAVIS_REPO_SLUG') or env.get('GITHUB_REPOSITORY')
                      or env.get('CI_PROJECT_PATH') or '',
        'branch': branch,
        'pr': pr or '',
        'sha': _git('rev-parse', 'HEAD') or '',
        'run': env.get('TRAVIS_BUILD_ID') or env.get('GITHUB_RUN_ID')
               or env.get('CI_PIPELINE_ID') or '',
        'job': env.get('TRAVIS_JOB_ID') or env.get('GITHUB_JOB')
               or env.get('CI_JOB_ID') or '',
        'lane': env.get('CI_REPLAY_LANE') or env.get('TOX_ENV') or 'default',
        'python': platform.python_version(),
    }
    return dict((key, str(value)[:400]) for key, value in values.items())


class ReplayHistory(object):
    def __init__(self, directory=None):
        self.context = _context()
        scope = [self.context[key] for key in
                 ('repository', 'branch', 'lane', 'python')]
        digest = hashlib.sha256(json.dumps(scope).encode('utf-8')).hexdigest()[:32]
        self.directory = os.path.abspath(directory or
                                         os.environ.get('CI_REPLAY_CACHE_DIR', '.ci-replay-cache'))
        self.path = os.path.join(self.directory, digest + '.json')
        self.state = {'version': 1, 'failed': [], 'pending': [],
                      'risk': {}, 'contexts': []}
        if os.path.islink(self.directory) or os.path.islink(self.path):
            raise ValueError('Invalid replay history: symbolic link')
        if os.path.exists(self.path):
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
            if os.path.getsize(self.path) > MAX_BYTES:
                raise ValueError('oversized state')
            with open(self.path) as stream:
                value = json.load(stream)
            if (not isinstance(value, dict) or value.get('version') != 1 or
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
            self.state = value
        except (ValueError, TypeError, AttributeError, IOError, OSError) as exc:
            raise ValueError('Invalid replay history: ' + str(exc))

    def save(self):
        if (len(self.state['failed']) > MAX_TESTS or
                len(self.state['pending']) > MAX_TESTS or
                len(self.state['risk']) > MAX_TESTS):
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

    def order(self, tests):
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
        pending_ids = []
        seen = set()
        for identifier in [test.id() for test in tests] + self.state['pending']:
            if identifier not in seen:
                pending_ids.append(identifier)
                seen.add(identifier)
        self.state['pending'] = pending_ids
        self.save()
        return tests

    def error(self, test):
        identifier = test.id()
        first_error = identifier not in self.failed_this_run
        self.failed_this_run.add(identifier)
        if identifier not in self.state['failed']:
            self.state['failed'].append(identifier)
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
        self.save()


class ReplayTextTestResult(unittest.TextTestResult):
    history = None

    def startTest(self, test):
        self._test_errors = set()
        unittest.TextTestResult.startTest(self, test)

    def addError(self, test, err):
        if not hasattr(self, '_test_errors'):
            self._test_errors = set()
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
        kwargs['failfast'] = True
        kwargs['resultclass'] = type(
            'ReplayBoundTextTestResult', (ReplayTextTestResult,),
            {'history': history})
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
        suite = unittest.TestSuite(history.order(tests))
        runner = ReplayTextTestRunner(
            verbosity=self.verbosity, replay_history=history)
        return runner.run(suite)
