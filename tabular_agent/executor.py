"""Connect a smolagents PythonExecutor to an isolated bubblewrap worker."""
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from smolagents.local_python_executor import CodeOutput, PythonExecutor
from .tools import AGENT_TOOLS
from .telemetry import is_enabled, record_tool_calls


class IsolatedExecutor(PythonExecutor):
    def __init__(self, business: Path, calendar: Path, timeout: float = 30, emit=None):
        if not shutil.which('bwrap'):
            raise RuntimeError('bubblewrap is required for generated code execution')
        self.business = business.resolve()
        self.calendar = calendar.resolve()
        self.timeout = timeout
        self.emit = emit or (lambda event: None)
        self.process = None
        self.scratch = None
        self.buffer = bytearray()
        self.dispatches = 0

    def send_tools(self, tools):
        expected = {tool.name: tool for tool in AGENT_TOOLS}
        if set(tools) != set(expected) | {'final_answer'} or any(tools[k] is not v for k, v in expected.items()):
            raise ValueError('Only the registered tools may execute in the worker')

    def send_variables(self, variables):
        if variables:
            raise ValueError('Initial variables are not supported')

    def _start(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='agent-worker-')
        cmd = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
               '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'HOME', '/tmp',
               '--setenv', 'PYTHONDONTWRITEBYTECODE', '1', '--setenv', 'OPENBLAS_NUM_THREADS', '1',
               '--setenv', 'OMP_NUM_THREADS', '1']
        for p in ('/usr', '/lib', '/lib64'):
            if Path(p).exists():
                cmd += ['--ro-bind', p, p]
        paths = {Path(sys.prefix), Path(sys.base_prefix)}
        executable = Path(sys.executable)
        while executable.is_symlink():
            target = executable.readlink()
            executable = target if target.is_absolute() else executable.parent / target
            paths.add(executable.parent.parent)
        for p in sorted(paths):
            if not p.is_relative_to('/usr'):
                cmd += ['--ro-bind', str(p), str(p)]
        here = Path(__file__).parent
        for name in ('worker.py', 'tools.py'):
            cmd += ['--ro-bind', str(here / name), f'/runtime/{name}']
        cmd += ['--ro-bind', str(self.business), '/data', '--ro-bind', str(self.calendar), '/calendar.csv',
                '--bind', self.scratch.name, '/tmp', '--proc', '/proc', '--dev', '/dev',
                '--chdir', '/tmp', sys.executable, '-I', '-u', '/runtime/worker.py']
        self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        start_new_session=True, bufsize=0)
        os.set_blocking(self.process.stdout.fileno(), False)
        if self._read()['status'] != 'ready':
            raise RuntimeError('Worker failed to start')

    def _read(self):
        limit = time.monotonic() + self.timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                remaining = limit - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError('Worker timed out')
                chunk = os.read(self.process.stdout.fileno(), 8192)
                if not chunk:
                    error = self.process.stderr.read(2000).decode(errors='replace')
                    raise RuntimeError(f'Worker exited: {error}')
                self.buffer.extend(chunk)
                if len(self.buffer) > 65536:
                    raise ValueError('Worker response too large')
        line, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        return json.loads(line)

    def __call__(self, code_action):
        try:
            if self.process is None:
                self._start()
            request = json.dumps({'code': code_action, 'trace_tools': is_enabled()}, ensure_ascii=False).encode() + b'\n'
            if len(request) > 65536:
                raise ValueError('Code action too large')
            self.dispatches += 1
            self.emit({'stage': 'code', 'code': code_action})
            self.process.stdin.write(request)
            response = self._read()
            record_tool_calls(response.get('tool_calls', []))
            if response['status'] != 'ok':
                self.cleanup()
                logs = response.get('logs', '')
                if logs:
                    self.emit({'stage': 'observation', 'text': logs})
                detail = ('\nOutput before error (worker state was discarded):\n' + logs) if logs else ''
                raise ValueError(response['error'] + '; worker reset, reload sources and recompute' + detail)
            self.emit({'stage': 'observation', 'text': response['logs']})
            return CodeOutput(response['output'], response['logs'], response['final'])
        except BaseException:
            self.cleanup()
            raise

    def cleanup(self):
        if self.process is not None:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            self.process = None
        if self.scratch is not None:
            self.scratch.cleanup()
            self.scratch = None
        self.buffer.clear()
