"""Count which questions of a split the deterministic intents claim, per business. Prints counts only.

It reads questions and bundles, never reference answers, and prints no question text or answer value, so it is safe on
the public split. A question is "claimed" when an intent answers it from the business's bundle; the rest go to the model.
An `unanswerable` claim means the asked period lies outside the data or the data does not record the information.

Usage: .venv-native/bin/python scripts/claim-coverage.py --split data/public_test --prepared artifacts/prepared-r3-public
"""
import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tabular_agent.contracts import read_jsonl  # noqa: E402
from tabular_agent.intents import Context, answer  # noqa: E402
from tabular_agent.sql_agent import find_bundles  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--split', required=True)
    parser.add_argument('--prepared', required=True)
    args = parser.parse_args()
    questions = read_jsonl(ROOT / args.split / 'questions.jsonl')
    bundles = find_bundles(ROOT / args.prepared)
    by_business = collections.defaultdict(list)
    for q in questions:
        by_business[q['business_id']].append(q)
    totals, kinds = collections.Counter(), collections.Counter()
    print(f"{'business':10} {'questions':>9} {'claimed':>8} {'of which unanswerable':>22} {'to the model':>13}")
    for business, items in sorted(by_business.items()):
        if business not in bundles:
            print(f'{business:10} {len(items):9} {"no bundle":>8}')
            continue
        ctx = Context(bundles[business])
        claimed = unanswerable = 0
        for q in items:
            hit = answer(q['question'], q['as_of_date'], ctx)
            if hit:
                claimed += 1
                unanswerable += hit[0]['type'] == 'unanswerable'
                kinds[hit[1]] += 1
        ctx.close()
        print(f'{business:10} {len(items):9} {claimed:8} {unanswerable:22} {len(items) - claimed:13}')
        totals.update(questions=len(items), claimed=claimed, unanswerable=unanswerable)
    print(f"{'all':10} {totals['questions']:9} {totals['claimed']:8} {totals['unanswerable']:22} "
          f"{totals['questions'] - totals['claimed']:13}")
    print('\nclaims by intent:', ', '.join(f'{k} {v}' for k, v in kinds.most_common()))


if __name__ == '__main__':
    main()
