"""Run one tox lane's replay-selected test body and optional adapter checks."""
from __future__ import print_function

import os
import subprocess
import sys


ADAPTER_TEST_LANE = 'py36-django-20'


def main(lane=None, phase=None, call=None):
    """Run adapter checks once, after the supervisor's global replay phase."""
    lane = lane or os.environ.get('CI_REPLAY_LANE', '')
    phase = phase or os.environ.get('CI_REPLAY_PHASE', '')
    call = call or subprocess.call
    if phase not in ('priority', 'canonical'):
        print('Invalid CI_REPLAY_PHASE: %s' % phase, file=sys.stderr)
        return 2

    if phase == 'canonical' and lane == ADAPTER_TEST_LANE:
        status = call([sys.executable, '-m', 'unittest', 'discover', '-s',
                       'ci_replay_tests'])
        if status:
            return status

    return call(['coverage', 'run', '--source', 'src/auditlog',
                 'src/runtests.py'])


if __name__ == '__main__':
    sys.exit(main())
