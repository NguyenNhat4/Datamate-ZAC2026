"""Typed output validation; shared by inference and the internal evaluator."""
import datetime
import json
import math
import re

TYPES = {'number', 'percent', 'string', 'date', 'boolean', 'list', 'table', 'unanswerable', 'abstain'}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def scalar(value):
    return isinstance(value, str) or number(value)


def validate_answer(obj):
    if not isinstance(obj, dict) or obj.get('type') not in TYPES:
        raise ValueError('Expected an object with a supported type')
    kind, value = obj['type'], obj.get('answer')
    if 'reason' in obj and not isinstance(obj['reason'], str):
        raise ValueError('reason must be a string')
    if kind in {'abstain', 'unanswerable'}:
        return {k: obj[k] for k in ('type', 'reason') if k in obj}
    valid = False
    if kind in {'number', 'percent'}:
        valid = number(value)
    elif kind == 'string':
        valid = isinstance(value, str)
    elif kind == 'boolean':
        valid = type(value) is bool
    elif kind == 'date' and isinstance(value, str):
        if re.fullmatch(r'\d{4}-\d{2}(-\d{2})?', value):
            try:
                datetime.date.fromisoformat(value if len(value) == 10 else value + '-01')
                valid = True
            except ValueError:
                pass
    elif kind == 'list':
        valid = isinstance(value, list) and all(scalar(x) for x in value)
    elif kind == 'table' and isinstance(value, dict):
        cols, rows = value.get('columns'), value.get('rows')
        valid = (isinstance(cols, list) and bool(cols) and all(isinstance(c, str) for c in cols)
                 and isinstance(rows, list) and all(isinstance(r, list) and len(r) == len(cols)
                 and all(scalar(c) for c in r) for r in rows))
    if not valid:
        raise ValueError(f'Invalid answer for type {kind}')
    return {'type': kind, 'answer': value}


def parse_json(text):
    text = text.strip()
    if text.startswith('```') and text.endswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return json.loads(text, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f'Invalid JSON: {x}')))


def read_jsonl(path):
    result = []
    seen = set()
    with open(path, encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            obj = parse_json(line)
            if not isinstance(obj, dict) or not isinstance(obj.get('id'), str):
                raise ValueError(f'{path}:{lineno}: missing string id')
            if obj['id'] in seen:
                raise ValueError(f'{path}:{lineno}: duplicate id')
            seen.add(obj['id'])
            result.append(obj)
    return result
