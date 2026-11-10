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
  For a product group use name_is(product_group, 'text'): the exact group name, ignoring accents and letter case;
  groups with similar names (trà, trà sữa) are different groups.
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

# (relation, columns the pattern needs, what it answers, query[, question kind]). A pattern is shown only when the
# bundle has the columns; one with a question kind is shown only for that kind of question (see _PATTERN_KINDS).
PATTERNS = (
    ('semantic.eligible_sales_lines', ('line_amount', 'order_date'),
     'Revenue (doanh thu, tiền bán, bán được bao nhiêu tiền) in a period',
     "SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.eligible_sales_lines', ('line_amount', 'order_date'),
     'Day with the highest revenue in a period, answer type date',
     "SELECT order_date FROM semantic.eligible_sales_lines WHERE order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30' "
     "GROUP BY order_date ORDER BY SUM(line_amount) DESC, order_date LIMIT 1"),
    ('semantic.eligible_sales_lines', ('line_amount', 'order_date'),
     'Number of days whose revenue reaches an amount (có mấy ngày doanh thu đạt X trở lên): compare daily totals, '
     'never single lines; use > for "trên / vượt", < for "dưới"',
     "SELECT COUNT(*) FROM (SELECT order_date FROM semantic.eligible_sales_lines WHERE order_date BETWEEN "
     "DATE '2025-06-01' AND DATE '2025-06-30' GROUP BY order_date HAVING SUM(line_amount) >= 49000000)", 'days'),
    ('semantic.period_comparison', ('revenue_change_percent',),
     'Percent change of revenue against an earlier period (so với tháng trước / cùng kỳ, tăng hay giảm bao nhiêu %), '
     'answer type percent; asked period first, earlier period second, both from the Date phrases. '
     'Use revenue_difference for a difference in money',
     "SELECT revenue_change_percent FROM semantic.period_comparison(DATE '2025-06-01', DATE '2025-06-30', DATE '2025-05-01', DATE '2025-05-31')",
     'comparison'),
    ('semantic.period_comparison', ('orders_change_percent',),
     'Percent change of the order count against an earlier period (số đơn tăng hay giảm bao nhiêu %), answer type percent',
     "SELECT orders_change_percent FROM semantic.period_comparison(DATE '2025-06-01', DATE '2025-06-30', DATE '2025-05-01', DATE '2025-05-31')",
     'comparison'),
    ('semantic.group_revenue_share', ('share_percent',),
     'Share of revenue of one product group (nhóm hàng ... chiếm bao nhiêu %, tỉ trọng nhóm), answer type percent',
     "SELECT share_percent FROM semantic.group_revenue_share(DATE '2025-06-01', DATE '2025-06-30') WHERE name_is(product_group, 'tên nhóm')"),
    ('semantic.shift_summary', ('revenue',),
     "Revenue of one shift (ca sáng = 'morning', ca chiều = 'afternoon'; bán được bao nhiêu tiền)",
     "SELECT revenue FROM semantic.shift_summary(DATE '2025-06-01', DATE '2025-06-30') WHERE shift = 'morning'", 'shift'),
    ('semantic.shift_summary', ('orders',),
     "Number of orders of one shift (số đơn, bill ca sáng / ca chiều)",
     "SELECT orders FROM semantic.shift_summary(DATE '2025-06-01', DATE '2025-06-30') WHERE shift = 'afternoon'", 'shift'),
    ('semantic.shift_summary', ('revenue_vs_morning_percent',),
     'Afternoon revenue compared with the morning in percent (doanh thu ca chiều so với ca sáng chênh bao nhiêu %), answer type percent',
     "SELECT revenue_vs_morning_percent FROM semantic.shift_summary(DATE '2025-06-01', DATE '2025-06-30') WHERE shift = 'afternoon'",
     'shift'),
    ('semantic.shift_summary', ('orders_vs_morning_percent',),
     'Afternoon order count compared with the morning in percent (số đơn ca chiều so với ca sáng chênh bao nhiêu %), answer type percent',
     "SELECT orders_vs_morning_percent FROM semantic.shift_summary(DATE '2025-06-01', DATE '2025-06-30') WHERE shift = 'afternoon'",
     'shift'),
    ('semantic.eligible_orders', ('order_date',), 'Number of orders (số đơn) in a period',
     "SELECT COUNT(*) FROM semantic.eligible_orders WHERE order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.eligible_orders', ('order_date', 'revenue'), 'Average revenue per order (doanh thu bình quân mỗi đơn)',
     "SELECT ROUND(SUM(revenue) / COUNT(*)) FROM semantic.eligible_orders WHERE order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.eligible_orders', ('order_date', 'customer_key'),
     'Customers by number of orders (walk-in orders have no customer_key and are not customers); here: customers with at least 3 orders',
     "SELECT COUNT(*) FROM (SELECT customer_key FROM semantic.eligible_orders WHERE customer_key IS NOT NULL "
     "AND order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30' GROUP BY customer_key HAVING COUNT(*) >= 3)"),
    ('semantic.eligible_orders', ('order_date', 'customer_key', 'customer_name', 'revenue'),
     'Customers who bought the most money (mua nhiều tiền nhất, chi tiêu nhiều nhất, top k khách): walk-in orders '
     'have no customer_key and are never ranked; answer type list of names in rank order (string for one customer)',
     "SELECT MIN(customer_name) FROM semantic.eligible_orders WHERE customer_key IS NOT NULL "
     "AND order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30' GROUP BY customer_key "
     "ORDER BY SUM(revenue) DESC, customer_key LIMIT 3", 'customers'),
    ('semantic.eligible_orders', ('order_date', 'customer_key', 'customer_name'),
     'Customer with the most orders (use SUM(revenue) for the customer who bought the most money), answer type string',
     "SELECT MIN(customer_name) FROM semantic.eligible_orders WHERE customer_key IS NOT NULL "
     "AND order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30' GROUP BY customer_key ORDER BY COUNT(*) DESC, customer_key LIMIT 1"),
    ('semantic.orders', ('order_date', 'order_state'),
     'Cancelled orders (đơn huỷ) in a period',
     "SELECT COUNT(*) FROM semantic.orders WHERE order_state = 'cancelled' AND order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.orders', ('order_date', 'order_state'),
     'Cancellation rate (tỉ lệ đơn huỷ): cancelled orders over all orders of every state, answer type percent',
     "SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE order_state = 'cancelled') / COUNT(*), 2) FROM semantic.orders "
     "WHERE order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.product_quantities', ('base_quantity', 'base_unit', 'order_date'),
     'Units sold of a base unit (bán được bao nhiêu <đơn vị>), all products with that base unit',
     "SELECT SUM(base_quantity) FROM semantic.product_quantities WHERE base_unit = 'gói' AND order_date = DATE '2025-06-30'"),
    ('semantic.product_quantities', ('base_quantity', 'product_name', 'order_date'),
     'Units sold of one named product (bán được mấy kg / ly / phần <tên hàng>); base_quantity is already in the base unit',
     "SELECT SUM(base_quantity) FROM semantic.product_quantities WHERE name_like(product_name, 'tên hàng') "
     "AND order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.unsold_products', ('product_name',),
     'Unsold products in a period (mặt hàng ế, không bán được, không ai mua; not hết hàng): count the rows for '
     '"mấy mặt hàng ế", list product_name for "những mặt hàng nào"',
     "SELECT COUNT(*) FROM semantic.unsold_products(DATE '2025-06-01', DATE '2025-06-30')", 'unsold'),
    ('semantic.top_products', ('rank', 'product_name'),
     'Best-selling products (bán chạy nhất, bán nhiều nhất, hot nhất, top k): answer type list of the names in rank order; '
     'a question about the single best seller (mặt hàng nào) is answer type string',
     "SELECT product_name FROM semantic.top_products(DATE '2025-06-01', DATE '2025-06-30') WHERE rank <= 3 ORDER BY rank"),
    ('semantic.debt_as_of', (), 'Total customer debt at the end of a date (còn nợ tính đến hết ngày X)',
     "SELECT SUM(outstanding) FROM semantic.debt_as_of(DATE '2025-06-30')"),
    ('semantic.debt_repayments', (), 'Debt repaid / collected in a period (trả nợ, thu nợ, thu hồi công nợ)',
     "SELECT SUM(amount) FROM semantic.debt_repayments WHERE repayment_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'"),
    ('semantic.stock_snapshot', ('whole_large_units',),
     'Stock of a product in large units (còn mấy hộp chẵn, mấy cái lẻ), answer as list [whole, loose]',
     "SELECT whole_large_units, loose_base_units FROM semantic.stock_snapshot WHERE name_like(product_name, 'tên hàng')"),
    ('semantic.stock_snapshot', ('stock_base_units',),
     'Stock of a product at the end of stock_date (tồn kho cuối ngày); valid only when the asked date equals stock_date',
     "SELECT stock_base_units, stock_date FROM semantic.stock_snapshot WHERE name_like(product_name, 'tên hàng')"),
)


