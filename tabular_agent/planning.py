"""Validate explicit calculation contracts and consistency of reported evidence.

Evidence is produced by generated code and is not trusted proof of correctness.
"""
from datetime import date
import re


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Date must be YYYY-MM-DD')
    return date.fromisoformat(value)


def evidence_date(value):
    """Accept ISO or an unambiguous slash date; never guess month/day order."""
    try:
        return iso_date(value)
    except ValueError:
        if not isinstance(value, str):
            raise
        match = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})', value)
        if not match:
            raise
        first, second, year = map(int, match.groups())
        if first > 12 and 1 <= second <= 12:
            return date(year, second, first)
        if second > 12 and 1 <= first <= 12:
            return date(year, first, second)
        raise ValueError('Ambiguous evidence date; use YYYY-MM-DD')


def validate_plan(plan, files):
    if not isinstance(plan, dict):
        raise ValueError('plan must be an object')
    for key in ('metric', 'grain', 'unit'):
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            raise ValueError(f'plan.{key} must be a nonempty string')
    sources = plan.get('sources')
    if not isinstance(sources, list) or not sources or any(not isinstance(s, str) for s in sources):
        raise ValueError('plan.sources must list at least one source filename')
    unknown = set(sources) - set(files) - {'calendar.csv'}
    if unknown:
        raise ValueError(f'Unknown planned sources: {sorted(unknown)}')
    filters = plan.get('filters')
    if not isinstance(filters, list) or any(not isinstance(x, str) for x in filters):
        raise ValueError('plan.filters must be a list of strings')
    period = plan.get('period')
    if period is not None:
        if not isinstance(period, dict) or set(period) != {'start', 'end'}:
            raise ValueError('plan.period must be null or an object with ISO start and end')
        try:
            start, end = iso_date(period['start']), iso_date(period['end'])
        except (ValueError, TypeError) as exc:
            raise ValueError('plan.period needs YYYY-MM-DD start and end') from exc
        if start > end:
            raise ValueError('plan.period start must not exceed end')
    risk = plan.get('risk')
    if risk not in {'normal', 'high'}:
        raise ValueError('plan.risk must be normal or high')
    return {key: plan[key] for key in ('metric', 'period', 'sources', 'grain', 'filters', 'unit', 'risk')}


def validate_evidence(evidence, plan):
    if not isinstance(evidence, dict):
        raise ValueError('Assign evidence with sources, row_count, date_min/date_max and checks')
    sources = evidence.get('sources')
    if (not isinstance(sources, list) or not sources or
            any(not isinstance(s, str) for s in sources) or set(sources) - set(plan['sources'])):
        raise ValueError('evidence.sources must list sources declared in the plan')
    rows = evidence.get('row_count')
    if type(rows) is not int or rows < 0:
        raise ValueError('evidence.row_count must be a non-negative integer')
    checks = evidence.get('checks')
    if not isinstance(checks, dict) or not checks:
        raise ValueError('evidence.checks must describe relevant filters, joins or conversions')
    period = plan['period']
    if period and rows:
        try:
            actual_start = evidence_date(evidence.get('date_min'))
            actual_end = evidence_date(evidence.get('date_max'))
        except (TypeError, ValueError) as exc:
            raise ValueError('For a dated calculation supply unambiguous evidence.date_min and date_max, preferably YYYY-MM-DD') from exc
        if not (iso_date(period['start']) <= actual_start <= actual_end <= iso_date(period['end'])):
            raise ValueError('Evidence dates fall outside the planned period')
    if period and rows == 0 and evidence.get('coverage') != 'covered':
        raise ValueError('Zero matching rows require evidence that the date range is covered')
    return evidence


def validate_absence_evidence(evidence, plan):
    """Check the shape of a full-source absence claim, not its truth."""
    if not isinstance(evidence, dict) or evidence.get('search_complete') is not True:
        raise ValueError('Missing-data claims require evidence.search_complete=true from a full-data diagnostic')
    sources = evidence.get('sources')
    if (not isinstance(sources, list) or not sources or
            any(not isinstance(s, str) for s in sources) or set(sources) - set(plan['sources'])):
        raise ValueError('Absence evidence must identify planned sources')
    rows = evidence.get('searched_rows')
    if type(rows) is not int or rows < 0:
        raise ValueError('Absence evidence needs a non-negative searched_rows count')
    if not isinstance(evidence.get('missing'), str) or not evidence['missing'].strip():
        raise ValueError('Absence evidence must describe what information is missing')
    if not isinstance(evidence.get('checks'), dict) or not evidence['checks']:
        raise ValueError('Absence evidence must describe the diagnostic checks')
    return evidence


def requires_recomputation(plan, policy='high-risk'):
    if policy == 'review':
        return False
    if policy == 'all':
        return True
    if policy != 'high-risk':
        raise ValueError('Unknown verification policy')
    units = plan['unit'].strip().casefold()
    return (plan['risk'] == 'high' or plan['period'] is not None or
            len(set(plan['sources']) - {'calendar.csv'}) > 1 or
            units in {'vnd', 'đồng', 'dong', 'vnđ'})
