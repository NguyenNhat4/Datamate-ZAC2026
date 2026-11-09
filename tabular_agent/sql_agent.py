"""Answer a question with read-only SQL over a prepared DuckDB bundle.

This is the consumer of ``tabular_agent.preparation`` bundles. The model sees the
published schema, the current rulebook, the business README, the question and
``as_of_date``; it never opens raw source files. Every query is parsed and
checked against the bundle's relation allowlist before it runs.
"""
import json
import re
import threading
import unicodedata
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from smolagents.models import ChatMessage, MessageRole

from .contracts import validate_answer
from .telemetry import question_span

MAX_ROWS = 30
MAX_RESULT_CHARS = 3000
MAX_LABELS = 12
QUERY_SECONDS = 15

INSTRUCTIONS = """You answer one Vietnamese business question by querying a prepared DuckDB database.
The tables are already cleaned and typed: dates are DATE/TIMESTAMP, money and quantities are DECIMAL.
Use only the relations and columns listed in the schema.

Reply with exactly one fenced block and nothing after it:
- To run a query, a ```sql block containing a single SELECT (WITH is allowed).
- To finish, a ```json block containing the typed answer, for example {"type": "number", "answer": 10}.

How to choose a relation:
- The semantic.* relations already apply the rulebook (which orders count, debt, stock, base units).
  Use the one whose description matches the question and follow the "Query patterns" given with the schema.
- Use clean.* tables only when no semantic relation covers the question.

Rules:
- Take dates from the "Dates" block; never compute "today" yourself. Each range there is inclusive.
- For a question about a day or a period, filter the date column to exactly that day or range.
- Filter text labels only with values shown in "Observed labels" or returned by a query.
- To match a product or customer named in the question, use name_like(column, 'text'): it ignores accents and
  letter case, for example WHERE name_like(product_name, 'trà vải'). Never compare names with = or LIKE.
- Questions are often typed without accents or with chat abbreviations: hnay = hôm nay, hqua = hôm qua,
  dthu = doanh thu, bn = bao nhiêu, sp = sản phẩm, dc/đc = được, ko/k = không, kh = khách hàng, sl = số lượng.
- If the asked period lies entirely outside "Data coverage", answer
  {"type": "unanswerable", "reason": "...", "period": ["YYYY-MM-DD", "YYYY-MM-DD"]} with the asked period.
  Unanswerable is accepted only when that period is outside the coverage.
- If a query inside the data coverage returns no rows or NULL, check the filter values and retry; do not
  conclude 0 or unanswerable from a wrong filter.
- If a needed column is withheld, or you are unsure, answer {"type": "abstain", "reason": "..."}.
- Answer types: number, percent, string, date (YYYY-MM-DD or YYYY-MM), boolean, list, table
  ({"type": "table", "answer": {"columns": [...], "rows": [[...]]}}), unanswerable, abstain.
  Money is a whole number of VND. Several numbers asked together are a list in the order asked,
  for example {"type": "list", "answer": [13, 2]}. A table has one named object per row.
- Give the final answer only from a query result you have seen, copying the value exactly.
"""

