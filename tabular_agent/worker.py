"""Run generated Python in a persistent, operating-system-isolated process."""
import contextlib
import io
import json
import os
import resource
import sys
import time
import traceback

sys.path.insert(0, '/runtime')
from tools import AGENT_TOOLS


class FinalAnswer(BaseException):
    def __init__(self, value):
        self.value = value


def final_answer(value):
    raise FinalAnswer(value)


class BoundedLog(io.StringIO):
    def write(self, value):
        super().write(value[:max(0, 8000 - self.tell())])
        return len(value)


def describe(value, depth=0):
    """Keep traces small and retain DataFrames in the worker."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return repr(value)[:300]
    if depth >= 3:
        return f'<{type(value).__name__}>'
    if type(value).__module__.startswith('pandas'):
        return f'<{type(value).__name__} shape={value.shape}>'
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(describe(item, depth + 1) for item in value[:5]) + (', ...' if len(value) > 5 else '') + ']'
    if isinstance(value, dict):
        from itertools import islice
        return '{' + ', '.join(f'{describe(key, depth + 1)}: {describe(item, depth + 1)}' for key, item in islice(value.items(), 5)) + '}'
    return f'<{type(value).__name__}>'


def main():
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
    protocol = os.fdopen(os.dup(1), 'w', buffering=1)
    output = open('/tmp/output', 'w')
    os.dup2(output.fileno(), 1)
    os.dup2(output.fileno(), 2)
    calls = []
    trace_enabled = [False]

    def bind(tool):
        def call(*args, **kwargs):
            if not trace_enabled[0] or len(calls) >= 20:
                return tool.forward(*args, **kwargs)
            item = {'name': tool.name, 'arguments': describe({'args': args, 'kwargs': kwargs})[:1000],
                    'started_ns': time.time_ns()}
            calls.append(item)
            try:
                result = tool.forward(*args, **kwargs)
                item['result'] = describe(result)[:1000]
                return result
            except Exception as error:
                item['error'] = f'{type(error).__name__}: {error}'[:500]
                raise
            finally:
                item['ended_ns'] = time.time_ns()
        return call

    scope = {tool.name: bind(tool) for tool in AGENT_TOOLS}
    scope.update({'__name__': '__main__', 'final_answer': final_answer})
    protocol.write('{"status":"ready"}\n')
    for line in sys.stdin:
        try:
            request = json.loads(line)
            calls.clear()
            trace_enabled[0] = request.get('trace_tools') is True
            logs = BoundedLog()
            usage = resource.getrusage(resource.RUSAGE_SELF)
            resource.setrlimit(resource.RLIMIT_CPU, (int(usage.ru_utime + usage.ru_stime) + 31, resource.RLIM_INFINITY))
            result, final = None, False
            try:
                with contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
                    exec(compile(request['code'], '<agent>', 'exec'), scope)
            except FinalAnswer as answer:
                result, final = answer.value, True
            response = {'status': 'ok', 'logs': logs.getvalue()[:8000], 'output': result, 'final': final,
                        'tool_calls': calls}
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > 65535:
                raise ValueError('Output too large; print a smaller summary')
        except BaseException as error:
            encoded = json.dumps({'status': 'error', 'error': f'{type(error).__name__}: {error}',
                                  'tool_calls': calls}, ensure_ascii=False)
        protocol.write(encoded + '\n')


if __name__ == '__main__':
    main()
