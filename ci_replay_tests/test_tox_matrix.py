"""Contract tests for single-job tox supervision."""
import os
import shutil
import sys
import tempfile
import time
import unittest

import ci_tox_matrix
from ci_tox_matrix import _combine_coverage, main, run


class ToxMatrixTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='ci-tox-matrix-')

    def tearDown(self):
        shutil.rmtree(self.directory)

    def _path(self, name, suffix):
        return os.path.join(self.directory, '%s-%s' % (name, suffix))

    def test_first_failure_cancels_active_processes_and_queued_envs(self):
        envs = ['slow-one', 'slow-two', 'slow-three', 'slow-four',
                'failure', 'queued']

        def command_builder(envname):
            started = self._path(envname, 'started')
            finished = self._path(envname, 'finished')
            if envname == 'failure':
                code = ('import time; open(%r, "w").close(); '
                        'time.sleep(0.2); raise SystemExit(7)') % started
            elif envname == 'queued':
                code = 'open(%r, "w").close()' % started
            else:
                code = ('import time; open(%r, "w").close(); '
                        'time.sleep(20); open(%r, "w").close()') % (
                            started, finished)
            return [sys.executable, '-c', code]

        start = time.time()
        status = run(envs, parallelism=5, grace_seconds=0.5,
                     poll_interval=0.02, command_builder=command_builder)
        elapsed = time.time() - start

        self.assertEqual(7, status)
        self.assertLess(elapsed, 4)
        for envname in envs[:5]:
            self.assertTrue(os.path.exists(self._path(envname, 'started')))
        self.assertFalse(os.path.exists(self._path('queued', 'started')))
        for envname in envs[:4]:
            self.assertFalse(os.path.exists(self._path(envname, 'finished')))

    def test_failure_observed_after_sibling_exit_blocks_next_tox_launch(self):
        failure_ready = [False]
        started = []

        class FailureProcess(object):
            pid = 99999999

            def poll(self):
                return 7 if failure_ready[0] else None

            def wait(self):
                return 7

        class SuccessProcess(object):
            pid = 99999998

            def poll(self):
                failure_ready[0] = True
                return 0

            def wait(self):
                return 0

        processes = {
            'failure': FailureProcess(),
            'success': SuccessProcess(),
        }

        def popen(command, environment):
            name = command[-1]
            started.append(name)
            return processes[name]

        status = run(['failure', 'success', 'queued'], parallelism=2,
                     grace_seconds=0, poll_interval=0.001,
                     command_builder=lambda name: ['tox', '-e', name],
                     popen=popen)

        self.assertEqual(7, status)
        self.assertEqual(['failure', 'success'], started)

    def test_timeout_interrupt_stops_active_groups_and_does_not_launch_queued(self):
        signals = []
        started = []

        class InterruptedProcess(object):
            pid = 12345

            def __init__(self):
                self.poll_count = 0
                self.waited = False

            def poll(self):
                self.poll_count += 1
                if self.poll_count == 1:
                    raise KeyboardInterrupt()
                return 0

            def wait(self):
                self.waited = True
                return 0

        process = InterruptedProcess()
        original_signal_group = ci_tox_matrix._signal_group
        original_group_exists = ci_tox_matrix._group_exists
        ci_tox_matrix._signal_group = lambda child, signum: signals.append(signum)
        ci_tox_matrix._group_exists = lambda child: True

        def popen(command, environment):
            started.append(command[-1])
            return process

        try:
            with self.assertRaises(KeyboardInterrupt):
                run(['active', 'queued'], parallelism=1, grace_seconds=0,
                    poll_interval=0.001, popen=popen,
                    command_builder=lambda name: ['tox', '-e', name])
        finally:
            ci_tox_matrix._signal_group = original_signal_group
            ci_tox_matrix._group_exists = original_group_exists

        self.assertEqual(['active'], started)
        self.assertEqual([ci_tox_matrix.signal.SIGTERM,
                          ci_tox_matrix.signal.SIGKILL], signals)
        self.assertTrue(process.waited)

    def test_cleanup_escalates_when_active_process_ignores_term(self):
        started = self._path('stubborn', 'started')
        finished = self._path('stubborn', 'finished')

        def command_builder(envname):
            if envname == 'failure':
                code = 'import time; time.sleep(0.2); raise SystemExit(9)'
            else:
                code = ('import signal, time; signal.signal(signal.SIGTERM, '
                        'signal.SIG_IGN); open(%r, "w").close(); '
                        'time.sleep(20); open(%r, "w").close()') % (
                            started, finished)
            return [sys.executable, '-c', code]

        start = time.time()
        status = run(['stubborn', 'failure'], parallelism=2,
                     grace_seconds=0.1, poll_interval=0.02,
                     command_builder=command_builder)

        self.assertEqual(9, status)
        self.assertLess(time.time() - start, 4)
        self.assertTrue(os.path.exists(started))
        self.assertFalse(os.path.exists(finished))

    def test_failure_cancels_orphaned_children_of_failed_env(self):
        child_finished = self._path('child', 'finished')

        def command_builder(envname):
            child_code = ('import signal, time; signal.signal(signal.SIGTERM, '
                          'signal.SIG_IGN); time.sleep(20); '
                          'open(%r, "w").close()') % child_finished
            parent_code = ('import subprocess, sys, time; '
                           'subprocess.Popen([sys.executable, "-c", %r]); '
                           'time.sleep(0.2); sys.exit(11)') % child_code
            return [sys.executable, '-c', parent_code]

        start = time.time()
        status = run(['failing-env', 'queued'], parallelism=1,
                     grace_seconds=0.1, poll_interval=0.02,
                     command_builder=command_builder)

        self.assertEqual(11, status)
        self.assertLess(time.time() - start, 4)
        self.assertFalse(os.path.exists(child_finished))

    def test_cleanup_kills_group_when_failed_leader_exits_before_child(self):
        child_started = self._path('child', 'started')
        child_finished = self._path('child', 'finished')

        def command_builder(envname):
            child_code = ('import signal, time; signal.signal(signal.SIGTERM, '
                          'signal.SIG_IGN); open(%r, "w").close(); '
                          'time.sleep(0.4); open(%r, "w").close()') % (
                              child_started, child_finished)
            parent_code = ('import subprocess, sys, time; '
                           'subprocess.Popen([sys.executable, "-c", %r]); '
                           'time.sleep(0.2); sys.exit(13)') % child_code
            return [sys.executable, '-c', parent_code]

        start = time.time()
        status = run(['failing-env', 'queued'], parallelism=1,
                     grace_seconds=0.1, poll_interval=0.01,
                     command_builder=command_builder)
        self.assertEqual(13, status)
        self.assertLess(time.time() - start, 3)
        self.assertTrue(os.path.exists(child_started))
        time.sleep(0.5)
        self.assertFalse(os.path.exists(child_finished))

    def test_coverage_aggregation_uses_coverage_combine(self):
        commands = []

        status = _combine_coverage(lambda command: commands.append(command) or 0)

        self.assertEqual(0, status)
        self.assertEqual([['coverage', 'combine']], commands)

    def test_coverage_is_not_combined_after_a_failed_matrix(self):
        combined = []

        status = main(lambda phase='canonical': 5,
                      lambda: combined.append(True) or 0)

        self.assertEqual(5, status)
        self.assertEqual([], combined)

    def test_coverage_is_combined_after_a_successful_matrix(self):
        combined = []

        phases = []
        status = main(lambda phase='canonical': phases.append(phase) or 0,
                      lambda: combined.append(True) or 0)

        self.assertEqual(0, status)
        self.assertEqual(['priority', 'canonical'], phases)
        self.assertEqual([True], combined)

    def test_full_matrix_waits_for_global_priority_phase(self):
        calls = []

        def run_phase(phase):
            calls.append(phase)
            return 0

        status = main(run_phase, lambda: 0)

        self.assertEqual(0, status)
        self.assertEqual(['priority', 'canonical'], calls)

    def test_priority_matrix_finishes_before_canonical_tox_jobs_start(self):
        calls = []

        def run_phase(phase):
            calls.extend((phase, envname) for envname in
                         ('py36-django-18', 'py27-django-20'))
            return 0

        status = main(run_phase, lambda: 0)

        self.assertEqual(0, status)
        self.assertEqual([
            ('priority', 'py36-django-18'), ('priority', 'py27-django-20'),
            ('canonical', 'py36-django-18'), ('canonical', 'py27-django-20')],
            calls)

    def test_priority_failure_prevents_canonical_matrix_and_coverage(self):
        calls = []

        def run_phase(phase):
            calls.append(phase)
            return 17

        combined = []
        status = main(run_phase, lambda: combined.append(True) or 0)

        self.assertEqual(17, status)
        self.assertEqual(['priority'], calls)
        self.assertEqual([], combined)

    def test_phase_is_passed_to_each_tox_process(self):
        observed = []

        class FinishedProcess(object):
            pid = 1

            def poll(self):
                return 0

            def wait(self):
                return 0

        status = run(['only'], phase='priority', parallelism=1,
                     poll_interval=0.001,
                     popen=lambda command, environment:
                     observed.append(environment['CI_REPLAY_PHASE']) or
                     FinishedProcess())

        self.assertEqual(0, status)
        self.assertEqual(['priority'], observed)


if __name__ == '__main__':
    unittest.main()