PATTERNS = (
    ('semantic.eligible_sales_lines', 'Revenue (doanh thu, tiền bán, bán được bao nhiêu tiền) in a period',
     "SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.eligible_orders', 'Number of orders (số đơn) in a period',
     "SELECT COUNT(*) FROM semantic.eligible_orders WHERE order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.eligible_orders', 'Average revenue per order (doanh thu bình quân mỗi đơn)',
     "SELECT ROUND(SUM(revenue) / COUNT(*)) FROM semantic.eligible_orders WHERE order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.orders', 'Cancelled orders (đơn huỷ) in a period; all orders regardless of state use no state filter',
     "SELECT COUNT(*) FROM semantic.orders WHERE order_state = 'cancelled' AND order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.product_quantities', 'Units sold of a base unit (bán được bao nhiêu <đơn vị>), all products with that base unit',
     "SELECT SUM(base_quantity) FROM semantic.product_quantities WHERE base_unit = 'gói' AND order_date = DATE '2025-06-30'"),
    ('semantic.debt_as_of', 'Total customer debt at the end of a date (còn nợ tính đến hết ngày X)',
     "SELECT SUM(outstanding) FROM semantic.debt_as_of(DATE '2025-06-30')"),
    ('semantic.debt_repayments', 'Debt repaid / collected in a period (trả nợ, thu nợ, thu hồi công nợ)',
     "SELECT SUM(amount) FROM semantic.debt_repayments WHERE repayment_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.stock_snapshot', 'Stock of a product in large units (còn mấy hộp chẵn, mấy cái lẻ), answer as list [whole, loose]',
     "SELECT whole_large_units, loose_base_units FROM semantic.stock_snapshot WHERE name_like(product_name, 'tên hàng')"),
)


_SYSTEM_DATE = re.compile(r'\b(?:current_date|current_timestamp|current_time|localtimestamp|localtime|'
                          r'get_current_timestamp|get_current_time|transaction_timestamp|now\s*\(|today\s*\()', re.I)
_DATE_LITERAL = re.compile(r"'(\d{4}-\d{2}-\d{2})(?:[ T][\d:.]+)?'")


class QueryRejected(ValueError):
    """A generated query is not a single allowlisted SELECT."""


def find_bundles(root):
    """Map business directory names to prepared bundle paths.

    Args:
        root: Directory written by the ``prepare`` command, containing
            ``run-*.json`` reports and one directory per bundle.

    Returns:
        Dictionary from business name to bundle Path. Later reports override
        earlier ones. Used for data routing only.

    Raises:
        ValueError: The directory holds no usable run report.
    """
    root = Path(root)
    bundles = {}
    for report in sorted(root.glob('run-*.json')):
        for item in json.loads(report.read_text(encoding='utf-8')).get('businesses', []):
            path = root / Path(item['path']).name
            if item.get('business') and (path / 'data.duckdb').is_file():
                bundles[item['business']] = path
    if not bundles:
        raise ValueError(f'No prepared bundles found under {root}')
    return bundles


def _walk(node, tables, ctes, functions):
    if isinstance(node, dict):
        kind = node.get('type')
        if kind == 'BASE_TABLE':
            tables.append((node.get('catalog_name') or '', node.get('schema_name') or '', node.get('table_name') or ''))
        elif kind == 'TABLE_FUNCTION':
            call = node.get('function') or {}
            functions.append(((call.get('schema') or '').lower(), (call.get('function_name') or '').lower()))
        for entry in (node.get('cte_map') or {}).get('map', []):
            ctes.add(entry['key'].lower())
        for value in node.values():
            _walk(value, tables, ctes, functions)
    elif isinstance(node, list):
        for value in node:
            _walk(value, tables, ctes, functions)


