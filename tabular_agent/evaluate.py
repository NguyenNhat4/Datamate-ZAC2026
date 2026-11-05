"""Internal approximation of the documented matcher, not the official evaluator."""
from decimal import Decimal, ROUND_HALF_UP
import json
import unicodedata
from .contracts import validate_answer


def normalize(value):
    text = unicodedata.normalize('NFC', value).lower()
    return ' '.join(''.join(c if c.isalnum() or c.isspace() else ' ' for c in text).split())


def cell_match(actual, expected, aliases=()):
    if isinstance(expected, str):
        return isinstance(actual, str) and normalize(actual) in {normalize(x) for x in (expected, *aliases) if isinstance(x, str)}
    if type(actual) in (int, float) and type(expected) in (int, float):
        return abs(Decimal(str(actual)) - Decimal(str(expected))) <= Decimal('0.00000001')
    return type(actual) is type(expected) and actual == expected


def match(actual, expected):
    kind = expected.get('type')
    if actual.get('type') != kind:
        return False
    if kind == 'unanswerable':
        return True
    a, b = actual.get('answer'), expected.get('answer')
    if kind == 'string':
        return cell_match(a, b, expected.get('alias', []))
    if kind == 'percent':
        try:
            return type(a) in (int, float) and Decimal(str(a)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) == Decimal(str(b)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        except Exception:
            return False
    if kind == 'list':
        aliases = expected.get('alias', [])
        return isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and all(
            cell_match(x, y, aliases[i] if i < len(aliases) and isinstance(aliases[i], list) else [])
            for i, (x, y) in enumerate(zip(a, b)))
    if kind == 'table':
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        ac, bc = a.get('columns', []), b.get('columns', [])
        ar, br = a.get('rows', []), b.get('rows', [])
        if len(ac) != len(bc):
            return False
        normalized = [normalize(c) for c in ac]
        target = [normalize(c) for c in bc]
        indices = [normalized.index(c) for c in target] if len(set(normalized)) == len(normalized) and set(normalized) == set(target) else list(range(len(ac)))
        predicted = list({json.dumps([row[i] for i in indices], ensure_ascii=False): [row[i] for i in indices] for row in ar}.values())
        aliases = expected.get('alias', [])
        reference = {}
        for i, row in enumerate(br):
            reference.setdefault(json.dumps(row, ensure_ascii=False), (row, aliases[i] if i < len(aliases) and isinstance(aliases[i], list) else []))
        rows = list(reference.values())
        if len(predicted) != len(rows):
            return False
        edges = [[j for j, (row, names) in enumerate(rows) if len(p) == len(row) and all(
            cell_match(x, y, names if k == 0 else []) for k, (x, y) in enumerate(zip(p, row)))] for p in predicted]
        assigned = {}
        def assign(i, seen):
            for j in edges[i]:
                if j not in seen:
                    seen.add(j)
                    if j not in assigned or assign(assigned[j], seen):
                        assigned[j] = i
                        return True
            return False
        return all(assign(i, set()) for i in range(len(predicted)))
    if kind == 'boolean':
        return type(a) is bool and a == b
    if kind == 'number':
        return cell_match(a, b)
    return a == b


def score(questions, predictions, references):
    guesses = {p['id']: p for p in predictions}
    truth = {r['id']: r for r in references}
    selected = {q['id'] for q in questions}
    if not selected or len(selected) != len(questions) or len(guesses) != len(predictions):
        raise ValueError('Questions and predictions must have unique IDs, and questions cannot be empty')
    if set(guesses) - selected:
        raise ValueError('Predictions include IDs outside the selected questions')
    outcomes = []
    for question in questions:
        key = question['id']
        if key not in truth:
            raise ValueError('A selected question has no reference')
        guess = guesses.get(key, {'type': 'abstain'})
        try:
            validate_answer(guess)
            status = 'abstain' if guess.get('type') == 'abstain' else 'correct' if match(guess, truth[key]) else 'wrong'
        except (ValueError, TypeError, KeyError):
            status = 'wrong'
        outcomes.append({'id': key, 'outcome': status})
    total = len(outcomes)
    correct = sum(item['outcome'] == 'correct' for item in outcomes)
    wrong = sum(item['outcome'] == 'wrong' for item in outcomes)
    return {'evaluator': 'internal-not-official', 'total': total, 'correct': correct, 'wrong': wrong, 'abstain': total - correct - wrong,
            'penalized_accuracy': (correct - wrong) / total if total else 0, 'questions': outcomes}
