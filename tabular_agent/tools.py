"""Tools given to the code agent. These execute inside its isolated worker."""
import csv
import os
from pathlib import Path
import sqlite3
from typing import Any

from smolagents import tool


def _source(name: str) -> Path:
    """Resolve a file alias or relative name within the worker's business mount.

    Args:
        name: One-based alias such as F1, or a path inside /data. Aliases use
            sorted supported data-file paths, matching list_files.

    Returns:
        Path to an existing nonsymlink file whose resolved location is in /data.

    Raises:
        ValueError: The alias is unknown, the file is absent, or path resolution
            escapes the business directory or selects a symlink.
    """
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
    """List available business data files and their aliases.

    Returns:
        Sorted list of dictionaries with alias (F1, F2, ...) and relative name
        for CSV, XLSX, SQLite, sqlite3 and db files under /data. No arguments
        are required; no file contents are read.
    """
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

    Returns:
        Metadata dictionary: sheets or tables when no selection is supplied;
        otherwise rows (up to eight for CSV/Excel, five for SQLite), plus the
        CSV delimiter, Excel sheet name or SQLite columns as applicable.

    Raises:
        ValueError: The file or selected sheet/table is unavailable or unsafe.
        csv.Error: A CSV delimiter cannot be inferred from the initial sample.

    Notes:
        Reads source data without modifying it. Library/file errors propagate.
    """
    path = _source(name)
    if path.suffix.lower() == '.xlsx':
        import openpyxl
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            if sheet is None:
                return {'sheets': book.sheetnames}
            if sheet not in book.sheetnames:
                raise ValueError(f'Unknown Excel sheet {sheet!r}; available sheets: {book.sheetnames}. '
                                 'Call inspect_table(name) to list sheets, then use sheet=<sheet name>.')
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
    """Read a complete CSV, Excel sheet or SQLite table as a pandas DataFrame. Inspect headers and formats first; values may be text even in Excel. A single Excel sheet is selected automatically.

    Args:
        name: File alias or relative filename.
        sheet: Excel sheet name.
        table: SQLite table name.
        header_row: Zero-based header row for CSV or Excel.
        delimiter: CSV delimiter; auto-detect if omitted.

    Returns:
        Complete pandas DataFrame. CSV/Excel use dtype=object to preserve
        source values; SQLite uses pandas' inferred types. Dates and formatted
        numbers may need explicit parsing. Does not modify source files.

    Raises:
        ValueError: A source/selection is invalid, or a workbook has multiple
            sheets without an explicit selection. Parser/file errors propagate.
    """
    import pandas as pd
    path = _source(name)
    if path.suffix.lower() == '.xlsx':
        if sheet is None:
            sheets = inspect_table.forward(name)['sheets']
            if len(sheets) != 1:
                raise ValueError(f'Choose an Excel sheet using sheet=<name>; available sheets: {sheets}')
            sheet = sheets[0]
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

    Returns:
        pandas DataFrame combining sheets in workbook order, with a fresh
        integer index and __sheet labels. Different columns align by name.

    Raises:
        ValueError: A sheet already contains the reserved __sheet column.
        KeyError: The selected source is not an Excel workbook.

    Notes:
        Reads every sheet without modifying the workbook; read errors propagate.
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

    Returns:
        pandas Series of Decimal values, preserving an input Series index.
        Missing or blank values become None; numeric scalars are converted
        through their string representation. Input values are not mutated.

    Raises:
        ValueError: Separators are invalid, or text has invalid numeric syntax
            or thousands grouping. Decimal conversion errors also propagate.
    """
    from decimal import Decimal
    import pandas as pd
    if decimal_separator not in ('.', ',') or thousands_separator not in ('', '.', ',', ' ') or decimal_separator == thousands_separator:
        raise ValueError('Invalid separators')
    def parse(value):
        """Parse one value using the enclosing call's explicit numeric separators.

        Args:
            value: Numeric scalar or formatted text; missing/blank values are allowed.

        Returns:
            Decimal for a valid number, or None for a missing/blank value.

        Raises:
            ValueError: Decimal syntax or thousands grouping does not match the
                selected separators. Decimal conversion errors also propagate.
        """
        if pd.isna(value) or str(value).strip() == '':
            return None
        if isinstance(value, (int, float, Decimal)):
            return Decimal(str(value))
        value = str(value).strip()
        integer, separator, fraction = value.partition(decimal_separator)
        integer = integer.lstrip('+-')
        if not integer or (separator and (not fraction.isdigit() or decimal_separator in fraction)):
            raise ValueError(f'Invalid numeric format: {value!r}. Check the actual separators; '
                             "for example '5,606,000' uses decimal_separator='.' and thousands_separator=','. ")
        if thousands_separator and thousands_separator in integer:
            parts = integer.split(thousands_separator)
            if not (1 <= len(parts[0]) <= 3 and parts[0].isdigit()
                    and all(len(part) == 3 and part.isdigit() for part in parts[1:])):
                raise ValueError(f'Invalid thousands grouping: {value!r}')
        elif not integer.isdigit():
            raise ValueError(f'Invalid numeric format: {value!r}. Check the actual separators; '
                             "for example '5,606,000' uses decimal_separator='.' and thousands_separator=','. ")
        if thousands_separator:
            value = value.replace(thousands_separator, '')
        return Decimal(value.replace(decimal_separator, '.'))
    return pd.Series(values).map(parse)


