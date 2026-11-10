"""Replay the intent layer against the open training questions and their reference answers, with no model.

Closed samples (gate100, holdout48) are never read. For every other training question the script runs the
deterministic intents against the business's prepared bundle and compares with the reference using the internal
scorer. It reports, per intent, how many questions were claimed and how many claims were right, and lists the wrong
claims so that a parser or query error can be fixed. It prints nothing about the closed samples.

Usage: .venv-native/bin/python scripts/oracle-replay.py --prepared artifacts/prepared-r2-train [--show N] [--json out]
"""
import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tabular_agent.contracts import read_jsonl, validate_answer  # noqa: E402
from tabular_agent.evaluate import match  # noqa: E402
from tabular_agent.intents import Context, answer  # noqa: E402
from tabular_agent.sql_agent import find_bundles  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--split', default='data/train')
    parser.add_argument('--prepared', required=True)
    parser.add_argument('--show', type=int, default=15, help='wrong claims to print per intent')
    parser.add_argument('--unclaimed', type=int, default=0, help='unclaimed questions to print')
    parser.add_argument('--business')
    parser.add_argument('--json')
    args = parser.parse_args()
    split = ROOT / args.split
    sets = json.loads((ROOT / 'docs/fix/2026-11-10_unseen_businesses/question_sets.json').read_text(encoding='utf-8'))
    closed = set(sets['gate100']) | set(sets['holdout48'])
    questions = [q for q in read_jsonl(split / 'questions.jsonl') if q['id'] not in closed]
    references = {a['id']: a for a in read_jsonl(split / 'answers.jsonl') if a['id'] not in closed}
    bundles = find_bundles(ROOT / args.prepared)
    by_business = collections.defaultdict(list)
    for q in questions:
        if not args.business or q['business_id'] == args.business:
            by_business[q['business_id']].append(q)
    stats = collections.defaultdict(lambda: [0, 0])  # intent -> [right, wrong]
    wrong, unclaimed, results = collections.defaultdict(list), [], {}
    for business, items in sorted(by_business.items()):
        ctx = Context(bundles[business])
        for q in items:
            hit = answer(q['question'], q['as_of_date'], ctx)
            if hit is None:
                unclaimed.append(q)
                continue
            result, name, _ = hit
            ok = match(validate_answer(result), references[q['id']])
            stats[name][0 if ok else 1] += 1
            results[q['id']] = {'intent': name, 'ok': ok}
            if not ok:
                wrong[name].append((business, q, result, references[q['id']]))
        ctx.close()
    total = len(questions) if not args.business else sum(len(v) for v in by_business.values())
    claimed = sum(r + w for r, w in stats.values())
    print(f'questions {total}  claimed {claimed}  unclaimed {len(unclaimed)}  '
          f'right {sum(r for r, _ in stats.values())}  wrong {sum(w for _, w in stats.values())}')
    for name, (right, bad) in sorted(stats.items(), key=lambda kv: -sum(kv[1])):
        print(f'  {name:24} claimed {right + bad:4}  right {right:4}  wrong {bad:3}')
    for name, items in wrong.items():
        print(f'\n== wrong claims: {name} ({len(items)})')
        for business, q, result, reference in items[:args.show]:
            ref = {k: v for k, v in reference.items() if k in ('type', 'answer')}
            print(f"{business} {q['as_of_date']} | {q['question']}\n    got {json.dumps(result, ensure_ascii=False)[:160]}"
                  f"\n    ref {json.dumps(ref, ensure_ascii=False)[:160]}")
    if args.unclaimed:
        print('\n== unclaimed sample')
        for q in unclaimed[:args.unclaimed]:
            print(f"{q['business_id']} | {q['question']}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
