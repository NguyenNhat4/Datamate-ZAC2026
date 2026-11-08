"""Run generated Python in a persistent, operating-system-isolated process."""
import contextlib
import ast
import datetime
from decimal import Decimal
import io
import json
import math
import os
import re
import resource
import sys
import time
import traceback
import unicodedata

sys.path.insert(0, '/runtime')
from tools import AGENT_TOOLS


class FinalAnswer(BaseException):
    def __init__(self, value):
        self.value = value


class ObserveFirst(BaseException):
    def __init__(self, value):
        self.value = value


def encode_decimal(value):
    if not isinstance(value, Decimal):
        raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')
    if not value.is_finite():
        raise ValueError('Final answer cannot contain non-finite numbers')
    if value == value.to_integral_value():
        return int(value)
    number = float(value)
    if not math.isfinite(number) or Decimal(str(number)) != value:
        raise ValueError('Decimal cannot be represented without loss as a JSON number; round explicitly')
    return number


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


def report_categories(frame):
    """Report bounded categorical values from a full read, not guesses from a preview."""
    import pandas as pd
    if not isinstance(frame, pd.DataFrame):
        return
    profiles = []
    for index in range(min(len(frame.columns), 40)):
        column = frame.iloc[:, index]
        values = column.dropna()
        if values.empty or not all(isinstance(value, str) for value in values.head(50)):
            continue
        counts = values.value_counts().head(13)
        if len(counts) > 12:
            continue
        profiles.append({'column': str(frame.columns[index])[:120],
                         'values': [str(value)[:120] for value in counts.index],
                         'missing': int(column.isna().sum())})
    if profiles:
        print('Observed low-cardinality columns (full read):', repr(profiles)[:4000])


def checked_equal(left, right):
    """Flag a missing categorical literal when a closely related value exists."""
    import pandas as pd
    series, literal = (left, right) if isinstance(left, pd.Series) else (right, left)
    result = left == right
    if not isinstance(series, pd.Series) or series.empty:
        return result
    if bool(result.fillna(False).any()):
        return result
    sample = series.dropna().head(30)
    if (isinstance(literal, datetime.datetime) and not sample.empty
            and all(isinstance(value, datetime.date) and not isinstance(value, datetime.datetime) for value in sample)):
        raise ValueError('Date types do not match. Compare the parsed timestamp column directly with pd.Timestamp(as_of_date).')
    if isinstance(literal, datetime.date) and not isinstance(literal, datetime.datetime):
        if (pd.api.types.is_datetime64_any_dtype(series.dtype) or
                (not sample.empty and all(isinstance(value, str) and re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}', value.strip())
                                          for value in sample))):
            raise ValueError('Date types do not match. Parse the date column and compare with pd.Timestamp(as_of_date).')
    if not isinstance(literal, str):
        return result
    if (re.fullmatch(r'\d{4}-\d{2}-\d{2}', literal) and not sample.empty
            and all(isinstance(value, str) and re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}', value.strip())
                    for value in sample)):
        raise ValueError('The date column uses day/month/year text but the comparison uses an ISO date. '
                         'Parse the column and compare dates before accepting an empty result.')
    values = series.dropna().unique()
    if len(values) > 12 or not all(isinstance(value, str) for value in values):
        return result
    def words(value):
        value = unicodedata.normalize('NFKD', value.casefold())
        return set(''.join(char for char in value if not unicodedata.combining(char)).split())
    query = words(literal)
    similar = [value for value in values if query and query <= words(value)]
    if similar:
        raise ValueError(f'No exact match for {literal!r}; similar observed values: {similar[:12]}. '
                         'Inspect the data and use exact values; if the empty result is intentional, '
                         'use the Series.eq method explicitly after checking.')
    return result


def checked_order(left, right, operation):
    """Reject comparisons that would lexically order day-first date strings."""
    import pandas as pd
    import operator
    series, literal = (left, right) if isinstance(left, pd.Series) and isinstance(right, str) else (right, left)
    if isinstance(series, pd.Series) and isinstance(literal, str):
        pattern = r'^\d{1,2}/\d{1,2}/\d{4}$'
        sample = series.dropna().head(30)
        if (re.fullmatch(pattern, literal) or re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}', literal)) and not sample.empty and all(
                isinstance(value, str) and re.fullmatch(pattern, value.strip()) for value in sample):
            raise ValueError('Day/month/year date strings cannot be ordered as text; parse the column and boundaries as dates first.')
    return {'lt': operator.lt, 'le': operator.le, 'gt': operator.gt, 'ge': operator.ge}[operation](left, right)


