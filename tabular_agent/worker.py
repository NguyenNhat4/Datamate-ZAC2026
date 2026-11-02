"""Runs ONLY inside a fresh bubblewrap sandbox. No host-side exec fallback."""
import json
import importlib.util
import os
from pathlib import Path
import resource
import sqlite3
import sys
import traceback


def json_value(value):
    """Preserve numeric types from pandas/numpy; never stringify unknown objects."""
    import numpy as np
    from datetime import date, datetime
    from decimal import Decimal
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal) and value.is_finite():
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f'Unsupported result type: {type(value).__name__}')


def profile_data():
    """Bounded schema discovery; samples are examples, never complete tables."""
    root = Path('/data')
    files = [p for p in sorted(root.rglob('*')) if p.suffix.lower() in
             {'.csv', '.xlsx', '.sqlite', '.sqlite3', '.db'} and p.is_file()]
    profiles = {}
    for path in files[:40]:
        name = str(path.relative_to(root))
        try:
            info = inspect_data({'file': name})
            if 'sheets' in info and info['sheets']:
                # First sheet is illustrative; other sheets may have different schemas.
                info['first_sheet_example'] = inspect_data({'file': name, 'sheet': info['sheets'][0]})
            profiles[name] = info
        except Exception as exc:
            profiles[name] = {'error': str(exc)}
    columns = {}
    for filename, info in profiles.items():
        candidates = info.get('first_sheet_example', {}).get('header_candidates', [])
        names = candidates[0]['columns'] if candidates else info.get('columns', [])
        for name in names:
            if isinstance(name, str):
                columns.setdefault(name, set()).add(filename)
    shared = [{'column': name, 'files': sorted(locations)} for name, locations in columns.items()
              if len(locations) > 1][:40]
    return {'files': profiles, 'shared_column_names': shared, 'omitted_files': max(0, len(files) - 40),
            'note': 'Samples are incomplete; other sheets may differ. Shared names do not prove a join.'}


