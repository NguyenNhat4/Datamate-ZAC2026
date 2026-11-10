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


_WALK_IN = ('khach le', 'kl', 'le', 'khach vang lai', 'vang lai', 'kvl', 'khach le vang lai', 'walk in', 'guest')


def _fold(expression):
    """SQL for an accent-, case- and spacing-insensitive comparison key."""
    # strip_accents keeps the letter đ, so it is folded separately.
    return ("replace(lower(strip_accents(trim(regexp_replace(CAST(" + expression +
            " AS VARCHAR), '\\s+', ' ', 'g')))), 'đ', 'd')")


def _columns(connection, sql):
    return {name: kind for name, kind, *_ in connection.execute('DESCRIBE ' + sql).fetchall()}


def shift_hours(rulebook):
    """Read the morning and afternoon shift hours from the rulebook, as minutes of the day."""
    text = text_key(rulebook)
    hours = {}
    for name, label in (('morning', 'ca sang'), ('afternoon', 'ca chieu')):
        match = re.search(label + r'\s*(\d{1,2})[:h.](\d{2})\s*[^\d\s]{1,3}\s*(\d{1,2})[:h.](\d{2})', text)
        if match:
            start_hour, start_minute, end_hour, end_minute = (int(part) for part in match.groups())
            hours[name] = (start_hour * 60 + start_minute, end_hour * 60 + end_minute)
    return hours if len(hours) == 2 else None


def order_extras(connection, relations, rulebook, core_sql):
    """Add rulebook-defined order attributes: the shift of the order time and who counts as a customer.

    `customer_key` is NULL for a walk-in order. With customer codes each code is
    one customer; without codes, names that differ only in accents, case or
    spacing are one customer.
    """
    columns = _columns(connection, core_sql)
    extras, joins = [], ''
    hours = shift_hours(rulebook)
    if hours and str(columns.get('created_at', '')).startswith('TIMESTAMP'):
        minute = 'hour(core.created_at) * 60 + minute(core.created_at)'
        cases = ' '.join(f"WHEN {minute} BETWEEN {first} AND {last} THEN '{name}'" for name, (first, last) in hours.items())
        extras.append(f'CASE {cases} END AS shift')
    if 'customer_id' in columns:
        extras.append("NULLIF(trim(CAST(core.customer_id AS VARCHAR)), '') AS customer_key")
        catalogues = [r for r in relations if r['grain'] == 'customer' and 'customer_name' in r['roles']
                      and r['keys'] == [r['roles'].get('customer_id')]]
        if len(catalogues) == 1 and 'customer_name' not in columns:
            roles = catalogues[0]['roles']
            joins = f' LEFT JOIN {_sql(catalogues[0])} cust ON core.customer_id = cust.{_q(roles["customer_id"])}'
            extras.append(f'cust.{_q(roles["customer_name"])} AS customer_name')
    elif 'customer_name' in columns:
        key = _fold('core.customer_name')
        walk_in = ','.join(_lit(label) for label in ('', *_WALK_IN))
        extras.append(f'CASE WHEN core.customer_name IS NULL OR {key} IN ({walk_in}) THEN NULL ELSE {key} END AS customer_key')
    if not extras:
        return core_sql
    return f'SELECT core.*, {", ".join(extras)} FROM ({core_sql}) core{joins}'