@tool
def parse_dates(values: Any, date_format: str) -> Any:
    """Parse a date column using an explicit format, preserving its index. Invalid nonempty values raise an error.

    Args:
        values: A pandas Series or list of dates.
        date_format: Python date format, for example %d/%m/%Y or %Y-%m-%d.

    Returns:
        pandas datetime Series with the input Series index preserved. Missing
        and blank values become NaT; the input is not modified.

    Raises:
        ValueError: A nonempty date cannot be parsed using date_format.
    """
    import pandas as pd
    series = pd.Series(values)
    return pd.to_datetime(series.replace(r'^\s*$', None, regex=True), format=date_format, errors='raise')


@tool
def profile_columns(frame: Any, columns: list[str]) -> dict:
    """Summarize selected columns of a complete DataFrame, including nulls and frequent values.

    Args:
        frame: DataFrame returned by read_table or read_workbook.
        columns: Actual column names to summarize.

    Returns:
        Dictionary keyed by column name, containing rows, nulls, distinct
        nonnull text representations, up to 12 frequent_values with counts,
        and a truncated flag. Displayed values are capped at 160 characters.

    Raises:
        ValueError: frame is not a DataFrame or a requested column is missing.
    """
    import pandas as pd
    if not isinstance(frame, pd.DataFrame):
        raise ValueError('profile_columns requires a DataFrame')
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise ValueError(f'Columns not found: {missing}; available columns: {list(frame.columns)}')
    result = {}
    for name in columns:
        column = frame[name]
        counts = column.dropna().astype(str).value_counts()
        result[name] = {'rows': len(column), 'nulls': int(column.isna().sum()),
                        'distinct': int(len(counts)),
                        'frequent_values': [{'value': str(value)[:160], 'count': int(count)}
                                            for value, count in counts.head(12).items()],
                        'truncated': len(counts) > 12}
    return result


@tool
def distinct_percentage(frame: Any, id_column: str, category_column: str,
                        selected_values: list[str], allow_absent: bool = False) -> float:
    """Percent of distinct IDs in a prefiltered DataFrame matching observed category values.

    Args:
        frame: Complete DataFrame already filtered to the requested period.
        id_column: Column identifying each entity, such as an order ID.
        category_column: Column containing the category for each entity.
        selected_values: Exact category values observed in the data to count.
        allow_absent: Set true only after confirming that an absent category legitimately means zero in this period.

    Returns:
        Unrounded percentage in units from 0 to 100: matching distinct IDs
        divided by all distinct IDs in frame, multiplied by 100. Does not
        filter dates, round the result or modify the input.

    Raises:
        ValueError: Input/columns/categories are invalid, IDs are missing,
            an entity has conflicting or unknown categories, a selected label
            is absent without permission, or the denominator is zero.
    """
    import pandas as pd
    if not isinstance(frame, pd.DataFrame):
        raise ValueError('distinct_percentage requires a DataFrame')
    missing = [column for column in (id_column, category_column) if column not in frame.columns]
    if missing:
        raise ValueError(f'Columns not found: {missing}; available columns: {list(frame.columns)}')
    if not selected_values or not all(isinstance(value, str) for value in selected_values):
        raise ValueError('Select one or more observed category strings')
    relevant = frame[[id_column, category_column]].copy()
    if relevant[id_column].isna().any():
        raise ValueError('Missing IDs: resolve them from the source rules before counting')
    relevant = relevant.drop_duplicates()
    observed = set(relevant[category_column].dropna().astype(str))
    absent = set(selected_values) - observed
    if absent and not allow_absent:
        raise ValueError(f'Selected category values not present: {sorted(absent)}; '
                         f'observed values: {sorted(observed)[:30]}. Verify the requested category and period.')
    inconsistent = relevant.dropna(subset=[category_column]).groupby(id_column)[category_column].nunique()
    if (inconsistent > 1).any():
        raise ValueError('Some IDs have conflicting category values; resolve them before counting')
    if relevant[id_column].nunique() != len(inconsistent):
        raise ValueError('Some IDs have no known category; resolve them before counting')
    total = relevant[id_column].nunique()
    if not total:
        raise ValueError('No IDs in period; check data coverage before answering')
    matching = relevant.loc[relevant[category_column].isin(selected_values), id_column].nunique()
    return 100.0 * matching / total


