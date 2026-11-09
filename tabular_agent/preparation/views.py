"""Rulebook-defined business views published on top of verified relations.

Each view encodes one rulebook definition (debt, debt repayments, stock in
large units) so a query does not have to restate it. A view is published only
when every relation, role and label it depends on is present and recognised;
otherwise nothing is published and the capability stays unavailable.
"""
import re

from .parsers import text_key

_DEBT_LABELS = {'debt', 'ghi no', 'no', 'cong no', 'mua chiu', 'ban chiu', 'tra sau', 'credit'}
_COD_LABELS = {'cod', 'thu ho', 'giao hang thu tien'}
_MARKETPLACE_WORDS = {'shopee', 'lazada', 'tiki', 'tiktok', 'sendo', 'tmdt', 'marketplace', 'ecommerce', 'san'}


def _q(name):
    return '"' + str(name).replace('"', '""') + '"'


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _sql(relation):
    return '.'.join(_q(part) for part in relation['name'].split('.'))


def _labels(connection, relation_sql, column):
    return [row[0] for row in connection.execute(
        f'SELECT DISTINCT {_q(column)} FROM {relation_sql} WHERE {_q(column)} IS NOT NULL').fetchall()]


def _in(column, labels):
    return f'{column} IN (' + ','.join(_lit(label) for label in labels) + ')' if labels else 'FALSE'


def _describe(connection, name, grain, meaning, meanings, sources, kind='view', signature=None, probe=None):
    described = connection.execute('DESCRIBE ' + (probe or 'SELECT * FROM ' + name)).fetchall()
    columns = [{'name': column, 'type': kind_, 'meaning': meanings.get(column, column.replace('_', ' ')),
                'nullable': nullable == 'YES', 'role': column} for column, kind_, nullable, *_ in described]
    relation = {'name': name, 'grain': grain, 'meaning': meaning, 'columns': columns, 'keys': [],
                'roles': {c['name']: c['name'] for c in columns}, 'source_relations': sources}
    if kind == 'function':
        relation['function'] = True
        relation['signature'] = signature
    return relation


def publish_debt_views(connection, relations, semantic, rulebook):
    """Publish debt definitions from rulebook section 4 when orders, payments and labels support them."""
    orders = next((r for r in semantic if r['name'] == 'semantic.orders'), None)
    payments = [r for r in relations if r['grain'] == 'payment'
                and {'order_id', 'payment_date', 'payment_amount'} <= r['roles'].keys()]
    rules = text_key(rulebook)
    if (orders is None or len(payments) != 1 or 'no mot don tai ngay' not in rules
            or not {'order_id', 'order_date', 'order_total', 'payment_method'} <= orders['roles'].keys()):
        return []
    payment = payments[0]
    pr = payment['roles']
    pay = (f'(SELECT {_q(pr["order_id"])} AS order_id, CAST({_q(pr["payment_date"])} AS DATE) AS payment_date, '
           f'{_q(pr["payment_amount"])} AS amount FROM {_sql(payment)})')
    unmatched = connection.execute(
        f'SELECT count(*) FROM {pay} p LEFT JOIN semantic.orders o USING (order_id) WHERE o.order_id IS NULL').fetchone()[0]
    if unmatched:
        return []
    methods = _labels(connection, 'semantic.orders', 'payment_method')
    debt = [label for label in methods if text_key(str(label)) in _DEBT_LABELS]
    cod = [label for label in methods if text_key(str(label)) in _COD_LABELS]
    excluded = [_in('o.payment_method', cod)] if cod else []
    if 'channel' in orders['roles']:
        channels = _labels(connection, 'semantic.orders', 'channel')
        marketplace = [label for label in channels if set(text_key(str(label)).split()) & _MARKETPLACE_WORDS]
        if marketplace:
            excluded.append(_in('o.channel', marketplace))
    not_excluded = ' AND '.join(f'NOT ({test})' for test in excluded) or 'TRUE'
    customer = ', o.customer_id' if 'customer_id' in orders['roles'] else ''
    published = []
    connection.execute(
        'CREATE MACRO semantic.debt_as_of(as_of) AS TABLE '
        f'SELECT o.order_id{customer}, o.order_date, o.order_total, COALESCE(p.paid, 0) AS paid, '
        'o.order_total - COALESCE(p.paid, 0) AS outstanding FROM semantic.orders o LEFT JOIN '
        f'(SELECT order_id, SUM(amount) AS paid FROM {pay} WHERE payment_date <= CAST(as_of AS DATE) GROUP BY 1) p '
        f'USING (order_id) WHERE NOT o.cancelled AND o.order_date <= CAST(as_of AS DATE) AND {not_excluded} '
        'AND o.order_total - COALESCE(p.paid, 0) > 0')
    published.append(_describe(
        connection, 'semantic.debt_as_of', 'order',
        'Orders still owing at the end of a date (rulebook: unpaid part of the order total, payments counted up to '
        'that date; cancelled, COD and marketplace orders excluded; returned orders still count). '
        "Total debt = SUM(outstanding). Call it as semantic.debt_as_of(DATE 'YYYY-MM-DD').",
        {'outstanding': 'Amount this order still owes at the end of the given date (always > 0).',
         'paid': 'Payments received for this order up to the given date.',
         'customer_id': 'Customer code; NULL for a walk-in customer (counts in total debt, not in debt by customer).'},
        [orders['name'], payment['name']], kind='function', signature="semantic.debt_as_of(DATE 'YYYY-MM-DD')",
        probe='SELECT * FROM semantic.debt_as_of(DATE \'2000-01-01\')'))
    if debt and 'tra no' in rules and 'sau ngay mua' in rules:
        connection.execute(
            'CREATE VIEW semantic.debt_repayments AS '
            f'SELECT p.order_id{customer}, p.payment_date AS repayment_date, p.amount FROM {pay} p '
            f'JOIN semantic.orders o USING (order_id) WHERE {_in("o.payment_method", debt)} AND NOT o.cancelled '
            'AND p.payment_date > o.order_date')
        published.append(_describe(
            connection, 'semantic.debt_repayments', 'payment',
            'Debt repayments / debt collected (rulebook: payments made after the purchase day on credit orders that '
            'are not cancelled). Money collected on debt in a period = SUM(amount) filtered on repayment_date.',
            {'repayment_date': 'Date the repayment was received; the only date that decides the period.',
             'amount': 'Repaid amount.'},
            [orders['name'], payment['name']]))
        connection.execute(
            'CREATE VIEW semantic.debt_added AS '
            f'SELECT o.order_id{customer}, o.order_date, o.order_total, COALESCE(p.paid, 0) AS paid_on_order_day, '
            'o.order_total - COALESCE(p.paid, 0) AS added_debt FROM semantic.orders o LEFT JOIN '
            f'(SELECT x.order_id, SUM(x.amount) AS paid FROM {pay} x JOIN semantic.orders y USING (order_id) '
            'WHERE x.payment_date <= y.order_date GROUP BY 1) p USING (order_id) '
            f'WHERE {_in("o.payment_method", debt)} AND NOT o.cancelled')
        published.append(_describe(
            connection, 'semantic.debt_added', 'order',
            'New debt of credit orders (rulebook: part of the order total not paid on the purchase day, credit orders '
            'not cancelled). New debt in a period = SUM(added_debt) filtered on order_date.',
            {'added_debt': 'Order total minus what was paid on the purchase day.'},
            [orders['name'], payment['name']]))
    return published