def line_extras(connection, relations, core_sql):
    """Give every sales line one product name per product, and the product's group and base unit.

    The rulebook treats different spellings of a name as one thing and follows
    the code where a file has one. With a verified catalogue the catalogue's
    name, group and base unit are used. Without one, the most frequent spelling
    per product code, or per accent-folded name, becomes the product name.
    """
    columns = _columns(connection, core_sql)
    identity = 'product_id' if 'product_id' in columns else 'product_name' if 'product_name' in columns else None
    if identity is None:
        return core_sql
    if identity == 'product_id':
        catalogues = [r for r in relations if r['grain'] == 'product' and r['keys'] == [r['roles'].get('product_id')]]
    else:
        # Lines that record only a name join a catalogue by name when no two catalogue names fold to the same key.
        catalogues = [r for r in relations if r['grain'] == 'product' and 'product_name' in r['roles']]
        if len(catalogues) == 1:
            name = _q(catalogues[0]['roles']['product_name'])
            clash = connection.execute(
                f'SELECT count(*) FROM (SELECT {_fold(name)} FROM {_sql(catalogues[0])} WHERE {name} IS NOT NULL '
                f'GROUP BY 1 HAVING count(*) > 1)').fetchone()[0]
            missing = connection.execute(f'SELECT count(*) FROM {_sql(catalogues[0])} WHERE {name} IS NULL').fetchone()[0]
            if clash or missing:
                catalogues = []
    if len(catalogues) == 1:
        roles = catalogues[0]['roles']
        extras, excluded = [], []
        if identity == 'product_id':
            on = f'core.product_id = prod.{_q(roles["product_id"])}'
        else:
            on = f'{_fold("core.product_name")} = {_fold("prod." + _q(roles["product_name"]))}'
        if 'product_name' in roles:
            if 'product_name' in columns:
                excluded.append('product_name')
                extras.append(f'COALESCE(prod.{_q(roles["product_name"])}, core.product_name) AS product_name')
            else:
                extras.append(f'prod.{_q(roles["product_name"])} AS product_name')
        for role in ('product_group', 'base_unit'):
            if role in roles and role not in columns:
                extras.append(f'prod.{_q(roles[role])} AS {role}')
        if not extras:
            return core_sql
        star = 'core.*' + (f' EXCLUDE ({", ".join(excluded)})' if excluded else '')
        return f'SELECT {star}, {", ".join(extras)} FROM ({core_sql}) core LEFT JOIN {_sql(catalogues[0])} prod ON {on}'
    if 'product_name' not in columns:
        return core_sql
    key = 'core.product_id' if identity == 'product_id' else _fold('core.product_name')
    inner = 'product_id' if identity == 'product_id' else _fold('product_name')
    return ('SELECT core.* EXCLUDE (product_name), COALESCE(canon.name, core.product_name) AS product_name '
            f'FROM ({core_sql}) core LEFT JOIN (SELECT key, first(name ORDER BY n DESC, name DESC) AS name FROM '
            f'(SELECT {inner} AS key, product_name AS name, count(*) AS n FROM ({core_sql}) '
            f'WHERE product_name IS NOT NULL GROUP BY 1, 2) GROUP BY 1) canon ON {key} = canon.key')


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
    if connection.execute(f'SELECT count(*) FROM {_sql(snapshot)} WHERE {_q(sr["inventory_quantity"])} IS NULL').fetchone()[0]:
        return []
    conversion = conversions[0] if len(conversions) == 1 else None
    if conversion:
        cr = conversion['roles']
        if connection.execute(f'SELECT count(*) FROM {_sql(conversion)} WHERE {_q(cr["conversion_factor"])} IS NULL '
                              f'OR {_q(cr["conversion_factor"])} <= 0').fetchone()[0]:
            conversion = None
    name = f's.{_q(sr["product_name"])} AS product_name, ' if 'product_name' in sr else ''
    code = f's.{_q(sr["product_id"])} AS product_id, ' if 'product_id' in sr else ''
    base = next((sr[role] for role in ('base_unit', 'unit') if role in sr), None)
    base_unit = f's.{_q(base)} AS base_unit, ' if base else ''
    stock = f's.{_q(sr["inventory_quantity"])}'
    select = (f'SELECT {code}{name}{base_unit}{stock} AS stock_base_units, '
              f'DATE {_lit(snapshot_dates[snapshot["name"]])} AS stock_date')
    meaning = ('Stock at the end of stock_date only (the last day with data); it is not valid for any other date. '
               'stock_base_units is the stock in the product base unit.')
    sources = [snapshot['name']]
    if conversion:
        factor = f'c.{_q(cr["conversion_factor"])}'
        select += (f', c.{_q(cr["unit"])} AS large_unit, {factor} AS base_units_per_large_unit, '
                   f'CAST(floor({stock} / {factor}) AS BIGINT) AS whole_large_units, '
                   f'CAST({stock} - floor({stock} / {factor}) * {factor} AS BIGINT) AS loose_base_units '
                   f'FROM {_sql(snapshot)} s LEFT JOIN {_sql(conversion)} c ON s.{_q(sr[identity])} = c.{_q(cr[identity])}')
        meaning += (' One row per product and large unit. Stock in large units is the list '
                    '[whole_large_units, loose_base_units]; products without a large unit have NULL large-unit columns.')
        sources.append(conversion['name'])
    else:
        select += f' FROM {_sql(snapshot)} s'
    connection.execute('CREATE VIEW semantic.stock_snapshot AS ' + select)
    return [_describe(
        connection, 'semantic.stock_snapshot', 'inventory_snapshot', meaning,
        {'stock_base_units': 'Stock in base units at the end of stock_date.',
         'stock_date': 'The only date this stock is valid for.',
         'whole_large_units': 'Whole large units in stock.',
         'loose_base_units': 'Base units left over after the whole large units.',
         'base_units_per_large_unit': 'How many base units one large unit holds.'},
        sources)]


