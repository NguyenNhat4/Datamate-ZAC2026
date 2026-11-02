"""Minimal host filesystem, isolated network/PID namespace, bounded execution."""
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time


def read_bounded(path, limit):
    """Never follow model-created symlinks into the host filesystem."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Worker output must be a regular file')
        return stream.read(limit)


class Sandbox:
    def __init__(self, timeout=40):
        self.timeout = timeout
        if not shutil.which('bwrap'):
            raise RuntimeError('bubblewrap missing: install with sudo apt-get install bubblewrap')

    def command(self, business, calendar, scratch):
        cmd = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session',
               '--cap-drop', 'ALL', '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
               '--setenv', 'HOME', '/tmp', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
               '--setenv', 'OPENBLAS_NUM_THREADS', '1', '--setenv', 'OMP_NUM_THREADS', '1']
        for path in ('/usr', '/lib', '/lib64'):
            if Path(path).exists():
                cmd += ['--ro-bind', path, path]
        # Only the interpreter's environment is exposed, never the project/home tree.
        prefix = Path(sys.prefix).resolve()
        if not prefix.is_relative_to('/usr'):
            cmd += ['--ro-bind', str(prefix), str(prefix)]
        base = Path(sys.base_prefix).resolve()
        if not base.is_relative_to('/usr') and base != prefix:
            cmd += ['--ro-bind', str(base), str(base)]
        cmd += ['--ro-bind', str(Path(__file__).with_name('worker.py')), '/worker.py',
                '--ro-bind', str(Path(__file__).with_name('data_tools.py')), '/data_tools.py',
                '--ro-bind', str(business), '/data', '--ro-bind', str(calendar), '/calendar.csv',
                '--bind', str(scratch), '/tmp', '--proc', '/proc', '--dev', '/dev',
                '--chdir', '/data', sys.executable, '-I', '/worker.py']
        return cmd

    def run(self, request, business, calendar, timeout=None):
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='analysis-worker-') as temp:
            scratch = Path(temp)
            with (scratch / 'stdout').open('wb') as stdout, (scratch / 'stderr').open('wb') as stderr:
                proc = subprocess.Popen(self.command(business, calendar, scratch), stdin=subprocess.PIPE,
                                        stdout=stdout, stderr=stderr, start_new_session=True)
                timed_out = False
                try:
                    proc.communicate(json.dumps(request).encode(), timeout=min(self.timeout, timeout or self.timeout))
                except subprocess.TimeoutExpired:
                    timed_out = True
                finally:
                    # Kill the namespace owner and descendants even after the main script exits.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
            observation = {'ok': False, 'error': 'Execution timed out' if timed_out else f'Worker exited {proc.returncode}'}
            path = scratch / 'observation.json'
            if not timed_out and proc.returncode == 0:
                try:
                    raw = read_bounded(path, 65537)
                    if len(raw) > 65536:
                        raise ValueError('Worker output too large')
                    observation = json.loads(raw)
                    if not isinstance(observation, dict):
                        raise ValueError('Worker output must be an object')
                except (ValueError, OSError):
                    observation = {'ok': False, 'error': 'Invalid worker result JSON'}
            for name, limit in (('stdout', 12000), ('stderr', 4000)):
                try:
                    observation[name] = read_bounded(scratch / name, limit).decode(errors='replace')
                except (ValueError, OSError):
                    observation[name] = '[invalid worker output file]'
            observation['elapsed_seconds'] = round(time.monotonic() - started, 3)
            return observation

    def check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'calendar.csv').write_text('date\n')
            result = self.run({'action': 'python', 'code': "import pandas, openpyxl; result = 'ready'"}, root, root / 'calendar.csv')
            if not result.get('ok') or result.get('result') != 'ready':
                raise RuntimeError(f'Sandbox preflight failed: {result}')
