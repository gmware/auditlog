"""Stable, bounded names for isolated tox database and coverage files."""
from __future__ import print_function

import hashlib
import os
import re
import tempfile


def _value(environment, name):
    return environment.get(name, '').strip()


def isolation_key(environment=None):
    """Return a filesystem/SQL-safe key for one tox lane and CI job."""
    environment = os.environ if environment is None else environment
    lane = _value(environment, 'CI_REPLAY_LANE') or _value(environment, 'TOX_ENV')
    if not lane:
        return None

    job = (_value(environment, 'TRAVIS_JOB_ID') or
           _value(environment, 'TRAVIS_BUILD_ID'))
    raw = lane + (('_' + job) if job else '')
    slug = re.sub(r'[^a-zA-Z0-9]+', '_', raw).strip('_').lower()
    digest = hashlib.sha1(raw.encode('utf-8')).hexdigest()[:10]
    return '%s_%s' % (slug[:24].rstrip('_') or 'tox', digest)


def database_names(environment=None):
    """Return SQLite and PostgreSQL database names scoped to one tox job."""
    key = isolation_key(environment)
    if key is None:
        return {'default': 'auditlog_tests.db',
                'postgres': 'auditlog_tests_db'}

    return {
        'default': os.path.join(tempfile.gettempdir(),
                                'auditlog_tests_%s.db' % key),
        'postgres': 'auditlog_tests_%s' % key,
    }


def coverage_file(root, key=None):
    """Return a non-clobbering coverage data file for this tox lane."""
    if key is None:
        return os.path.join(root, '.coverage')
    return os.path.join(root, '.coverage.%s' % key)
