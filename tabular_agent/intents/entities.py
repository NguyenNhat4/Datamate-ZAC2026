"""Find the product, product group and unit a question names, from the bundle's own catalogue.

Names are matched on accent-folded tokens, longest first, against the canonical names the bundle publishes
(semantic.product_quantities). A name that two products share once folded is never matched, so a recognised name
always points to one product. Only names the bundle holds can be found: nothing is guessed from similar spellings.
"""
from dataclasses import dataclass

from .text import prepare, variants

_UNIT_LEADS = frozenset({'so', 'nhieu', 'nhiu', 'may', 'theo', 'mot', 'bn', 'bnhiu', 'bnhieu'})


@dataclass(frozen=True)
class Entity:
    """A catalogue name found in a question: kind is product, group, unit, size or customer."""
    kind: str
    name: str
    unit: str = ''


class Catalog:
    """Folded names of the products, groups and base units of one bundle."""

    def __init__(self, ctx):
        self.products, self.groups, self.units, self.customers = {}, {}, {}, {}
        relation = 'semantic.product_quantities'
        # The base unit when the bundle converts quantities; else the unit a sales line is written in.
        unit_column = next((c for c in ('base_unit', 'unit') if ctx.has(relation, c)), None)
        if ctx.has(relation, 'product_name'):
            unit = f'CASE WHEN COUNT(DISTINCT {unit_column}) = 1 THEN MIN({unit_column}) END' if unit_column else 'NULL'
            self._add(self.products, ctx.rows(f'SELECT product_name, {unit} FROM {relation} '
                                              'WHERE product_name IS NOT NULL GROUP BY 1'))
        if ctx.has(relation, 'product_group'):
            self._add(self.groups, ctx.rows(f'SELECT DISTINCT product_group, NULL FROM {relation} '
                                            'WHERE product_group IS NOT NULL'))
        if unit_column:
            self._add(self.units, ctx.rows(f'SELECT DISTINCT {unit_column}, NULL FROM {relation} WHERE {unit_column} IS NOT NULL'))
        if ctx.has('semantic.orders', 'customer_name'):
            self._add(self.customers, ctx.rows('SELECT DISTINCT customer_name, NULL FROM semantic.orders '
                                               'WHERE customer_name IS NOT NULL'))
            self.customers = {k: v for k, v in self.customers.items() if len(k) >= 2}
        self.longest = max([len(k) for d in (self.products, self.groups, self.units, self.customers) for k in d] or [1])

    @staticmethod
    def _add(table, rows):
        clash = set()
        for name, unit in rows:
            key = tuple(prepare(name).split())
            if not key:
                continue
            if key in table and table[key].name != str(name):
                clash.add(key)
            table[key] = Entity('', str(name), prepare(unit) if unit else '')
        for key in clash:
            del table[key]

    def mask(self, tokens):
        """Replace the names found in a token list by @p (product), @g (group), @u (unit), @z (size) and @k (customer).

        Args:
            tokens: Output of text.prepare(...).split().

        Returns:
            (masked tokens, entities in order of appearance).
        """
        out, found, i = [], [], 0
        while i < len(tokens):
            token = tokens[i]
            if 'nhom' in variants(token):
                j = i + 1
                if j < len(tokens) and tokens[j] == 'hang':
                    j += 1
                hit = self._longest(self.groups, tokens, j)
                if hit:
                    length, entity = hit
                    found.append(Entity('group', entity.name))
                    out.append('@g')
                    i = j + length
                    continue
            hit = self._longest(self.products, tokens, i)
            if hit:
                length, entity = hit
                found.append(Entity('product', entity.name, entity.unit))
                out.append('@p')
                i += length
                continue
            hit = self._longest(self.customers, tokens, i)
            if hit:
                length, entity = hit
                found.append(Entity('customer', entity.name))
                out.append('@k')
                i += length
                continue
            if token == 'size' and i + 1 < len(tokens) and tokens[i + 1] in ('m', 'l'):
                found.append(Entity('size', tokens[i + 1]))
                out.append('@z')
                i += 2
                continue
            hit = self._longest(self.units, tokens, i)
            if hit and i > 0 and self._lead(tokens[i - 1]) and not (tokens[i:i + 1] == ['phan'] and tokens[i + 1:i + 2] == ['tram']):
                length, entity = hit
                found.append(Entity('unit', entity.name))
                out.append('@u')
                i += length
                continue
            out.append(token)
            i += 1
        return out, found

    def _longest(self, table, tokens, start):
        for length in range(min(self.longest, len(tokens) - start), 0, -1):
            entity = table.get(tuple(tokens[start:start + length]))
            if entity:
                return length, entity
        return None

    @staticmethod
    def _lead(token):
        """True when the token is a word that precedes a unit: so, bao nhieu, may, theo, mot."""
        return any(v in _UNIT_LEADS for v in variants(token))