_SYSTEM_DATE = re.compile(r'\b(?:current_date|current_timestamp|current_time|localtimestamp|localtime|'
                          r'get_current_timestamp|get_current_time|transaction_timestamp|now\s*\(|today\s*\()', re.I)
_DATE_LITERAL = re.compile(r"'(\d{4}-\d{2}-\d{2})(?:[ T][\d:.]+)?'")


class QueryRejected(ValueError):
    """A generated query is not a single allowlisted SELECT."""


# A cleaned relation of an order, order-line or sale-recording grain holds sales records when it records a sale
# fact or amount; one that records none (a delivery book, a credit-terms sheet) does not. An unclassified relation
# holds sales records when it carries a sales amount or an order id and no purchase-receipt column.
_SALES_GRAINS = frozenset({'order', 'order_item', 'sale_recording'})
_SALE_FACTS = frozenset({'status', 'order_date', 'created_at', 'line_amount', 'unit_price', 'order_goods_amount',
                         'order_total'})
_SALES_ROLES = frozenset({'order_id', 'line_amount', 'unit_price', 'order_goods_amount', 'order_total'})
_RECEIPT_ROLES = frozenset({'receipt_id', 'receipt_date', 'receipt_amount'})
UNREADY_REASON = 'sales data is not rulebook-ready'


