"""Resolve the periods a question names into inclusive date ranges, by the rulebook's time rules.

The rules applied (rulebook section 2):
- today is the question's `as_of_date`, never the system date;
- a week runs Monday to Sunday; "tuần này" ends today, "tuần trước/rồi" is the whole week before;
- "tháng M/Y" is the whole month; "tháng này" runs from the 1st to today;
- compared with last month, "tháng này" takes the same number of days of last month;
- a month or quarter named without a year belongs to this year unless it starts after today, then to last year;
- "N ngày gần nhất" are N consecutive days ending today.
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta

# Canonical words the patterns below are written in; text.canonical() maps typing variants onto them.
WORDS = frozenset('''thang nam quy tuan ngay hom nay qua truoc roi dau den toi het tu gan nhat vua lien cung ky do giai
doan tinh'''.split())

_DATE = r'(?:ngay )?(\d{1,2})/(\d{1,2})/(\d{4})'


@dataclass(frozen=True)
class Period:
    """One period named in a question.

    Attributes:
        kind: day, week, month, quarter, month_to_date, year_to_date, last_days, range, this_month, last_month,
            this_quarter, last_quarter or this_week.
        first: First day, inclusive.
        last: Last day, inclusive.
        year_given: True when the question stated the year.
    """
    kind: str
    first: date
    last: date
    year_given: bool = True


def month_range(year, month):
    """First and last day of a calendar month."""
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def previous_month(day):
    """First and last day of the calendar month before the one containing `day`."""
    last = day.replace(day=1) - timedelta(days=1)
    return month_range(last.year, last.month)


def shift_months(day, count):
    """The same day number `count` months away, capped at that month's last day."""
    index = day.year * 12 + day.month - 1 + count
    year, month = divmod(index, 12)
    last = month_range(year, month + 1)[1]
    return date(year, month + 1, min(day.day, last.day))


def quarter_range(year, quarter):
    """First and last day of a calendar quarter."""
    return month_range(year, 3 * quarter - 2)[0], month_range(year, 3 * quarter)[1]


def _year_for(start, today, year):
    """Year of a month or quarter named without one: this year unless it starts after today."""
    if year:
        return int(year)
    return today.year if start <= today else today.year - 1