@tool
def date_window(as_of_date: str, period: str) -> dict:
    """Return inclusive dates for a named period relative to as_of_date.

    Args:
        as_of_date: Reference date in ISO format.
        period: One of today, yesterday, this_week, last_week, this_month, previous_month_comparable, or year_to_date.

    Returns:
        Dictionary with ISO start/end strings and inclusive=True. Weeks begin
        Monday; current periods end at as_of_date. previous_month_comparable
        ends at the matching day capped to the previous month's length.

    Raises:
        ValueError: as_of_date is invalid or period is unsupported.
    """
    from datetime import date, timedelta
    import calendar
    today = date.fromisoformat(as_of_date)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    previous_end = month_start - timedelta(days=1)
    windows = {
        'today': (today, today), 'yesterday': (today - timedelta(days=1), today - timedelta(days=1)),
        'this_week': (week_start, today), 'last_week': (week_start - timedelta(days=7), week_start - timedelta(days=1)),
        'this_month': (month_start, today), 'year_to_date': (today.replace(month=1, day=1), today),
        'previous_month_comparable': (previous_end.replace(day=1), previous_end.replace(day=min(today.day, calendar.monthrange(previous_end.year, previous_end.month)[1]))),
    }
    if period not in windows:
        raise ValueError('Unsupported period; use explicit boundaries from the current rulebook')
    start, end = windows[period]
    return {'start': start.isoformat(), 'end': end.isoformat(), 'inclusive': True}


@tool
def filter_categories(frame: Any, column: str, values: list[Any], allow_absent: bool = False) -> Any:
    """Filter by exact observed category labels and show row counts.

    Args:
        frame: Complete DataFrame to filter.
        column: Actual category column name.
        values: Exact values to include, using the same types as the column.
        allow_absent: True only after checking an absent category is valid.

    Returns:
        Independent DataFrame copy containing rows whose column values match
        values exactly, preserving their index. Prints before/after row counts.

    Raises:
        ValueError: values is empty, or requested labels are absent without
            allow_absent. Flags recognizable ISO/day-first date mismatches.
        KeyError: The selected column does not exist.
    """
    if not values:
        raise ValueError('Supply one or more exact values')
    mask = frame[column].isin(values)
    missing = [str(value) for value in values if not frame[column].isin([value]).any()]
    if missing and not allow_absent:
        import re
        sample = frame[column].dropna().head(30)
        if (len(values) == 1 and isinstance(values[0], str)
                and re.fullmatch(r'\d{4}-\d{2}-\d{2}', values[0]) and not sample.empty
                and all(isinstance(value, str) and re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}', value.strip())
                        for value in sample)):
            raise ValueError('Date values use day/month/year, not ISO text. Parse the column with '
                             "parse_dates(column, date_format='%d/%m/%Y') and compare to pd.Timestamp(as_of_date).")
        observed = sorted(set(frame[column].dropna().astype(str)))
        raise ValueError(f'Category labels not present: {missing}; observed: {observed[:30]}')
    result = frame.loc[mask].copy()
    print(f'Category filter {column}: {len(frame)} rows before, {len(result)} after')
    return result


