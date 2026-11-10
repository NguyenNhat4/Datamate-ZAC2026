"""Question kinds the system answers with a fixed, reviewed query instead of a model-written one.

An intent is recognised only when every word of the question is explained: by the intent's own cue words, by common
filler, or by a period or number slot. A question with an unexplained word is left to the model. Each intent's query
applies the rulebook once, here, so a recognised question gets the same answer every time.
"""
import re
from dataclasses import dataclass
from functools import lru_cache
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

from . import periods as P
from .context import percent, whole
from .entities import Catalog
from .text import FILLER, canonical_tokens, prepare

COMMON = frozenset('trong ngay rieng tinh xet theo vao luc tai da co'.split())
SLOTS = frozenset({'@t', '@n'})
_NUMBER = re.compile(r'^(\d{1,3}(?:\.\d{3})+|\d+)d?$')


@dataclass
class Parsed:
    """A question reduced to canonical words, period slots and number slots."""
    question: str
    today: date
    words: list
    periods: list
    numbers: list
    next_word: list = None
    entities: list = None


@dataclass
class Intent:
    """One recognised kind of question.

    Attributes:
        name: Identifier used in logs.
        words: Canonical cue words this intent explains.
        need: Predicate on (set of words, Parsed) that must hold.
        periods: Allowed (min, max) number of period slots.
        numbers: Allowed (min, max) number of number slots.
        entities: Kinds of catalogue names the question must contain, exactly (product, group, unit, size, customer).
        optional: Kinds that may also occur, any number of times (they are ignored).
        repeat: True when the same period may be named twice ("tháng 3, trong tổng doanh thu tháng 3") and counts once.
        product_level: True when the answer depends on what each product is called or counts as. Not answered in a
            bundle whose product names join a dish and its toppings ("Trà đào (M) + Trân châu (7.000đ)"): the rulebook
            makes the topping its own product, and preparation does not split them yet.
        run: Executor (Context, Parsed) -> answer dict or None. None means "cannot answer here": the model is asked.
    """
    name: str
    words: frozenset
    need: Callable
    periods: tuple
    numbers: tuple
    entities: tuple
    optional: tuple
    repeat: bool
    product_level: bool
    run: Callable


INTENTS = []


def intent(name, words, need, periods=(1, 1), numbers=(0, 0), entities=(), optional=(), repeat=False,
           product_level=False):
    """Register an intent; decorates its executor."""
    def register(run):
        INTENTS.append(Intent(name, frozenset(words.split()), need, periods, numbers, tuple(sorted(entities)),
                              tuple(optional), repeat, product_level, run))
        return run
    return register


# Words that only occur inside the joined phrases of text._PHRASES; known so that typing variants of them are repaired.
# "zalo oa" is a sales channel that appears in the businesses' data.
PHRASE_PARTS = frozenset('''dien thoai tai quay zalo oa tien mat chuyen khoan vi tu xuat hien it lan luot ca sang chieu
trung binh phan tram tro len thanh doi y ua chuong dat khach cao diem'''.split())


@lru_cache(maxsize=1)
def known_words():
    """Every canonical word the registered intents or the period patterns use (computed once, after registration)."""
    out = set(FILLER) | set(COMMON) | set(P.WORDS) | PHRASE_PARTS
    for item in INTENTS:
        out |= item.words
    return frozenset(out)


def analyze(question, as_of_date, catalog=None):
    """Reduce a question to canonical words and slots; None when it names an impossible date.

    Args:
        question: Raw question text.
        as_of_date: ISO date of "today".
        catalog: Optional entities.Catalog of the business; names it holds are replaced by slot tokens before the
            words are normalised.
    """
    tokens, entities = prepare(question).split(), []
    if catalog is not None:
        tokens, entities = catalog.mask(tokens)
    text = canonical_tokens(tokens, known_words())
    masked, found = P.extract(text, as_of_date)
    if found is None:
        return None
    words, numbers, following = [], [], []
    pieces = masked.split()
    for index, token in enumerate(pieces):
        match = _NUMBER.match(token)
        if match:
            numbers.append(Decimal(match[1].replace('.', '')))
            following.append(pieces[index + 1] if index + 1 < len(pieces) else '')
            words.append('@n')
        else:
            words.append(token)
    return Parsed(question, date.fromisoformat(as_of_date), words, found, numbers, following, entities)


def number(value):
    return {'type': 'number', 'answer': value}


def unanswerable(reason):
    return {'type': 'unanswerable', 'reason': reason}


NO_DATA = 'The asked period lies outside the dates the data covers.'


# ---- shared queries ------------------------------------------------------------------------------------------------

def amount_column(ctx):
    """Name of the order-level revenue column of semantic.eligible_orders (the goods amount when no revenue column)."""
    for column in ('revenue', 'order_goods_amount'):
        if ctx.has('semantic.eligible_orders', column):
            return column
    return None


def daily_revenue_relation(ctx):
    """(relation, amount column) to total revenue by day: sales lines, else rulebook-ready orders; None without either."""
    if ctx.has('semantic.eligible_sales_lines', 'line_amount', 'order_date'):
        return 'semantic.eligible_sales_lines', 'line_amount'
    if amount_column(ctx) and ctx.has('semantic.eligible_orders', 'order_date'):
        return 'semantic.eligible_orders', amount_column(ctx)
    return None


def revenue(ctx, first, last):
    """Rulebook revenue of [first, last]: line amounts of orders that count as sales; None when no source exists."""
    if ctx.has('semantic.eligible_sales_lines', 'line_amount', 'order_date'):
        row = ctx.one('SELECT SUM(line_amount) FROM semantic.eligible_sales_lines '
                      'WHERE CAST(order_date AS DATE) BETWEEN ? AND ?', first, last)
    elif amount_column(ctx) and ctx.has('semantic.eligible_orders', 'order_date'):
        row = ctx.one(f'SELECT SUM({amount_column(ctx)}) FROM semantic.eligible_orders '
                      'WHERE CAST(order_date AS DATE) BETWEEN ? AND ?', first, last)
    else:
        return None
    return Decimal(0) if row[0] is None else Decimal(str(row[0]))


def order_count(ctx, first, last):
    """Number of orders that count as sales in [first, last]; None when the bundle has no orders."""
    if not ctx.has('semantic.eligible_orders', 'order_date'):
        return None
    return ctx.one('SELECT COUNT(*) FROM semantic.eligible_orders WHERE CAST(order_date AS DATE) BETWEEN ? AND ?',
                   first, last)[0]


def _one_period(parsed):
    return parsed.periods[0]


# ---- intents -------------------------------------------------------------------------------------------------------

@intent('revenue', 'doanhthu tong cong ban duoc tien thu ve ra hang nam nay',
        lambda w, p: 'doanhthu' in w or ('tien' in w and bool({'ban', 'thu'} & w)))
def run_revenue(ctx, parsed):
    period = _one_period(parsed)
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    value = revenue(ctx, period.first, period.last)
    return None if value is None else number(whole(value))


@intent('orders', 'so don co tong cong ban duoc ra',
        lambda w, p: 'don' in w and not {'huy', 'tile'} & w)
