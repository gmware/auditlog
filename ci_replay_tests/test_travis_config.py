"""Structural checks for the ordered Travis job graph."""
import os
import re
import unittest

from ci_tox_matrix import TOX_ENVS


EXPECTED_TOX_ENVS = [
    'py36-django-18', 'py35-django-18', 'py34-django-18', 'py27-django-18',
    'py36-django-110', 'py35-django-110', 'py34-django-110', 'py27-django-110',
    'py36-django-111', 'py35-django-111', 'py34-django-111', 'py27-django-111',
    'py36-django-20', 'py35-django-20', 'py34-django-20',
]


def _section(text, name):
    match = re.search(r'^' + re.escape(name) + r':\s*\n(.*?)(?=^[^ \t#][^\n]*:|\Z)',
                      text, re.MULTILINE | re.DOTALL)
    if not match:
        return ''
    return match.group(1)


def _included_jobs(text):
    jobs = _section(text, 'jobs')
    include = re.search(r'^  include:\s*\n(.*?)(?=^  [^ \t#][^\n]*:|\Z)',
                        jobs, re.MULTILINE | re.DOTALL)
    if not include:
        return []
    rows = []
    current = None
    for line in include.group(1).splitlines():
        match = re.match(r'^    - stage: ([^\n]+)$', line)
        if match:
            if current is not None:
                rows.append(current)
            current = {'stage': match.group(1).strip().strip('"\'')}
            continue
        if current is None:
            continue
        match = re.match(r'^      (python|env|script):\s*(.*)$', line)
        if match:
            current[match.group(1)] = match.group(2).strip().strip('"\'')
    if current is not None:
        rows.append(current)
    return rows


class TravisJobGraphTests(unittest.TestCase):
    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, '.travis.yml')) as stream:
            self.config = stream.read()

    def test_tox_environments_are_singleton_ordered_stages(self):
        jobs = _included_jobs(self.config)
        stages = re.findall(r'^  - ([^\n]+)$', _section(self.config, 'stages'),
                            re.MULTILINE)

        self.assertEqual(EXPECTED_TOX_ENVS, list(TOX_ENVS))
        self.assertEqual(['test', 'deploy'], stages)
        self.assertEqual(1, len(jobs))
        self.assertFalse(any(job.get('stage') == 'test' for job in jobs))
        self.assertNotIn('allow_failures', self.config)
        self.assertNotIn('fast_finish', self.config)

    def test_single_base_job_is_the_matrix_supervisor(self):
        env = _section(self.config, 'env')
        self.assertTrue(re.search(r'^  - ""\s*$', env, re.MULTILINE),
                        'the only base matrix row must have an empty env')
        self.assertNotIn('exclude:', _section(self.config, 'jobs'))
        self.assertTrue(re.search(r'(?m)^python: "3\.6"$', self.config))
        self.assertIn(
            'script: timeout --foreground --signal=INT --kill-after=30s '
            '40m python ci_tox_matrix.py', self.config)

    def test_deploy_is_last_and_keeps_original_release_gate(self):
        jobs = _included_jobs(self.config)
        self.assertEqual(1, len(jobs))
        deploy = jobs[0]

        self.assertEqual('deploy', deploy.get('stage'))
        self.assertEqual('3.6', deploy.get('python'))
        self.assertEqual('TOX_ENV=py36-django-20', deploy.get('env'))
        self.assertEqual('skip', deploy.get('script'))
        self.assertTrue(re.search(
            r'(?m)^          repo: jjkester/django-auditlog$', self.config))
        self.assertTrue(re.search(r'(?m)^          branch: stable$', self.config))
        self.assertTrue(re.search(
            r'(?m)^          condition: \$TOX_ENV = py36-django-20$', self.config))

    def test_supervisor_combines_isolated_coverage_files_before_upload(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'tox.ini')) as stream:
            tox_config = stream.read()
        self.assertIn('CI_REPLAY_LANE = {envname}', tox_config)
        self.assertIn('COVERAGE_FILE = {toxinidir}/.coverage.{envname}',
                      tox_config)
        self.assertIn('passenv = TRAVIS_JOB_ID TRAVIS_BUILD_ID CI_REPLAY_PHASE CI_REPLAY_LANE',
                      tox_config)
        self.assertTrue(re.search(
            r'(?m)^  - codecov -f \.coverage$',
            self.config))
        self.assertIn('commands =\n    python ci_tox_test.py', tox_config)

    def test_travis_tox_path_invokes_the_replay_django_runner(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'tox.ini')) as stream:
            tox_config = stream.read()
        with open(os.path.join(root, 'src', 'runtests.py')) as stream:
            test_entrypoint = stream.read()
        with open(os.path.join(root, 'ci_tox_test.py')) as stream:
            tox_entrypoint = stream.read()
        with open(os.path.join(root, 'ci_tox_matrix.py')) as stream:
            matrix_entrypoint = stream.read()

        self.assertIn(
            'script: timeout --foreground --signal=INT --kill-after=30s '
            '40m python ci_tox_matrix.py', self.config)
        self.assertIn("['tox', '-e', envname]", matrix_entrypoint)
        self.assertIn('python ci_tox_test.py', tox_config)
        self.assertIn("'src/runtests.py'", tox_entrypoint)
        self.assertIn('ReplayDiscoverRunner()', test_entrypoint)
        self.assertNotIn('get_runner(settings)', test_entrypoint)

    def test_travis_runs_the_global_replay_matrix_wrapper(self):
        self.assertTrue(re.search(
            r'(?m)^script:\s*timeout --foreground --signal=INT '
            r'--kill-after=30s 40m python ci_tox_matrix\.py\s*$', self.config),
            'the bounded Travis command must enter the replay-first coordinator')


if __name__ == '__main__':
    unittest.main()
