"""Answer a question with read-only SQL over a prepared DuckDB bundle.

This is the consumer of ``tabular_agent.preparation`` bundles. The model sees the
published schema, the current rulebook, the business README, the question and
``as_of_date``; it never opens raw source files. Every query is parsed and
checked against the bundle's relation allowlist before it runs.
"""
import json
import re
import threading
from decimal import Decimal
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
Use only the relations and columns listed in the schema. Prefer semantic.* relations when they fit.

Reply with exactly one fenced block and nothing after it:
- To run a query, a ```sql block containing a single SELECT (WITH is allowed).
- To finish, a ```json block containing the typed answer, for example {"type": "number", "answer": 10}.

Rules:
- Resolve relative time ("hôm nay", "hôm qua", "tuần rồi", "tháng này") from as_of_date, never from the system date.
- Rows dated after as_of_date have not happened yet: filter the date column to <= DATE 'as_of_date'.
  Cancellation and return status still applies to an order even when recorded later.
- Apply the rulebook: ordinary revenue and order counts exclude cancelled and returned orders unless the question says otherwise.
- Count distinct order IDs, not rows. Line tables repeat an order ID once per product.
- Filter text labels only with values shown in "Observed labels" or returned by a query. To find a product or
  customer named in the question, search first, e.g. WHERE strip_accents(lower(col)) LIKE '%tra vai%'.
- If a query returns no rows or NULL, do not conclude 0 or unanswerable: check the filter values and retry.
- Answer types: number, percent, string, date (YYYY-MM-DD or YYYY-MM), boolean, list, table
  ({"columns": [...], "rows": [[...]]}), unanswerable, abstain. Follow the rulebook for rounding and formats.
- Use {"type": "unanswerable", "reason": "..."} only when the business never records the information or the
  period is entirely outside the data. If a needed column is withheld, or you are unsure, use
  {"type": "abstain", "reason": "..."}.
- Give the final answer only from a query result you have seen.
"""


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
            functions.append(kind)
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
    parsed = json.loads(connection.execute('SELECT json_serialize_sql(?)', [sql]).fetchone()[0])
    if parsed.get('error'):
        raise QueryRejected(parsed.get('error_message') or 'Only SELECT statements are allowed')
    if len(parsed.get('statements', [])) != 1:
        raise QueryRejected('Send exactly one SELECT statement')
    tables, ctes, functions = [], set(), []
    _walk(parsed, tables, ctes, functions)
    if functions:
        raise QueryRejected('Table functions are not allowed; query the listed relations')
    basenames = {name.split('.', 1)[1] for name in allowed}
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
        note += '. No rows matched: check filter values and dates before concluding anything.'
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
        A (text, allowed) pair: prompt text listing relations, columns,
        withheld columns, capabilities and date coverage, and the set of
        lower-case allowlisted relation names. Label columns with at most
        MAX_LABELS distinct values list those values; identifier, name and
        free-text columns never do.
    """
    lines, allowed = [], set()
    quoted = lambda name: '.'.join('"' + part.replace('"', '""') + '"' for part in name.split('.'))
    for relation in schema.get('relations', []):
        name = relation['name']
        allowed.add(name.lower())
        lines.append(f"{name}  (one row per {relation.get('grain', 'unknown')}; {relation.get('rows', '?')} rows)")
        for column in relation.get('columns', []):
            meaning = (column.get('meaning') or column.get('source_alias') or '').replace('\n', ' ')[:120]
            line = f"  - {column['name']} {column['type']}: {meaning}"
            role = column.get('role') or ''
            if column['type'] == 'VARCHAR' and not any(word in role for word in ('name', 'id', 'note', 'phone', 'address')):
                labels = _labels(connection, quoted(name), column['name'])
                if labels:
                    line += ' | Observed labels: ' + json.dumps(labels, ensure_ascii=False)
            lines.append(line)
        withheld = relation.get('blocked_columns') or []
        if withheld:
            lines.append('  Withheld (failed cleaning, cannot be queried): ' + ', '.join(str(item) for item in withheld))
        lines.append('')
    ready = [f"{name} -> {value['relation']}" for name, value in schema.get('capabilities', {}).items()
             if value.get('status') == 'available' and value.get('relation')]
    if ready:
        lines += ['Ready-made relations: ' + '; '.join(ready), '']
    coverage = [f"{value.get('role')}: {value.get('observed_min')} to {value.get('observed_max')}"
                for value in schema.get('temporal_coverage', {}).values() if isinstance(value, dict) and value.get('observed_min')]
    if coverage:
        lines.append('Observed date ranges: ' + '; '.join(sorted(set(coverage))))
    return '\n'.join(lines), allowed


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
        connection.execute('SET lock_configuration=true')
        schema_text, allowed = describe_schema(connection, schema)
        context = '\n\n'.join([
            '# Rulebook\n' + rulebook,
            '# Business README\n' + (readme.read_text(encoding='utf-8') if readme.exists() else ''),
            '# Schema\n' + schema_text,
            f"# Question\nas_of_date: {question['as_of_date']}\n{question['question']}",
        ])
        emit({'stage': 'schema', 'text': schema_text})
        text = lambda role, value: ChatMessage(role=role, content=[{'type': 'text', 'text': value}])
        messages = [text(MessageRole.SYSTEM, INSTRUCTIONS), text(MessageRole.USER, context)]
        succeeded = False
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
                        answer = validate_answer(json.loads(body))
                    except (ValueError, TypeError) as error:
                        feedback = f'Invalid final answer: {error}. Send a ```json block with a valid typed answer.'
                    else:
                        if succeeded or answer['type'] == 'abstain':
                            return answer
                        feedback = 'Run a query first; answer only from a query result.'
                elif kind in ('sql', '') and body:
                    emit({'stage': 'sql', 'sql': body})
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
