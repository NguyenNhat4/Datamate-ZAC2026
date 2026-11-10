"""Summarize a predict.sh run with aggregate counts only.

Prints no question text and no answer value. Reports:
- submission validity;
- answered and abstained questions per business;
- abstention reasons by category;
- model errors;
- timing;
- each business's preparation status, and whether its run-time bundle matches an earlier
  preparation (same bundle id, or the same schema.json).

Usage, from the repository root:
    .venv-native/bin/python scripts/dry-run-report.py <run dir> <work dir> <earlier prepared dir>
"""
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tabular_agent.contracts import validate_answer  # noqa: E402
from tabular_agent.sql_agent import find_bundles  # noqa: E402

# Abstention reasons written by the code, by their fixed text. Model-written reasons form one bucket.
FIXED = {
    'No prepared data for this business.': 'no bundle for the business',
    'Question deadline exceeded': 'question deadline (60 s)',
    'Unanswerable could not be verified against the data coverage.': 'unanswerable not confirmed',
    'Answer is not a value from the query result.': 'answer not in the query result',
    'Step limit reached without a validated answer.': 'step limit',
}


def category(reason):
    """Return the category of an abstention reason without exposing model-written text."""
    reason = reason or ''
    if reason in FIXED:
        return FIXED[reason]
    if 'rulebook-ready' in reason.lower():   # the fixed reason the refusal text asks the model to give
        return 'sales data not rulebook-ready'
    if 'context' in reason.lower() and 'exceed' in reason.lower():
        return 'request over the context size'
    if reason.startswith("ValueError: Incomplete model response ('length')"):
        return 'reply cut off at the length limit'   # usually a prompt that leaves too little room in the context
    match = re.match(r'([A-Z][A-Za-z]+(?:Error|Exception|Exceeded)):', reason)
    if match:
        return 'exception: ' + match.group(1)
    return 'model chose to abstain'


def preparation_log(path):
    """Map each business to its summary line in a preparation log."""
    out = {}
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if line.startswith('{'):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[item['business']] = item
    return out


def main(run, work, earlier):
    questions = [json.loads(line) for line in open(f'{run}/questions.jsonl', encoding='utf-8')]
    rows = [json.loads(line) for line in open(f'{run}/submission.jsonl', encoding='utf-8')]
    timings_path = Path(run) / 'timings.json'
    timings = {t['id']: t for t in json.load(open(timings_path, encoding='utf-8'))} if timings_path.exists() else {}
    business = {q['id']: q['business_id'] for q in questions}

    ids = [r['id'] for r in rows]
    invalid = 0
    for r in rows:
        try:
            validate_answer(r)
        except (ValueError, TypeError, KeyError):
            invalid += 1
    print(f'questions {len(questions)}; submission rows {len(rows)}; distinct ids {len(set(ids))}; '
          f'missing {len(set(business) - set(ids))}; unknown {len(set(ids) - set(business))}; invalid rows {invalid}')
    if not timings:
        print('timings.json is absent: the run did not finish, so timing and error counts are skipped')

    per_business = defaultdict(Counter)
    reasons = Counter()
    reasons_by_business = defaultdict(Counter)
    types = Counter()
    for r in rows:
        b = business[r['id']]
        per_business[b]['questions'] += 1
        if r['type'] == 'abstain':
            per_business[b]['abstain'] += 1
            c = category(r.get('reason', ''))
            reasons[c] += 1
            reasons_by_business[b][c] += 1
        else:
            per_business[b]['answered'] += 1
            types[r['type']] += 1
    print('\nanswer types:', dict(types.most_common()))
    print('abstentions by reason:', dict(reasons.most_common()))

    errors = Counter()
    errors_by_business = defaultdict(Counter)
    for qid, t in timings.items():
        for e in set(t.get('errors', [])):
            errors[e] += 1
            errors_by_business[business[qid]][e] += 1
    print('questions with each error type:', dict(errors.most_common()))

    now = preparation_log(f'{work}/prepare.log')
    fresh, old = find_bundles(f'{work}/prepared'), find_bundles(earlier)

    def schema(path):
        return json.load(open(path / 'schema.json', encoding='utf-8'))

    print('\nbusiness  questions  answered  abstain  abstain%  prep status  prep s  same id  same schema  '
          'main abstain reasons  error types')
    for b in sorted(per_business):
        c = per_business[b]
        item = now.get(b, {})
        same_id = b in fresh and b in old and fresh[b].name == old[b].name
        same_schema = b in fresh and b in old and schema(fresh[b]) == schema(old[b])
        top = ', '.join(f'{k} {v}' for k, v in reasons_by_business[b].most_common(3))
        errs = ', '.join(f'{k} {v}' for k, v in errors_by_business[b].most_common(2))
        print(f"{b:8}  {c['questions']:9}  {c['answered']:8}  {c['abstain']:7}  {100 * c['abstain'] / c['questions']:7.0f}%  "
              f"{item.get('status', '-'):11}  {item.get('measurements', {}).get('elapsed_seconds', 0):6.0f}  "
              f"{str(same_id):7}  {str(same_schema):11}  {top}  {errs}")
    answered = sum(c['answered'] for c in per_business.values())
    print(f'answered {answered} of {len(rows)}; abstained {len(rows) - answered}')
    total_prep = sum(i.get('measurements', {}).get('elapsed_seconds', 0) for i in now.values())
    print(f'preparation: {len(now)} businesses, {total_prep:.0f} s summed; '
          f'statuses {dict(Counter(i.get("status") for i in now.values()))}')

    if timings:
        seconds = sorted(t['seconds'] for t in timings.values())

        def pct(p):
            return seconds[max(0, math.ceil(p * len(seconds)) - 1)]   # nearest rank, as in timing-summary.json

        print(f'\nanswering: {len(seconds)} questions, {sum(seconds) / 3600:.2f} h summed; median {pct(.5):.1f} s, '
              f'p90 {pct(.9):.1f} s, max {seconds[-1]:.1f} s; over 30 s {sum(s > 30 for s in seconds)}; '
              f'at the 60 s deadline {sum(s >= 59.5 for s in seconds)}')
        calls = [t['model_calls'] for t in timings.values()]
        print(f'model calls per question: mean {sum(calls) / len(calls):.2f}, max {max(calls)}')
    pipeline = Path(work) / 'pipeline.log'
    if pipeline.exists():
        print('\nphases (pipeline.log):')
        print(pipeline.read_text(encoding='utf-8').rstrip())


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    main(*sys.argv[1:4])