def check_query(connection, sql, allowed):
    """Reject anything except one SELECT over allowlisted relations.

    Args:
        connection: DuckDB connection used only to parse the statement.
        sql: Generated SQL text.
        allowed: Set of lower-case ``schema.table`` names the agent may read.

    Returns:
        The SQL text with surrounding whitespace and a trailing semicolon removed.

    Raises:
        QueryRejected: The text is not exactly one SELECT, uses a table
            function, or references a relation outside the allowlist.
    """
    sql = sql.strip().rstrip(';').strip()
    if not sql:
        raise QueryRejected('Empty query')
    if _SYSTEM_DATE.search(re.sub(r"'(?:[^']|'')*'", "''", sql)):
        raise QueryRejected('The system date is not allowed; use the explicit dates from the Dates block')
    parsed = json.loads(connection.execute('SELECT json_serialize_sql(?)', [sql]).fetchone()[0])
    if parsed.get('error'):
        raise QueryRejected(parsed.get('error_message') or 'Only SELECT statements are allowed')
    if len(parsed.get('statements', [])) != 1:
        raise QueryRejected('Send exactly one SELECT statement')
    tables, ctes, functions = [], set(), []
    _walk(parsed, tables, ctes, functions)
    basenames = {name.split('.', 1)[1] for name in allowed}
    for schema, name in functions:
        if f'{schema}.{name}' not in allowed and not (not schema and name in basenames):
            raise QueryRejected('Table functions are not allowed; query the listed relations')
    for catalog, schema, table in tables:
        schema, table = schema.lower(), table.lower()
        if catalog:
            raise QueryRejected('Catalog-qualified names are not allowed')
        if schema:
            if f'{schema}.{table}' not in allowed:
                raise QueryRejected(f'Relation {schema}.{table} is not in the schema')
        elif table not in ctes and table not in basenames:
            raise QueryRejected(f'Relation {table} is not in the schema')
    return sql


def _cell(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value) if value is not None else 'NULL'
    return text if len(text) <= 80 else text[:77] + '...'


def run_query(connection, sql, allowed):
    """Run one checked query and return a bounded text result.

    Args:
        connection: Read-only DuckDB connection to the bundle.
        sql: Generated SQL text.
        allowed: Allowlisted ``schema.table`` names.

    Returns:
        Text with the column names, up to MAX_ROWS rows and the total row
        count, truncated to MAX_RESULT_CHARS.

    Raises:
        QueryRejected: The query fails the allowlist check.
        duckdb.Error: The query is invalid or was interrupted at the time limit.
    """
    sql = check_query(connection, sql, allowed)
    timer = threading.Timer(QUERY_SECONDS, connection.interrupt)
    timer.start()
    try:
        cursor = connection.execute(sql)
        columns = [item[0] for item in cursor.description]
        rows = cursor.fetchmany(MAX_ROWS + 1)
        more = len(rows) > MAX_ROWS
        total = MAX_ROWS + len(cursor.fetchall()) + 1 if more else len(rows)
    finally:
        timer.cancel()
    lines = [' | '.join(columns)] + [' | '.join(_cell(value) for value in row) for row in rows[:MAX_ROWS]]
    note = f'{total} row(s)' + (f'; first {MAX_ROWS} shown' if more else '')
    if total == 0:
        note += ('. No rows matched: check the filter values and dates before concluding anything. '
                 "Match names with name_like(column, 'text').")
    return ('\n'.join(lines) + '\n' + note)[:MAX_RESULT_CHARS]


def _labels(connection, relation, column):
    name = '"' + column.replace('"', '""') + '"'
    rows = connection.execute(f'SELECT DISTINCT {name} FROM {relation} WHERE {name} IS NOT NULL LIMIT {MAX_LABELS + 1}').fetchall()
    if 0 < len(rows) <= MAX_LABELS and all(len(str(row[0])) <= 40 for row in rows):
        return sorted(str(row[0]) for row in rows)
    return None


