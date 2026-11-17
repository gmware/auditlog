"""Focused replay adapter contract tests, separate from Django discovery."""
import io
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest

import ci_replay
from ci_replay import (ReplayHistory, ReplayTextTestResult,
                       ReplayTextTestRunner, ReplayDiscoverRunner)


class SampleTest(unittest.TestCase):
    def test_pass(self):
        pass

    def test_skip(self):
        self.skipTest('skip')


class ReplayHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='ci-replay-test-')
        self.old_cache = os.environ.get('CI_REPLAY_CACHE_DIR')
        os.environ['CI_REPLAY_CACHE_DIR'] = self.directory
        self.history = ReplayHistory()

    def tearDown(self):
        if self.old_cache is None:
            os.environ.pop('CI_REPLAY_CACHE_DIR', None)
        else:
            os.environ['CI_REPLAY_CACHE_DIR'] = self.old_cache
        shutil.rmtree(self.directory)

    def _result(self, known_tests=()):
        result = ReplayTextTestResult(
            stream=io.StringIO(), descriptions=False, verbosity=0)
        result.history = self.history
        result.known_tests = tuple(known_tests)
        return result

    def test_skip_does_not_clear_previous_failure(self):
        test = SampleTest('test_skip')
        self.history.error(test)
        self.history.state['pending'] = [test.id()]
        self.history.save()
        origins = list(self.history.state['origins'][test.id()])

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addSkip(test, 'skip')

        self.assertEqual([test.id()], self.history.state['failed'])
        self.assertEqual([], self.history.state['pending'])
        self.assertEqual(origins, self.history.state['origins'][test.id()])

    def test_expected_failure_does_not_clear_previous_failure(self):
        test = SampleTest('test_pass')
        self.history.state['failed'] = [test.id()]
        self.history.state['pending'] = [test.id()]
        self.history.save()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addExpectedFailure(
            test, (AssertionError, AssertionError('expected'), None))

        self.assertEqual([test.id()], self.history.state['failed'])
        self.assertEqual([], self.history.state['pending'])

    def test_only_clean_success_clears_previous_failure(self):
        test = SampleTest('test_pass')
        self.history.error(test)
        self.history.failed_this_run = set()
        self.history.state['pending'] = [test.id()]
        self.history.save()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addSuccess(test)

        self.assertEqual([], self.history.state['failed'])
        self.assertEqual([], self.history.state['pending'])
        self.assertNotIn(test.id(), self.history.state['origins'])

    def test_timed_out_test_fails_fast_and_keeps_failure_history(self):
        executed = []

        class DeadlineTests(unittest.TestCase):
            def test_times_out(self):
                executed.append('timed-out')
                time.sleep(2)

            def test_later_test_does_not_start(self):
                executed.append('later')

        previous_timeout = getattr(ci_replay, 'TEST_TIMEOUT_SECONDS', None)
        ci_replay.TEST_TIMEOUT_SECONDS = 1
        tests = [DeadlineTests('test_times_out'),
                 DeadlineTests('test_later_test_does_not_start')]
        try:
            runner = ReplayTextTestRunner(
                stream=io.StringIO(), descriptions=False, verbosity=0,
                replay_history=self.history, known_tests=tests)
            result = runner.run(unittest.TestSuite(tests))
        finally:
            if previous_timeout is None:
                del ci_replay.TEST_TIMEOUT_SECONDS
            else:
                ci_replay.TEST_TIMEOUT_SECONDS = previous_timeout

        self.assertFalse(result.wasSuccessful())
        self.assertEqual(1, result.testsRun)
        self.assertEqual(['timed-out'], executed)
        self.assertEqual([tests[0].id()], self.history.state['failed'])

    def test_unresolved_failure_origins_survive_context_history_limit(self):
        test = SampleTest('test_pass')
        original = dict(self.history.context)
        self.history.error(test)
        original_key = self.history.state['origins'][test.id()][0]

        for index in range(15):
            context = dict(original)
            context['run'] = 'run-%s' % index
            context['sha'] = 'sha-%s' % index
            self.history.context = context
            self.history.failed_this_run = set()
            self.history.state['contexts'].append(context)
            self.history.state['contexts'] = self.history.state['contexts'][-12:]
            self.history.error(test)

        self.history.save()
        restored = ReplayHistory(directory=self.directory)
        retained = restored.state['origin_contexts'][original_key]

        self.assertEqual(original['run'], retained['run'])
        self.assertEqual(original['sha'], retained['sha'])
        self.assertEqual(original['repository'], retained['repository'])
        self.assertLessEqual(len(restored.state['contexts']), 12)

    def test_pr_history_is_isolated_by_pr_and_from_push_history(self):
        keys = ('TRAVIS_PULL_REQUEST', 'TRAVIS_PULL_REQUEST_BRANCH',
                'TRAVIS_BRANCH', 'TRAVIS_REPO_SLUG', 'GITHUB_REF',
                'GITHUB_HEAD_REF', 'GITHUB_BASE_REF', 'CI_REPLAY_BRANCH',
                'CI_REPLAY_HEAD_REF', 'CI_REPLAY_BASE_REF')
        previous = dict((key, os.environ.get(key)) for key in keys)
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ['TRAVIS_PULL_REQUEST'] = '123'
            os.environ['TRAVIS_PULL_REQUEST_BRANCH'] = 'replay-fix'
            os.environ['TRAVIS_BRANCH'] = 'master'
            os.environ['TRAVIS_REPO_SLUG'] = 'gmware/auditlog'
            first = ReplayHistory(directory=self.directory)
            test = SampleTest('test_pass')
            first.error(test)
            first.save()

            os.environ['TRAVIS_PULL_REQUEST'] = '124'
            second = ReplayHistory(directory=self.directory)
            self.assertEqual([], second.state['failed'])
            result = self._result()
            result.history = second
            result.startTest(test)
            result.addSuccess(test)
            result.stopTest(test)
            second.save()

            os.environ['TRAVIS_PULL_REQUEST'] = '123'
            still_failed = ReplayHistory(directory=self.directory)

            os.environ['TRAVIS_PULL_REQUEST'] = 'false'
            os.environ.pop('TRAVIS_PULL_REQUEST_BRANCH', None)
            push = ReplayHistory(directory=self.directory)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertNotEqual(first.path, second.path)
        self.assertNotEqual(first.path, push.path)
        self.assertNotEqual(second.path, push.path)
        self.assertEqual([test.id()], still_failed.state['failed'])

    def test_origin_keeps_available_provider_context_fields(self):
        keys = ('TRAVIS_PULL_REQUEST', 'TRAVIS_PULL_REQUEST_BRANCH',
                'TRAVIS_BRANCH', 'TRAVIS_REPO_SLUG', 'TRAVIS_COMMIT',
                'TRAVIS_BUILD_ID', 'TRAVIS_JOB_ID', 'CI_REPLAY_BASE_SHA',
                'CI_REPLAY_RUN_ATTEMPT', 'TOX_ENV')
        previous = dict((key, os.environ.get(key)) for key in keys)
        try:
            os.environ.update({
                'TRAVIS_PULL_REQUEST': '321',
                'TRAVIS_PULL_REQUEST_BRANCH': 'feature/replay',
                'TRAVIS_BRANCH': 'master',
                'TRAVIS_REPO_SLUG': 'gmware/auditlog',
                'TRAVIS_COMMIT': 'b' * 40,
                'TRAVIS_BUILD_ID': 'build-1',
                'TRAVIS_JOB_ID': 'job-2',
                'CI_REPLAY_BASE_SHA': 'a' * 40,
                'CI_REPLAY_RUN_ATTEMPT': '3',
                'TOX_ENV': 'py36-django-20',
            })
            history = ReplayHistory(directory=self.directory)
            test = SampleTest('test_pass')
            history.error(test)
            key = history.state['origins'][test.id()][0]
            origin = history.state['origin_contexts'][key]
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual({
            'repository': 'gmware/auditlog',
            'branch': 'master',
            'head_ref': 'feature/replay',
            'base_ref': 'master',
            'base_sha': 'a' * 40,
            'pr': '321',
            'sha': 'b' * 40,
            'run': 'build-1',
            'run_attempt': '3',
            'job': 'job-2',
            'lane': 'py36-django-20',
        }, dict((name, origin[name]) for name in (
            'repository', 'branch', 'head_ref', 'base_ref', 'base_sha', 'pr',
            'sha', 'run', 'run_attempt', 'job', 'lane')))

    def test_legacy_v1_cache_keeps_failures_and_replays_full_suite(self):
        test = SampleTest('test_pass')
        legacy_state = {
            'version': 1,
            'failed': [test.id()],
            'pending': [],
            'risk': {},
            'contexts': [{'sha': 'a' * 40}],
        }
        with open(self.history.path, 'w') as stream:
            json.dump(legacy_state, stream)

        restored = ReplayHistory(directory=self.directory)
        ordered = restored.order([test])

        self.assertEqual(2, restored.state['version'])
        self.assertEqual([test.id()], restored.state['failed'])
        self.assertEqual([test.id()], [item.id() for item in ordered])
        self.assertTrue(restored.state['origins'][test.id()])
        self.assertEqual([test.id()], restored.state['legacy_unattributed'])

    def test_legacy_cache_path_is_migrated_only_for_non_pr_scope(self):
        keys = ('TRAVIS_PULL_REQUEST', 'CI_MERGE_REQUEST_IID', 'GITHUB_REF',
                'TRAVIS_BRANCH', 'TRAVIS_PULL_REQUEST_BRANCH', 'GITHUB_HEAD_REF',
                'TRAVIS_REPO_SLUG', 'TOX_ENV')
        previous = dict((key, os.environ.get(key)) for key in keys)
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ.update({
                'TRAVIS_PULL_REQUEST': 'false',
                'TRAVIS_BRANCH': 'master',
                'TRAVIS_REPO_SLUG': 'gmware/auditlog',
                'TOX_ENV': 'py36-django-20',
            })
            current = ReplayHistory(directory=self.directory)
            legacy_scope = [current.context[key] for key in
                            ('repository', 'branch', 'lane', 'python')]
            legacy_digest = hashlib.sha256(
                json.dumps(legacy_scope).encode('utf-8')).hexdigest()[:32]
            legacy_path = os.path.join(self.directory, legacy_digest + '.json')
            with open(legacy_path, 'w') as stream:
                json.dump({
                    'version': 1,
                    'failed': [SampleTest('test_pass').id()],
                    'pending': [],
                    'risk': {},
                    'contexts': [],
                }, stream)
            restored = ReplayHistory(directory=self.directory)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(legacy_path, restored.load_path)
        self.assertNotEqual(legacy_path, restored.path)
        self.assertEqual([SampleTest('test_pass').id()], restored.state['failed'])

    def test_call_success_after_teardown_error_does_not_clear_failure(self):
        test = SampleTest('test_pass')
        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addError(test, (RuntimeError, RuntimeError('teardown'), None))
        result.addSuccess(test)

        self.assertEqual([test.id()], self.history.state['failed'])

    def test_class_fixture_holder_maps_to_collected_tests(self):
        class FixtureGroup(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError('class setup')

            def test_one(self):
                pass

            def test_two(self):
                pass

        tests = [FixtureGroup('test_one'), FixtureGroup('test_two')]
        identifiers = [test.id() for test in tests]
        self.history.state['pending'] = list(identifiers)
        self.history.save()
        runner = ReplayTextTestRunner(
            stream=io.StringIO(), descriptions=False, verbosity=0,
            replay_history=self.history, known_tests=tests)
        result = runner.run(unittest.TestSuite(tests))

        self.assertEqual(identifiers, self.history.state['failed'])
        self.assertEqual(identifiers, self.history.state['pending'])
        self.assertEqual(1, len(result.errors))
        self.assertTrue(all(error[0].id() not in identifiers
                            for error in result.errors))
        for identifier in identifiers:
            self.assertTrue(self.history.state['origins'][identifier])

        self.history.failed_this_run = set()
        clean_result = self._result()
        clean_result.startTest(tests[0])
        clean_result.addSuccess(tests[0])
        self.assertNotIn(identifiers[0], self.history.state['failed'])
        self.assertIn(identifiers[1], self.history.state['failed'])

    def test_class_teardown_holder_maps_to_collected_tests(self):
        class FixtureGroup(unittest.TestCase):
            @classmethod
            def tearDownClass(cls):
                raise RuntimeError('class teardown')

            def test_one(self):
                pass

            def test_two(self):
                pass

        tests = [FixtureGroup('test_one'), FixtureGroup('test_two')]
        identifiers = [test.id() for test in tests]
        runner = ReplayTextTestRunner(
            stream=io.StringIO(), descriptions=False, verbosity=0,
            replay_history=self.history, known_tests=tests)
        result = runner.run(unittest.TestSuite(tests))

        self.assertEqual(identifiers, self.history.state['failed'])
        self.assertEqual(identifiers, self.history.state['pending'])
        self.assertEqual(1, len(result.errors))
        self.assertTrue(all(error[0].id() not in identifiers
                            for error in result.errors))

    def test_module_fixture_holder_maps_to_collected_tests(self):
        module = types.ModuleType('ci_replay_fixture_module')

        def fail_module_setup():
            raise RuntimeError('module setup')

        module.setUpModule = fail_module_setup
        sys.modules[module.__name__] = module
        class FixtureGroup(unittest.TestCase):
            __module__ = module.__name__

            def test_one(self):
                pass

            def test_two(self):
                pass

        tests = [FixtureGroup('test_one'), FixtureGroup('test_two')]
        identifiers = [test.id() for test in tests]
        try:
            self.history.state['pending'] = list(identifiers)
            self.history.save()
            runner = ReplayTextTestRunner(
                stream=io.StringIO(), descriptions=False, verbosity=0,
                replay_history=self.history, known_tests=tests)
            result = runner.run(unittest.TestSuite(tests))
        finally:
            sys.modules.pop(module.__name__, None)

        self.assertEqual(identifiers, self.history.state['failed'])
        self.assertEqual(identifiers, self.history.state['pending'])
        self.assertEqual(1, len(result.errors))

    def test_module_teardown_holder_maps_to_collected_tests(self):
        module = types.ModuleType('ci_replay_fixture_module')

        def fail_module_teardown():
            raise RuntimeError('module teardown')

        module.tearDownModule = fail_module_teardown
        sys.modules[module.__name__] = module
        class FixtureGroup(unittest.TestCase):
            __module__ = module.__name__

            def test_one(self):
                pass

            def test_two(self):
                pass

        tests = [FixtureGroup('test_one'), FixtureGroup('test_two')]
        identifiers = [test.id() for test in tests]
        try:
            runner = ReplayTextTestRunner(
                stream=io.StringIO(), descriptions=False, verbosity=0,
                replay_history=self.history, known_tests=tests)
            result = runner.run(unittest.TestSuite(tests))
        finally:
            sys.modules.pop(module.__name__, None)

        self.assertEqual(identifiers, self.history.state['failed'])
        self.assertEqual(identifiers, self.history.state['pending'])
        self.assertEqual(1, len(result.errors))

    def test_failed_then_pending_then_historically_risky_order(self):
        class OrderedTests(unittest.TestCase):
            def test_remaining(self):
                pass

            def test_risky(self):
                pass

            def test_pending(self):
                pass

            def test_failed(self):
                pass

        tests = [OrderedTests(name) for name in (
            'test_remaining', 'test_risky', 'test_pending', 'test_failed')]
        remaining, risky, pending, failed = [test.id() for test in tests]
        self.history.state['failed'] = [failed]
        self.history.state['pending'] = [pending]
        self.history.state['risk'] = {risky: 7}
        ordered = self.history.order(tests)

        self.assertEqual([failed, pending, risky, remaining],
                         [test.id() for test in ordered])

    def test_priority_phase_selects_prior_failure_pending_and_risk_across_lane(self):
        class OrderedTests(unittest.TestCase):
            def test_remaining(self):
                pass

            def test_risky(self):
                pass

            def test_pending(self):
                pass

            def test_failed(self):
                pass

        tests = [OrderedTests(name) for name in (
            'test_remaining', 'test_risky', 'test_pending', 'test_failed')]
        remaining, risky, pending, failed = [test.id() for test in tests]
        self.history.state['failed'] = [failed]
        self.history.state['pending'] = [pending]
        self.history.state['risk'] = {risky: 7}
        self.history._changed_files = lambda: set()

        selected = self.history.priority_ids(tests)

        self.assertEqual(set([failed, pending, risky]), selected)
        self.assertNotIn(remaining, selected)

    def test_changed_source_marks_full_suite_as_priority_candidates(self):
        tests = [SampleTest('test_pass'), SampleTest('test_skip')]
        self.history._changed_files = lambda: set(['src/auditlog/models.py'])

        self.assertEqual(set(test.id() for test in tests),
                         self.history.priority_ids(tests))

    def test_priority_runner_executes_prior_candidate_before_other_tests(self):
        executed = []

        class OrderedTests(unittest.TestCase):
            def test_regular(self):
                executed.append('regular')

            def test_prior_failure(self):
                executed.append('prior')

        priority_test = OrderedTests('test_prior_failure')
        self.history.state['failed'] = [priority_test.id()]
        self.history.save()
        previous_phase = os.environ.get('CI_REPLAY_PHASE')
        os.environ['CI_REPLAY_PHASE'] = 'priority'
        original_changed_files = ReplayHistory._changed_files
        ReplayHistory._changed_files = lambda history: set()
        try:
            runner = ReplayDiscoverRunner()
            runner.verbosity = 0
            result = runner.run_suite(unittest.TestSuite([
                OrderedTests('test_regular'), priority_test]))
        finally:
            ReplayHistory._changed_files = original_changed_files
            if previous_phase is None:
                os.environ.pop('CI_REPLAY_PHASE', None)
            else:
                os.environ['CI_REPLAY_PHASE'] = previous_phase

        self.assertEqual(['prior'], executed)
        self.assertEqual(1, result.testsRun)

    def test_priority_runner_does_not_mark_unstarted_tests_pending(self):
        class OrderedTests(unittest.TestCase):
            def test_regular(self):
                self.fail('non-priority test must not execute')

            def test_prior_failure(self):
                pass

        priority_test = OrderedTests('test_prior_failure')
        regular_test = OrderedTests('test_regular')
        self.history.state['failed'] = [priority_test.id()]
        self.history.save()
        previous_phase = os.environ.get('CI_REPLAY_PHASE')
        os.environ['CI_REPLAY_PHASE'] = 'priority'
        original_changed_files = ReplayHistory._changed_files
        ReplayHistory._changed_files = lambda history: set()
        try:
            runner = ReplayDiscoverRunner()
            runner.verbosity = 0
            result = runner.run_suite(unittest.TestSuite([
                regular_test, priority_test]))
        finally:
            ReplayHistory._changed_files = original_changed_files
            if previous_phase is None:
                os.environ.pop('CI_REPLAY_PHASE', None)
            else:
                os.environ['CI_REPLAY_PHASE'] = previous_phase

        restored = ReplayHistory(directory=self.directory)
        self.assertEqual(1, result.testsRun)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual([], restored.state['pending'])
        self.assertEqual([], restored.state['failed'])

    def test_priority_phase_fails_closed_when_a_previous_failure_is_skipped(self):
        test = SampleTest('test_skip')
        self.history.error(test)
        previous_phase = os.environ.get('CI_REPLAY_PHASE')
        os.environ['CI_REPLAY_PHASE'] = 'priority'
        original_changed_files = ReplayHistory._changed_files
        ReplayHistory._changed_files = lambda history: set()
        try:
            runner = ReplayDiscoverRunner()
            runner.verbosity = 0
            with self.assertRaises(RuntimeError):
                runner.run_suite(unittest.TestSuite([test]))
        finally:
            ReplayHistory._changed_files = original_changed_files
            if previous_phase is None:
                os.environ.pop('CI_REPLAY_PHASE', None)
            else:
                os.environ['CI_REPLAY_PHASE'] = previous_phase

        restored = ReplayHistory(directory=self.directory)
        self.assertEqual([test.id()], restored.state['failed'])
        self.assertNotIn(test.id(), restored.passed_tests)

    def test_priority_phase_fails_closed_when_a_previous_failure_is_missing(self):
        missing = 'removed.module.TestCase.test_missing'
        self.history.state['failed'] = [missing]
        self.history.save()
        previous_phase = os.environ.get('CI_REPLAY_PHASE')
        os.environ['CI_REPLAY_PHASE'] = 'priority'
        original_changed_files = ReplayHistory._changed_files
        ReplayHistory._changed_files = lambda history: set()
        try:
            runner = ReplayDiscoverRunner()
            runner.verbosity = 0
            with self.assertRaises(RuntimeError):
                runner.run_suite(unittest.TestSuite([SampleTest('test_pass')]))
        finally:
            ReplayHistory._changed_files = original_changed_files
            if previous_phase is None:
                os.environ.pop('CI_REPLAY_PHASE', None)
            else:
                os.environ['CI_REPLAY_PHASE'] = previous_phase

    def test_priority_phase_fails_closed_when_a_pending_test_is_skipped(self):
        test = SampleTest('test_skip')
        self.history.state['pending'] = [test.id()]
        self.history.save()
        previous_phase = os.environ.get('CI_REPLAY_PHASE')
        os.environ['CI_REPLAY_PHASE'] = 'priority'
        original_changed_files = ReplayHistory._changed_files
        ReplayHistory._changed_files = lambda history: set()
        try:
            runner = ReplayDiscoverRunner()
            runner.verbosity = 0
            with self.assertRaises(RuntimeError):
                runner.run_suite(unittest.TestSuite([test]))
        finally:
            ReplayHistory._changed_files = original_changed_files
            if previous_phase is None:
                os.environ.pop('CI_REPLAY_PHASE', None)
            else:
                os.environ['CI_REPLAY_PHASE'] = previous_phase

    def test_runner_stops_after_first_failure_and_keeps_remaining_pending(self):
        executed = []

        class OrderedTests(unittest.TestCase):
            def test_first_fails(self):
                executed.append('first')
                self.fail('expected failure')

            def test_second_is_not_started(self):
                executed.append('second')

        suite = unittest.TestSuite([
            OrderedTests('test_first_fails'),
            OrderedTests('test_second_is_not_started'),
        ])
        tests = list(suite)
        first_id, second_id = tests[0].id(), tests[1].id()
        suite = unittest.TestSuite(self.history.order(tests))
        runner = ReplayTextTestRunner(
            stream=io.StringIO(), descriptions=False, verbosity=0,
            replay_history=self.history)

        result = runner.run(suite)

        self.assertEqual(['first'], executed)
        self.assertEqual(1, result.testsRun)
        self.assertEqual([first_id],
                         self.history.state['failed'])
        self.assertEqual([second_id],
                         self.history.state['pending'])


if __name__ == '__main__':
    unittest.main()