@tool
def count_entities(frame: Any, id_column: str, allow_flagged_order_status: bool = False) -> int:
    """Count distinct nonmissing entity identifiers; flag cancelled/returned order statuses.

    Args:
        frame: DataFrame containing the selected entities.
        id_column: Column identifying the entity.
        allow_flagged_order_status: Set true only if the question or rulebook requires the flagged statuses.

    Returns:
        Integer count of distinct IDs, including zero for an empty selection.
        Prints row and entity counts without modifying frame.

    Raises:
        ValueError: IDs are missing/blank, or recognizable order identifiers
            coexist with cancelled/returned status labels without permission.
        KeyError: id_column is absent.

    Notes:
        Status detection is heuristic; it does not apply all rulebook policies.
    """
    import unicodedata
    def normalize(value):
        """Normalize a column name or status label for eligibility heuristics.

        Args:
            value: Label converted to text, casefolded and normalized with NFKD.

        Returns:
            Text with combining marks removed, crossed d mapped to d, punctuation
            replaced by spaces and whitespace collapsed.
        """
        value = unicodedata.normalize('NFKD', str(value).casefold().replace('đ', 'd'))
        return ' '.join(''.join(c if c.isalnum() else ' ' for c in value if not unicodedata.combining(c)).split())
    identifier = normalize(id_column)
    is_order_id = ('don hang' in identifier or 'order' in identifier) and any(
        part in identifier.split() for part in ('ma', 'id', 'code', 'number', 'so'))
    if is_order_id and not allow_flagged_order_status:
        for column in frame.columns:
            if not any(term in normalize(column) for term in ('trang thai', 'status')):
                continue
            labels = {str(value): normalize(value) for value in frame[column].dropna().unique()}
            excluded = [label for label, value in labels.items()
                        if any(term in value for term in ('huy', 'hoan hang', 'tra hang', 'cancel', 'return'))]
            if excluded:
                raise ValueError(f'Order statuses include {excluded}. Check the question and '
                                 'rulebook, filter eligible orders using observed labels before counting, or set '
                                 'allow_flagged_order_status=True only when the question or rulebook requires those statuses.')
    ids = frame[id_column]
    if ids.isna().any() or ids.astype(str).str.strip().eq('').any():
        raise ValueError('Resolve missing entity identifiers before counting')
    result = int(ids.nunique())
    print(f'Entity count {id_column}: {len(frame)} rows, {result} distinct')
    return result


@tool
def count_orders(frame: Any, id_column: str, date_column: str, status_column: str,
                 start_date: str, end_date: str, date_format: str,
                 exclude_cancelled_and_returned: bool) -> int:
    """Count distinct orders in an inclusive period with explicit eligibility; inspect unfamiliar status labels.

    Args:
        frame: Complete order line DataFrame.
        id_column: Column identifying each order.
        date_column: Column containing the order date.
        status_column: Column containing the order status.
        start_date: Inclusive ISO start date.
        end_date: Inclusive ISO end date.
        date_format: Explicit source date format, for example %d/%m/%Y.
        exclude_cancelled_and_returned: True when the applicable rule requires excluding cancelled and returned orders.

    Returns:
        Integer count of distinct orders after inclusive timestamp filtering.
        Rows with missing/blank IDs are dropped. When exclusions are enabled,
        any cancellation/return label in the selected period excludes that
        entire order ID. Prints observed statuses and exclusion/count details.

    Raises:
        ValueError: Eligibility is not a boolean, columns/dates are invalid,
            retained rows have missing dates, the period is outside observed
            coverage, or exclusions encounter missing/ambiguous status labels.

    Notes:
        Does not infer the correct eligibility policy or guarantee full period
        coverage. Boundaries are midnight timestamps; parse or normalize source
        timestamps appropriately. Does not mutate the input DataFrame.
    """
    from datetime import date
    import unicodedata
    import pandas as pd
    if type(exclude_cancelled_and_returned) is not bool:
        raise ValueError('Order eligibility must be an explicit boolean')
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start > end:
        raise ValueError('Start date must not be after end date')
    for column in (id_column, date_column, status_column):
        if column not in frame.columns:
            raise ValueError(f'Column {column!r} not found; available: {list(frame.columns)}')
    ids = frame[id_column]
    rows = frame.loc[ids.notna() & ids.astype(str).str.strip().ne('')].copy()
    raw_dates = rows[date_column].replace(r'^\s*$', None, regex=True)
    dates = pd.to_datetime(raw_dates, format=date_format, errors='raise')
    if dates.isna().any():
        raise ValueError('Some orders have missing dates; resolve them before counting')
    if dates.empty or pd.Timestamp(end) < dates.min().normalize() or pd.Timestamp(start) > dates.max().normalize():
        raise ValueError('Requested period is outside the observed order-date coverage; inspect the source before answering')
    rows = rows.loc[dates.between(pd.Timestamp(start), pd.Timestamp(end), inclusive='both')]
    if exclude_cancelled_and_returned and (rows[status_column].isna().any() or rows[status_column].astype(str).str.strip().eq('').any()):
        raise ValueError('Some selected orders have missing status; resolve eligibility before counting')
    def normalize(value):
        """Normalize an observed order status before cancellation/return matching.

        Args:
            value: Status converted to text, casefolded and normalized with NFKD.

        Returns:
            Text with combining marks removed, crossed d mapped to d, punctuation
            replaced by spaces and whitespace collapsed.
        """
        value = unicodedata.normalize('NFKD', str(value).casefold().replace('đ', 'd'))
        return ' '.join(''.join(c if c.isalnum() else ' ' for c in value if not unicodedata.combining(c)).split())
    statuses = rows[status_column].fillna('').map(normalize)
    if exclude_cancelled_and_returned and statuses.str.contains(r'\b(?:khong|chua|not|no) (?:huy|hoan hang|tra hang|cancel\w*|return\w*)\b', regex=True).any():
        raise ValueError('Negated cancellation or return status is ambiguous; resolve status labels explicitly')
    excluded = statuses.str.contains(r'\b(?:huy|hoan hang|tra hang|cancel\w*|return\w*)\b', regex=True)
    invalid_ids = set(rows.loc[excluded, id_column]) if exclude_cancelled_and_returned else set()
    result = int(rows.loc[~rows[id_column].isin(invalid_ids), id_column].nunique())
    print(f'Orders: period {start} to {end}, statuses={sorted(set(rows[status_column].astype(str)))[:30]}, '
          f'excluded order IDs={len(invalid_ids)}, distinct included={result}')
    return result


