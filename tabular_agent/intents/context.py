"""Read-only access to one prepared bundle for the fixed queries of the intent layer."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path

CENT = Decimal('0.01')


def whole(value):
    """A money value rounded half up to whole currency units, as an int."""
    return int(Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def percent(part, whole_):
    """100 * part / whole rounded half up to two decimals, as a float; None when the whole is zero."""
    if not whole_:
        return None
    value = (Decimal(100) * Decimal(str(part)) / Decimal(str(whole_))).quantize(CENT, rounding=ROUND_HALF_UP)
    return float(value)


class Context:
    """One bundle opened read-only, with the relations and columns it publishes.

    Attributes:
        connection: DuckDB connection (one thread, no external access).
        columns: Mapping from relation name (``semantic.orders``) to its set of column names.
        coverage: (first, last) sales dates of the bundle, or None.
    """

    def __init__(self, bundle):
        import duckdb
        bundle = Path(bundle)
        self.schema = json.loads((bundle / 'schema.json').read_text(encoding='utf-8'))
        self.connection = duckdb.connect(str(bundle / 'data.duckdb'), read_only=True,
                                         config={'enable_external_access': False, 'autoinstall_known_extensions': False,
                                                 'autoload_known_extensions': False, 'memory_limit': '1GB',
                                                 'threads': 1})
        self.connection.execute("SET search_path='semantic,clean'")
        self.connection.execute('SET lock_configuration=true')
        self.columns = {r['name']: {c['name'] for c in r.get('columns', [])} for r in self.schema['relations']}
        self.functions = {r['name'] for r in self.schema['relations'] if r.get('function')}
        self.coverage = self._coverage()

    def close(self):
        """Close the connection."""
        self.connection.close()

    def has(self, relation, *columns):
        """True when the bundle publishes the relation with every named column."""
        return relation in self.columns and set(columns) <= self.columns[relation]

    def one(self, sql, *params):
        """First row of a query, or None."""
        return self.connection.execute(sql, list(params)).fetchone()

    def rows(self, sql, *params):
        """All rows of a query."""
        return self.connection.execute(sql, list(params)).fetchall()

    def _coverage(self):
        for name in ('semantic.orders', 'semantic.eligible_sales_lines'):
            if self.has(name, 'order_date'):
                first, last = self.one(f'SELECT MIN(CAST(order_date AS DATE)), MAX(CAST(order_date AS DATE)) FROM {name}')
                if first and last:
                    return first, last
        return None

    def outside(self, first, last):
        """True when [first, last] lies entirely before or after the sales dates of the bundle."""
        return bool(self.coverage) and (last < self.coverage[0] or first > self.coverage[1])