def _percent(part, whole):
    """SQL for 100 * part / whole rounded half-up to two decimals; NULL when whole is 0 or NULL."""
    # The division runs in DOUBLE; six decimals remove its representation error before rounding.
    return (f'CASE WHEN ({whole}) = 0 THEN NULL ELSE CAST(round(CAST(100.0 * ({part}) / ({whole}) '
            'AS DECIMAL(38,6)), 2) AS DECIMAL(18,2)) END')


_PERIOD = 'order_date BETWEEN CAST(first_day AS DATE) AND CAST(last_day AS DATE)'
_BASE_PERIOD = 'order_date BETWEEN CAST(base_first_day AS DATE) AND CAST(base_last_day AS DATE)'
_SERVICE_LINES = ('phi phuc vu',)


def publish_period_functions(connection, semantic, rulebook):
    """Publish period functions for question types that need several steps of rulebook arithmetic.

    Each function takes an inclusive date range and returns the finished
    figures, so a question is one call: revenue share by product group,
    change against a base period, the morning and afternoon shifts,
    best-selling products, and products not sold in a period. A function is
    published only when the sales relations carry every column it needs.
    """
    columns = {r['name']: set(r['roles']) for r in semantic}
    lines = columns.get('semantic.eligible_sales_lines', set())
    orders = columns.get('semantic.eligible_orders', set())
    quantities = columns.get('semantic.product_quantities', set())
    rules = text_key(rulebook)
    if 'order_date' not in lines:
        return []
    amounts = 'line_amount' in lines
    published = []

    def function(name, parameters, grain, meaning, meanings, sources, body):
        connection.execute(f'CREATE MACRO semantic.{name}({", ".join(parameters)}) AS TABLE {body}')
        published.append(_describe(
            connection, f'semantic.{name}', grain, meaning, meanings, sources, kind='function',
            signature=f'semantic.{name}({", ".join(p + " DATE" for p in parameters)})',
            probe=f"SELECT * FROM semantic.{name}({', '.join(['DATE ' + _lit('2000-01-01')] * len(parameters))})"))

    if amounts and 'product_group' in lines:
        function(
            'group_revenue_share', ['first_day', 'last_day'], 'product group',
            'Revenue of each product group from first_day to last_day and its share of the whole shop\'s revenue '
            'in the same days (rulebook: the denominator includes lines without a group and service-fee lines). '
            "Pick the one group the question names with name_is(product_group, 'name'); never add up similar groups.",
            {'product_group': 'Product group; NULL for lines without a group (they still count in the total).',
             'revenue': 'Revenue of this group in the period.',
             'shop_revenue': 'Revenue of the whole shop in the same period.',
             'share_percent': 'revenue / shop_revenue in percent, rounded to 2 decimals; already the percent answer.'},
            ['semantic.eligible_sales_lines'],
            'WITH g AS (SELECT product_group, SUM(line_amount) AS revenue FROM semantic.eligible_sales_lines '
            f'WHERE {_PERIOD} GROUP BY product_group), t AS (SELECT SUM(revenue) AS total FROM g) '
            f'SELECT g.product_group, g.revenue, t.total AS shop_revenue, {_percent("g.revenue", "t.total")} AS share_percent '
            'FROM g, t ORDER BY g.revenue DESC, g.product_group')

    has_orders = 'order_date' in orders
    order_counts = (
        f', (SELECT COUNT(*) FROM semantic.eligible_orders WHERE {_PERIOD}) AS n, '
        f'(SELECT COUNT(*) FROM semantic.eligible_orders WHERE {_BASE_PERIOD}) AS base_n') if has_orders else ''
    # An empty period has no data, which is not 0: its order count is NULL like its revenue.
    order_columns = (
        ', CASE WHEN r IS NULL THEN NULL ELSE n END AS orders, CASE WHEN base_r IS NULL THEN NULL ELSE base_n END AS base_orders, '
        'CASE WHEN r IS NULL OR base_r IS NULL THEN NULL ELSE n - base_n END AS orders_difference, '
        f'CASE WHEN r IS NULL OR base_r IS NULL THEN NULL ELSE {_percent("n - base_n", "base_n")} END AS orders_change_percent'
    ) if has_orders else ''
    if amounts:
        function(
            'period_comparison', ['first_day', 'last_day', 'base_first_day', 'base_last_day'], 'comparison',
            'Revenue' + (' and order count' if has_orders else '') + ' of the asked period (first_day to last_day) against '
            'a base period (base_first_day to base_last_day), for "so với kỳ trước / tháng trước / cùng kỳ", '
            '"tăng hay giảm bao nhiêu (%)", "hơn hay kém". Rulebook: difference = asked − base, and a fall is negative. '
            'Take both ranges from the Dates block. One row.',
            {'revenue': 'Revenue of the asked period; NULL when it has no sales data.',
             'base_revenue': 'Revenue of the base period; NULL when it has no sales data.',
             'revenue_difference': 'revenue − base_revenue, in VND.',
             'revenue_change_percent': 'Percent change of revenue against the base period, rounded to 2 decimals.',
             'orders': 'Orders that count as sales in the asked period.',
             'base_orders': 'Orders that count as sales in the base period.',
             'orders_difference': 'orders − base_orders.',
             'orders_change_percent': 'Percent change of the order count against the base period, rounded to 2 decimals.'},
            ['semantic.eligible_sales_lines'] + (['semantic.eligible_orders'] if has_orders else []),
            f'SELECT r AS revenue, base_r AS base_revenue, r - base_r AS revenue_difference, '
            f'{_percent("r - base_r", "base_r")} AS revenue_change_percent{order_columns} FROM ('
            f'SELECT (SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE {_PERIOD}) AS r, '
            f'(SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE {_BASE_PERIOD}) AS base_r{order_counts})')

    if {'shift', 'revenue', 'order_date'} <= orders:
        function(
            'shift_summary', ['first_day', 'last_day'], 'shift',
            "Revenue and order count of the morning shift (ca sáng) and the afternoon shift (ca chiều) from first_day to "
            "last_day, by the rulebook's hours of the order time. Comparisons use the morning shift as the base "
            '(rulebook: "so ca chiều với ca sáng: mốc là ca sáng"). Orders outside both shifts are in neither row.',
            {'shift': "'morning' (ca sáng) or 'afternoon' (ca chiều).",
             'revenue': 'Revenue of orders placed in this shift.',
             'orders': 'Orders that count as sales placed in this shift.',
             'revenue_vs_morning_percent': 'Percent by which this shift\'s revenue differs from the morning shift\'s, '
                                           'rounded to 2 decimals (0 for the morning row).',
             'orders_vs_morning_percent': 'Percent by which this shift\'s order count differs from the morning '
                                          'shift\'s, rounded to 2 decimals (0 for the morning row).'},
            ['semantic.eligible_orders'],
            'WITH s AS (SELECT shift, SUM(revenue) AS revenue, COUNT(*) AS orders FROM semantic.eligible_orders '
            f"WHERE shift IS NOT NULL AND {_PERIOD} GROUP BY shift), m AS (SELECT revenue AS mr, orders AS mo FROM s "
            f"WHERE shift = 'morning') SELECT s.shift, s.revenue, s.orders, "
            f'{_percent("s.revenue - m.mr", "m.mr")} AS revenue_vs_morning_percent, '
            f'{_percent("s.orders - m.mo", "m.mo")} AS orders_vs_morning_percent '
            "FROM s LEFT JOIN m ON TRUE ORDER BY CASE s.shift WHEN 'morning' THEN 1 ELSE 2 END")

    if {'base_quantity', 'order_date', 'line_amount'} <= quantities and {'product_id', 'product_name'} & quantities:
        identity = 'product_id' if 'product_id' in quantities else 'product_name'
        named = 'product_name' in quantities
        # Rulebook: a service-fee line is not a product; it is neither ranked nor counted.
        fees = [label for label in _SERVICE_LINES if label in rules] if named else []
        excluded = (f" AND {_fold('product_name')} NOT IN ({', '.join(_lit(label) for label in fees)})" if fees else '')
        code = 'product_id, ' if identity == 'product_id' else ''
        name = 'min(product_name) AS product_name, ' if identity == 'product_id' and named else 'product_name, '
        unit = 'min(base_unit) AS base_unit, ' if 'base_unit' in quantities else ''
        function(
            'top_products', ['first_day', 'last_day'], 'product',
            'Products ranked by quantity sold from first_day to last_day (rulebook "bán chạy, bán nhiều nhất, hot '
            'nhất": quantity in base units, cancelled and returned orders excluded, products with different base units '
            'ranked together, ties by ' + ('code' if identity == 'product_id' else 'name') + ('; service-fee lines are not products' if fees else '') +
            '). Top k = the first k rows by rank; answer their product_name values as a list in rank order.',
            {'rank': '1 for the best-selling product.',
             'base_quantity': 'Quantity sold in the product base unit.',
             'revenue': 'Revenue of this product in the period.'},
            ['semantic.product_quantities'],
            f'SELECT row_number() OVER (ORDER BY SUM(base_quantity) DESC, {identity}) AS rank, {code}{name}{unit}'
            'SUM(base_quantity) AS base_quantity, SUM(line_amount) AS revenue FROM semantic.product_quantities '
            f'WHERE {_PERIOD}{excluded} AND {identity} IS NOT NULL GROUP BY {identity} ORDER BY rank')

    if {'product_id', 'product_name'} & lines:
        identity = 'product_id' if 'product_id' in lines else 'product_name'
        named = 'product_name' in lines
        fees = [label for label in _SERVICE_LINES if label in rules] if named else []
        excluded = (f" AND {_fold('product_name')} NOT IN ({', '.join(_lit(label) for label in fees)})" if fees else '')
        code = 'product_id, ' if identity == 'product_id' else ''
        name = 'min(product_name) AS product_name, ' if identity == 'product_id' and named else (
            'product_name, ' if named else '')
        function(
            'unsold_products', ['first_day', 'last_day'], 'product',
            'Unsold products (mặt hàng ế): sold before first_day, not from first_day to last_day; counted sales only'
            + ('; no service fees' if fees else '') + '.',
            {'last_sold': 'Last day the product was sold before first_day.'},
            ['semantic.eligible_sales_lines'],
            f'SELECT {code}{name}MAX(order_date) AS last_sold FROM semantic.eligible_sales_lines '
            f'WHERE order_date < CAST(first_day AS DATE){excluded} AND {identity} IS NOT NULL AND {identity} NOT IN '
            f'(SELECT {identity} FROM semantic.eligible_sales_lines WHERE {_PERIOD} AND {identity} IS NOT NULL) '
            f'GROUP BY {identity} ORDER BY {identity}')
    return published


def snapshot_dates(relations, readme):
    """Map inventory snapshots to their documented date: the README's last day with data."""
    cutoff = re.search(r'(?:đến hết|den het)\s+(\d{1,2})/(\d{1,2})/(\d{4})', readme, re.I)
    if not cutoff or 'ton cuoi ngay cuoi co du lieu' not in text_key(readme):
        return {}
    day, month, year = (int(part) for part in cutoff.groups())
    stamp = f'{year:04d}-{month:02d}-{day:02d}'
    return {r['name']: stamp for r in relations if r['grain'] == 'inventory_snapshot'}
