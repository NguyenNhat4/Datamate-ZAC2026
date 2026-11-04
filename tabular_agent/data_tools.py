"""Explicit, conservative table readers for model-generated sandbox programs."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from numbers import Real
from pathlib import Path
import csv
import re
import sqlite3

ROOT = Path('/data')


def source_path(filename):
    if not isinstance(filename, str) or not filename:
        raise ValueError('filename must be a nonempty relative path')
    path = (ROOT / filename).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError('file must be inside the current business directory')
    return path


def header_candidates(rows):
    """Return plausible header rows; indices are zero-based, values remain raw."""
    candidates = []
    for index, row in enumerate(rows):
        cells = [x.strip() for x in row if isinstance(x, str) and x.strip()]
        labels = [x for x in cells if not re.fullmatch(r'[+\-\d\s.,:/()%]+', x)]
        if len(labels) < 2 or len(set(cells)) != len(cells):
            continue
        candidates.append({'row': index, 'columns': cells, 'score': len(labels)})
    return sorted(candidates, key=lambda c: (-c['score'], c['row']))


def choose_header(rows):
    candidates = header_candidates(rows)
    if not candidates:
        raise ValueError('No header found; supply an explicit header_row')
    if len(candidates) > 1 and candidates[0]['score'] == candidates[1]['score']:
        raise ValueError('Ambiguous header; inspect the file and supply header_row explicitly')
    return candidates[0]['row']


def read_table(filename, sheet=None, table=None, header_row=None, delimiter=None, encoding='utf-8-sig',
               numeric_columns=None, decimal_separator=None, thousands_separator=None):
    """Read one table, with numeric conversion only for explicitly selected columns."""
    import pandas as pd
    path = source_path(filename)
    suffix = path.suffix.lower()
    if header_row is not None and (type(header_row) is not int or header_row < 0):
        raise ValueError('header_row must be a zero-based non-negative integer')
    if suffix == '.xlsx':
        import openpyxl
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            if sheet is None:
                if len(book.sheetnames) != 1:
                    raise ValueError(f'Choose a sheet from: {book.sheetnames}')
                sheet = book.sheetnames[0]
            if sheet not in book.sheetnames:
                raise ValueError(f'Unknown sheet; available: {book.sheetnames}')
            preview = []
            for row in book[sheet].iter_rows(values_only=True):
                preview.append(row)
                if len(preview) >= 8:
                    break
        finally:
            book.close()
        header = choose_header(preview) if header_row is None else header_row
        frame = pd.read_excel(path, sheet_name=sheet, header=header, dtype=object)
    elif suffix == '.csv':
        if delimiter is not None and (not isinstance(delimiter, str) or len(delimiter) != 1):
            raise ValueError('delimiter must be one character')
        with path.open(encoding=encoding, newline='') as stream:
            sample = stream.read(8192)
            stream.seek(0)
            if delimiter is None:
                try:
                    delimiter = csv.Sniffer().sniff(sample, delimiters=',;\t|').delimiter
                except csv.Error as exc:
                    raise ValueError('CSV delimiter is ambiguous; supply delimiter explicitly') from exc
            rows = []
            for row in csv.reader(stream, delimiter=delimiter):
                rows.append(row)
                if len(rows) >= 8:
                    break
        header = choose_header(rows) if header_row is None else header_row
        frame = pd.read_csv(path, sep=delimiter, header=header, encoding=encoding,
                            dtype=object, skip_blank_lines=False)
    elif suffix in {'.sqlite', '.db', '.sqlite3'}:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            names = [x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if table not in names:
                raise ValueError(f'Choose a table from: {names}')
            quoted = '"' + table.replace('"', '""') + '"'
            frame = pd.read_sql_query(f'SELECT * FROM {quoted}', db)
    else:
        raise ValueError('Supported table formats are xlsx, csv and sqlite')
    if numeric_columns is not None:
        if not isinstance(numeric_columns, list) or not all(isinstance(name, str) for name in numeric_columns):
            raise ValueError('numeric_columns must be a list of column names')
        if decimal_separator is None or thousands_separator is None:
            raise ValueError('Numeric columns require explicit decimal_separator and thousands_separator')
        for name in numeric_columns:
            if name not in frame.columns:
                raise ValueError(f'Missing numeric column: {name}; available columns: {list(frame.columns)}')
            frame[name] = parse_numbers(frame[name], decimal_separator=decimal_separator,
                                         thousands_separator=thousands_separator)
    return frame


def parse_numbers(values, *, decimal_separator, thousands_separator):
    """Parse explicitly specified separators using Decimal; fail on ambiguous formats."""
    import pandas as pd
    if decimal_separator not in {'.', ','} or thousands_separator not in {'.', ',', '', ' '}:
        raise ValueError('Provide explicit decimal and thousands separators')
    if decimal_separator == thousands_separator:
        raise ValueError('Decimal and thousands separators must differ')
    seq = values if isinstance(values, pd.Series) else pd.Series(values)
    output = []
    for raw in seq:
        if raw is None or (not isinstance(raw, (str, bool)) and pd.isna(raw)) or raw == '':
            output.append(None)
            continue
        if isinstance(raw, bool):
            raise ValueError('Boolean is not a number')
        # Locale separators describe strings, not native spreadsheet numbers.
        # Applying them to str(1234.5) under a comma-decimal locale would yield 12345.
        if isinstance(raw, (Real, Decimal)):
            value = Decimal(str(raw))
            if not value.is_finite():
                raise ValueError('Non-finite numbers are not supported')
            output.append(value)
            continue
        text = str(raw).strip()
        signless = text[1:] if text.startswith(('-', '+')) else text
        decimal_parts = signless.split(decimal_separator)
        if len(decimal_parts) > 2 or (len(decimal_parts) == 2 and not decimal_parts[1].isdigit()):
            raise ValueError(f'Invalid number: {text!r}')
        integer = decimal_parts[0]
        if thousands_separator and thousands_separator in integer:
            groups = integer.split(thousands_separator)
            if not (groups[0].isdigit() and 1 <= len(groups[0]) <= 3 and all(g.isdigit() and len(g) == 3 for g in groups[1:])):
                raise ValueError(f'Ambiguous thousands separator: {text!r}')
        elif not integer.isdigit():
            raise ValueError(f'Invalid number: {text!r}')
        normalized = text.replace(thousands_separator, '') if thousands_separator else text
        normalized = normalized.replace(decimal_separator, '.')
        try:
            value = Decimal(normalized)
        except InvalidOperation as exc:
            raise ValueError(f'Invalid number: {text!r}') from exc
        if not value.is_finite():
            raise ValueError('Non-finite numbers are not supported')
        output.append(value)
    return pd.Series(output, index=seq.index, dtype=object)


def parse_dates(values, *, dayfirst, year=None):
    """Parse dates only with an explicit day/month convention and explicit missing year."""
    import pandas as pd
    if type(dayfirst) is not bool:
        raise ValueError('Specify dayfirst=True or False explicitly')
    seq = values if isinstance(values, pd.Series) else pd.Series(values)
    converted = []
    for raw in seq:
        if raw is None or (not isinstance(raw, str) and pd.isna(raw)) or raw == '':
            converted.append(pd.NaT)
            continue
        if isinstance(raw, (date, datetime)):
            converted.append(pd.Timestamp(raw))
            continue
        if not isinstance(raw, str):
            raise ValueError(f'Numeric date needs explicit conversion: {raw!r}')
        value = raw.strip()
        if not re.search(r'(?<!\d)\d{4}(?!\d)', value):
            if type(year) is not int or not 1000 <= year <= 9999:
                raise ValueError(f'Date has no four-digit year; supply year explicitly: {value!r}')
            value = f'{value}/{year}'
        # ISO year-first strings are unambiguous even when slash dates use day-first.
        iso = bool(re.match(r'^\d{4}-\d{2}-\d{2}(?:\D|$)', value))
        converted.append(pd.to_datetime(value, dayfirst=False if iso else dayfirst, errors='raise'))
    return pd.Series(converted, index=seq.index, dtype='datetime64[ns]')


def checked_join(left, right, *, on, how='left', relationship='many_to_one', require_match=True):
    """Join with explicit cardinality; reject unmatched keys unless requested."""
    import pandas as pd
    if how not in {'left', 'inner', 'outer'}:
        raise ValueError('Unsupported join type')
    if relationship not in {'one_to_one', 'one_to_many', 'many_to_one', 'many_to_many'}:
        raise ValueError('Specify a supported join relationship')
    keys = [on] if isinstance(on, str) else list(on)
    if not keys or left[keys].isna().any().any() or right[keys].isna().any().any():
        raise ValueError('Join keys must be present and non-null')
    if require_match and how == 'inner':
        # An inner join discards unmatched left rows before its indicator is visible.
        coverage = pd.merge(left[keys], right[keys].drop_duplicates(), on=keys, how='left', indicator=True)
        if (coverage['_merge'] != 'both').any():
            raise ValueError('Join contains unmatched keys; inspect source coverage')
    result = pd.merge(left, right, on=on, how=how, validate=relationship, indicator=True)
    if require_match and (result['_merge'] != 'both').any():
        raise ValueError('Join contains unmatched keys; inspect source coverage')
    return result.drop(columns='_merge')


def split_units(quantity, factor):
    """Split an integral base-unit quantity into whole large units and remainder."""
    if type(quantity) is not int or type(factor) is not int or quantity < 0 or factor <= 0:
        raise ValueError('quantity must be non-negative and factor a positive integer')
    return divmod(quantity, factor)
