"""Persistent CPython session. Launch only through the bubblewrap executor.

    Parent --JSON--> worker --exec--> question variables
    Parent <--JSON-- worker <--result/logs-- generated code

Only this process executes generated code. Each question owns a new process.
"""
import contextlib
from datetime import date, datetime
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import resource
import sys
import traceback

# The parent mounts only these trusted modules, not the source repository.
sys.path.insert(0, "/runtime")
import data_tools
from tools import DataTools

MAX_MESSAGE_BYTES = 65536
MAX_LOG_CHARACTERS = 8000


def json_value(value):
    """Preserve native numbers; never stringify arbitrary final values."""
    import numpy as np
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal) and value.is_finite():
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


class BoundedLog(io.StringIO):
    def write(self, value):
        remaining = MAX_LOG_CHARACTERS - self.tell()
        super().write(value[:max(0, remaining)])
        return len(value)


class FinalAnswer(BaseException):
    def __init__(self, value):
        self.value = value


def final_answer(answer):
    raise FinalAnswer(answer)


def summarize_variables(scope):
    import pandas as pd
    return {name: {"rows": len(value), "columns": [str(column) for column in value.columns[:25]]}
            for name, value in list(scope.items()) if isinstance(value, pd.DataFrame)}


def initialize(request):
    aliases = request["aliases"]
    if not isinstance(aliases, dict) or any(
        not isinstance(alias, str) or not alias.startswith("F") or not alias[1:].isdigit()
        or not isinstance(name, str) for alias, name in aliases.items()
    ):
        raise ValueError("Invalid file aliases")
    tools = DataTools(aliases)
    for alias in aliases:
        _, path = tools.source(alias)
        (Path("/tmp") / alias).symlink_to(path)
    scope = {
        "__name__": "__main__", "DATA_DIR": Path("/data"),
        "CALENDAR_PATH": Path("/calendar.csv"), "AS_OF_DATE": request["as_of_date"],
        "FILES": {alias: str(tools.source(alias)[1]) for alias in aliases},
        "read_table": tools.read_table, "read_workbook": tools.read_workbook,
        "inspect_table": tools.inspect_table,
        "table_profile": tools.table_profile, "parse_dates": data_tools.parse_dates,
        "parse_numbers": data_tools.parse_numbers, "checked_join": data_tools.checked_join,
        "split_units": data_tools.split_units, "final_answer": final_answer,
    }
    return tools, scope


def execute(code, scope, tools):
    logs = BoundedLog()
    result = None
    is_final = False
    # CPU time is per action, while memory and file-size limits cover the session.
    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu_limit = int(usage.ru_utime + usage.ru_stime) + 31
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, resource.RLIM_INFINITY))
    with contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
        try:
            exec(compile(code, "<agent-action>", "exec"), scope)
        except FinalAnswer as answer:
            result, is_final = answer.value, True
        except BaseException as error:
            return {"status": "error", "error": f"{type(error).__name__}: {error}",
                    "traceback": traceback.format_exc()[-3000:], "logs": logs.getvalue()}
    return {"status": "ok", "output": result, "is_final": is_final,
            "logs": logs.getvalue(), "reads": tools.reads, "variables": summarize_variables(scope)}


def main():
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
    # Reserve a protocol stream. Direct writes to fd 1/2 go to a bounded file,
    # so generated print output cannot accidentally become protocol messages.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    with open("/tmp/worker-output", "w") as raw_output:
        os.dup2(raw_output.fileno(), 1)
        os.dup2(raw_output.fileno(), 2)
        protocol.write('{"status":"ready"}\n')
        tools, scope = None, None
        while True:
            line = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                return
            try:
                request = json.loads(line)
                operation = request["operation"]
                if operation == "initialize" and scope is None:
                    tools, scope = initialize(request)
                    response = {"status": "ok"}
                elif operation == "manifest" and tools is not None:
                    response = {"status": "ok", "output": tools.manifest()}
                elif operation == "execute" and scope is not None:
                    response = execute(request["code"], scope, tools)
                else:
                    raise ValueError("Invalid worker operation")
                encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, default=json_value)
                if len(encoded.encode()) + 1 > MAX_MESSAGE_BYTES:
                    raise ValueError("Worker response exceeds message limit; summarize output")
            except BaseException as error:
                encoded = json.dumps({"status": "error", "error": f"{type(error).__name__}: {error}"})
            protocol.write(encoded + "\n")


if __name__ == "__main__":
    main()
