"""Concurrency isolation checks for tox-backed database and coverage state."""
import os
import re
import unittest

from ci_isolation import coverage_file, database_names, isolation_key


class IsolationKeyTests(unittest.TestCase):
    def test_tox_lanes_get_distinct_database_and_coverage_paths(self):
        first = isolation_key({'CI_REPLAY_LANE': 'py27-django-18'})
        second = isolation_key({'CI_REPLAY_LANE': 'py36-django-20'})

        self.assertNotEqual(first, second)
        self.assertNotEqual(database_names({'CI_REPLAY_LANE': 'py27-django-18'}),
                            database_names({'CI_REPLAY_LANE': 'py36-django-20'}))
        self.assertNotEqual(coverage_file('/repo', first),
                            coverage_file('/repo', second))

    def test_overlapping_travis_jobs_get_distinct_database_paths(self):
        first = {'CI_REPLAY_LANE': 'py36-django-18', 'TRAVIS_JOB_ID': '101'}
        second = {'CI_REPLAY_LANE': 'py36-django-18', 'TRAVIS_JOB_ID': '102'}

        self.assertNotEqual(database_names(first), database_names(second))

    def test_lane_identifiers_are_safe_and_postgres_names_are_bounded(self):
        names = database_names({'CI_REPLAY_LANE': 'py27/unsafe ' * 20})

        self.assertLessEqual(len(names['postgres']), 63)
        self.assertTrue(re.match(r'^[a-z0-9_]+$', names['postgres']))
        self.assertTrue(os.path.isabs(names['default']))

    def test_no_lane_preserves_the_existing_local_database_names(self):
        self.assertEqual({'default': 'auditlog_tests.db',
                          'postgres': 'auditlog_tests_db'}, database_names({}))


if __name__ == '__main__':
    unittest.main()