def _roles(relation):
    return {column.get('role') for column in relation.get('columns', [])}


def unready_sales_relations(schema):
    """Name the cleaned sales relations that the rulebook could not be applied to.

    Args:
        schema: Parsed ``schema.json`` dictionary of one bundle.

    Returns:
        Set of lower-case ``clean.*`` relation names. It is empty when the
        bundle publishes ``semantic.eligible_sales_lines``. Otherwise it holds
        every cleaned relation of an order, order-line or sale-recording grain
        that records a sale fact or amount, and every unclassified relation
        with a sales amount or order id and no purchase-receipt column. These
        relations apply no order eligibility (cancellations, returns,
        unfinished orders), so totals read from them are not the rulebook's
        figures.
    """
    relations = schema.get('relations', [])
    if any(relation['name'] == 'semantic.eligible_sales_lines' for relation in relations):
        return set()
    unready = set()
    for relation in relations:
        if relation['name'].startswith('semantic.'):
            continue
        roles, grain = _roles(relation), relation.get('grain')
        if ((grain in _SALES_GRAINS and roles & _SALE_FACTS)
                or (grain == 'unknown' and roles & _SALES_ROLES and not roles & _RECEIPT_ROLES)):
            unready.add(relation['name'].lower())
    return unready


def _ready_orders(schema):
    """Return the published rulebook-ready order relation (semantic.eligible_orders), or None."""
    return next((relation for relation in schema.get('relations', [])
                 if relation['name'] == 'semantic.eligible_orders'), None)


def _unready_refusal(name, orders_ready):
    if orders_ready:
        return (f'{name} holds sales records that preparation could not make rulebook-ready. Do not query it. '
                'Order counts, cancelled orders and order revenue come from semantic.orders and '
                'semantic.eligible_orders, which apply the rulebook. If the question needs individual sales lines '
                f'(quantities, products, prices), answer {{"type": "abstain", "reason": "{UNREADY_REASON}"}}.')
    return (f'{name} holds sales records that preparation could not make rulebook-ready: cancelled, returned '
            'and unfinished orders are not separated, so revenue, order counts, quantities sold and rankings '
            'read from it are wrong. Do not query it. If the question needs sales records, answer '
            f'{{"type": "abstain", "reason": "{UNREADY_REASON}"}}.')


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


