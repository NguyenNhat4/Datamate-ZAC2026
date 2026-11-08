"""Connect a smolagents PythonExecutor to an isolated bubblewrap worker."""
import json
from datetime import date
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
    def __init__(self, business: Path, calendar: Path, timeout: float = 30, emit=None, reject_repeated=False,
                 as_of_date=None):
        """Configure a lazy-starting isolated worker for one business.

        Args:
            business: Business directory to resolve and mount read-only at /data.
            calendar: Calendar file to resolve and mount at /calendar.csv.
            timeout: Maximum seconds to wait for each worker response.
            emit: Optional callback accepting execution event dictionaries.
            reject_repeated: Reject code identical to the last failed worker action.
            as_of_date: Optional canonical ISO date restored in worker scope.

        Returns:
            None. Initializes worker state without launching a process.

        Raises:
            RuntimeError: bubblewrap is not available on PATH.
            ValueError: as_of_date is not a valid canonical ISO date.
        """
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
        self.reject_repeated = reject_repeated
        if as_of_date is not None and date.fromisoformat(as_of_date).isoformat() != as_of_date:
            raise ValueError('as_of_date must be an ISO date')
        self.as_of_date = as_of_date
        self.failed_action = None

    def send_tools(self, tools):
        """Validate the framework's tools against the worker's registered helpers.

        Args:
            tools: Mapping from tool names to framework tool instances.

        Returns:
            None; tools already exist in the worker and are not serialized here.

        Raises:
            ValueError: Names differ from AGENT_TOOLS plus final_answer, or any
                registered helper is replaced by a different object.
        """
        expected = {tool.name: tool for tool in AGENT_TOOLS}
        if set(tools) != set(expected) | {'final_answer'} or any(tools[k] is not v for k, v in expected.items()):
            raise ValueError('Only the registered tools may execute in the worker')

    def send_variables(self, variables):
        """Validate the framework's initial-variable request.

        Args:
            variables: Mapping of variables the framework wants to transfer.

        Returns:
            None for an empty mapping; variables are not transferred to the worker.

        Raises:
            ValueError: The supplied mapping is nonempty.
        """
        if variables:
            raise ValueError('Initial variables are not supported')

    def _start(self):
        """Launch this executor's worker and wait for its ready message.

        Returns:
            None. Sets process and scratch directory state; mounts runtime and
            business inputs read-only, provides writable /tmp, and isolates the
            worker's network and other namespaces using bubblewrap.

        Raises:
            OSError: Temporary storage or the subprocess cannot be created.
            RuntimeError: The worker exits or does not send ready status.
            TimeoutError: The worker fails to respond before self.timeout.
            ValueError: The startup protocol is invalid or oversized.
        """
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
        """Read one newline-delimited JSON response from the running worker.

        Returns:
            Decoded JSON response. Retains bytes after the first newline for the
            next response; uses self.timeout as the wait limit in seconds.

        Raises:
            TimeoutError: No complete response arrives within the timeout.
            RuntimeError: Worker stdout closes before a complete response arrives.
            ValueError: The buffer exceeds 65,536 bytes or JSON decoding fails.
        """
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
        """Execute generated Python, preserving worker variables after success.

        Args:
            code_action: Python source string to execute in the worker's scope.

        Returns:
            CodeOutput containing the returned value, bounded logs and final flag.
            Starts the worker lazily and emits code, timing and observation events.

        Raises:
            ValueError: Code repeats a rejected failure, the request is too large,
                or the worker reports an execution or serialization error.
            RuntimeError: The worker cannot start or exits unexpectedly.
            TimeoutError: A worker response exceeds the configured timeout.

        Notes:
            Any failure cleans up the process and discards its variables and table
            cache. Subsequent calls start fresh and must reload their source data.
        """
        try:
            if self.reject_repeated and code_action == self.failed_action:
                raise ValueError('Repeated failed action; repair the error or abstain')
            if self.process is None:
                started = time.monotonic()
                self._start()
                self.emit({'stage': 'timing', 'operation': 'worker_start', 'seconds': time.monotonic() - started})
            request = json.dumps({'code': code_action, 'trace_tools': is_enabled(), 'as_of_date': self.as_of_date}, ensure_ascii=False).encode() + b'\n'
            if len(request) > 65536:
                raise ValueError('Code action too large')
            self.dispatches += 1
            self.emit({'stage': 'code', 'code': code_action})
            self.process.stdin.write(request)
            started = time.monotonic()
            response = self._read()
            self.emit({'stage': 'timing', 'operation': 'execution', 'seconds': time.monotonic() - started,
                       'tools': response.get('measurements', [])})
            record_tool_calls(response.get('tool_calls', []))
            if response['status'] != 'ok':
                self.failed_action = code_action
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
        """Terminate the worker and remove its temporary files.

        Returns:
            None. Kills the running process group, waits, closes protocol streams,
            removes scratch storage and clears buffered output. Safe to call again
            after successful cleanup; counters and failed_action are retained.
        """
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
