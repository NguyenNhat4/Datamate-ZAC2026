"""Check prepared bundles against each other, with no question and no reference answer.

For every bundle of a folder written by `cli prepare` the script compares figures that must agree if the rulebook
was applied consistently:
- revenue of the sales lines equals revenue of the orders that count as sales (month by month);
- the orders that count as sales are exactly the completed and shipping ones;
- no sales date is missing, and the dates form one continuous range of months;
- the semantic orders hold every order id of the sales lines.
A line that fails is printed; a clean bundle prints one summary line.

Usage: .venv-native/bin/python scripts/bundle-invariants.py artifacts/prepared-r3-public
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tabular_agent.intents import Context  # noqa: E402
from tabular_agent.sql_agent import find_bundles  # noqa: E402


def check(ctx):
    """Return (summary, problems) for one bundle."""
    problems = []
    has_lines = ctx.has('semantic.eligible_sales_lines', 'line_amount', 'order_date')
    has_orders = ctx.has('semantic.eligible_orders', 'order_date')
    amount = next((c for c in ('revenue', 'order_goods_amount') if ctx.has('semantic.eligible_orders', c)), None)
    if not has_lines and not has_orders:
        return 'no rulebook-ready sales relation', problems
    if has_lines and has_orders and amount:
        rows = ctx.rows(f'SELECT m, l, o FROM (SELECT strftime(CAST(order_date AS DATE), \'%Y-%m\') m, SUM(line_amount) l '
                        'FROM semantic.eligible_sales_lines GROUP BY 1) FULL JOIN (SELECT strftime(CAST(order_date AS DATE), '
                        f"'%Y-%m') m, SUM({amount}) o FROM semantic.eligible_orders GROUP BY 1) USING (m) "
                        'WHERE COALESCE(l, 0) <> COALESCE(o, 0) ORDER BY 1')
        for month, lines, orders in rows[:5]:
            problems.append(f'revenue differs in {month}: lines {lines}, orders {orders}')
        if ctx.has('semantic.orders', 'order_id') and ctx.has('semantic.eligible_sales_lines', 'order_id'):
            missing = ctx.one('SELECT COUNT(DISTINCT order_id) FROM semantic.eligible_sales_lines WHERE order_id NOT IN '
                              '(SELECT order_id FROM semantic.eligible_orders)')[0]
            if missing:
                problems.append(f'{missing} order ids of the sales lines are not eligible orders')
    if ctx.has('semantic.orders', 'order_state', 'counts_as_sale'):
        bad = ctx.one("SELECT COUNT(*) FROM semantic.orders WHERE counts_as_sale <> (order_state IN ('completed', 'shipping'))")[0]
        if bad:
            problems.append(f'{bad} orders whose counts_as_sale disagrees with their state')
    source = 'semantic.eligible_sales_lines' if has_lines else 'semantic.eligible_orders'
    nulls = ctx.one(f'SELECT COUNT(*) FROM {source} WHERE order_date IS NULL')[0]
    if nulls:
        problems.append(f'{nulls} sales rows without a date')
    months = [r[0] for r in ctx.rows(f"SELECT DISTINCT strftime(CAST(order_date AS DATE), '%Y-%m') FROM {source} ORDER BY 1")]
    index = [int(m[:4]) * 12 + int(m[5:]) for m in months]
    if any(b - a != 1 for a, b in zip(index, index[1:])):
        problems.append(f'months with no sales at all between {months[0]} and {months[-1]}')
    first, last = ctx.coverage or (None, None)
    return f'{len(months)} months {months[0]}..{months[-1]}, coverage {first}..{last}', problems


def main():
    folder = ROOT / sys.argv[1] if len(sys.argv) > 1 else None
    if folder is None:
        raise SystemExit(__doc__)
    bundles = find_bundles(folder)
    failed = 0
    for business, path in sorted(bundles.items()):
        ctx = Context(path)
        try:
            summary, problems = check(ctx)
        finally:
            ctx.close()
        print(f"{business:8} {'FAIL' if problems else 'ok  '} {summary}")
        for line in problems:
            print(f'           - {line}')
        failed += bool(problems)
    print(f'\n{len(bundles)} bundles, {failed} with problems')


if __name__ == '__main__':
    main()