def check_query(connection, sql, allowed, refused=(), orders_ready=False):
    """Reject anything except one SELECT over allowlisted relations.

    Args:
        connection: DuckDB connection used only to parse the statement.
        sql: Generated SQL text.
        allowed: Set of lower-case ``schema.table`` names the agent may read.
        refused: Lower-case ``schema.table`` names that exist but must not be
            read (see unready_sales_relations); a query naming one is rejected
            with the reason and an instruction to abstain.
        orders_ready: True when the bundle publishes rulebook-ready orders; the
            refusal then points to them for order-level questions.

    Returns:
        The SQL text with surrounding whitespace and a trailing semicolon removed.

    Raises:
        QueryRejected: The text is not exactly one SELECT, uses a table
            function, or references a relation outside the allowlist.
    """
    sql = sql.strip().rstrip(';').strip()
    if not sql:
        raise QueryRejected('Empty query')
    bare = re.sub(r"'(?:[^']|'')*'", "''", sql)
    if _SYSTEM_DATE.search(bare):
        raise QueryRejected('The system date is not allowed; use the explicit dates from the Dates block')
    if re.search(r'\b(?:i?like|similar\s+to)\b', bare, re.I):
        raise QueryRejected("LIKE is not allowed because it misses accents and spelling; "
                            "match names with name_like(column, 'text')")
    parsed = json.loads(connection.execute('SELECT json_serialize_sql(?)', [sql]).fetchone()[0])
    if parsed.get('error'):
        raise QueryRejected(parsed.get('error_message') or 'Only SELECT statements are allowed')
    if len(parsed.get('statements', [])) != 1:
        raise QueryRejected('Send exactly one SELECT statement')
    tables, ctes, functions = [], set(), []
    _walk(parsed, tables, ctes, functions)
    basenames = {name.split('.', 1)[1] for name in allowed}
    refused_names = {name.split('.', 1)[1]: name for name in refused}
    for schema, name in functions:
        if f'{schema}.{name}' not in allowed and not (not schema and name in basenames):
            raise QueryRejected('Table functions are not allowed; query the listed relations')
    for catalog, schema, table in tables:
        schema, table = schema.lower(), table.lower()
        if catalog:
            raise QueryRejected('Catalog-qualified names are not allowed')
        if f'{schema}.{table}' in refused or (not schema and table not in ctes and table not in basenames
                                               and table in refused_names):
            raise QueryRejected(_unready_refusal(f'{schema}.{table}' if schema else refused_names[table], orders_ready))
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


# A query that multiplies by 100 returns percentages.
_TIMES_100 = re.compile(r'(?<![\w.])100(?:\.0*)?\s*\*|\*\s*100(?:\.0*)?(?![\w.])')


def run_query(connection, sql, allowed, cells=None, percent_cells=None, refused=(), orders_ready=False):
    """Run one checked query and return a bounded text result.

    Args:
        connection: Read-only DuckDB connection to the bundle.
        sql: Generated SQL text.
        allowed: Allowlisted ``schema.table`` names.
        cells: Optional list replaced with the numeric cells of the shown rows.
        percent_cells: Optional list replaced with the shown numeric cells that
            are already percentages: those of a column named like ``*percent*``,
            or every numeric cell when the query multiplies by 100.
        refused: Relation names that must not be read; passed to check_query.
        orders_ready: Passed to check_query.

    Returns:
        Text with the column names, up to MAX_ROWS rows and the total row
        count, truncated to MAX_RESULT_CHARS.

    Raises:
        QueryRejected: The query fails the allowlist check.
        duckdb.Error: The query is invalid or was interrupted at the time limit.
    """
    sql = check_query(connection, sql, allowed, refused, orders_ready)
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
    numeric = lambda value: isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    if cells is not None:
        cells.clear()
        cells.extend(value for row in rows[:MAX_ROWS] for value in row if numeric(value))
    if percent_cells is not None:
        scaled = _TIMES_100.search(sql) is not None
        percent_cells.clear()
        percent_cells.extend(value for row in rows[:MAX_ROWS] for name, value in zip(columns, row)
                             if numeric(value) and (scaled or 'percent' in name.lower()))
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