def publish_stock_view(connection, relations, readme, snapshot_dates):
    """Publish end-of-data stock split into whole large units and loose base units (rulebook section 5)."""
    snapshots = [r for r in relations if r['grain'] == 'inventory_snapshot' and 'inventory_quantity' in r['roles']
                 and {'product_id', 'product_name'} & r['roles'].keys() and r['name'] in snapshot_dates]
    if len(snapshots) != 1:
        return []
    snapshot = snapshots[0]
    sr = snapshot['roles']
    identity = 'product_id' if 'product_id' in sr else 'product_name'
    conversions = [r for r in relations if r['grain'] == 'unit_conversion'
                   and {identity, 'unit', 'conversion_factor'} <= r['roles'].keys()]
    if len(conversions) != 1:
        return []
    conversion = conversions[0]
    cr = conversion['roles']
    bad = connection.execute(
        f'SELECT count(*) FROM {_sql(conversion)} WHERE {_q(cr["conversion_factor"])} IS NULL '
        f'OR {_q(cr["conversion_factor"])} <= 0').fetchone()[0]
    negative = connection.execute(
        f'SELECT count(*) FROM {_sql(snapshot)} WHERE {_q(sr["inventory_quantity"])} IS NULL').fetchone()[0]
    if bad or negative:
        return []
    name = f's.{_q(sr["product_name"])} AS product_name, ' if 'product_name' in sr else ''
    code = f's.{_q(sr["product_id"])} AS product_id, ' if 'product_id' in sr else ''
    base = next((sr[role] for role in ('base_unit', 'unit') if role in sr), None)
    base_unit = f's.{_q(base)} AS base_unit, ' if base else ''
    stock = f's.{_q(sr["inventory_quantity"])}'
    factor = f'c.{_q(cr["conversion_factor"])}'
    connection.execute(
        'CREATE VIEW semantic.stock_snapshot AS '
        f'SELECT {code}{name}{base_unit}{stock} AS stock_base_units, DATE {_lit(snapshot_dates[snapshot["name"]])} AS stock_date, '
        f'c.{_q(cr["unit"])} AS large_unit, {factor} AS base_units_per_large_unit, '
        f'CAST(floor({stock} / {factor}) AS BIGINT) AS whole_large_units, '
        f'CAST({stock} - floor({stock} / {factor}) * {factor} AS BIGINT) AS loose_base_units '
        f'FROM {_sql(snapshot)} s LEFT JOIN {_sql(conversion)} c ON s.{_q(sr[identity])} = c.{_q(cr[identity])}')
    return [_describe(
        connection, 'semantic.stock_snapshot', 'inventory_snapshot',
        'Stock at the end of stock_date only (the last day with data); it is not valid for any other date. '
        'One row per product and large unit. Stock in large units is the list [whole_large_units, loose_base_units]; '
        'products without a large unit have NULL large-unit columns.',
        {'stock_base_units': 'Stock in base units at the end of stock_date.',
         'stock_date': 'The only date this stock is valid for.',
         'whole_large_units': 'Whole large units in stock.',
         'loose_base_units': 'Base units left over after the whole large units.',
         'base_units_per_large_unit': 'How many base units one large unit holds.'},
        [snapshot['name'], conversion['name']])]


def snapshot_dates(relations, readme):
    """Map inventory snapshots to their documented date: the README's last day with data."""
    cutoff = re.search(r'(?:đến hết|den het)\s+(\d{1,2})/(\d{1,2})/(\d{4})', readme, re.I)
    if not cutoff or 'ton cuoi ngay cuoi co du lieu' not in text_key(readme):
        return {}
    day, month, year = (int(part) for part in cutoff.groups())
    stamp = f'{year:04d}-{month:02d}-{day:02d}'
    return {r['name']: stamp for r in relations if r['grain'] == 'inventory_snapshot'}
