"""Compare two generations of prepared bundles, relation by relation, with no question and no answer.

For every business present in both folders the script compares, for each relation: the columns with their types and
roles, the number of rows, and an order-independent content hash. A table function is called with dates taken from the
bundle's own sales range (one date, a range, or a month and the month before) and its result is compared the same way.
It prints one line per business and lists the differences, so that a change in preparation code can be checked to
change exactly what it was meant to change (docs/fix/README.md, lesson 12).

Usage: .venv-native/bin/python scripts/compare-bundles.py artifacts/prepared-r3-train artifacts/prepared-r4-train
"""
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tabular_agent.intents import Context  # noqa: E402
from tabular_agent.sql_agent import find_bundles  # noqa: E402


def sample_dates(ctx, relation):
    """SQL arguments for a table function: dates of the bundle's own sales range, one per DATE parameter."""
    first, last = ctx.coverage
    count = (relation.get('signature') or '').count('DATE')
    days = {1: [last], 2: [last - timedelta(days=29), last],
            4: [last - timedelta(days=29), last, last - timedelta(days=59), last - timedelta(days=30)]}.get(count, [])
    if not days:
        raise ValueError('unsupported signature')
    return ', '.join(f"DATE '{day.isoformat()}'" for day in days)


def describe(path):
    """{relation: (columns, rows, hash)} for one bundle; functions carry their signature."""
    ctx = Context(path)
    out = {}
    try:
        for relation in ctx.schema['relations']:
            name = relation['name']
            columns = [(c['name'], c['type'], c.get('role')) for c in relation.get('columns', [])]
            try:
                if relation.get('function'):
                    rows, digest = ctx.one(f'SELECT COUNT(*), SUM(hash(t)::HUGEINT) FROM {name}({sample_dates(ctx, relation)}) t')
                else:
                    rows, digest = ctx.one(f'SELECT COUNT(*), SUM(hash(t)::HUGEINT) FROM {name} t')
            except Exception as error:  # a relation the schema lists but the file cannot read
                rows, digest = f'error: {type(error).__name__}', None
            out[name] = (columns, rows, str(digest))
    finally:
        ctx.close()
    return out


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    old, new = (find_bundles(ROOT / folder) for folder in sys.argv[1:3])
    changed = 0
    for business in sorted(set(old) & set(new)):
        a, b = describe(old[business]), describe(new[business])
        notes = []
        for name in sorted(set(a) | set(b)):
            if name not in a:
                notes.append(f'  + {name} (new relation, {len(b[name][0])} columns)')
            elif name not in b:
                notes.append(f'  - {name} (relation gone)')
            else:
                if a[name][0] != b[name][0]:
                    left, right = {c[0]: c for c in a[name][0]}, {c[0]: c for c in b[name][0]}
                    for column in sorted(set(left) | set(right)):
                        if left.get(column) != right.get(column):
                            notes.append(f'  ~ {name}.{column}: {left.get(column)} -> {right.get(column)}')
                if a[name][1:] != b[name][1:]:
                    notes.append(f'  ~ {name}: rows/content {a[name][1]} -> {b[name][1]}')
        print(f"{business:8} {'same' if not notes else 'CHANGED'}")
        for line in notes[:40]:
            print(line)
        changed += bool(notes)
    only = sorted(set(old) ^ set(new))
    print(f'\n{len(set(old) & set(new))} businesses compared, {changed} changed' + (f'; in one folder only: {only}' if only else ''))


if __name__ == '__main__':
    main()
