"""The tox command runs adapter checks only after replay candidates pass."""
import unittest

from ci_tox_test import main


class ToxCommandTests(unittest.TestCase):
    def test_priority_phase_runs_only_the_discovered_app_candidates(self):
        commands = []

        status = main('py36-django-18', 'priority',
                      lambda command: commands.append(command) or 0)

        self.assertEqual(0, status)
        self.assertEqual([['coverage', 'run', '--source', 'src/auditlog',
                           'src/runtests.py']], commands)

    def test_non_owner_canonical_lane_does_not_repeat_adapter_checks(self):
        commands = []

        status = main('py27-django-20', 'canonical',
                      lambda command: commands.append(command) or 0)

        self.assertEqual(0, status)
        self.assertEqual([['coverage', 'run', '--source', 'src/auditlog',
                           'src/runtests.py']], commands)

    def test_adapter_checks_run_after_priority_on_one_supported_lane(self):
        commands = []

        status = main('py36-django-20', 'canonical',
                      lambda command: commands.append(command) or 0)

        self.assertEqual(0, status)
        self.assertEqual(2, len(commands))
        self.assertEqual(['-m', 'unittest', 'discover', '-s',
                          'ci_replay_tests'], commands[0][1:])
        self.assertEqual(['coverage', 'run', '--source', 'src/auditlog',
                          'src/runtests.py'], commands[1])

    def test_adapter_failure_prevents_canonical_app_tests(self):
        commands = []

        def call(command):
            commands.append(command)
            return 23

        status = main('py36-django-20', 'canonical', call)

        self.assertEqual(23, status)
        self.assertEqual(1, len(commands))


if __name__ == '__main__':
    unittest.main()
