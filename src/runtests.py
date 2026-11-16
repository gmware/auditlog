#!/usr/bin/env python
import os
import sys

import django

if __name__ == "__main__":
    os.environ['DJANGO_SETTINGS_MODULE'] = 'auditlog_tests.test_settings'
    django.setup()
    from ci_replay import ReplayDiscoverRunner
    test_runner = ReplayDiscoverRunner()
    failures = test_runner.run_tests(["auditlog_tests"])
    sys.exit(bool(failures))
