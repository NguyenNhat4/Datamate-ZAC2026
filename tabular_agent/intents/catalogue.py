"""Question kinds the system answers with a fixed, reviewed query instead of a model-written one.

An intent is recognised only when every word of the question is explained: by the intent's own cue words, by common
filler, or by a period or number slot. A question with an unexplained word is left to the model. Each intent's query
applies the rulebook once, here, so a recognised question gets the same answer every time.
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

from . import periods as P
from .context import percent, whole
from .entities import Catalog
from .text import FILLER, canonical_tokens, prepare

COMMON = frozenset('trong ngay rieng tinh xet theo vao luc tai da'.split())
SLOTS = frozenset({'@t', '@n'})
ENTITY_SLOTS = {'product': '@p', 'group': '@g', 'unit': '@u', 'size': '@z'}
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
        entities: Kinds of catalogue names the question must contain, exactly (product, group, unit, size).
        run: Executor (Context, Parsed) -> answer dict or None. None means "cannot answer here": the model is asked.
    """
    name: str
    words: frozenset
    need: Callable
    periods: tuple
    numbers: tuple
    entities: tuple
    run: Callable


INTENTS = []


def intent(name, words, need, periods=(1, 1), numbers=(0, 0), entities=()):
    """Register an intent; decorates its executor."""
    def register(run):
        INTENTS.append(Intent(name, frozenset(words.split()), need, periods, numbers, tuple(sorted(entities)), run))
        return run
    return register


def known_words():
    """Every canonical word the registered intents or the period patterns use."""
    out = set(FILLER) | set(COMMON) | set(P.WORDS)
    for item in INTENTS:
        out |= item.words
    return out


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

def revenue(ctx, first, last):
    """Rulebook revenue of [first, last]: line amounts of orders that count as sales; None when no source exists."""
    if ctx.has('semantic.eligible_sales_lines', 'line_amount', 'order_date'):
        row = ctx.one('SELECT SUM(line_amount) FROM semantic.eligible_sales_lines '
                      'WHERE CAST(order_date AS DATE) BETWEEN ? AND ?', first, last)
    elif ctx.has('semantic.eligible_orders', 'revenue', 'order_date'):
        row = ctx.one('SELECT SUM(revenue) FROM semantic.eligible_orders WHERE CAST(order_date AS DATE) BETWEEN ? AND ?',
                      first, last)
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