def run_orders(ctx, parsed):
    period = _one_period(parsed)
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    value = order_count(ctx, period.first, period.last)
    return None if value is None else number(value)


@intent('cancelled_orders', 'so don huy bi da dem co tong cong trang thai o',
        lambda w, p: 'huy' in w and 'don' in w and not {'tile', 'phantram'} & w)
def run_cancelled(ctx, parsed):
    period = _one_period(parsed)
    if not ctx.has('semantic.orders', 'order_date', 'order_state'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    return number(ctx.one("SELECT COUNT(*) FROM semantic.orders WHERE order_state = 'cancelled' "
                          'AND CAST(order_date AS DATE) BETWEEN ? AND ?', period.first, period.last)[0])


@intent('cancellation_rate', 'tile don huy bi co phantram la',
        lambda w, p: 'huy' in w and 'don' in w and bool({'tile', 'phantram'} & w))
def run_cancellation_rate(ctx, parsed):
    period = _one_period(parsed)
    if not ctx.has('semantic.orders', 'order_date', 'order_state'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    cancelled, total = ctx.one("SELECT COUNT(*) FILTER (WHERE order_state = 'cancelled'), COUNT(*) FROM semantic.orders "
                               'WHERE CAST(order_date AS DATE) BETWEEN ? AND ?', period.first, period.last)
    value = percent(cancelled, total)
    return None if value is None else {'type': 'percent', 'answer': value}


@intent('average_order_value', 'trungbinh moi mot tren don mang ve doanhthu gia tri tong cong',
        lambda w, p: 'trungbinh' in w and 'don' in w)
def run_average_order(ctx, parsed):
    period = _one_period(parsed)
    if not amount_column(ctx) or not ctx.has('semantic.eligible_orders', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    total, count = ctx.one(f'SELECT SUM({amount_column(ctx)}), COUNT(*) FROM semantic.eligible_orders '
                           'WHERE CAST(order_date AS DATE) BETWEEN ? AND ?', period.first, period.last)
    if not count:
        return None
    return number(whole(Decimal(str(total)) / count))


@intent('best_day', 'ngay nao ban duoc nhieu tien nhat doanhthu cao dat cuahang',
        lambda w, p: 'nao' in w and 'nhat' in w and ('doanhthu' in w or 'tien' in w))
def run_best_day(ctx, parsed):
    period, source = _one_period(parsed), daily_revenue_relation(ctx)
    if source is None:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    row = ctx.one(f'SELECT CAST(order_date AS DATE) AS d FROM {source[0]} '
                  f'WHERE CAST(order_date AS DATE) BETWEEN ? AND ? GROUP BY 1 ORDER BY SUM({source[1]}) DESC, 1 LIMIT 1',
                  period.first, period.last)
    return None if row is None else {'type': 'date', 'answer': row[0].isoformat()}


# ---- comparison with an earlier period -----------------------------------------------------------------------------

def comparison_periods(parsed):
    """(asked, base) of a "compared with the earlier period" question, or None when the words leave it unclear."""
    words, today = parsed.words, parsed.today
    slots = iter(parsed.periods)
    mentions, after_against = [], []
    seen_against = False
    for word in words:
        if word == 'sovoi':
            seen_against = True
        elif word == '@t':
            mentions.append(next(slots))
            after_against.append(seen_against)
    year_ago = 'cungky' in words and 'nam' in words and bool({'truoc', 'ngoai', 'roi'} & set(words))
    named = [(m, a) for m, a in zip(mentions, after_against) if m.kind != 'last_month']
    cue_months = [m for m in mentions if m.kind == 'last_month']
    if len(mentions) == 2 and sum(m.base for m in mentions) == 1:
        return (next(m for m in mentions if not m.base), next(m for m in mentions if m.base))
    if len(named) == 1 and len(mentions) <= 2:
        asked = named[0][0]
        if cue_months and asked.kind not in ('month', 'this_month', 'month_to_date', 'quarter'):
            return None
        base = P.base_of(asked, today, year_ago=year_ago)
        return (asked, base) if base else None
    if len(named) == 2 and not cue_months:
        (a, a_after), (b, b_after) = named
        if a_after == b_after:
            return None
        return (a, b) if b_after else (b, a)
    return None


@intent('revenue_change', 'doanhthu tang giam hay thay doi sovoi so hon kem chenh phantram truong thang truoc lientruoc '
        'truocdo cungky nam ngoai roi mot nhieu tong cuahang dip am lich ngay truoc',
        lambda w, p: 'doanhthu' in w and 'phantram' in w and bool({'tang', 'giam', 'thay', 'hon', 'kem', 'chenh', 'truong'} & w),
        periods=(1, 3))
def run_revenue_change(ctx, parsed):
    pair = comparison_periods(parsed)
    if pair is None:
        return None
    asked, base = pair
    if ctx.outside(asked.first, asked.last) or ctx.outside(base.first, base.last):
        return unanswerable(NO_DATA)
    a, b = revenue(ctx, asked.first, asked.last), revenue(ctx, base.first, base.last)
    if a is None or b is None:
        return None
    value = percent(a - b, b)
    return None if value is None else {'type': 'percent', 'answer': value}


# ---- several months ------------------------------------------------------------------------------------------------

def month_list(parsed):
    """The calendar months a question lists or spans, oldest first as the question orders them; None when unclear.

    Accepted: every month named one by one (two or more), "từ tháng A đến tháng B" (a range of whole months), and
    "N tháng liền nhau kết thúc ở tháng B" (N months ending at B).
    """
    words = parsed.words
    # "tháng sau có luôn cao hơn tháng trước": the words "tháng trước" are a relation between the months, not a period.
    months = [m for m in parsed.periods if not ('sau' in words and m.kind == 'last_month')]
    if not months or any(m.kind != 'month' or not m.year_given for m in months):
        return None
    if len(months) == 1 and 'ketthuc' in words and len(parsed.numbers) == 1 and 2 <= parsed.numbers[0] <= 12:
        last = months[0].first
        return [P.month_at(last, -i) for i in range(int(parsed.numbers[0]) - 1, -1, -1)]
    if len(months) == 2 and 'tu' in words and bool({'den', 'toi'} & set(words)) and 'ketthuc' not in words and 'qua' not in words:
        a, b = months[0].first, months[1].first
        if a >= b:
            return None
        span = (b.year - a.year) * 12 + b.month - a.month
        return [P.month_at(a, i) for i in range(span + 1)]
    unique = list(dict.fromkeys((m.first, m.last) for m in months))
    if len(unique) >= 2 and 'ketthuc' not in words and len(parsed.numbers) <= 1:
        if parsed.numbers and parsed.numbers[0] != len(unique):
            return None
        return unique
    return None


def monthly_revenue(ctx, months):
    """Revenue of each month as whole money; None when a month lies outside the data or no source exists."""
    out = []
    for first, last in months:
        if ctx.outside(first, last):
            return None
        value = revenue(ctx, first, last)
        if value is None:
            return None
        out.append(value)
    return out


_MONTHS_WORDS = 'doanhthu thang cua va tu den toi cac ba moi lanluot ghi theo thutu do thoigian so lietke ke liennhau lientiep cu tung'


@intent('monthly_revenue', _MONTHS_WORDS + ' ketthuc o', lambda w, p: 'doanhthu' in w and bool({'moi', 'lanluot', 'ghi', 'lietke', 'ketthuc'} & w),
        periods=(1, 12), numbers=(0, 1))
def run_monthly_revenue(ctx, parsed):
    months = month_list(parsed)
    values = monthly_revenue(ctx, months) if months else None
    return None if values is None else {'type': 'list', 'answer': [whole(v) for v in values]}


@intent('best_month', _MONTHS_WORDS + ' nao nhat cao lon nhieu tien ban duoc cuahang vao', lambda w, p: 'thang' in w and 'nao' in w and 'nhat' in w
        or ('thang' in w and 'nao' in w and bool({'lon', 'cao'} & w)),
        periods=(2, 12), numbers=(0, 1))
def run_best_month(ctx, parsed):
    months = month_list(parsed)
    values = monthly_revenue(ctx, months) if months else None
    if values is None:
        return None
    top = max(values)
    if values.count(top) > 1:
        return None
    return {'type': 'date', 'answer': months[values.index(top)][0].strftime('%Y-%m')}


@intent('revenue_trend', _MONTHS_WORDS + ' co tang lientuc khong qua luon hon sau truoc ky la roi xu huong phai cao →',
        lambda w, p: 'doanhthu' in w and (bool({'lientuc', 'luon'} & w) and 'khong' in w or {'xu', 'huong'} <= w),
        periods=(2, 12), numbers=(0, 1))
def run_trend(ctx, parsed):
    months = month_list(parsed)
    values = monthly_revenue(ctx, months) if months else None
    if values is None:
        return None
    return {'type': 'boolean', 'answer': all(a < b for a, b in zip(values, values[1:]))}


# ---- morning and afternoon shifts ----------------------------------------------------------------------------------

def shift_rows(ctx, period):
    """{'morning': (revenue, orders, rev_vs_morning, orders_vs_morning), ...} from semantic.shift_summary; None if absent."""
    if 'semantic.shift_summary' not in ctx.functions:
        return None
    rows = ctx.rows('SELECT shift, revenue, orders, revenue_vs_morning_percent, orders_vs_morning_percent '
                    'FROM semantic.shift_summary(?, ?)', period.first, period.last)
    return {row[0]: row[1:] for row in rows}


_SHIFT_WORDS = 'casang cachieu doanhthu don so co tong cong ban duoc tien rieng trong cuahang dem hang'


@intent('shift_total', _SHIFT_WORDS, lambda w, p: bool({'casang', 'cachieu'} & w) and not {'casang', 'cachieu'} <= w
        and bool({'doanhthu', 'don', 'tien'} & w))
def run_shift_total(ctx, parsed):
    period = _one_period(parsed)
    words = set(parsed.words)
    if ('doanhthu' in words or 'tien' in words) and 'don' in words:
        return None
    rows = shift_rows(ctx, period)
    if rows is None:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    key = 'morning' if 'casang' in words else 'afternoon'
    row = rows.get(key)
    revenue_, orders_ = (row[0], row[1]) if row else (0, 0)
    return number(whole(revenue_) if 'don' not in words else orders_)


@intent('shift_comparison', _SHIFT_WORDS + ' sovoi chenh hon kem tang giam phantram thi hay', lambda w, p: {'casang', 'cachieu'} <= w
        and 'phantram' in w and bool({'sovoi', 'chenh', 'hon', 'kem', 'tang', 'giam'} & w))
def run_shift_comparison(ctx, parsed):
    period = _one_period(parsed)
    words = set(parsed.words)
    if ('doanhthu' in words) == ('don' in words):
        return None
    rows = shift_rows(ctx, period)
    if rows is None or 'afternoon' not in rows:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    value = rows['afternoon'][2 if 'doanhthu' in words else 3]
    return None if value is None else {'type': 'percent', 'answer': float(value)}


# ---- amounts that must be reached ----------------------------------------------------------------------------------

def operator(words):
    """The comparison a question states ('>=', '>', '<', '<='); None when absent or contradictory."""
    w = set(words)
    found = set()
    if w & {'gte', 'trolen', 'itnhat', 'dat'}:
        found.add('>=')
    if w & {'gt', 'tren', 'vuot', 'hon', 'qua'}:
        found.add('>')
    if w & {'lt', 'duoi'}:
        found.add('<')
    if w & {'lte', 'troxuong', 'toida'}:
        found.add('<=')
    return found.pop() if len(found) == 1 else None


_THRESHOLD_WORDS = 'dem so ma ca tu trolen troxuong dong gte lte gt lt tren duoi vuot dat itnhat hon qua toida co la'
_OPERATORS = {'>=': '>=', '>': '>', '<': '<', '<=': '<='}  # a whitelist: an operator is never taken from the text


@intent('days_over_amount', _THRESHOLD_WORDS + ' doanhthu ban duoc cuahang', lambda w, p: 'ngay' in w and bool({'doanhthu', 'ban'} & w)
        and 'don' not in w and 'nao' not in w, numbers=(1, 1))
def run_days_over(ctx, parsed):
    period, op, source = _one_period(parsed), operator(parsed.words), daily_revenue_relation(ctx)
    if op is None or source is None:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    # Only days inside the sales dates count: a period partly outside them is computed on the part that has data.
    period = P.Period(period.kind, max(period.first, ctx.coverage[0]), min(period.last, ctx.coverage[1]))
    sql = ('SELECT COUNT(*) FROM (SELECT CAST(d AS DATE) AS day FROM generate_series(?::DATE, ?::DATE, INTERVAL 1 DAY) t(d)) '
           f'LEFT JOIN (SELECT CAST(order_date AS DATE) AS day, SUM({source[1]}) AS total FROM {source[0]} '
           'GROUP BY 1) USING (day) '
           f'WHERE COALESCE(total, 0) {_OPERATORS[op]} ?')
    return number(ctx.one(sql, period.first, period.last, parsed.numbers[0])[0])


@intent('orders_over_amount', _THRESHOLD_WORDS + ' don tien hang doanhthu', lambda w, p: 'don' in w and bool({'tien', 'doanhthu'} & w),
        numbers=(1, 1))
def run_orders_over(ctx, parsed):
    period, op = _one_period(parsed), operator(parsed.words)
    if op is None or not amount_column(ctx) or not ctx.has('semantic.eligible_orders', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    return number(ctx.one('SELECT COUNT(*) FROM semantic.eligible_orders WHERE CAST(order_date AS DATE) BETWEEN ? AND ? '
                          f'AND {amount_column(ctx)} {_OPERATORS[op]} ?', period.first, period.last, parsed.numbers[0])[0])


# ---- products, groups and units ------------------------------------------------------------------------------------

def _entity(parsed, kind):
    return next(e for e in parsed.entities if e.kind == kind)


def quantity(value):
    """A quantity as an int when whole, else a float."""
    value = Decimal(str(value))
    return int(value) if value == value.to_integral_value() else float(value)


def product_sum(ctx, column, first, last, name):
    """Sum of a sales-line column over one product's counted sales in [first, last]; None without the columns."""
    relation = 'semantic.product_quantities' if column == 'base_quantity' else 'semantic.eligible_sales_lines'
    if not ctx.has(relation, column, 'product_name', 'order_date'):
        return None
    row = ctx.one(f'SELECT SUM({column}) FROM {relation} WHERE product_name = ? '
                  'AND CAST(order_date AS DATE) BETWEEN ? AND ?', name, first, last)
    return Decimal(0) if row[0] is None else Decimal(str(row[0]))


@intent('product_revenue', 'doanhthu cua tong cong ban duoc tien thu ve ra @p mathang hang mon',
        lambda w, p: '@p' in w and ('doanhthu' in w or 'tien' in w), entities=('product',), product_level=True)
def run_product_revenue(ctx, parsed):
    period, product = _one_period(parsed), _entity(parsed, 'product')
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    value = product_sum(ctx, 'line_amount', period.first, period.last, product.name)
    return None if value is None else number(whole(value))


@intent('product_quantity', 'so ban duoc tong cong ra @p @u mathang hang mon',
        lambda w, p: '@p' in w and '@u' in w, entities=('product', 'unit'), product_level=True)
def run_product_quantity(ctx, parsed):
    period, product, unit = _one_period(parsed), _entity(parsed, 'product'), _entity(parsed, 'unit')
    if not product.unit or prepare(unit.name) != product.unit:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    value = product_sum(ctx, 'base_quantity', period.first, period.last, product.name)
    return None if value is None else number(quantity(value))


@intent('unit_total', 'so ban duoc tong cong ra @u cong moi mathang mon tinh theo',
        lambda w, p: '@u' in w, entities=('unit',))
def run_unit_total(ctx, parsed):
    period, unit = _one_period(parsed), _entity(parsed, 'unit')
    if not ctx.has('semantic.product_quantities', 'base_quantity', 'base_unit', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    row = ctx.one('SELECT SUM(base_quantity) FROM semantic.product_quantities WHERE base_unit = ? '
                  'AND CAST(order_date AS DATE) BETWEEN ? AND ?', unit.name, period.first, period.last)
    return number(quantity(row[0] or 0))


@intent('size_total', 'so ban duoc tong cong ra @u @z cong moi mon mathang ca cac tat loai uong do',
        lambda w, p: '@u' in w and '@z' in w, entities=('unit', 'size'), product_level=True)
def run_size_total(ctx, parsed):
    period, size = _one_period(parsed), _entity(parsed, 'size')
    if not ctx.has('semantic.product_quantities', 'base_quantity', 'product_name', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    row = ctx.one("SELECT SUM(base_quantity) FROM semantic.product_quantities WHERE lower(product_name) LIKE ? "
                  'AND CAST(order_date AS DATE) BETWEEN ? AND ?', f'%({size.name})', period.first, period.last)
    return number(quantity(row[0] or 0))


@intent('group_revenue', 'doanhthu cua tong cong ban duoc tien thu ve ra @g cac mathang hang',
        lambda w, p: '@g' in w and ('doanhthu' in w or 'tien' in w) and 'phantram' not in w, entities=('group',), product_level=True)
def run_group_revenue(ctx, parsed):
    period, group = _one_period(parsed), _entity(parsed, 'group')
    if not ctx.has('semantic.eligible_sales_lines', 'line_amount', 'product_group', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    row = ctx.one('SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE product_group = ? '
                  'AND CAST(order_date AS DATE) BETWEEN ? AND ?', group.name, period.first, period.last)
    return number(whole(row[0] or 0))


@intent('group_share', 'doanhthu cua tong chiem phantram trong @g dong gop cua ca tiem tile titrong ti',
        lambda w, p: '@g' in w and 'doanhthu' in w and 'phantram' in w, entities=('group',), periods=(1, 2), repeat=True, product_level=True)
def run_group_share(ctx, parsed):
    period, group = _one_period(parsed), _entity(parsed, 'group')
    if 'semantic.group_revenue_share' not in ctx.functions:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    row = ctx.one('SELECT share_percent FROM semantic.group_revenue_share(?, ?) WHERE product_group = ?',
                  period.first, period.last, group.name)
    return {'type': 'percent', 'answer': float(row[0])} if row and row[0] is not None else None


@intent('top_products', 'top chay nhat ban nhieu hot mathang hang mon la gi chi nhung nao lietke cho cac @n',
        lambda w, p: bool({'chay', 'hot', 'top'} & w) or ('ban' in w and 'nhieu' in w and 'nhat' in w and bool({'mathang', 'hang', 'mon'} & w)),
        numbers=(0, 1), product_level=True)
def run_top_products(ctx, parsed):
    period = _one_period(parsed)
    if 'semantic.top_products' not in ctx.functions:
        return None
    k = int(parsed.numbers[0]) if parsed.numbers else 1
    if not 1 <= k <= 20:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    rows = ctx.rows('SELECT product_name FROM semantic.top_products(?, ?) WHERE rank <= ? ORDER BY rank',
                    period.first, period.last, k)
    names = [r[0] for r in rows]
    if len(names) != k:
        return None
    if not parsed.numbers:
        return {'type': 'string', 'answer': names[0]}
    return {'type': 'list', 'answer': names}


@intent('unsold_products', 'baonhieu unsold han khong khong ban duoc ai mua mon cai nao co tong cong ra mathang ban',
        lambda w, p: 'unsold' in w or ({'khong', 'mua'} <= w and 'mathang' in w), product_level=True)
def run_unsold(ctx, parsed):
    period = _one_period(parsed)
    if 'semantic.unsold_products' not in ctx.functions:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    return number(ctx.one('SELECT COUNT(*) FROM semantic.unsold_products(?, ?)', period.first, period.last)[0])


@intent('products_in_orders', 'baonhieu mathang xuathien itnhat @n don khacnhau trolen duoc mua o co tong it nhat tu mat gte dem so la',
        lambda w, p: 'mathang' in w and bool({'xuathien', 'mua', 'mat'} & w) and 'don' in w, numbers=(1, 1), product_level=True)
def run_products_in_orders(ctx, parsed):
    period, op = _one_period(parsed), operator(parsed.words)
    if op != '>=' or not ctx.has('semantic.eligible_sales_lines', 'order_id', 'product_name', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    return number(ctx.one("SELECT COUNT(*) FROM (SELECT product_name FROM semantic.eligible_sales_lines "
                          "WHERE CAST(order_date AS DATE) BETWEEN ? AND ? AND product_name IS NOT NULL "
                          "AND lower(strip_accents(product_name)) <> 'phi phuc vu' "
                          'GROUP BY product_name HAVING COUNT(DISTINCT order_id) >= ?)',
                          period.first, period.last, parsed.numbers[0])[0])


@intent('price_change_effect', 'so ban ra trong ketu ngay tanggia len tu voi ngaytruoc ngay do luong tinh theo @u @p @n @t '
        'hay giam tang nhieu it hon thay doi phantram lientruoc truoc truocdo duoc sovoi vao ke dong',
        lambda w, p: '@p' in w and 'tanggia' in w and 'phantram' in w, periods=(1, 2), numbers=(2, 6), entities=('product',),
        optional=('unit',), product_level=True)
def run_price_change(ctx, parsed):
    day = parsed.periods[0]
    if day.kind != 'day' or any(p != day for p in parsed.periods):
        return None
    sizes = [int(n) for n, nxt in zip(parsed.numbers, parsed.next_word) if nxt == 'ngay']
    if len(sizes) != 2 or sizes[0] != sizes[1] or not 1 <= sizes[0] <= 90:
        return None
    n, product = sizes[0], _entity(parsed, 'product')
    after = product_sum(ctx, 'base_quantity', day.first, day.first + timedelta(days=n - 1), product.name)
    before = product_sum(ctx, 'base_quantity', day.first - timedelta(days=n), day.first - timedelta(days=1), product.name)
    if after is None or before is None or not before:
        return None
    return {'type': 'percent', 'answer': percent(after - before, before)}


# ---- customers -----------------------------------------------------------------------------------------------------

_CUSTOMER_HINTS = ('khach', 'customer', 'kh_', '_kh', 'ma_kh', 'ten_kh')
NOT_RECORDED = 'The data does not record this information.'


def customer_amount(ctx):
    """Order-level amount column when semantic.eligible_orders identifies customers, else None."""
    if ctx.has('semantic.eligible_orders', 'customer_key', 'customer_name', 'order_date'):
        return amount_column(ctx)
    return None


def no_customers(ctx):
    """True when the bundle has rulebook-ready orders and nothing in its sources looks like a customer."""
    return (ctx.has('semantic.eligible_orders', 'order_date') and customer_amount(ctx) is None
            and not ctx.source_mentions(_CUSTOMER_HINTS))


_CUSTOMER_WORDS = 'khach hang ai la mua nhieu tien nhat chi tieu cao tong lietke top cho nao @n co'


@intent('top_customers_by_money', _CUSTOMER_WORDS, lambda w, p: ('khach' in w or {'ai', 'mua'} <= w) and bool({'nhat', 'top'} & w)
        and bool({'tien', 'tieu'} & w) and not {'don', 'lan'} & w, numbers=(0, 1))
def run_top_customers(ctx, parsed):
    period, amount = _one_period(parsed), customer_amount(ctx)
    k = int(parsed.numbers[0]) if parsed.numbers else 1
    if no_customers(ctx):
        return unanswerable(NOT_RECORDED)
    if amount is None or not 1 <= k <= 20:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    rows = ctx.rows(f'SELECT MIN(customer_name) FROM semantic.eligible_orders WHERE customer_key IS NOT NULL '
                    f'AND CAST(order_date AS DATE) BETWEEN ? AND ? GROUP BY customer_key '
                    f'ORDER BY SUM({amount}) DESC, customer_key LIMIT {k}', period.first, period.last)
    names = [r[0] for r in rows]
    if len(names) != k or None in names:
        return None
    return {'type': 'string', 'answer': names[0]} if not parsed.numbers else {'type': 'list', 'answer': names}


@intent('top_customer_by_orders', 'khach hang ai la mua nhieu don lan nhat tinh theo so cho nao',
        lambda w, p: 'khach' in w and 'nhat' in w and bool({'don', 'lan'} & w) and not {'tien', 'tieu'} & w)
def run_top_customer_orders(ctx, parsed):
    period = _one_period(parsed)
    if no_customers(ctx):
        return unanswerable(NOT_RECORDED)
    if customer_amount(ctx) is None:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    rows = ctx.rows('SELECT MIN(customer_name) FROM semantic.eligible_orders WHERE customer_key IS NOT NULL '
                    'AND CAST(order_date AS DATE) BETWEEN ? AND ? GROUP BY customer_key '
                    'ORDER BY COUNT(*) DESC, customer_key LIMIT 2', period.first, period.last)
    return {'type': 'string', 'answer': rows[0][0]} if rows and rows[0][0] else None


_MIN_ORDERS_WORDS = 'khach mua tu @n don trolen itnhat co baonhieu so la hang da'


@intent('customers_with_min_orders', _MIN_ORDERS_WORDS, lambda w, p: 'khach' in w and 'don' in w and 'phantram' not in w,
        numbers=(1, 1))
def run_customers_min_orders(ctx, parsed):
    period = _one_period(parsed)
    if operator(parsed.words) != '>=':
        return None
    if no_customers(ctx):
        return unanswerable(NOT_RECORDED)
    if customer_amount(ctx) is None:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    return number(ctx.one('SELECT COUNT(*) FROM (SELECT customer_key FROM semantic.eligible_orders '
                          'WHERE customer_key IS NOT NULL AND CAST(order_date AS DATE) BETWEEN ? AND ? '
                          'GROUP BY customer_key HAVING COUNT(*) >= ?)', period.first, period.last, parsed.numbers[0])[0])


@intent('customers_min_orders_share', _MIN_ORDERS_WORDS + ' chiem phantram tile trong',
        lambda w, p: 'khach' in w and 'don' in w and 'phantram' in w, numbers=(1, 1))
def run_customers_share(ctx, parsed):
    period = _one_period(parsed)
    if operator(parsed.words) != '>=':
        return None
    if no_customers(ctx):
        return unanswerable(NOT_RECORDED)
    if customer_amount(ctx) is None:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    many, total = ctx.one('SELECT COUNT(*) FILTER (WHERE n >= ?), COUNT(*) FROM (SELECT COUNT(*) AS n '
                          'FROM semantic.eligible_orders WHERE customer_key IS NOT NULL '
                          'AND CAST(order_date AS DATE) BETWEEN ? AND ? GROUP BY customer_key)',
                          parsed.numbers[0], period.first, period.last)
    value = percent(many, total)
    return None if value is None else {'type': 'percent', 'answer': value}


# ---- debt ----------------------------------------------------------------------------------------------------------

_DEBT_WORDS = 'khach con no cua hang tong cong baonhieu tien tinh den het ngay cong so la chua tra thieu voi va toi dang'


def _debt_day(ctx, parsed):
    """The day a debt question is asked at, when the bundle publishes debt views; else None."""
    if 'semantic.debt_as_of' not in ctx.functions or len(parsed.periods) != 1 or parsed.periods[0].kind != 'day':
        return None
    return parsed.periods[0].first


@intent('total_debt', _DEBT_WORDS, lambda w, p: bool({'no', 'thieu'} & w) and 'khach' in w and bool({'tien', 'tong'} & w)
        and 'khach' in w and not any(a == 'baonhieu' and b == 'khach' for a, b in zip(p.words, p.words[1:])))
def run_total_debt(ctx, parsed):
    day = _debt_day(ctx, parsed)
    if day is None:
        return None
    return number(whole(ctx.one('SELECT COALESCE(SUM(outstanding), 0) FROM semantic.debt_as_of(?)', day)[0]))


@intent('debtor_count', _DEBT_WORDS + ' hoi', lambda w, p: bool({'no', 'thieu'} & w) and 'khach' in w and 'tien' not in w and 'nhat' not in w
        and any((a in ('baonhieu', 'so') and b == 'khach') for a, b in zip(p.words, p.words[1:])))
def run_debtor_count(ctx, parsed):
    day = _debt_day(ctx, parsed)
    if day is None or 'customer_id' not in ctx.columns.get('semantic.debt_as_of', set()):
        return None
    return number(ctx.one('SELECT COUNT(DISTINCT customer_id) FROM semantic.debt_as_of(?) WHERE customer_id IS NOT NULL', day)[0])


@intent('top_debtors', _DEBT_WORDS + ' cho bang top ai nao dang nhieu nhat lon kem moi nguoi tung nhung lietke @n',
        lambda w, p: 'no' in w and 'khach' in w and 'nhat' in w or ('no' in w and 'lon' in w and 'khach' in w), numbers=(1, 1))
def run_top_debtors(ctx, parsed):
    day, k = _debt_day(ctx, parsed), int(parsed.numbers[0])
    if day is None or not 1 <= k <= 20 or not ctx.has('semantic.orders', 'order_id', 'customer_name'):
        return None
    if 'customer_id' not in ctx.columns.get('semantic.debt_as_of', set()):
        return None
    rows = ctx.rows('SELECT MIN(o.customer_name), SUM(d.outstanding) FROM semantic.debt_as_of(?) d '
                    'JOIN semantic.orders o USING (order_id) WHERE d.customer_id IS NOT NULL GROUP BY d.customer_id '
                    f'ORDER BY SUM(d.outstanding) DESC, d.customer_id LIMIT {k}', day)
    if len(rows) != k or any(r[0] is None for r in rows):
        return None
    return {'type': 'table', 'answer': {'columns': ['khach_hang', 'so_tien_no'], 'rows': [[r[0], whole(r[1])] for r in rows]}}


@intent('debt_collected', 'tien khach tra no bot cac don mua chiu duoc cong thu hoi tu cua hang tong cong baonhieu ve',
        lambda w, p: 'no' in w and bool({'tra', 'hoi', 'thu'} & w) and '@k' not in w)
def run_debt_collected(ctx, parsed):
    period = _one_period(parsed)
    if not ctx.has('semantic.debt_repayments', 'repayment_date', 'amount'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    return number(whole(ctx.one('SELECT COALESCE(SUM(amount), 0) FROM semantic.debt_repayments '
                                'WHERE repayment_date BETWEEN ? AND ?', period.first, period.last)[0]))


@intent('customer_debt_flow', 'khach @k ghi no them baonhieu va tra duoc da bot mua chiu nhieu thutu nhu cua hang co tien '
        'tra loi hai so ghi',
        lambda w, p: '@k' in w and 'no' in w and 'them' in w and 'tra' in w, entities=('customer',))
def run_customer_debt_flow(ctx, parsed):
    period, customer = _one_period(parsed), _entity(parsed, 'customer')
    words = parsed.words
    if not (ctx.has('semantic.debt_added', 'customer_id', 'order_date') and ctx.has('semantic.debt_repayments', 'customer_id')):
        return None
    if words.index('them') > words.index('tra'):
        return None
    ids = ctx.rows("SELECT DISTINCT customer_id FROM semantic.orders WHERE customer_id IS NOT NULL AND "
                   "replace(lower(strip_accents(trim(regexp_replace(customer_name, '\\s+', ' ', 'g')))), 'đ', 'd') = ?",
                   ' '.join(prepare(customer.name).split()))
    if len(ids) != 1:
        return None
    key = ids[0][0]
    added = ctx.one('SELECT COALESCE(SUM(added_debt), 0) FROM semantic.debt_added WHERE customer_id = ? '
                    'AND order_date BETWEEN ? AND ?', key, period.first, period.last)[0]
    repaid = ctx.one('SELECT COALESCE(SUM(amount), 0) FROM semantic.debt_repayments WHERE customer_id = ? '
                     'AND repayment_date BETWEEN ? AND ?', key, period.first, period.last)[0]
    return {'type': 'list', 'answer': [whole(added), whole(repaid)]}


# ---- stock ---------------------------------------------------------------------------------------------------------

_STOCK_WORDS = 'ton kho cuoi ngay hom nay trong con lai het la baonhieu @p @u luc duoc mathang hang tinh den may kho va'


def _stock_rows(ctx, parsed):
    """Rows of semantic.stock_snapshot for the named product when the question is about its stock date, else None."""
    if not ctx.has('semantic.stock_snapshot', 'product_name', 'stock_base_units', 'stock_date') or len(parsed.periods) != 1:
        return None
    day, product = parsed.periods[0], _entity(parsed, 'product')
    if day.kind != 'day':
        return None
    rows = ctx.rows('SELECT stock_base_units, stock_date, whole_large_units, loose_base_units FROM semantic.stock_snapshot '
                    'WHERE product_name = ?' if ctx.has('semantic.stock_snapshot', 'whole_large_units') else
                    'SELECT stock_base_units, stock_date, NULL, NULL FROM semantic.stock_snapshot WHERE product_name = ?',
                    product.name)
    if len(rows) != 1 or rows[0][1] != day.first:
        return None
    return rows[0]


@intent('stock_units', _STOCK_WORDS, lambda w, p: '@p' in w and bool({'ton', 'kho'} & w) and 'chan' not in w,
        entities=('product',), optional=('unit',))
def run_stock_units(ctx, parsed):
    row = _stock_rows(ctx, parsed)
    return None if row is None or row[0] is None else number(quantity(row[0]))


@intent('stock_large_units', _STOCK_WORDS + ' chan le', lambda w, p: '@p' in w and 'chan' in w and bool({'ton', 'kho'} & w),
        entities=('product',), optional=('unit',))
def run_stock_large(ctx, parsed):
    row = _stock_rows(ctx, parsed)
    if row is None or row[2] is None:
        return None
    return {'type': 'list', 'answer': [quantity(row[2]), quantity(row[3])]}


# ---- sales channel and payment method ------------------------------------------------------------------------------

_CHANNELS = {'chphone': {'phone', 'dien thoai'}, 'chstore': {'store', 'tai quay', 'quay'}, 'chzalo': {'zalo oa', 'zalo_oa', 'zalo'}}
_PAYMENTS = {'pcash': {'cash', 'tien mat'}, 'pcod': {'cod', 'thu ho cod', 'thu ho'},
             'ptransfer': {'transfer', 'chuyen khoan'}, 'pewallet': {'ewallet', 'vi dien tu', 'e wallet'}}
_BY_ORDER_WORDS = ('doanhthu cac don ban qua tra bang tong chiem phantram trong moi loai baonhieu va so lanluot sovoi '
                   'bao la cho tile cua mot nhieu tien ti titrong ben')


def _label(ctx, column, wanted):
    """The bundle's own label for a channel or payment method, when exactly one matches."""
    if not ctx.has('semantic.eligible_orders', column):
        return None
    found = [r[0] for r in ctx.rows(f'SELECT DISTINCT {column} FROM semantic.eligible_orders WHERE {column} IS NOT NULL')
             if ' '.join(prepare(r[0]).split()) in wanted]
    return found[0] if len(found) == 1 else None


_HINTS = {'channel': ('kenh', 'channel', 'nguon'), 'payment_method': ('thanh_toan', 'payment', 'hinh_thuc', 'pttt')}


def _by_order(ctx, parsed, table, column):
    """(period, labels in question order) for a channel or payment question, or None when a label is not in the bundle.

    Returns the string 'absent' when the bundle has rulebook-ready orders but neither the column nor any source column
    that looks like it: the data does not record it.
    """
    period = _one_period(parsed)
    if (ctx.has('semantic.eligible_orders', 'order_date') and not ctx.has('semantic.eligible_orders', column)
            and not ctx.source_mentions(_HINTS[column])):
        return 'absent'
    marks = [w for w in parsed.words if w in table]
    labels = [_label(ctx, column, table[w]) for w in marks]
    if not marks or len(set(marks)) != len(marks) or None in labels or amount_column(ctx) is None:
        return None
    return period, labels


def _order_part(ctx, column, label, period):
    return Decimal(str(ctx.one(f'SELECT COALESCE(SUM({amount_column(ctx)}), 0) FROM semantic.eligible_orders '
                               f'WHERE {column} = ? AND CAST(order_date AS DATE) BETWEEN ? AND ?',
                               label, period.first, period.last)[0]))


def _order_total(ctx, period):
    return Decimal(str(ctx.one(f'SELECT COALESCE(SUM({amount_column(ctx)}), 0) FROM semantic.eligible_orders '
                               'WHERE CAST(order_date AS DATE) BETWEEN ? AND ?', period.first, period.last)[0]))


def _order_group(table, column, name, prefix):
    marks = frozenset(table)

    @intent(name, _BY_ORDER_WORDS + ' ' + ' '.join(table), lambda w, p: bool(w & marks) and not w & _OTHER[prefix]
            and bool({'doanhthu', 'tien'} & w), periods=(1, 2), repeat=True)
    def run(ctx, parsed):
        found = _by_order(ctx, parsed, table, column)
        if found is None:
            return None
        if found == 'absent':
            return unanswerable(NOT_RECORDED)
        period, labels = found
        if ctx.outside(period.first, period.last):
            return unanswerable(NO_DATA)
        parts = [_order_part(ctx, column, label, period) for label in labels]
        if 'phantram' in parsed.words:
            if len(parts) != 1:
                return None
            value = percent(parts[0], _order_total(ctx, period))
            return None if value is None else {'type': 'percent', 'answer': value}
        if len(parts) == 1:
            return number(whole(parts[0]))
        return {'type': 'list', 'answer': [whole(v) for v in parts]}
    return run


_OTHER = {'channel': frozenset(_PAYMENTS), 'payment': frozenset(_CHANNELS)}
_order_group(_CHANNELS, 'channel', 'channel_revenue', 'channel')
_order_group(_PAYMENTS, 'payment_method', 'payment_revenue', 'payment')


# ---- rankings the split's rulebook defines by name -------------------------------------------------------------------
# Defined terms of the public rulebook (section 3): the loyal customer has the most days with a purchase, the big
# spender the highest average order among customers with at least 3 orders, the fickle customer the most cancelled
# orders, the favourite product is in the most orders, the busiest day has the most orders, the peak hour is the
# whole hour of order time with the most orders (answered by its starting hour). Ties go to the lower code.

_RANK_WORDS = 'khach hang ai la nhat trong cho cuahang nao mat cua'


def _customer_winner(ctx, sql, period, *args):
    row = ctx.one(sql, period.first, period.last, *args)
    return None if row is None or row[0] is None else {'type': 'string', 'answer': row[0]}


@intent('loyal_customer', _RANK_WORDS + ' trungthanh', lambda w, p: 'trungthanh' in w and bool({'khach', 'ai'} & w))
def run_loyal_customer(ctx, parsed):
    period = _one_period(parsed)
    if no_customers(ctx):
        return unanswerable(NOT_RECORDED)
    if customer_amount(ctx) is None:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    return _customer_winner(ctx, 'SELECT MIN(customer_name) FROM semantic.eligible_orders WHERE customer_key IS NOT NULL '
                                 'AND CAST(order_date AS DATE) BETWEEN ? AND ? GROUP BY customer_key '
                                 'ORDER BY COUNT(DISTINCT CAST(order_date AS DATE)) DESC, customer_key LIMIT 1', period)


@intent('big_spender', _RANK_WORDS + ' sop', lambda w, p: 'sop' in w and bool({'khach', 'ai'} & w))
def run_big_spender(ctx, parsed):
    period, amount = _one_period(parsed), customer_amount(ctx)
    if no_customers(ctx):
        return unanswerable(NOT_RECORDED)
    if amount is None:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    return _customer_winner(ctx, 'SELECT MIN(customer_name) FROM semantic.eligible_orders WHERE customer_key IS NOT NULL '
                                 'AND CAST(order_date AS DATE) BETWEEN ? AND ? GROUP BY customer_key HAVING COUNT(*) >= 3 '
                                 f'ORDER BY SUM({amount})::DOUBLE / COUNT(*) DESC, customer_key LIMIT 1', period)


@intent('fickle_customer', _RANK_WORDS + ' doiy hay', lambda w, p: 'doiy' in w and bool({'khach', 'ai'} & w))
def run_fickle_customer(ctx, parsed):
    period = _one_period(parsed)
    if not ctx.has('semantic.orders', 'customer_key', 'customer_name', 'order_state', 'order_date'):
        return unanswerable(NOT_RECORDED) if no_customers(ctx) else None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    return _customer_winner(ctx, "SELECT MIN(customer_name) FROM semantic.orders WHERE customer_key IS NOT NULL "
                                 "AND order_state = 'cancelled' AND CAST(order_date AS DATE) BETWEEN ? AND ? "
                                 'GROUP BY customer_key ORDER BY COUNT(*) DESC, customer_key LIMIT 1', period)


@intent('favourite_product', _RANK_WORDS + ' uuchuong mathang',
        lambda w, p: 'uuchuong' in w and 'mathang' in w, product_level=True)
def run_favourite_product(ctx, parsed):
    period = _one_period(parsed)
    if not ctx.has('semantic.eligible_sales_lines', 'order_id', 'product_name', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    code = 'product_id' if ctx.has('semantic.eligible_sales_lines', 'product_id') else 'product_name'
    row = ctx.one(f"SELECT MIN(product_name) FROM semantic.eligible_sales_lines WHERE product_name IS NOT NULL "
                  "AND lower(strip_accents(product_name)) <> 'phi phuc vu' AND CAST(order_date AS DATE) BETWEEN ? AND ? "
                  f'GROUP BY {code} ORDER BY COUNT(DISTINCT order_id) DESC, {code} LIMIT 1', period.first, period.last)
    return {'type': 'string', 'answer': row[0]} if row and row[0] else None


@intent('busiest_day', _RANK_WORDS + ' ngay datkhach', lambda w, p: 'datkhach' in w and 'ngay' in w)
def run_busiest_day(ctx, parsed):
    period = _one_period(parsed)
    if not ctx.has('semantic.eligible_orders', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    row = ctx.one('SELECT CAST(order_date AS DATE) FROM semantic.eligible_orders '
                  'WHERE CAST(order_date AS DATE) BETWEEN ? AND ? GROUP BY 1 ORDER BY COUNT(*) DESC, 1 LIMIT 1',
                  period.first, period.last)
    return {'type': 'date', 'answer': row[0].isoformat()} if row else None


@intent('peak_hour', _RANK_WORDS + ' gio caodiem', lambda w, p: 'caodiem' in w and 'gio' in w)
def run_peak_hour(ctx, parsed):
    period = _one_period(parsed)
    if not ctx.has('semantic.eligible_orders', 'order_date', 'created_at'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    row = ctx.one('SELECT hour(created_at) FROM semantic.eligible_orders WHERE created_at IS NOT NULL '
                  'AND CAST(order_date AS DATE) BETWEEN ? AND ? GROUP BY 1 ORDER BY COUNT(*) DESC, 1 LIMIT 1',
                  period.first, period.last)
    return number(row[0]) if row else None


def resolve_tet(ctx, parsed):
    """Give the Tết periods of a parsed question explicit dates from the bundle's lunar calendar.

    Returns:
        The Parsed with tet and tet_before periods replaced by ranges, the same Parsed when it has none, or None when
        the calendar is missing, a Tết is not in it, or "N days before Tết" does not span the length of that Tết.
    """
    if not any(p.kind in ('tet', 'tet_before') for p in parsed.periods):
        return parsed
    if not ctx.has('clean.calendar', 'date', 'lunar_day', 'lunar_month', 'lunar_year', 'lunar_leap'):
        return None
    tets = [P.tet_range(ctx.connection, p.year) if p.kind == 'tet' else None for p in parsed.periods]
    resolved = []
    for period, span in zip(parsed.periods, tets):
        if period.kind == 'tet':
            if span is None:
                return None
            resolved.append(P.Period('range', span[0], span[1]))
        elif period.kind == 'tet_before':
            owners = [s for s in tets if s]
            if len(owners) != 1 or period.days != (owners[0][1] - owners[0][0]).days + 1:
                return None
            resolved.append(P.Period('range', owners[0][0] - timedelta(days=period.days),
                                     owners[0][0] - timedelta(days=1), base=True))
        else:
            resolved.append(period)
    return Parsed(parsed.question, parsed.today, parsed.words, resolved, parsed.numbers, parsed.next_word, parsed.entities)


# What no shop records: asking for it is asking for something the data cannot support (rulebook section 7). Each
# entry is a phrase pattern on the accent-folded question and the source-column names that would make the question
# answerable after all; a bundle with such a column is left to the model. "Đường" is also sugar, so the street
# patterns need "ở đường" or "khách đường" and never match "mua đường".
_ABSENT = (
    ('the street a customer lives in', r'\b(?:o|nha o|tai) duong\b|\bkhach(?: hang)? duong\b',
     ('dia_chi', 'addr', 'address', 'duong', 'khu_vuc', 'street')),
    ('the gender of a customer', r'\bkhach(?: hang)? (?:nam|nu)\b', ('gioi_tinh', 'gender', 'sex')),
    ('how customers or staff rate each other',
     r'\b(?:le phep|tan tam|chu dao|lanh nghe|tay nghe|quy men)\b|\bkhach hay khen\b|\bkhen nhan vien\b',
     ('danh_gia', 'khen', 'rating', 'feedback', 'hai_long')),
    ('the reason of a return or a cancellation', r'\b(?:vi loi|mua nham|giao tre)\b', ('ly_do', 'reason')),
)


def absent_attribute(question, ctx):
    """An unanswerable answer for a question about information no shop records, when the bundle has no such column."""
    folded = ' ' + prepare(question) + ' '
    for label, pattern, hints in _ABSENT:
        if re.search(pattern, folded) and not ctx.source_mentions(hints):
            return unanswerable(f'The data does not record {label}.')
    return None


def has_toppings_in_names(ctx):
    """True when product names of the sales lines join a dish and its toppings with " + " (cached per bundle)."""
    if not hasattr(ctx, 'toppings_in_names'):
        ctx.toppings_in_names = (ctx.has('semantic.eligible_sales_lines', 'product_name') and ctx.one(
            "SELECT COUNT(*) FROM (SELECT product_name FROM semantic.eligible_sales_lines WHERE product_name LIKE '% + %' "
            'LIMIT 1)')[0] > 0)
    return ctx.toppings_in_names


def _spec(parsed):
    """What the intent understood, for the log: the periods, names and numbers it used."""
    return {'periods': [[p.first.isoformat(), p.last.isoformat()] for p in parsed.periods],
            'entities': [[e.kind, e.name] for e in parsed.entities or []],
            'numbers': [str(n) for n in parsed.numbers]}


def answer(question, as_of_date, ctx):
    """Answer a question by the first intent that explains it.

    Returns:
        (answer dict, intent name, spec) or None when no intent claims the question or the claimed intent cannot
        answer it from this bundle. The spec lists the periods (ISO first and last day), names and numbers the intent
        read from the question, so that a log shows what was understood. None means: leave the question to the model.
    """
    refusal = absent_attribute(question, ctx)
    if refusal is not None:
        return refusal, 'absent_attribute', {'periods': [], 'entities': [], 'numbers': []}
    if not hasattr(ctx, 'catalog'):
        ctx.catalog = Catalog(ctx)
    plain = analyze(question, as_of_date)
    named = analyze(question, as_of_date, ctx.catalog) if (ctx.catalog.products or ctx.catalog.groups or ctx.catalog.units) else None
    for item in INTENTS:
        if item.product_level and has_toppings_in_names(ctx):
            continue
        parsed = named if item.entities else plain
        if parsed is not None and any(p.kind in ('tet', 'tet_before') for p in parsed.periods):
            parsed = resolve_tet(ctx, parsed)
        if parsed is None:
            continue
        if item.repeat and len(parsed.periods) > 1 and all(p == parsed.periods[0] for p in parsed.periods):
            parsed = Parsed(parsed.question, parsed.today, parsed.words, parsed.periods[:1], parsed.numbers,
                            parsed.next_word, parsed.entities)
        words = set(parsed.words)
        if not item.need(words, parsed):
            continue
        if not item.periods[0] <= len(parsed.periods) <= item.periods[1]:
            continue
        if not item.numbers[0] <= len(parsed.numbers) <= item.numbers[1]:
            continue
        kinds = tuple(sorted({(e.kind, e.name) for e in (parsed.entities or []) if e.kind not in item.optional}))
        if tuple(kind for kind, _ in kinds) != item.entities:
            continue
        if words - item.words - FILLER - COMMON - SLOTS:
            continue
        result = item.run(ctx, parsed)
        if result is not None:
            return result, item.name, _spec(parsed)
    return None
