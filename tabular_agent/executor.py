"""smolagents PythonExecutor backed by one persistent bubblewrap process."""
import copy
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

from .data import BusinessContext
from .evidence import validate_candidate
from .runtime import Deadline, QuestionStopped

MAX_MESSAGE_BYTES = 65536


class IsolatedExecutor(PythonExecutor):
    """Own worker state and recovery; never execute code in the parent process."""

    def __init__(self, context: BusinessContext, as_of_date: str, deadline: Deadline,
                 action_seconds: float = 30, emit=None):
        if not shutil.which("bwrap"):
            raise RuntimeError("bubblewrap is required; there is no host execution fallback")
        self.context = context
        self.as_of_date = as_of_date
        self.deadline = deadline
        self.action_seconds = action_seconds
        self.emit = emit or (lambda event: None)
        self.generation = 0
        self.process = None
        self.scratch = None
        self.stderr = None
        self.buffer = bytearray()
        self.reads = []
        self.last_candidate = None
        self.candidate_generation = None
        self.dispatches = 0

    def send_tools(self, tools):
        if set(tools) != {"final_answer"}:
            raise ValueError("Only the trusted worker tool registry and final_answer are supported")

    def send_variables(self, variables):
        if variables:
            raise ValueError("Initial worker variables come from the validated question context")

    def command(self, scratch: Path) -> list[str]:
        command = [
            "bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/tmp",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "OPENBLAS_NUM_THREADS", "1",
            "--setenv", "OMP_NUM_THREADS", "1",
        ]
        for path in (Path("/usr"), Path("/lib"), Path("/lib64")):
            if path.exists():
                command += ["--ro-bind", str(path), str(path)]
        runtime_paths = {Path(sys.prefix), Path(sys.base_prefix)}
        executable = Path(sys.executable)
        while executable.is_symlink():
            target = executable.readlink()
            executable = target if target.is_absolute() else executable.parent / target
            runtime_paths.add(executable.parent.parent)
        # uv-managed interpreters may point through a version alias. Mount both
        # the alias and resolved runtime so the venv's executable remains valid.
        for path in sorted(runtime_paths):
            if not path.is_relative_to("/usr"):
                command += ["--ro-bind", str(path), str(path)]
        source = Path(__file__).parent
        for name in ("worker.py", "tools.py", "data_tools.py"):
            command += ["--ro-bind", str(source / name), f"/runtime/{name}"]
        command += [
            "--ro-bind", str(self.context.directory), "/data",
            "--ro-bind", str(self.context.calendar), "/calendar.csv",
            "--bind", str(scratch), "/tmp", "--proc", "/proc", "--dev", "/dev",
            "--chdir", "/tmp", sys.executable, "-I", "-u", "/runtime/worker.py",
        ]
        return command

    def _wait_for(self, stream, event, until):
        remaining = min(until - time.monotonic(), self.deadline.remaining())
        if remaining <= 0:
            raise QuestionStopped("Worker action timed out")
        with selectors.DefaultSelector() as selector:
            selector.register(stream, event)
            if not selector.select(remaining):
                raise QuestionStopped("Worker action timed out")

    def _write(self, request, until):
        encoded = json.dumps(request, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise ValueError("Code/request exceeds the worker message limit")
        offset = 0
        while offset < len(encoded):
            self._wait_for(self.process.stdin, selectors.EVENT_WRITE, until)
            try:
                offset += os.write(self.process.stdin.fileno(), encoded[offset:])
            except BlockingIOError:
                continue

    def _read(self, until):
        while b"\n" not in self.buffer:
            self._wait_for(self.process.stdout, selectors.EVENT_READ, until)
            try:
                chunk = os.read(self.process.stdout.fileno(), 8192)
            except BlockingIOError:
                continue
            if not chunk:
                raise QuestionStopped("Worker exited before returning a response")
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_MESSAGE_BYTES:
                raise QuestionStopped("Invalid worker protocol: oversized response")
        line, _, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        try:
            response = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError) as error:
            raise QuestionStopped("Invalid worker protocol: expected finite JSON") from error
        if not isinstance(response, dict) or response.get("status") not in {"ready", "ok", "error"}:
            raise QuestionStopped("Invalid worker protocol response")
        return response

    def _start(self):
        if self.process is not None:
            return
        self.deadline.remaining()
        self.scratch = tempfile.TemporaryDirectory(prefix="table-session-")
        scratch = Path(self.scratch.name)
        self.stderr = (scratch / "startup-stderr").open("w+b")
        self.process = subprocess.Popen(
            self.command(scratch), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr, start_new_session=True, bufsize=0,
        )
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        until = time.monotonic() + min(self.action_seconds, self.deadline.remaining())
        if self._read(until).get("status") != "ready":
            raise QuestionStopped("Worker failed its startup handshake")
        self._write({"operation": "initialize", "aliases": self.context.aliases,
                     "as_of_date": self.as_of_date}, until)
        initialized = self._read(until)
        if initialized["status"] != "ok":
            raise QuestionStopped(f"Worker initialization failed: {initialized.get('error')}")
        self.generation += 1
        self.emit({"stage": "worker_start", "generation": self.generation})

    def _request(self, request):
        try:
            self._start()
            until = time.monotonic() + min(self.action_seconds, self.deadline.remaining())
            if request["operation"] == "execute":
                self.dispatches += 1
                self.emit({"stage": "code", "generation": self.generation, "code": request["code"]})
            self._write(request, until)
            return self._read(until)
        except (BrokenPipeError, OSError) as error:
            self.cleanup()
            raise QuestionStopped(f"Worker communication failed: {type(error).__name__}") from error
        except QuestionStopped as error:
            details = ""
            if self.stderr is not None:
                self.stderr.seek(0)
                details = self.stderr.read(2000).decode(errors="replace").strip()
            self.cleanup()
            if details:
                raise QuestionStopped(f"{error}; worker diagnostics: {details}") from error
            raise

    def manifest(self):
        response = self._request({"operation": "manifest"})
        if response["status"] != "ok":
            raise QuestionStopped(f"Manifest inspection failed: {response.get('error')}")
        return response["output"]

    def __call__(self, code_action: str) -> CodeOutput:
        self.last_candidate = None
        self.candidate_generation = None
        response = self._request({"operation": "execute", "code": code_action})
        self.emit({"stage": "observation", "generation": self.generation, **response})
        if response["status"] == "error":
            error = str(response.get("error", "Unknown execution error"))
            self.cleanup()
            self.emit({"stage": "worker_reset", "generation": self.generation, "reason": error})
            raise ValueError(f"{error}\nWorker reset: all computed variables were lost. Reload sources and recompute.")
        if (type(response.get("is_final")) is not bool or not isinstance(response.get("reads"), list)
                or not isinstance(response.get("logs"), str)):
            self.cleanup()
            raise QuestionStopped("Invalid execution response from worker")
        self.reads = response["reads"]
        if response["is_final"]:
            self.last_candidate = copy.deepcopy(response["output"])
            self.candidate_generation = self.generation
        logs = response["logs"]
        if response.get("variables"):
            logs += "\nDataFrame shapes: " + json.dumps(response["variables"], ensure_ascii=False)
        return CodeOutput(output=response["output"], logs=logs, is_final_answer=response["is_final"])

    def validate_final(self, candidate):
        if (self.candidate_generation != self.generation or self.last_candidate is None
                or candidate != self.last_candidate):
            raise ValueError("Answer must come from a successful final_answer call in the current worker session")
        return validate_candidate(candidate, self.reads)

    def cleanup(self):
        """Kill the namespace owner and descendants before removing private scratch."""
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None
        if self.stderr is not None:
            self.stderr.close()
            self.stderr = None
        if self.scratch is not None:
            self.scratch.cleanup()
            self.scratch = None
        self.buffer.clear()
        self.reads = []
        self.last_candidate = None
        self.candidate_generation = None