def checked_row_count(value):
    """Flag ambiguous row counts when a table has repeated identifier values."""
    import pandas as pd
    if isinstance(value, pd.DataFrame):
        for name in value.columns:
            if not isinstance(name, str):
                continue
            normalized = ''.join(c for c in unicodedata.normalize('NFKD', name.casefold())
                                 if not unicodedata.combining(c)).replace('_', ' ')
            words = normalized.split()
            if not (words and (words[0] in {'id', 'ma', 'code'} or words[-1] in {'id', 'code'})):
                continue
            column = value[name]
            if isinstance(column, pd.Series) and column.dropna().duplicated().any():
                raise ValueError(f'Row count is not necessarily an entity count: identifier column {name!r} '
                                 'has repeated values. For an order count, use count_orders with the actual date, '
                                 'ID, status columns and rulebook eligibility; for other entities use nunique(); '
                                 'if the question really asks for rows use len(frame.index).')
    return len(value)


def compile_action(code):
    """Show bare inspection calls as a notebook would, without evaluating twice."""
    tree = ast.parse(code, filename='<agent>', mode='exec')
    class ShowInspections(ast.NodeTransformer):
        def visit_Call(self, node):
            self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id == 'len' and len(node.args) == 1 and not node.keywords:
                return ast.copy_location(ast.Call(func=ast.Name(id='_checked_row_count', ctx=ast.Load()),
                                                  args=node.args, keywords=[]), node)
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'nunique':
                return ast.copy_location(ast.Call(func=ast.Name(id='_checked_nunique', ctx=ast.Load()),
                                                  args=[node.func.value, *node.args], keywords=node.keywords), node)
            return node

        def visit_Subscript(self, node):
            self.generic_visit(node)
            if (isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Attribute)
                    and node.value.attr == 'shape' and isinstance(node.slice, ast.Constant)
                    and node.slice.value == 0):
                return ast.copy_location(ast.Call(func=ast.Name(id='_checked_row_count', ctx=ast.Load()),
                                                  args=[node.value.value], keywords=[]), node)
            return node

        def visit_Compare(self, node):
            self.generic_visit(node)
            order_ops = {ast.Lt: 'lt', ast.LtE: 'le', ast.Gt: 'gt', ast.GtE: 'ge'}
            if (len(node.ops) == 1 and type(node.ops[0]) in order_ops
                    and (isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
                         or isinstance(node.comparators[0], ast.Constant) and isinstance(node.comparators[0].value, str))):
                return ast.copy_location(ast.Call(func=ast.Name(id='_checked_order', ctx=ast.Load()),
                                                  args=[node.left, node.comparators[0], ast.Constant(order_ops[type(node.ops[0])])],
                                                  keywords=[]), node)
            if len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
                return ast.copy_location(ast.Call(func=ast.Name(id='_checked_equal', ctx=ast.Load()),
                                                  args=[node.left, node.comparators[0]], keywords=[]), node)
            return node

        def visit_Expr(self, node):
            self.generic_visit(node)
            call = node.value
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id in {'list_files', 'inspect_table', 'profile_columns'}):
                return ast.copy_location(ast.Expr(value=ast.Call(
                    func=ast.Name(id='_show_inspection', ctx=ast.Load()),
                    args=[call], keywords=[])), node)
            return node
    return compile(ast.fix_missing_locations(ShowInspections().visit(tree)), '<agent>', 'exec')


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
    measurements = []
    table_cache = {}
    trace_enabled = [False]
    reads = {'current': False, 'previous': False}

    def bind(tool):
        def call(*args, **kwargs):
            started = time.monotonic()
            cache_hit = False
            item = None
            if trace_enabled[0] and len(calls) < 20:
                item = {'name': tool.name, 'arguments': describe({'args': args, 'kwargs': kwargs})[:1000],
                        'started_ns': time.time_ns()}
                calls.append(item)
            try:
                cache_key = None
                if tool.name in {'read_table', 'read_workbook'}:
                    cache_key = json.dumps([tool.name, args, kwargs], sort_keys=True, ensure_ascii=False)
                    cache_hit = cache_key in table_cache
                if cache_hit:
                    result = table_cache[cache_key].copy(deep=True)
                else:
                    result = tool.forward(*args, **kwargs)
                    if cache_key is not None:
                        table_cache[cache_key] = result.copy(deep=True)
                if item is not None:
                    item['result'] = describe(result)[:1000]
                if tool.name in {'read_table', 'read_workbook'}:
                    report_categories(result)
                    reads['current'] = True
                return result
            except Exception as error:
                if item is not None:
                    item['error'] = f'{type(error).__name__}: {error}'[:500]
                raise
            finally:
                if len(measurements) < 100:
                    measurements.append({'tool': tool.name, 'seconds': time.monotonic() - started,
                                         'cache_hit': cache_hit})
                if item is not None:
                    item['ended_ns'] = time.time_ns()
        return call

    scope = {tool.name: bind(tool) for tool in AGENT_TOOLS}
    import pandas as pd
    def checked_nunique(value, *args, **kwargs):
        def normalized(text):
            text = str(text).casefold().replace('đ', 'd')
            return ''.join(c for c in unicodedata.normalize('NFKD', text)
                           if not unicodedata.combining(c)).replace('_', ' ')
        if (isinstance(value, pd.Series) and isinstance(value.name, str)
                and ('don hang' in normalized(value.name) or 'order' in normalized(value.name))):
            # Check the same selected rows, not an unrelated or unfiltered table.
            for frame in reversed(list(scope.values())):
                if (isinstance(frame, pd.DataFrame) and frame.index.is_unique and value.index.is_unique
                        and value.name in frame.columns and value.index.isin(frame.index).all()
                        and any('trang thai' in normalized(c) or 'status' in normalized(c) for c in frame.columns)):
                    selected = frame.loc[value.index]
                    if selected[value.name].equals(value):
                        from tools import count_entities
                        count_entities.forward(selected, value.name)
                        break
        return value.nunique(*args, **kwargs)
    def show_inspection(value):
        print(repr(value)[:6000])

    def observed_final_answer(value):
        if (isinstance(value, dict) and value.get('type') == 'unanswerable'
                and not reads['current'] and not reads['previous']):
            raise ValueError('A preview cannot establish that information is absent. '
                             'Read the relevant sources fully before returning unanswerable; '
                             'if you cannot establish absence, abstain.')
        if reads['current'] and not reads['previous'] and not (isinstance(value, dict) and value.get('type') == 'abstain'):
            raise ObserveFirst(value)
        final_answer(value)

    scope.update({'__name__': '__main__', 'final_answer': observed_final_answer, '_show_inspection': show_inspection,
                  '_checked_equal': checked_equal, '_checked_order': checked_order,
                  '_checked_row_count': checked_row_count, '_checked_nunique': checked_nunique,
                  'pd': pd, 'datetime': datetime})
    protocol.write('{"status":"ready"}\n')
    for line in sys.stdin:
        logs = None
        try:
            request = json.loads(line)
            if request.get('as_of_date') is not None:
                scope['as_of_date'] = request['as_of_date']
            calls.clear()
            measurements.clear()
            reads['current'] = False
            trace_enabled[0] = request.get('trace_tools') is True
            logs = BoundedLog()
            usage = resource.getrusage(resource.RUSAGE_SELF)
            resource.setrlimit(resource.RLIMIT_CPU, (int(usage.ru_utime + usage.ru_stime) + 31, resource.RLIM_INFINITY))
            result, final = None, False
            try:
                with contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
                    exec(compile_action(request['code']), scope)
            except FinalAnswer as answer:
                result, final = answer.value, True
            except ObserveFirst as candidate:
                result = candidate.value
                logs.write('\nReview the full-read observations before answering. Check observed values, '
                           'the question period and the candidate shown as the last output; then call '
                           'final_answer in a new step.\n')
            if reads['current']:
                reads['previous'] = True
            response = {'status': 'ok', 'logs': logs.getvalue()[:8000], 'output': result, 'final': final,
                        'tool_calls': calls, 'measurements': measurements}
            try:
                encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, default=encode_decimal)
            except TypeError as error:
                if final:
                    raise ValueError(f'Final answer must contain JSON values; convert pandas objects to scalar numbers or lists: {error}') from error
                raise
            if len(encoded.encode()) > 65535:
                raise ValueError('Output too large; print a smaller summary')
        except BaseException as error:
            encoded = json.dumps({'status': 'error', 'error': f'{type(error).__name__}: {error}',
                                  'logs': logs.getvalue()[:8000] if logs is not None else '',
                                  'tool_calls': calls, 'measurements': measurements}, ensure_ascii=False)
        protocol.write(encoded + '\n')


if __name__ == '__main__':
    main()