def _valid_date(day, month, year):
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def extract(text, as_of_date):
    """Find the periods in canonical text.

    Args:
        text: Canonical question text (text.canonical), where typing variants of `thang`, `hom nay` and so on are
            already normalised.
        as_of_date: ISO date of "today".

    Returns:
        (masked, periods): the text with every period replaced by the token `@t`, and the periods in the order
        their tokens appear. A phrase that names an impossible date (30/02) makes the whole result None.
    """
    today = date.fromisoformat(as_of_date)
    monday = today - timedelta(days=today.weekday())
    found = []  # (start, end, Period)

    def add(match, period):
        found.append((match.start(), match.end(), period))

    def free(match):
        return not any(match.start() < end and start < match.end() for start, end, _ in found)

    patterns = []

    def pattern(regex, build):
        patterns.append((re.compile(regex), build))

    def date_range(m):
        a, b = _valid_date(m[1], m[2], m[3]), _valid_date(m[4], m[5], m[6])
        return Period('range', a, b) if a and b and a <= b else False

    pattern(r'(?:giai doan )?tu ' + _DATE + r' (?:den|toi) (?:het )?' + _DATE, date_range)
    pattern(r'(?:nam nay )?(?:tinh )?tu (?:dau nam|(?:ngay )?1/1(?: nam nay)?)(?: (?:den|toi)(?: het)?(?: (?:hom nay|nay))?)?',
            lambda m: Period('year_to_date', date(today.year, 1, 1), today))
    pattern(r'(?:tinh )?tu dau thang(?: nay)?(?: (?:den|toi)(?: het)?(?: (?:hom nay|nay))?)?',
            lambda m: Period('month_to_date', today.replace(day=1), today))
    pattern(r'(\d{1,3}) ngay gan nhat',
            lambda m: Period('last_days', today - timedelta(days=int(m[1]) - 1), today) if int(m[1]) else False)
    pattern(_DATE, lambda m: (lambda d: Period('day', d, d) if d else False)(_valid_date(m[1], m[2], m[3])))
    pattern(r'thang (\d{1,2})(?:/| nam )(\d{4})',
            lambda m: Period('month', *month_range(int(m[2]), int(m[1]))) if 1 <= int(m[1]) <= 12 else False)
    pattern(r'quy ([1-4])(?:/| nam )(\d{4})', lambda m: Period('quarter', *quarter_range(int(m[2]), int(m[1]))))
    pattern(r'(?<![\d/])(\d{1,2})/(\d{4})(?![\d/])',
            lambda m: Period('month', *month_range(int(m[2]), int(m[1]))) if 1 <= int(m[1]) <= 12 else False)
    pattern(r'thang (\d{1,2})(?= |$)',
            lambda m: (Period('month', *month_range(_year_for(date(today.year, int(m[1]), 1), today, None), int(m[1])),
                              year_given=False) if 1 <= int(m[1]) <= 12 else False))
    pattern(r'quy ([1-4])(?= |$)',
            lambda m: Period('quarter', *quarter_range(_year_for(quarter_range(today.year, int(m[1]))[0], today, None),
                                                       int(m[1])), year_given=False))
    pattern(r'tuan nay', lambda m: Period('this_week', monday, today))
    pattern(r'tuan (?:vua )?(?:truoc|roi|qua)',
            lambda m: Period('week', monday - timedelta(days=7), monday - timedelta(days=1)))
    pattern(r'hom nay', lambda m: Period('day', today, today))
    pattern(r'hom qua', lambda m: Period('day', today - timedelta(days=1), today - timedelta(days=1)))
    pattern(r'thang nay', lambda m: Period('this_month', today.replace(day=1), today))
    pattern(r'thang (?:vua )?(?:truoc|roi|qua)(?! do)', lambda m: Period('last_month', *previous_month(today)))
    pattern(r'quy nay', lambda m: Period('this_quarter', quarter_range(today.year, (today.month - 1) // 3 + 1)[0], today))

    for regex, build in patterns:
        for match in regex.finditer(text):
            if not free(match):
                continue
            period = build(match)
            if period is False:
                return text, None
            add(match, period)
    found.sort(key=lambda item: item[0])
    pieces, position = [], 0
    for start, end, _ in found:
        pieces.append(text[position:start])
        pieces.append('@t')
        position = end
    pieces.append(text[position:])
    masked = re.sub(r'\s+', ' ', ''.join(pieces).replace('@t', ' @t ')).strip()
    return masked, [period for _, _, period in found]


def base_of(asked, today, year_ago=False):
    """The period an "against the previous period" comparison uses as its base.

    Args:
        asked: The asked Period.
        today: Reference date.
        year_ago: True for "cùng kỳ năm trước" (the same period a year earlier).

    Returns:
        The base Period, or None when the rulebook gives no base for this kind of period. A whole month is compared
        with the month before; "tháng này" and "từ đầu tháng đến hết hôm nay" take the same number of days of the
        month before; a quarter is compared with the quarter before.
    """
    if year_ago:
        if asked.kind in ('month', 'quarter', 'this_month', 'month_to_date', 'year_to_date'):
            first, last = shift_months(asked.first, -12), shift_months(asked.last, -12)
            if asked.kind in ('month', 'quarter') and asked.last.day != last.day:
                last = (month_range(last.year, last.month)[1])
            return Period(asked.kind, first, last)
        return None
    if asked.kind == 'month':
        return Period('month', *previous_month(asked.first))
    if asked.kind in ('this_month', 'month_to_date'):
        first = shift_months(asked.first, -1)
        return Period('month_to_date', first, shift_months(asked.last, -1))
    if asked.kind == 'quarter':
        first = shift_months(asked.first, -3)
        return Period('quarter', first, month_range(*divmod(first.year * 12 + first.month + 1, 12)[:1] +
                                                    (divmod(first.year * 12 + first.month + 1, 12)[1] + 1,))[1])
    return None