def inspect_data(request):
    import pandas as pd
    root = Path('/data')
    if not request.get('file'):
        return {'files': [str(p.relative_to(root)) for p in sorted(root.rglob('*')) if p.is_file()]}
    path = (root / request['file']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('file must be inside /data')
    suffix = path.suffix.lower()
    if suffix in {'.md', '.txt'}:
        return {'text': path.read_text(encoding='utf-8')[:12000]}
    if suffix == '.csv':
        # Match read_table and pandas' default handling of the UTF-8 BOM. The
        # Python CSV engine otherwise leaks it into the first column name.
        df = pd.read_csv(path, nrows=5, sep=None, engine='python', encoding='utf-8-sig')
    elif suffix == '.xlsx':
        import openpyxl
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            if request.get('sheet') is None:
                return {'sheets': book.sheetnames}
            if request['sheet'] not in book.sheetnames:
                return {'error': 'Unknown sheet; choose an actual sheet name', 'sheets': book.sheetnames}
            sheet = book[request['sheet']]
            rows = []
            for row in sheet.iter_rows(values_only=True):
                rows.append(list(row))
                if len(rows) == 6:
                    break
            # Candidate header indices are explicit suggestions, never silent normalization.
            candidates = tools_module().header_candidates(rows)
            return {'sheet': sheet.title, 'sample_rows': rows,
                    'header_candidates': candidates[:3],
                    'reported_rows': sheet.max_row, 'reported_columns': sheet.max_column}
        finally:
            book.close()
    elif suffix in {'.sqlite', '.db', '.sqlite3'}:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            names = [x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if request.get('table') is None:
                schemas = {}
                for name in names[:30]:
                    quoted = '"' + name.replace('"', '""') + '"'
                    schemas[name] = {'columns': [{'name': row[1], 'type': row[2], 'primary_key': bool(row[5])}
                                                 for row in db.execute(f'PRAGMA table_info({quoted})')],
                                     'foreign_keys': [list(row[2:5]) for row in db.execute(f'PRAGMA foreign_key_list({quoted})')]}
                return {'tables': names, 'schema': schemas}
            name = request['table']
            if name not in names:
                raise ValueError('Unknown table')
            quoted = '"' + name.replace('"', '""') + '"'
            cur = db.execute(f'SELECT * FROM {quoted} LIMIT 5')
            return {'columns': [c[0] for c in cur.description], 'sample_rows': cur.fetchall()}
    else:
        raise ValueError('Supported: CSV, XLSX, SQLite, Markdown')
    return {'columns': list(df.columns), 'dtypes': {str(k): str(v) for k, v in df.dtypes.items()},
            'sample_rows': json.loads(df.to_json(orient='records', date_format='iso'))}


def tools_module():
    spec = importlib.util.spec_from_file_location('safe_table_tools', '/data_tools.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def summarize_scope(scope):
    """Return bounded shapes/columns, never dataframe row contents."""
    pandas = sys.modules.get('pandas')
    if pandas is None or scope is None:
        return {}
    summaries = {}
    for name, value in list(scope.items()):
        if not isinstance(name, str) or name.startswith('_'):
            continue
        if isinstance(value, pandas.DataFrame):
            summaries[name[:120]] = {'kind': 'dataframe', 'rows': len(value),
                                     'columns': [str(column)[:60] for column in value.columns[:20]]}
        elif isinstance(value, pandas.Series):
            summaries[name[:120]] = {'kind': 'series', 'rows': len(value), 'dtype': str(value.dtype)[:120]}
        if len(summaries) >= 8:
            break
    return summaries


def main():
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
    request = json.load(sys.stdin)
    # Output file is opened by the trusted wrapper before executing model code.
    with open('/tmp/observation.json', 'w', encoding='utf-8') as output:
        scope = None
        try:
            if request['action'] == 'profile':
                observation = {'ok': True, 'data': profile_data()}
            elif request['action'] == 'inspect':
                data = inspect_data(request)
                observation = {'ok': 'error' not in data, 'data': data}
                if 'error' in data:
                    observation['error'] = data['error']
            elif request['action'] == 'python':
                helpers = tools_module()
                aliases = request.get('file_aliases', {})
                if not isinstance(aliases, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                        for k, v in aliases.items()):
                    raise ValueError('Invalid file aliases')
                paths = {key: helpers.source_path(filename) for key, filename in aliases.items()}

                # Give generated programs stable relative names without modifying /data.
                # Original relative filenames remain available for existing programs.
                if paths:
                    work = Path('/tmp/work')
                    work.mkdir()
                    for key, path in paths.items():
                        if not key.startswith('F') or not key[1:].isdigit() or '/' in key:
                            raise ValueError('Invalid file alias')
                        (work / key).symlink_to(path)
                    for key, filename in aliases.items():
                        relative = Path(filename)
                        if relative.is_absolute() or '..' in relative.parts:
                            raise ValueError('Invalid source path')
                        if relative.parts and relative.parts[0] in paths:
                            continue  # Alias names take precedence; /data keeps original paths.
                        target = work / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if not target.exists() and not target.is_symlink():
                            target.symlink_to(paths[key])
                    os.chdir(work)

                def read_aliased(name, *args, **kwargs):
                    return helpers.read_table(aliases.get(name, name), *args, **kwargs)

                scope = {'__name__': '__main__', 'DATA_DIR': Path('/data'),
                         'CALENDAR_PATH': Path('/calendar.csv'),
                         'AS_OF_DATE': request.get('as_of_date'),
                         'FILES': paths, 'read_table': read_aliased, 'parse_numbers': helpers.parse_numbers,
                         'parse_dates': helpers.parse_dates, 'checked_join': helpers.checked_join,
                         'split_units': helpers.split_units}
                exec(compile(request['code'], '<agent>', 'exec'), scope)
                observation = {'ok': True, 'result': scope.get('result'), 'evidence': scope.get('evidence')}
            else:
                raise ValueError('Unknown worker action')
        except BaseException as exc:
            observation = {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                           'traceback': traceback.format_exc()[-4000:]}
        variables = summarize_scope(scope)
        if variables:
            observation['variables'] = variables
        try:
            encoded = json.dumps(observation, ensure_ascii=False, default=json_value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            encoded = json.dumps({'ok': False, 'error': f'Result serialization: {exc}'})
        output.write(encoded)


if __name__ == '__main__':
    main()