def _is_period(word):
    return word == '@t'


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
    if not ctx.has('semantic.eligible_orders', 'order_date', 'revenue'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    total, count = ctx.one('SELECT SUM(revenue), COUNT(*) FROM semantic.eligible_orders '
                           'WHERE CAST(order_date AS DATE) BETWEEN ? AND ?', period.first, period.last)
    if not count:
        return None
    return number(whole(Decimal(str(total)) / count))


@intent('best_day', 'ngay nao ban duoc nhieu tien nhat doanhthu cao dat cuahang',
        lambda w, p: 'nao' in w and 'nhat' in w and ('doanhthu' in w or 'tien' in w))
def run_best_day(ctx, parsed):
    period = _one_period(parsed)
    if not ctx.has('semantic.eligible_sales_lines', 'line_amount', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    row = ctx.one('SELECT CAST(order_date AS DATE) AS d FROM semantic.eligible_sales_lines '
                  'WHERE CAST(order_date AS DATE) BETWEEN ? AND ? GROUP BY 1 ORDER BY SUM(line_amount) DESC, 1 LIMIT 1',
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


@intent('revenue_change', 'doanhthu tang giam hay thay doi sovoi hon kem chenh phantram truong thang truoc lientruoc '
        'truocdo cungky nam ngoai roi mot nhieu tong cuahang',
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
    months = parsed.periods
    if not months or any(m.kind != 'month' or not m.year_given for m in months):
        return None
    words = parsed.words
    if len(months) == 1 and 'ketthuc' in words and len(parsed.numbers) == 1 and 2 <= parsed.numbers[0] <= 12:
        last = months[0].first
        return [P.month_range(*divmod(last.year * 12 + last.month - 1 - i, 12)[:1] +
                              (divmod(last.year * 12 + last.month - 1 - i, 12)[1] + 1,))
                for i in range(int(parsed.numbers[0]) - 1, -1, -1)]
    if len(months) == 2 and 'tu' in words and bool({'den', 'toi'} & set(words)) and 'ketthuc' not in words:
        a, b = months[0].first, months[1].first
        if a >= b:
            return None
        span = (b.year - a.year) * 12 + b.month - a.month
        return [P.month_range(*divmod(a.year * 12 + a.month - 1 + i, 12)[:1] + (divmod(a.year * 12 + a.month - 1 + i, 12)[1] + 1,))
                for i in range(span + 1)]
    if len(months) >= 2 and 'tu' not in words and len(parsed.numbers) <= 1:
        if parsed.numbers and parsed.numbers[0] != len(months):
            return None
        return [(m.first, m.last) for m in months]
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


_MONTHS_WORDS = 'doanhthu thang cua va tu den toi cac ba moi lanluot ghi theo thutu do thoigian so lietke ke liennhau lientiep'


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


@intent('revenue_trend', _MONTHS_WORDS + ' co tang lientuc khong qua luon hon sau truoc ky la',
        lambda w, p: 'doanhthu' in w and bool({'lientuc', 'luon'} & w) and 'khong' in w,
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


_SHIFT_WORDS = 'casang cachieu doanhthu don so co tong cong ban duoc tien rieng trong cuahang dem'


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


@intent('shift_comparison', _SHIFT_WORDS + ' sovoi chenh hon kem tang giam phantram thi', lambda w, p: {'casang', 'cachieu'} <= w
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
_OPERATORS = {'>=': '>=', '>': '>', '<': '<', '<=': '<='}


@intent('days_over_amount', _THRESHOLD_WORDS + ' doanhthu', lambda w, p: 'ngay' in w and 'doanhthu' in w,
        numbers=(1, 1))
def run_days_over(ctx, parsed):
    period, op = _one_period(parsed), operator(parsed.words)
    if op is None or not ctx.has('semantic.eligible_sales_lines', 'line_amount', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    sql = ('SELECT COUNT(*) FROM (SELECT CAST(d AS DATE) AS day FROM generate_series(?::DATE, ?::DATE, INTERVAL 1 DAY) t(d)) '
           'LEFT JOIN (SELECT CAST(order_date AS DATE) AS day, SUM(line_amount) AS total FROM semantic.eligible_sales_lines '
           'GROUP BY 1) USING (day) '
           f'WHERE COALESCE(total, 0) {_OPERATORS[op]} ?')
    return number(ctx.one(sql, period.first, period.last, parsed.numbers[0])[0])


@intent('orders_over_amount', _THRESHOLD_WORDS + ' don tien hang doanhthu', lambda w, p: 'don' in w and bool({'tien', 'doanhthu'} & w),
        numbers=(1, 1))
def run_orders_over(ctx, parsed):
    period, op = _one_period(parsed), operator(parsed.words)
    if op is None or not ctx.has('semantic.eligible_orders', 'revenue', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    return number(ctx.one('SELECT COUNT(*) FROM semantic.eligible_orders WHERE CAST(order_date AS DATE) BETWEEN ? AND ? '
                          f'AND revenue {_OPERATORS[op]} ?', period.first, period.last, parsed.numbers[0])[0])


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


@intent('product_revenue', 'doanhthu cua tong cong ban duoc tien thu ve ra @p mathang hang',
        lambda w, p: '@p' in w and ('doanhthu' in w or 'tien' in w), entities=('product',))
def run_product_revenue(ctx, parsed):
    period, product = _one_period(parsed), _entity(parsed, 'product')
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    value = product_sum(ctx, 'line_amount', period.first, period.last, product.name)
    return None if value is None else number(whole(value))


@intent('product_quantity', 'so ban duoc tong cong ra @p @u mathang hang',
        lambda w, p: '@p' in w and '@u' in w, entities=('product', 'unit'))
def run_product_quantity(ctx, parsed):
    period, product, unit = _one_period(parsed), _entity(parsed, 'product'), _entity(parsed, 'unit')
    if not product.unit or prepare(unit.name) != product.unit:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    value = product_sum(ctx, 'base_quantity', period.first, period.last, product.name)
    return None if value is None else number(quantity(value))


@intent('unit_total', 'so ban duoc tong cong ra @u cong moi mathang tinh theo',
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


@intent('size_total', 'so ban duoc tong cong ra @u @z cong moi mon mathang',
        lambda w, p: '@u' in w and '@z' in w, entities=('unit', 'size'))
def run_size_total(ctx, parsed):
    period, size = _one_period(parsed), _entity(parsed, 'size')
    if not ctx.has('semantic.product_quantities', 'base_quantity', 'product_name', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    row = ctx.one("SELECT SUM(base_quantity) FROM semantic.product_quantities WHERE lower(product_name) LIKE ? "
                  'AND CAST(order_date AS DATE) BETWEEN ? AND ?', f'%({size.name})', period.first, period.last)
    return number(quantity(row[0] or 0))


@intent('group_revenue', 'doanhthu cua tong cong ban duoc tien thu ve ra @g',
        lambda w, p: '@g' in w and ('doanhthu' in w or 'tien' in w) and 'phantram' not in w, entities=('group',))
def run_group_revenue(ctx, parsed):
    period, group = _one_period(parsed), _entity(parsed, 'group')
    if not ctx.has('semantic.eligible_sales_lines', 'line_amount', 'product_group', 'order_date'):
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    row = ctx.one('SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE product_group = ? '
                  'AND CAST(order_date AS DATE) BETWEEN ? AND ?', group.name, period.first, period.last)
    return number(whole(row[0] or 0))


@intent('group_share', 'doanhthu cua tong chiem phantram trong @g dong gop cua ca tiem tile',
        lambda w, p: '@g' in w and 'doanhthu' in w and 'phantram' in w, entities=('group',))
def run_group_share(ctx, parsed):
    period, group = _one_period(parsed), _entity(parsed, 'group')
    if 'semantic.group_revenue_share' not in ctx.functions:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    row = ctx.one('SELECT share_percent FROM semantic.group_revenue_share(?, ?) WHERE product_group = ?',
                  period.first, period.last, group.name)
    return {'type': 'percent', 'answer': float(row[0])} if row and row[0] is not None else None


@intent('top_products', 'top chay nhat ban nhieu hot mathang hang mon la gi nhung nao lietke cho cac @n',
        lambda w, p: bool({'chay', 'hot', 'top'} & w) or ('ban' in w and 'nhieu' in w and 'nhat' in w and bool({'mathang', 'hang', 'mon'} & w)),
        numbers=(0, 1))
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
        lambda w, p: 'unsold' in w or ({'khong', 'mua'} <= w and 'mathang' in w))
def run_unsold(ctx, parsed):
    period = _one_period(parsed)
    if 'semantic.unsold_products' not in ctx.functions:
        return None
    if ctx.outside(period.first, period.last):
        return unanswerable(NO_DATA)
    return number(ctx.one('SELECT COUNT(*) FROM semantic.unsold_products(?, ?)', period.first, period.last)[0])


@intent('products_in_orders', 'baonhieu mathang xuathien itnhat @n don khacnhau trolen duoc mua o co tong',
        lambda w, p: 'mathang' in w and bool({'xuathien', 'mua'} & w) and 'don' in w, numbers=(1, 1))
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


@intent('price_change_effect', 'so ban ra trong ketu ngay tanggia len tu voi ngaytruoc ngay do luong tinh theo @u @p @n '
        'hay giam tang nhieu it hon thay doi phantram lientruoc truoc duoc sovoi vao ke dong',
        lambda w, p: '@p' in w and 'tanggia' in w and 'phantram' in w, numbers=(2, 4), entities=('product',))
def run_price_change(ctx, parsed):
    day = next((p for p in parsed.periods if p.kind == 'day'), None)
    if day is None or len(parsed.periods) != 1:
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


def answer(question, as_of_date, ctx):
    """Answer a question by the first intent that explains it.

    Returns:
        (answer dict, intent name) or None when no intent claims the question or the claimed intent cannot answer it
        from this bundle. None means: leave the question to the model.
    """
    if not hasattr(ctx, 'catalog'):
        ctx.catalog = Catalog(ctx)
    plain = analyze(question, as_of_date)
    named = analyze(question, as_of_date, ctx.catalog) if (ctx.catalog.products or ctx.catalog.groups or ctx.catalog.units) else None
    for item in INTENTS:
        parsed = named if item.entities else plain
        if parsed is None:
            continue
        words = set(parsed.words)
        if not item.need(words, parsed):
            continue
        if not item.periods[0] <= len(parsed.periods) <= item.periods[1]:
            continue
        if not item.numbers[0] <= len(parsed.numbers) <= item.numbers[1]:
            continue
        if tuple(sorted(e.kind for e in (parsed.entities or []))) != item.entities:
            continue
        if words - item.words - FILLER - COMMON - SLOTS:
            continue
        result = item.run(ctx, parsed)
        if result is not None:
            return result, item.name
    return None
