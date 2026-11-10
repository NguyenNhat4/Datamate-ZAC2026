"""Draw the closed gate100 and the open work100 samples from training questions in no frozen set.

gate100 is drawn first, from question ids and business ids only: no question text, answer or
answer type is read to choose it, and its answer types are never printed. work100 is drawn
afterwards from the questions left; it reads answer types, only to reach minimum counts of rare
types and of one business.

Both draws are reproducible. Questions are ordered by a seeded SHA-256 hash of their id. Each
business gets a share of the 100 in proportion to its remaining questions, with at least two
(largest remainder; ties by business id).

Usage, from the repository root:
    .venv-native/bin/python scripts/draw-samples.py            # print the draw
    .venv-native/bin/python scripts/draw-samples.py --write    # also add both sets to the frozen-set file
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETS = ROOT / 'docs/fix/2026-11-10_unseen_businesses/question_sets.json'
QUESTIONS = ROOT / 'data/train/questions.jsonl'
ANSWERS = ROOT / 'data/train/answers.jsonl'
SEED = 'samples-2026-11-10'
SIZE = 100
FLOOR = 2
# work100 only. Rarest types first; a natural draw of 100 gives 0 to 6 of each.
TYPE_MINIMUMS = {'table': 4, 'boolean': 4, 'unanswerable': 6, 'string': 6, 'date': 6, 'list': 6}
# work100 only. biz_110 is the one training business whose bundle has rulebook-ready orders but no
# rulebook-ready sales lines, like 7 public-test businesses. Sample design only: no answer logic uses it.
BUSINESS_MINIMUMS = {'biz_110': 6}


def order(salt, ids):
    """Return ids sorted by a seeded hash, so the draw does not depend on file order."""
    return sorted(ids, key=lambda i: hashlib.sha256(f'{SEED}:{salt}:{i}'.encode()).hexdigest())


def allocate(counts):
    """Split SIZE over businesses in proportion to counts, at least FLOOR each (largest remainder)."""
    total = sum(counts.values())
    exact = {b: SIZE * c / total for b, c in counts.items()}
    share = {b: min(counts[b], max(FLOOR, math.floor(x))) for b, x in exact.items()}
    while sum(share.values()) < SIZE:
        b = min((b for b in counts if share[b] < counts[b]), key=lambda b: (share[b] - exact[b], b))
        share[b] += 1
    while sum(share.values()) > SIZE:
        b = min((b for b in counts if share[b] > FLOOR), key=lambda b: (exact[b] - share[b], b))
        share[b] -= 1
    return share


def draw(salt, pool):
    """Draw SIZE questions: each business's share, first in hash order."""
    counts = Counter(pool.values())
    share = allocate(counts)
    picked = []
    for b in sorted(share):
        picked += order(salt, [i for i, owner in pool.items() if owner == b])[:share[b]]
    return picked


def top_up(sample, rest, types, business):
    """Swap rare-type and minimum-business questions into a sample, taking out 'number' questions.

    An added question replaces a 'number' question of its own business when there is one, so the
    spread by business stays as drawn; otherwise one of the business with the most questions.
    """
    sample = list(sample)
    candidates = order('work100-top-up', rest)
    swaps = []

    def count(pred):
        return sum(pred(i) for i in sample)

    def take_out(prefer):
        sizes = Counter(business[i] for i in sample)
        def removable(i):
            b = business[i]
            return (types[i] == 'number' and sizes[b] > max(FLOOR, BUSINESS_MINIMUMS.get(b, 0))
                    and i not in added)
        own = [i for i in sample if business[i] == prefer and types[i] == 'number' and i not in added]
        if own:
            return own[-1]
        others = [i for i in sample if removable(i)]
        biggest = min(others, key=lambda i: (-sizes[business[i]], business[i]))
        return [i for i in others if business[i] == business[biggest]][-1]

    added = set()

    def swap_in(new, prefer):
        old = take_out(prefer)
        sample[sample.index(old)] = new
        added.add(new)
        swaps.append((new, old))

    for b, minimum in BUSINESS_MINIMUMS.items():
        for new in candidates:
            if count(lambda i: business[i] == b) >= minimum:
                break
            if business[new] == b and new not in sample:
                swap_in(new, prefer=None)          # take out from another business, or b never grows
    for t, minimum in TYPE_MINIMUMS.items():
        for new in candidates:
            if count(lambda i: types[i] == t) >= minimum:
                break
            if types[new] == t and new not in sample:
                swap_in(new, prefer=business[new])
    return sample, swaps


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--write', action='store_true', help='add gate100 and work100 to the frozen-set file')
    args = parser.parse_args()
    sets = json.loads(SETS.read_text(encoding='utf-8'))
    used = set().union(*map(set, (v for k, v in sets.items() if k not in ('gate100', 'work100'))))
    business = {}
    for line in QUESTIONS.read_text(encoding='utf-8').splitlines():
        q = json.loads(line)
        business[q['id']] = q['business_id']
    pool = {i: b for i, b in business.items() if i not in used}
    print(f'pool: {len(pool)} questions in {len(set(pool.values()))} businesses, outside {len(used)} frozen ids')

    gate = draw('gate100', pool)
    gate_by = Counter(business[i] for i in gate)
    assert len(gate) == SIZE and len(set(gate)) == SIZE and len(gate_by) == len(set(pool.values()))
    assert min(gate_by.values()) >= FLOOR
    print(f'\ngate100: {len(gate)} questions, {len(gate_by)} businesses, {min(gate_by.values())} to '
          f'{max(gate_by.values())} each (answer types not read)')
    print(' ', ', '.join(f'{b} {n}' for b, n in sorted(gate_by.items())))

    # Answer types are read only now, and only for questions outside gate100.
    rest_pool = {i: b for i, b in pool.items() if i not in set(gate)}
    types = {}
    for line in ANSWERS.read_text(encoding='utf-8').splitlines():
        a = json.loads(line)
        if a['id'] in rest_pool:
            types[a['id']] = a['type']
    natural = draw('work100', rest_pool)
    work, swaps = top_up(natural, list(rest_pool), types, business)
    work_by = Counter(business[i] for i in work)
    assert len(work) == SIZE and len(set(work)) == SIZE and not set(work) & set(gate) and not set(work) & used
    print(f'\nwork100: {len(work)} questions, {len(work_by)} businesses, {min(work_by.values())} to '
          f'{max(work_by.values())} each; {len(swaps)} swaps after the natural draw')
    print(' ', ', '.join(f'{b} {n}' for b, n in sorted(work_by.items())))
    print('  answer types, natural draw:', dict(Counter(types[i] for i in natural).most_common()))
    print('  answer types, final:       ', dict(Counter(types[i] for i in work).most_common()))
    print('  swapped in by type:', dict(Counter(types[new] for new, _ in swaps).most_common()),
          '| by business:', dict(Counter(business[new] for new, _ in swaps).most_common()))
    short = {t: m for t, m in TYPE_MINIMUMS.items() if sum(types[i] == t for i in work) < m}
    if short:
        print('  minimum not reached (too few left in the pool):', short)

    if args.write:
        for name, ids in (('gate100', gate), ('work100', work)):
            if name in sets and sets[name] != ids:
                raise SystemExit(f'{name} already exists with other ids; not overwritten')
            sets[name] = ids
        text = SETS.read_text(encoding='utf-8')
        SETS.write_text(json.dumps(sets, indent=1) + ('\n' if text.endswith('\n') else ''), encoding='utf-8')
        print(f'\nwritten to {SETS.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