@tool
def checked_join(left: Any, right: Any, on: str, cardinality: str, how: str = 'left') -> Any:
    """Join two tables with explicit cardinality and unmatched row counts.

    Args:
        left: Left DataFrame.
        right: Right DataFrame.
        on: Shared join column.
        cardinality: One of one_to_one, one_to_many, many_to_one, or many_to_many.
        how: Join type: left, right, inner, or outer.

    Returns:
        New pandas DataFrame from merge, with cardinality validated. Prints
        input/output sizes and counts of rows with unmatched keys.

    Raises:
        ValueError: Join mode/cardinality is unsupported or a join key is null.
        KeyError: The join column is absent.
        pandas.errors.MergeError: Key multiplicities violate cardinality.
    """
    import pandas as pd
    if cardinality not in {'one_to_one', 'one_to_many', 'many_to_one', 'many_to_many'} or how not in {'left', 'right', 'inner', 'outer'}:
        raise ValueError('Invalid join cardinality or join type')
    if left[on].isna().any() or right[on].isna().any():
        raise ValueError('Resolve null join keys before joining')
    left_missing = int((~left[on].isin(right[on])).sum())
    right_missing = int((~right[on].isin(left[on])).sum())
    result = pd.merge(left, right, on=on, how=how, validate=cardinality)
    print(f'Join {on}: {len(left)} left, {len(right)} right, {len(result)} output; unmatched rows left={left_missing}, right={right_missing}')
    return result


@tool
def round_decimal(value: Any, places: int, mode: str = 'half_up') -> Any:
    """Round a numeric value using an explicit Decimal rounding mode.

    Args:
        value: Numeric value to round.
        places: Number of decimal places from 0 to 6.
        mode: Rounding mode: half_up or half_even.

    Returns:
        Decimal quantized to places decimal positions using the chosen mode.
        Convert it explicitly if a caller requires a JSON-native scalar.

    Raises:
        ValueError: places/mode is invalid or the number is nonfinite.
        decimal.InvalidOperation: value cannot be parsed or quantized under
            the active Decimal context.
    """
    from decimal import Decimal, ROUND_HALF_UP, ROUND_HALF_EVEN
    if type(places) is not int or not 0 <= places <= 6 or mode not in {'half_up', 'half_even'}:
        raise ValueError('Invalid rounding precision or mode')
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('Cannot round a non-finite value')
    return number.quantize(Decimal(1).scaleb(-places), rounding={'half_up': ROUND_HALF_UP, 'half_even': ROUND_HALF_EVEN}[mode])


AGENT_TOOLS = [list_files, inspect_table, read_table, read_workbook, parse_numbers, parse_dates,
               profile_columns, distinct_percentage, date_window, filter_categories, count_entities, count_orders,
               checked_join, round_decimal]