def describe_schema(connection, schema):
    """Render the published schema as compact prompt text.

    Args:
        connection: Read-only DuckDB connection to the bundle.
        schema: Parsed ``schema.json`` dictionary.

    Returns:
        A (text, allowed) pair: prompt text listing rulebook-defined semantic
        relations first, then cleaned source tables, with query patterns for
        the relations this bundle has and the observed sales date range; and
        the set of lower-case allowlisted relation and function names. Label
        columns with at most MAX_LABELS distinct values list those values;
        identifier, name and free-text columns never do.
    """
    allowed = set()
    quoted = lambda name: '.'.join('"' + part.replace('"', '""') + '"' for part in name.split('.'))

    def render(relation):
        name = relation['name']
        allowed.add(name.lower())
        if relation.get('function'):
            lines = [f"{relation.get('signature', name)}  (table function; one row per {relation.get('grain', 'row')})"]
        else:
            lines = [f"{name}  (one row per {relation.get('grain', 'unknown')})"]
        if relation.get('meaning'):
            lines.append('  ' + relation['meaning'])
        for column in relation.get('columns', []):
            meaning = (column.get('meaning') or column.get('source_alias') or '').replace('\n', ' ')[:140]
            line = f"  - {column['name']} {column['type']}: {meaning}"
            role = column.get('role') or ''
            if (not relation.get('function') and column['type'] == 'VARCHAR'
                    and not any(word in role for word in ('name', 'id', 'note', 'phone', 'address'))):
                labels = _labels(connection, quoted(name), column['name'])
                if labels:
                    line += ' | Observed labels: ' + json.dumps(labels, ensure_ascii=False)
            lines.append(line)
        withheld = relation.get('blocked_columns') or []
        if withheld:
            lines.append('  Withheld (failed cleaning, cannot be queried): ' + ', '.join(str(item) for item in withheld))
        return lines + ['']

    relations = schema.get('relations', [])
    semantic = [r for r in relations if r['name'].startswith('semantic.')]
    clean = [r for r in relations if not r['name'].startswith('semantic.')]
    lines = []
    if semantic:
        lines += ['## Rulebook-ready relations (use these first)', '']
        for relation in semantic:
            lines += render(relation)
    lines += ['## Cleaned source tables', '']
    for relation in clean:
        lines += render(relation)
    patterns = [f'- {title}:\n  {sql}' for name, title, sql in PATTERNS if name in allowed]
    if patterns:
        lines += ['## Query patterns (replace dates, units and names)', *patterns, '']
    coverage = sales_coverage(connection, schema)
    if coverage:
        lines.append(f'## Data coverage\nSales records run from {coverage[0]} to {coverage[1]}. '
                     'A period entirely before or after this range has no data: unanswerable, not 0.')
    else:
        observed = sorted({f"{value.get('role')}: {value.get('observed_min')} to {value.get('observed_max')}"
                           for value in schema.get('temporal_coverage', {}).values()
                           if isinstance(value, dict) and value.get('observed_min')})
        if observed:
            lines.append('## Data coverage\nObserved date ranges: ' + '; '.join(observed))
    return '\n'.join(lines), allowed


def sales_coverage(connection, schema):
    """Return the first and last sales date published in the bundle, or None when no sales relation has one."""
    names = {relation['name']: relation for relation in schema.get('relations', [])}
    for name in ('semantic.orders', 'semantic.eligible_sales_lines'):
        relation = names.get(name)
        if relation and any(column['name'] == 'order_date' for column in relation['columns']):
            first, last = connection.execute(f'SELECT MIN(order_date), MAX(order_date) FROM {name}').fetchone()
            if first and last:
                return first, last
    return None


def asked_period(answer, last_sql):
    """Read the period an unanswerable answer refers to: its own "period", else the date literals of the last query."""
    period = answer.get('period') if isinstance(answer, dict) else None
    values = period if isinstance(period, list) and len(period) == 2 else _DATE_LITERAL.findall(last_sql or '')
    try:
        dates = sorted(date.fromisoformat(str(value)[:10]) for value in values)
    except ValueError:
        return None
    return (dates[0], dates[-1]) if dates else None


def outside_coverage(period, coverage):
    """True when a period lies entirely before or entirely after the data's sales dates."""
    if not period or not coverage:
        return False
    return period[1] < coverage[0] or period[0] > coverage[1]


def _fold(text):
    text = unicodedata.normalize('NFD', str(text).replace('đ', 'd').replace('Đ', 'D'))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in text if not unicodedata.combining(c)).lower()).split())