def describe_schema(connection, schema, question=None):
    """Render the published schema as compact prompt text.

    Args:
        connection: Read-only DuckDB connection to the bundle.
        schema: Parsed ``schema.json`` dictionary.
        question: Optional question text. When given, patterns for a kind of
            question (a comparison with an earlier period, a shift) are shown
            only when the question is of that kind.

    Returns:
        A (text, allowed) pair: prompt text listing rulebook-defined semantic
        relations first, then cleaned source tables, with query patterns for
        the relations this bundle has and the observed sales date range; and
        the set of lower-case allowlisted relation and function names. Label
        columns with at most MAX_LABELS distinct values list those values;
        identifier, name and free-text columns never do. Relations named by
        unready_sales_relations are listed by name only, with the instruction
        to abstain (or, when rulebook-ready orders exist, to use them), and are
        not in the allowlist. Order-grain relations without sale facts in such
        a bundle carry a note on what they can answer.
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
        if name.lower() in limited:
            lines.append('  Records no sale amounts or sale dates: use it only for what it records (for example '
                         'delivery rounds or credit terms), never for revenue, order counts or quantities sold.')
        return lines + ['']

    relations = schema.get('relations', [])
    unready = unready_sales_relations(schema)
    limited = {relation['name'].lower() for relation in relations if unready and relation.get('grain') in _SALES_GRAINS
               and relation['name'].startswith('clean.') and relation['name'].lower() not in unready}
    shown = lambda relation: (question is None or relation['name'] not in _RELATION_KINDS
                              or _PATTERN_KINDS[_RELATION_KINDS[relation['name']]](question))
    semantic = [r for r in relations if r['name'].startswith('semantic.') and shown(r)]
    clean = [r for r in relations if not r['name'].startswith('semantic.') and r['name'].lower() not in unready]
    lines = []
    if semantic:
        lines += ['## Rulebook-ready relations (use these first)', '']
        for relation in semantic:
            lines += render(relation)
    lines += ['## Cleaned source tables', '']
    for relation in clean:
        lines += render(relation)
    extra_patterns = []
    if unready:
        abstain = f'{{"type": "abstain", "reason": "{UNREADY_REASON}"}}'
        price = ('The current price or cost in a catalogue is not the selling price on a past date.')
        orders = _ready_orders(schema)
        lines += ['## Sales tables that cannot be used', '']
        if orders:
            columns = {column['name'] for column in orders.get('columns', [])}
            lines.append('These sales tables are not rulebook-ready and cannot be queried: ' + ', '.join(sorted(unready))
                         + '. semantic.orders and semantic.eligible_orders above apply the rulebook: use them for '
                         'order counts, cancelled orders and order revenue (see the query patterns).')
            if 'order_goods_amount' in columns and 'revenue' not in columns and 'order_date' in columns:
                # Revenue is the goods amount of eligible orders (before order discount and delivery fee), not the
                # amount the customer pays; shown with the other patterns, where the model looks for queries.
                extra_patterns.append('- Revenue (doanh thu) in a period: the goods amount of eligible orders, not '
                                      'order_total:\n  SELECT SUM(order_goods_amount) FROM semantic.eligible_orders '
                                      "WHERE order_date BETWEEN DATE '2025-06-01' AND DATE '2025-06-30'")
            lines += ['A question that needs individual sales lines (quantities sold, best-selling products, revenue '
                      f'of a product or group, the selling price on a date) cannot be answered here: answer {abstain}. '
                      + price, '']
        else:
            lines += ['Preparation could not apply the rulebook to these sales tables (cancelled, returned and '
                      'unfinished orders are not separated), so they cannot be queried: ' + ', '.join(sorted(unready)) + '.',
                      'A question that needs sales records (revenue, number of orders, quantities sold, best-selling '
                      'products, top customers, the selling price on a date) cannot be answered for this business: '
                      f'answer {abstain}. ' + price + ' Other tables listed above can be used for what they record.', '']
    published = {relation['name']: {column['name'] for column in relation.get('columns', [])} for relation in relations
                 if shown(relation)}
    patterns = extra_patterns + [f'- {title}:\n  {sql}' for name, needed, title, sql, *kind in PATTERNS
                if name in published and set(needed) <= published[name]
                and (question is None or not kind or _PATTERN_KINDS[kind[0]](question))]
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
    ('last week', ('tuan truoc', 'tuan roi', 'tuan vua roi', 'tuan vua qua', 'tuan qua', 'tuan trc')),
    ('this month', ('thang nay',)),
    ('last month', ('thang truoc', 'thang roi', 'thang vua roi', 'thang vua qua', 'thang trc', 'th truoc', 'thg truoc', 'th trc')),
    ('this quarter', ('quy nay',)),
    ('this year', ('tu dau nam', 'nam nay')),
)


# Words naming the period before (or a year before) the asked one. Beside a named month or quarter they make the
# question a comparison with that earlier period; without them a named period is only the period itself.
_EARLIER = tuple(sorted(set(dict(_PHRASES)['last month']) | {
    'cung ky', 'ky truoc', 'ky trc', 'truoc do', 'trc do', 'lien truoc', 'lien trc', 'tang truong',
    'quy truoc', 'quy trc', 'quy roi', 'nam truoc', 'nam trc', 'nam ngoai', 'nam roi'}))
# Words of change or comparison. A bare "so" is not one: "số" (number) folds to it.
_COMPARE = ('so voi', 'so vs', 'tang', 'giam', 'chenh', 'hon hay kem', 'thay doi')


def mentions_earlier_period(question):
    """True when the question names the period before, or a year before, the one it asks about."""
    folded = f' {_fold(question)} '
    return any(f' {cue} ' in folded for cue in _EARLIER)


def asks_shift(question):
    """True when the question is about the morning or afternoon shift."""
    return re.search(r' ca (?:sang|chieu)', f' {_fold(question)} ') is not None


def _words_of(question):
    """Accent-folded question with punctuation as spaces, padded with spaces for whole-word matching."""
    return ' ' + re.sub(r'[^a-z0-9 ]+', ' ', _fold(question)) + ' '


def ranks_customers(question):
    """True when the question ranks customers by what they bought (not by debt)."""
    words = _words_of(question)
    return (re.search(r' (?:khach|kh|nguoi mua|ai mua|ai la nguoi mua)', words) is not None
            and re.search(r'nhat |top|hang dau|dung dau|chi tieu|tong tien', words) is not None
            and re.search(r' (?:no|cong no|con no|tra no) ', words) is None)


def counts_days_over_amount(question):
    """True when the question counts or lists days whose revenue is above or below an amount."""
    words = _words_of(question)
    return (re.search(r' (?:may|bao nhieu|bn|nhieu|nhung|cac|so) ngay ', words) is not None
            and (re.search(r' (?:tro le\w*|tro xuong|tren|duoi|vuot|it nhat|toi thieu|hon|dat|qua) ', words) is not None
                 or re.search(r'[≥≤<>]', question) is not None))


def asks_unsold(question):
    """True when the question is about products not sold in a period (hàng ế, không bán được)."""
    words = _words_of(question)
    product = r'(?:mat hang|mawtj hang|mat hag|hang|mon|san pham|sp)'
    cue = (re.search(product + r' (?:bi )?(?:e|ees|es) ', words) or re.search(r' e (?:han|am|qua|lam) ', words)
           or re.search(r' (?:khong|ko|k|hong|chua) (?:ban|ban ra|ban duoc|ban dc|ai mua|ai dat|co don nao)', words)
           or re.search(r' ban (?:cham|khong chay|ko chay|e) ', words))
    return bool(cue and re.search(' ' + product + ' ', words))


def compares_periods(question):
    """True when the question compares a period with another period, not the two shifts of one period."""
    if mentions_earlier_period(question):
        return True
    # "giảm giá" (discount) and the group "giảm đau" (pain relief) are not a fall.
    folded = f' {_fold(question)} '.replace(' giam gia ', ' ').replace(' giam dau ', ' ')
    return not asks_shift(question) and any(f' {cue} ' in folded for cue in _COMPARE)


_PATTERN_KINDS = {'comparison': compares_periods, 'shift': asks_shift, 'customers': ranks_customers,
                  'days': counts_days_over_amount, 'unsold': asks_unsold}
# Relations described only for their kind of question: every prompt token counts in the largest bundles.
_RELATION_KINDS = {'semantic.unsold_products': 'unsold'}


# Accent-, case- and spacing-insensitive comparison key; strip_accents keeps the letter đ, so it is folded separately.
_FOLD_SQL = "replace(lower(strip_accents(trim(regexp_replace({}, '\\s+', ' ', 'g')))), 'đ', 'd')"


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

    def year_for(start_month, year):
        # A month or quarter named without a year is this year's unless it starts after today.
        if year:
            return int(year)
        return today.year if date(today.year, start_month, 1) <= today else today.year - 1

    def months(first, count):
        end_month = first.month + count
        last = date(first.year + (end_month - 1) // 12, (end_month - 1) % 12 + 1, 1) - timedelta(days=1)
        return first, last

    def before(first, count):
        month = first.month - count
        return months(date(first.year + (month - 1) // 12, (month - 1) % 12 + 1, 1), count)

    def base(first, count, unit):
        # "Cùng kỳ năm trước" compares with the same months a year earlier; otherwise the period just before.
        if year_ago:
            return f'the same {unit} a year earlier is {span(*months(first.replace(year=first.year - 1), count))}'
        return f'the {unit} before it is {span(*before(first, count))}'

    year_ago = re.search(r' cung (?:ky|thang|quy) (?:cua )?nam (?:truoc|ngoai|roi|trc) ', folded) is not None
    # "thangs" is "tháng" typed with an unconverted Telex tone key.
    month_matches = [match for match in re.finditer(r'(?<= )(?:thang|thangs|thg|th) (\d{1,2})(?:(?: nam)? (\d{4}))?(?= )', folded)
                     if 1 <= int(match.group(1)) <= 12]
    quarter_matches = [match for match in re.finditer(r'(?<= )quy (\d)(?:(?: nam)? (\d{4}))?(?= )', folded)
                       if 1 <= int(match.group(1)) <= 4]
    # The earlier period is given only when the question compares one named period with the one before it;
    # otherwise the model used it as the asked period. With two named periods the question names its own base.
    earlier = mentions_earlier_period(question) and len(month_matches) + len(quarter_matches) == 1
    for match in month_matches:
        month = int(match.group(1))
        first, last = months(date(year_for(month, match.group(2)), month, 1), 1)
        lines.append(f'- "{match.group(0)}" means {span(first, last)}' + (f'; {base(first, 1, "month")}' if earlier else ''))
    for match in quarter_matches:
        start = 3 * int(match.group(1)) - 2
        first, last = months(date(year_for(start, match.group(2)), start, 1), 3)
        lines.append(f'- "{match.group(0)}" means {span(first, last)}' + (f'; {base(first, 3, "quarter")}' if earlier else ''))
    named = bool(month_matches or quarter_matches)
    for name, phrases in _PHRASES:
        found = next((phrase for phrase in phrases if f' {phrase} ' in folded), None)
        if found and not (named and name == 'last month'):
            # Beside a named month, "the month before" refers to that month, not to today.
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


def asks_percent(question):
    """True when the question asks for a percentage or a rate, whose answer type is percent."""
    folded = f' {_fold(question)} '
    # Telex typing leaves doubled vowels and tone keys in a word ("phaanf trawm", "tir leej"), so cue words allow them.
    if re.search(r' ph[aw]{1,2}[frsxj]?n[frsxj]? tr[aw]{1,2}[frsxj]?m[frsxj]? '
                 r'| t[iy][frsxj]? l[e]{1,2}[frsxj]? | t[iy][frsxj]? tr[o]{1,2}[frsxj]?ng[frsxj]? ', folded):
        return True
    # A % sign that does not follow a number asks for a percentage ("bao nhiêu %"); "10%" is part of a name.
    return re.search(r'(?<![\d.,\s])\s*%|(?<![\d.,])\s+%', question) is not None


def grounded(value, cells):
    """True when a numeric answer is a value of the last result, exactly or rounded half-up to whole or two decimals."""
    try:
        answer = Decimal(str(value))
    except ArithmeticError:
        return False
    for cell in cells:
        number = Decimal(str(cell))
        if not number.is_finite():
            continue
        for places in (None, '1', '0.01'):
            candidate = number if places is None else number.quantize(Decimal(places), rounding=ROUND_HALF_UP)
            if abs(answer - candidate) <= Decimal('0.000001'):
                return True
    return False


def rescaled_percent(value, percent_cells):
    """The percentage a percent answer meant when it multiplied an already-percent result cell by 100 again, else None."""
    for cell in percent_cells:
        number = Decimal(str(cell))
        if number.is_finite() and number != 0 and grounded(value, [number * 100]):
            return float(number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    return None


def _block(text):
    """Return (kind, body, held_answer) for a reply.

    A reply that holds both a query and a final answer is read as the query,
    because an answer written before its result is seen cannot be checked
    against it; held_answer is then the answer's text, otherwise None.
    """
    blocks = [(kind.lower(), body.strip()) for kind, body in re.findall(r'```(\w*)\s*\n(.*?)```', text or '', flags=re.S)]
    if blocks:
        queries = [block for block in blocks if block[0] == 'sql'
                   or (block[0] == '' and re.match(r'(?:select|with)\b', block[1], re.I))]
        answers = [body for kind, body in blocks if kind == 'json']
        if queries and answers:
            return queries[0][0], queries[0][1], answers[-1]
        kind, body = blocks[-1]
        return kind, body, None
    stripped = (text or '').strip()
    if stripped.startswith('{') and stripped.endswith('}'):
        return 'json', stripped, None
    return None, stripped, None


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
    # One thread: with parallel execution, rows that tie in an ORDER BY ... LIMIT come back in a different order on
    # each run, so the same question could get a different answer. Queries here take well under a second.
    connection = duckdb.connect(str(bundle / 'data.duckdb'), read_only=True,
                                config={'enable_external_access': False, 'autoinstall_known_extensions': False,
                                        'autoload_known_extensions': False, 'memory_limit': '1GB', 'threads': 1})
    try:
        connection.execute("SET search_path='semantic,clean'")
        # strip_accents keeps the letter đ, so it is folded separately.
        connection.execute("CREATE TEMP MACRO name_like(col, txt) AS "
                           "replace(strip_accents(lower(CAST(col AS VARCHAR))), 'đ', 'd') "
                           "LIKE '%' || replace(strip_accents(lower(trim(txt))), 'đ', 'd') || '%'")
        connection.execute("CREATE TEMP MACRO name_is(col, txt) AS "
                           f"{_FOLD_SQL.format('CAST(col AS VARCHAR)')} = {_FOLD_SQL.format('CAST(txt AS VARCHAR)')}")
        connection.execute('SET lock_configuration=true')
        schema_text, allowed = describe_schema(connection, schema, question['question'])
        refused = unready_sales_relations(schema)
        orders_ready = _ready_orders(schema) is not None
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
        last_ok_sql = None
        unverified = 0
        ungrounded = 0
        percent_refused = False
        cells, percent_cells = [], []
        with question_span(question):
            for step in range(1, max_steps + 1):
                response = model.generate(messages)
                content = response.content if isinstance(response.content, str) else str(response.content)
                usage = response.token_usage
                emit({'stage': 'memory_step', 'step': str(step), 'model_output': content, 'error': None,
                      'token_usage': {'input_tokens': getattr(usage, 'input_tokens', 0),
                                      'output_tokens': getattr(usage, 'output_tokens', 0)} if usage else {}})
                messages.append(text(MessageRole.ASSISTANT, content))
                kind, body, held_answer = _block(content)
                if held_answer is not None and ' '.join(body.split()) == last_ok_sql:
                    # The query repeats the last one whose result the model has seen, so its answer can be checked.
                    kind, body, held_answer = 'json', held_answer, None
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
                        elif answer['type'] == 'abstain':
                            return answer
                        elif not succeeded:
                            feedback = 'Run a query first; answer only from a query result.'
                        else:
                            asked_percent = asks_percent(question['question'])
                            if answer['type'] == 'number' and asked_percent:
                                # The question asks for a percentage, so the answer type is percent.
                                answer = validate_answer(normalize_answer({'type': 'percent', 'answer': answer['answer']}))
                            values = (answer['answer'] if answer['type'] == 'list' else
                                      [answer['answer']] if answer['type'] in ('number', 'percent') else [])
                            numbers = [value for value in values if type(value) in (int, float)]
                            rescaled = (rescaled_percent(answer['answer'], percent_cells)
                                        if answer['type'] == 'percent' and numbers and not grounded(numbers[0], cells) else None)
                            if rescaled is not None:
                                # The cell was already a percentage; multiplying it by 100 again is a copy slip.
                                answer = {**answer, 'answer': rescaled}
                                numbers = [rescaled]
                            if answer['type'] == 'percent' and not asked_percent and not percent_refused:
                                # A percentage for a question that asks for an amount or a count comes from the
                                # wrong query; ask once, and accept a second percent answer.
                                percent_refused = True
                                feedback = ('Not accepted: the question does not ask for a percentage or a rate. '
                                            'Answer the amount or count it asks for, from a query that computes it.')
                            elif all(grounded(value, cells) for value in numbers):
                                return answer
                            else:
                                ungrounded += 1
                                if ungrounded > 1:
                                    return {'type': 'abstain', 'reason': 'Answer is not a value from the query result.'}
                                shown = ', '.join(_cell(cell) for cell in cells[:8]) or 'no numeric value'
                                feedback = (f'Not accepted: your answer is not a value of the last result ({shown}). '
                                            'Do the whole calculation in SQL, including percentages and differences, '
                                            'then copy the result value exactly. A NULL or empty result is not 0.')
                elif kind in ('sql', '') and body:
                    emit({'stage': 'sql', 'sql': body})
                    last_sql = body
                    try:
                        feedback = 'Result:\n' + run_query(connection, body, allowed, cells, percent_cells, refused, orders_ready)
                        succeeded = True
                        last_ok_sql = ' '.join(body.split())
                    except QueryRejected as error:
                        feedback = f'Query rejected: {error}'
                    except duckdb.Error as error:
                        feedback = f'Query error: {str(error)[:600]}'
                    if held_answer is not None:
                        feedback += ('\nYour reply also held a final answer. It was not read: answer in a separate reply, '
                                     'after you have seen this result.')
                else:
                    feedback = 'Reply with one ```sql block or one ```json block.'
                emit({'stage': 'observation', 'text': feedback})
                if step == max_steps - 1:
                    feedback += '\nThis is your last step: send the final ```json answer now, or abstain.'
                messages.append(text(MessageRole.USER, feedback))
        return {'type': 'abstain', 'reason': 'Step limit reached without a validated answer.'}
    finally:
        connection.close()
