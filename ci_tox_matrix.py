"""Run tox environments with bounded in-job fail-fast cancellation.

The command is intended for a single Linux CI job. It does not provide
cross-job cancellation; keep the CI provider matrix to this one test job.
Python 2.7-compatible because the supported tox environments include py27.
"""
from __future__ import print_function

import errno
import os
import signal
import subprocess
import sys
import time


TOX_ENVS = (
    'py36-django-18', 'py35-django-18', 'py34-django-18', 'py27-django-18',
    'py36-django-110', 'py35-django-110', 'py34-django-110', 'py27-django-110',
    'py36-django-111', 'py35-django-111', 'py34-django-111', 'py27-django-111',
    'py36-django-20', 'py35-django-20', 'py34-django-20',
)


def _command(envname):
    return ['tox', '-e', envname]


def _combine_coverage(call=None):
    """Aggregate per-environment coverage files after every tox job passes."""
    call = call or subprocess.call
    return call(['coverage', 'combine'])


def main(run_jobs=None, combine=None):
    """Complete every lane's priority pass before starting canonical tests."""
    run_jobs = run_jobs or run
    combine = combine or _combine_coverage
    for phase in ('priority', 'canonical'):
        status = run_jobs(phase=phase)
        if status:
            return status
    return combine()


def _start(command, env):
    return subprocess.Popen(command, env=env, preexec_fn=os.setsid)


def _signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except OSError as error:
        if error.errno != errno.ESRCH:
            raise


def _group_exists(process):
    try:
        os.killpg(process.pid, 0)
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        raise
    return True


def _stop_all(active, grace_seconds, poll_interval):
    for process, _ in active:
        _signal_group(process, signal.SIGTERM)

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if all(process.poll() is not None for process, _ in active):
            break
        time.sleep(poll_interval)

    for process, _ in active:
        if _group_exists(process):
            _signal_group(process, signal.SIGKILL)

    deadline = time.time() + max(1, grace_seconds)
    while time.time() < deadline:
        if all(process.poll() is not None for process, _ in active):
            break
        time.sleep(poll_interval)

    survivors = [envname for process, envname in active
                 if process.poll() is None]
    for process, _ in active:
        if process.poll() is not None:
            process.wait()
    if survivors:
        raise RuntimeError('Process groups did not stop after SIGKILL: %s' %
                           ', '.join(survivors))


def run(envs=None, parallelism=5, grace_seconds=5, poll_interval=0.05,
        command_builder=None, popen=None, phase='canonical'):
    """Run envs, stop dequeuing on first error, and terminate active process trees."""
    if parallelism < 1 or grace_seconds < 0 or poll_interval <= 0:
        raise ValueError('invalid supervisor limits')
    if phase not in ('priority', 'canonical'):
        raise ValueError('invalid test phase')
    envs = list(TOX_ENVS if envs is None else envs)
    command_builder = command_builder or _command
    popen = popen or _start
    pending = list(envs)
    active = []
    first_failure = None

    try:
        while pending or active:
            finished = []
            for process, envname in active:
                status = process.poll()
                if status is not None:
                    print('Finished tox environment %s with status %d' %
                          (envname, status))
                    finished.append((process, envname, status))
                    if status and first_failure is None:
                        first_failure = status
            active = [(process, envname) for process, envname in active
                      if all(process is not item[0] for item in finished)]

            if first_failure is not None:
                failed_groups = [(process, envname)
                                 for process, envname, status in finished
                                 if status]
                _stop_all(active + failed_groups, grace_seconds, poll_interval)
                active = []
                pending = []
                break

            # Recheck remaining workers before filling a slot vacated by a
            # sibling; a failure may have completed during the prior poll.
            if finished:
                continue

            if pending and len(active) < parallelism:
                envname = pending.pop(0)
                child_env = os.environ.copy()
                child_env['CI_REPLAY_LANE'] = envname
                child_env['CI_REPLAY_PHASE'] = phase
                command = command_builder(envname)
                print('Starting tox environment %s' % envname)
                try:
                    process = popen(command, child_env)
                except OSError as error:
                    print('Could not start tox environment %s: %s' %
                          (envname, error), file=sys.stderr)
                    first_failure = 127
                    _stop_all(active, grace_seconds, poll_interval)
                    active = []
                    pending = []
                    break
                active.append((process, envname))
                continue

            if active:
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        _stop_all(active, grace_seconds, poll_interval)
        raise

    return first_failure or 0


if __name__ == '__main__':
    sys.exit(main())
