"""Tools given to the code agent. These execute inside its isolated worker."""
import csv
import os
from pathlib import Path
import sqlite3
from typing import Any

from smolagents import tool


def _source(name: str) -> Path:
    root = Path('/data')
    aliases = sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in {'.csv', '.xlsx', '.sqlite', '.sqlite3', '.db'})
    if name.startswith('F') and name[1:].isdigit():
        index = int(name[1:]) - 1
        if index < 0 or index >= len(aliases):
            raise ValueError('Unknown file alias')
        path = aliases[index]
    else:
        path = root / name
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file() or path.is_symlink():
        raise ValueError('File is not available in the business directory')
    return path


@tool
def list_files() -> list:
    """List available business data files and their aliases."""
    root = Path('/data')
    files = sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in {'.csv', '.xlsx', '.sqlite', '.sqlite3', '.db'})
    return [{'alias': f'F{i}', 'name': str(p.relative_to(root))} for i, p in enumerate(files, 1)]


@tool
def inspect_table(name: str, sheet: str | None = None, table: str | None = None) -> dict:
    """Inspect a file, its sheets or tables, and a small sample. A preview is not a full read.

    Args:
        name: File alias such as F1 or relative filename.
        sheet: Excel sheet name; omit to list sheets.
        table: SQLite table name; omit to list tables.
    """
    path = _source(name)
    if path.suffix.lower() == '.xlsx':
        import openpyxl
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            if sheet is None:
                return {'sheets': book.sheetnames}
            if sheet not in book.sheetnames:
                raise ValueError(f'Choose sheet from {book.sheetnames}')
            from itertools import islice
            return {'sheet': sheet, 'rows': list(islice(book[sheet].values, 8))}
        finally:
            book.close()
    if path.suffix.lower() == '.csv':
        from itertools import islice
        with path.open(encoding='utf-8-sig', newline='') as stream:
            sample = stream.read(8192)
            stream.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
            return {'delimiter': dialect.delimiter, 'rows': list(islice(csv.reader(stream, dialect), 8))}
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as database:
        names = [row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if table is None:
            return {'tables': names}
        if table not in names:
            raise ValueError(f'Choose table from {names}')
        quoted = '"' + table.replace('"', '""') + '"'
        cursor = database.execute(f'SELECT * FROM {quoted} LIMIT 5')
        return {'columns': [item[0] for item in cursor.description], 'rows': cursor.fetchall()}


@tool
def read_table(name: str, sheet: str | None = None, table: str | None = None,
               header_row: int = 0, delimiter: str | None = None) -> Any:
    """Read a complete CSV, Excel sheet or SQLite table as a pandas DataFrame. Inspect headers and formats first; CSV values remain text.

    Args:
        name: File alias or relative filename.
        sheet: Excel sheet name.
        table: SQLite table name.
        header_row: Zero-based header row for CSV or Excel.
        delimiter: CSV delimiter; auto-detect if omitted.
    """
    import pandas as pd
    path = _source(name)
    if path.suffix.lower() == '.xlsx':
        if sheet is None:
            raise ValueError('Inspect the workbook and specify a sheet')
        return pd.read_excel(path, sheet_name=sheet, header=header_row, dtype=object)
    if path.suffix.lower() == '.csv':
        if delimiter is None:
            delimiter = inspect_table.forward(name)['delimiter']
        return pd.read_csv(path, sep=delimiter, header=header_row, encoding='utf-8-sig', dtype=object)
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as database:
        names = [row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if table not in names:
            raise ValueError(f'Choose table from {names}')
        quoted = '"' + table.replace('"', '""') + '"'
        return pd.read_sql_query(f'SELECT * FROM {quoted}', database)


@tool
def read_workbook(name: str, header_row: int = 0) -> Any:
    """Read all sheets in a workbook into one DataFrame, with a __sheet provenance column.

    Args:
        name: XLSX file alias or relative filename.
        header_row: Zero-based header row.
    """
    import pandas as pd
    sheets = inspect_table.forward(name)['sheets']
    frames = []
    for sheet in sheets:
        frame = read_table.forward(name, sheet=sheet, header_row=header_row)
        if '__sheet' in frame.columns:
            raise ValueError('Reserved column __sheet already exists')
        frame['__sheet'] = sheet
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


@tool
def parse_numbers(values: Any, decimal_separator: str, thousands_separator: str) -> Any:
    """Parse a pandas Series or list into Decimal numbers; specify the source format explicitly.

    Args:
        values: Values to parse.
        decimal_separator: Decimal character, for example comma or period.
        thousands_separator: Thousands character, or empty string if absent.
    """
    from decimal import Decimal
    import pandas as pd
    if decimal_separator not in ('.', ',') or thousands_separator not in ('', '.', ',', ' ') or decimal_separator == thousands_separator:
        raise ValueError('Invalid separators')
    def parse(value):
        if pd.isna(value) or str(value).strip() == '':
            return None
        if isinstance(value, (int, float, Decimal)):
            return Decimal(str(value))
        value = str(value).strip()
        integer, separator, fraction = value.partition(decimal_separator)
        integer = integer.lstrip('+-')
        if not integer or (separator and (not fraction.isdigit() or decimal_separator in fraction)):
            raise ValueError(f'Invalid numeric format: {value!r}')
        if thousands_separator and thousands_separator in integer:
            parts = integer.split(thousands_separator)
            if not (1 <= len(parts[0]) <= 3 and parts[0].isdigit()
                    and all(len(part) == 3 and part.isdigit() for part in parts[1:])):
                raise ValueError(f'Invalid thousands grouping: {value!r}')
        elif not integer.isdigit():
            raise ValueError(f'Invalid numeric format: {value!r}')
        if thousands_separator:
            value = value.replace(thousands_separator, '')
        return Decimal(value.replace(decimal_separator, '.'))
    return pd.Series(values).map(parse)


AGENT_TOOLS = [list_files, inspect_table, read_table, read_workbook, parse_numbers]
