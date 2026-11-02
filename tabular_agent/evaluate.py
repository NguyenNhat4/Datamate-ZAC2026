"""Internal approximation of the documented matcher, NOT the official scorer."""
from decimal import Decimal, ROUND_HALF_UP
import json
import unicodedata
from .contracts import number, validate_answer


def normalize(value):
    text = unicodedata.normalize('NFC', value).lower()
    return ' '.join(''.join(c if c.isalnum() or c.isspace() else ' ' for c in text).split())


def cell_match(pred, ref, aliases=()):
    if isinstance(ref, str):
        return isinstance(pred, str) and normalize(pred) in {normalize(x) for x in [ref, *aliases] if isinstance(x, str)}
    if number(ref):
        return number(pred) and abs(Decimal(str(pred)) - Decimal(str(ref))) <= Decimal('0.00000001')
    return type(pred) is type(ref) and pred == ref


def match(pred, ref):
    pred = validate_answer(pred)
    if pred['type'] != ref['type']:
        return False
    kind = ref['type']
    if kind == 'unanswerable':
        return True
    p, r, aliases = pred.get('answer'), ref.get('answer'), ref.get('alias', [])
    if kind == 'percent':
        return Decimal(str(p)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) == Decimal(str(r)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    if kind == 'date':
        return p == r
    if kind == 'list':
        return len(p) == len(r) and all(cell_match(x, y, aliases[i] if i < len(aliases) and isinstance(aliases[i], list) else [])
                                      for i, (x, y) in enumerate(zip(p, r)))
    if kind == 'table':
        pc, rc = [normalize(x) for x in p['columns']], [normalize(x) for x in r['columns']]
        if len(pc) != len(rc):
            return False
        order = [pc.index(c) for c in rc] if len(set(pc)) == len(pc) and set(pc) == set(rc) else list(range(len(rc)))
        prows = list({json.dumps([row[j] for j in order], ensure_ascii=False): tuple(row[j] for j in order) for row in p['rows']}.values())
        # Reference aliases belong to original row indices (first/name column).
        rrows = []
        seen = set()
        for i, row in enumerate(r['rows']):
            key = json.dumps(row, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                rrows.append((row, aliases[i] if i < len(aliases) and isinstance(aliases[i], list) else []))
        if len(prows) != len(rrows):
            return False
        edges = [[j for j, (rr, aa) in enumerate(rrows)
                  if all(cell_match(x, y, aa if k == 0 else []) for k, (x, y) in enumerate(zip(pr, rr)))] for pr in prows]
        # Bipartite matching avoids greedy failures when aliases overlap.
        assigned = {}
        def assign(i, visited):
            for j in edges[i]:
                if j not in visited:
                    visited.add(j)
                    if j not in assigned or assign(assigned[j], visited):
                        assigned[j] = i
                        return True
            return False
        return all(assign(i, set()) for i in range(len(prows)))
    return cell_match(p, r, aliases if kind == 'string' else [])


def score(questions, predictions, references):
    expected = {q['id'] for q in questions}
    if len(expected) != len(questions) or not expected:
        raise ValueError('Expected a nonempty unique question set')
    refs = {r['id']: r for r in references}
    preds = {p['id']: p for p in predictions}
    if len(preds) != len(predictions):
        raise ValueError('Duplicate prediction IDs')
    if set(preds) - expected:
        raise ValueError('Predictions include IDs outside selected questions')
    if expected - set(refs):
        raise ValueError('References missing for selected questions')
    counts = {'correct': 0, 'wrong': 0, 'abstain': 0, 'invalid': 0}
    by_type, by_business, details = {}, {}, []
    for q in questions:
        ref, pred = refs[q['id']], preds.get(q['id'])
        if pred is None:
            outcome = 'abstain'
        else:
            try:
                valid = validate_answer(pred)
                outcome = 'abstain' if valid['type'] == 'abstain' else ('correct' if match(valid, ref) else 'wrong')
            except (ValueError, TypeError, KeyError):
                outcome = 'wrong'
                counts['invalid'] += 1
        counts[outcome] += 1
        for grouping, key in ((by_type, ref['type']), (by_business, q['business_id'])):
            row = grouping.setdefault(key, {'correct': 0, 'wrong': 0, 'abstain': 0})
            row[outcome] += 1
        details.append({'id': q['id'], 'outcome': outcome})
    n = len(questions)
    answered = counts['correct'] + counts['wrong']
    return {'evaluator': 'internal-v1-not-official', 'total': n, **counts,
            'pa': (counts['correct'] - counts['wrong']) / n,
            'coverage': answered / n, 'answered_accuracy': counts['correct'] / answered if answered else None,
            'by_type': by_type, 'by_business': by_business, 'questions': details}
