"""Focused replay adapter contract tests, separate from Django discovery."""
import io
import os
import shutil
import tempfile
import unittest

from ci_replay import (ReplayHistory, ReplayTextTestResult,
                       ReplayTextTestRunner)


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

    def _result(self):
        return ReplayTextTestResult(
            stream=io.StringIO(), descriptions=False, verbosity=0)

    def test_skip_does_not_clear_previous_failure(self):
        test = SampleTest('test_skip')
        self.history.state['failed'] = [test.id()]
        self.history.state['pending'] = [test.id()]
        self.history.save()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addSkip(test, 'skip')

        self.assertEqual([test.id()], self.history.state['failed'])
        self.assertEqual([], self.history.state['pending'])

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
        self.history.state['failed'] = [test.id()]
        self.history.state['pending'] = [test.id()]
        self.history.save()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addSuccess(test)

        self.assertEqual([], self.history.state['failed'])
        self.assertEqual([], self.history.state['pending'])

    def test_call_success_after_teardown_error_does_not_clear_failure(self):
        test = SampleTest('test_pass')
        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addError(test, (RuntimeError, RuntimeError('teardown'), None))
        result.addSuccess(test)

        self.assertEqual([test.id()], self.history.state['failed'])

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