_PHRASES = (
    ('today', ('hom nay', 'hnay', 'bua nay', 'hom ni')),
    ('yesterday', ('hom qua', 'hqua', 'bua qua', 'hom wa', 'bua wa')),
    ('this week', ('tuan nay',)),
    ('last week', ('tuan truoc', 'tuan roi', 'tuan vua roi', 'tuan vua qua', 'tuan qua')),
    ('this month', ('thang nay',)),
    ('last month', ('thang truoc', 'thang roi', 'thang vua roi', 'thang vua qua')),
    ('this quarter', ('quy nay',)),
    ('this year', ('tu dau nam', 'nam nay')),
)


def date_phrases(question, as_of_date):
    """Resolve the relative date phrases that occur in a question, by the rulebook's time rules.

    Returns one line per phrase found, each giving the phrase's meaning as an
    explicit date or inclusive range for this reference date. Phrases are
    matched on accent-folded words, so unaccented typing is recognised.
    """
    today = date.fromisoformat(as_of_date)
    monday = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    previous_end = month_start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    same_day = previous_start.replace(day=min(today.day, previous_end.day))
    quarter_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    span = lambda first, last: f'{first.isoformat()} to {last.isoformat()}'
    meaning = {
        'today': f'the single day {today.isoformat()}',
        'yesterday': f'the single day {(today - timedelta(days=1)).isoformat()}',
        'this week': span(monday, today),
        'last week': span(monday - timedelta(days=7), monday - timedelta(days=1)),
        'this month': span(month_start, today),
        'last month': f'{span(previous_start, previous_end)} (whole month); when compared with this month use '
                      f'{span(previous_start, same_day)}',
        'this quarter': span(quarter_start, today),
        'this year': span(date(today.year, 1, 1), today),
    }
    folded = f' {_fold(question)} '
    lines = []
    for name, phrases in _PHRASES:
        found = next((phrase for phrase in phrases if f' {phrase} ' in folded), None)
        if found:
            lines.append(f'- "{found}" means {name}: {meaning[name]}')
    for count, _ in re.findall(r' (\d{1,3}) ngay (gan nhat|gan day|vua qua|qua) ', folded):
        days = int(count)
        if days:
            lines.append(f'- "{count} ngay gan nhat" means the last {days} days: {span(today - timedelta(days=days - 1), today)}')
    return lines


def date_block(as_of_date):
    """List the rulebook's relative periods as explicit inclusive date ranges for one reference date."""
    today = date.fromisoformat(as_of_date)
    monday = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    previous_end = month_start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    same_day = previous_start.replace(day=min(today.day, previous_end.day))
    quarter_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    span = lambda first, last: f'{first.isoformat()} to {last.isoformat()}'
    return '\n'.join([
        f'today (hôm nay, hnay, bữa nay): {today.isoformat()}',
        f'yesterday (hôm qua, hqua, bữa qua): {(today - timedelta(days=1)).isoformat()}',
        f'this week (tuần này): {span(monday, today)}',
        f'last week (tuần trước, tuần rồi): {span(monday - timedelta(days=7), monday - timedelta(days=1))}',
        f'this month (tháng này): {span(month_start, today)}',
        f'last month, whole (tháng trước): {span(previous_start, previous_end)}',
        f'last month, same number of days (so với tháng trước): {span(previous_start, same_day)}',
        f'this quarter to date (quý này): {span(quarter_start, today)}',
        f'this year to date (từ đầu năm): {span(date(today.year, 1, 1), today)}',
        f'last 7 days (7 ngày gần nhất): {span(today - timedelta(days=6), today)}',
        f'last 30 days (30 ngày gần nhất): {span(today - timedelta(days=29), today)}',
        'A month or quarter named without a year is this year\'s if it does not start after today, otherwise last year\'s.',
        'Records dated after today have not happened yet.',
    ])


_NUMERIC_TEXT = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?')


def _number(value):
    """Turn numeric text or an integral float into a JSON number; leave codes and other text unchanged."""
    if isinstance(value, str) and _NUMERIC_TEXT.fullmatch(value.strip()):
        value = float(value) if '.' in value else int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def normalize_answer(answer):
    """Apply the answer format rules that do not depend on the question.

    Numbers written as text become JSON numbers (text with a leading zero is a
    code and stays text), integral floats become integers, and percentages are
    rounded half-up to two decimals. A table body written beside "type" is
    moved under "answer"; a one-row table of numbers only becomes a list, since
    a table row needs a named object. Other values are returned unchanged.
    """
    if not isinstance(answer, dict):
        return answer
    if answer.get('type') == 'table' and 'answer' not in answer and 'columns' in answer and 'rows' in answer:
        # The table body was written beside "type" instead of under "answer".
        answer = {**{k: v for k, v in answer.items() if k not in ('columns', 'rows')},
                  'answer': {'columns': answer['columns'], 'rows': answer['rows']}}
    if 'answer' not in answer:
        return answer
    kind, value = answer.get('type'), answer['answer']
    if kind in ('number', 'percent'):
        value = _number(value)
        if kind == 'percent' and isinstance(value, float):
            value = float(Decimal(repr(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    elif kind == 'list' and isinstance(value, list):
        value = [_number(item) for item in value]
    elif kind == 'table' and isinstance(value, dict) and isinstance(value.get('rows'), list):
        rows = [[_number(cell) for cell in row] if isinstance(row, list) else row for row in value['rows']]
        value = {**value, 'rows': rows}
        numeric = lambda cell: type(cell) in (int, float)
        if len(rows) == 1 and isinstance(rows[0], list) and len(rows[0]) > 1 and all(numeric(cell) for cell in rows[0]):
            # The rulebook's table has one named object per row; several numbers asked together are a list.
            return {**answer, 'type': 'list', 'answer': rows[0]}
    return {**answer, 'answer': value}


def _block(text):
    blocks = re.findall(r'```(\w*)\s*\n(.*?)```', text or '', flags=re.S)
    if blocks:
        kind, body = blocks[-1]
        return kind.lower(), body.strip()
    stripped = (text or '').strip()
    if stripped.startswith('{') and stripped.endswith('}'):
        return 'json', stripped
    return None, stripped


def answer_question_sql(model, question, business, bundle, rulebook, *, max_steps=6, emit=None):
    """Solve one question with read-only SQL over a prepared bundle.

    Args:
        model: Configured smolagents model.
        question: Mapping with question text and ISO as_of_date.
        business: Business directory; only its README.md is read.
        bundle: Prepared bundle directory containing data.duckdb and schema.json.
        rulebook: Current split's rules as text.
        max_steps: Maximum model calls.
        emit: Optional callback receiving sql, observation and memory events.

    Returns:
        Validated typed answer dictionary. Exhausting the step budget, or
        answering before any successful query, yields an abstention.

    Notes:
        Reference answers are never read. The connection is read-only with
        external file access disabled and configuration locked.
    """
    import duckdb

    emit = emit or (lambda event: None)
    bundle = Path(bundle)
    schema = json.loads((bundle / 'schema.json').read_text(encoding='utf-8'))
    readme = Path(business) / 'README.md'
    connection = duckdb.connect(str(bundle / 'data.duckdb'), read_only=True,
                                config={'enable_external_access': False, 'autoinstall_known_extensions': False,
                                        'autoload_known_extensions': False, 'memory_limit': '1GB', 'threads': 2})
    try:
        connection.execute("SET search_path='semantic,clean'")
        connection.execute("CREATE TEMP MACRO name_like(col, txt) AS strip_accents(lower(CAST(col AS VARCHAR))) "
                           "LIKE '%' || strip_accents(lower(trim(txt))) || '%'")
        connection.execute('SET lock_configuration=true')
        schema_text, allowed = describe_schema(connection, schema)
        coverage = sales_coverage(connection, schema)
        context = '\n\n'.join([
            '# Rulebook\n' + rulebook,
            '# Business README\n' + (readme.read_text(encoding='utf-8') if readme.exists() else ''),
            '# Schema\n' + schema_text,
            '# Dates\n' + date_block(question['as_of_date']),
            '# Question\n' + question['question'],
        ])
        phrases = date_phrases(question['question'], question['as_of_date'])
        if phrases:
            context += '\n\nDate phrases in this question (use exactly these dates):\n' + '\n'.join(phrases)
        emit({'stage': 'schema', 'text': schema_text})
        text = lambda role, value: ChatMessage(role=role, content=[{'type': 'text', 'text': value}])
        messages = [text(MessageRole.SYSTEM, INSTRUCTIONS), text(MessageRole.USER, context)]
        succeeded = False
        last_sql = ''
        unverified = 0
        with question_span(question):
            for step in range(1, max_steps + 1):
                response = model.generate(messages)
                content = response.content if isinstance(response.content, str) else str(response.content)
                usage = response.token_usage
                emit({'stage': 'memory_step', 'step': str(step), 'model_output': content, 'error': None,
                      'token_usage': {'input_tokens': getattr(usage, 'input_tokens', 0),
                                      'output_tokens': getattr(usage, 'output_tokens', 0)} if usage else {}})
                messages.append(text(MessageRole.ASSISTANT, content))
                kind, body = _block(content)
                if kind == 'json':
                    try:
                        raw_answer = json.loads(body)
                        answer = validate_answer(normalize_answer(raw_answer))
                    except (ValueError, TypeError) as error:
                        feedback = (f'Invalid final answer: {error}. Valid forms: {{"type": "number", "answer": 10}}; '
                                    'several numbers in the order asked: {"type": "list", "answer": [13, 2]}; '
                                    'one named object per row: {"type": "table", "answer": {"columns": ["name", "value"], '
                                    '"rows": [["A", 1]]}}. Send one corrected ```json block.')
                    else:
                        if answer['type'] == 'unanswerable':
                            # "No data" is accepted only when code can confirm it; an empty result from a
                            # wrong filter must not become a wrong answer.
                            if outside_coverage(asked_period(raw_answer, last_sql), coverage):
                                return answer
                            unverified += 1
                            if unverified > 1:
                                return {'type': 'abstain', 'reason': 'Unanswerable could not be verified against the data coverage.'}
                            span = f'{coverage[0]} to {coverage[1]}' if coverage else 'unknown'
                            feedback = ('Not accepted. Unanswerable is confirmed only for a period entirely outside the data '
                                        f'coverage ({span}); state it as "period": ["YYYY-MM-DD", "YYYY-MM-DD"] in the answer. '
                                        'If the period is inside the coverage, the data exists and your filters are wrong: '
                                        "match names with name_like(column, 'text'), take dates from the Dates block, and use "
                                        'only listed columns and labels. Fix the query, or answer {"type": "abstain"}.')
                        elif succeeded or answer['type'] == 'abstain':
                            return answer
                        else:
                            feedback = 'Run a query first; answer only from a query result.'
                elif kind in ('sql', '') and body:
                    emit({'stage': 'sql', 'sql': body})
                    last_sql = body
                    try:
                        feedback = 'Result:\n' + run_query(connection, body, allowed)
                        succeeded = True
                    except QueryRejected as error:
                        feedback = f'Query rejected: {error}'
                    except duckdb.Error as error:
                        feedback = f'Query error: {str(error)[:600]}'
                else:
                    feedback = 'Reply with one ```sql block or one ```json block.'
                emit({'stage': 'observation', 'text': feedback})
                if step == max_steps - 1:
                    feedback += '\nThis is your last step: send the final ```json answer now, or abstain.'
                messages.append(text(MessageRole.USER, feedback))
        return {'type': 'abstain', 'reason': 'Step limit reached without a validated answer.'}
    finally:
        connection.close()
