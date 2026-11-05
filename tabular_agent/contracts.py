"""Submission format and JSONL utilities."""
import datetime
import json
import math
import re


def read_jsonl(path):
    result, seen = [], set()
    with open(path, encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            item = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] in seen:
                raise ValueError('JSONL rows need unique string IDs')
            seen.add(item['id'])
            result.append(item)
    return result


def validate_answer(answer):
    if not isinstance(answer, dict):
        raise ValueError('Expected an answer dictionary')
    kind = answer.get('type')
    value = answer.get('answer')
    if kind in ('abstain', 'unanswerable'):
        reason = answer.get('reason', '')
        if not isinstance(reason, str):
            raise ValueError('reason must be text')
        return {'type': kind, 'reason': reason}
    is_number = lambda x: type(x) in (int, float) and math.isfinite(x)
    is_scalar = lambda x: isinstance(x, str) or is_number(x)
    if kind in ('number', 'percent'):
        valid = is_number(value)
    elif kind == 'string':
        valid = isinstance(value, str)
    elif kind == 'boolean':
        valid = type(value) is bool
    elif kind == 'date':
        valid = False
        if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}(-\d{2})?', value):
            try:
                datetime.date.fromisoformat(value if len(value) == 10 else value + '-01')
                valid = True
            except ValueError:
                pass
    elif kind == 'list':
        valid = isinstance(value, list) and all(is_scalar(item) for item in value)
    elif kind == 'table':
        valid = (isinstance(value, dict) and isinstance(value.get('columns'), list) and bool(value['columns'])
                 and all(isinstance(c, str) for c in value['columns']) and isinstance(value.get('rows'), list)
                 and all(isinstance(row, list) and len(row) == len(value['columns'])
                         and all(is_scalar(cell) for cell in row) for row in value['rows']))
    else:
        valid = False
    if not valid:
        raise ValueError(f'Invalid answer for type {kind}')
    return {'type': kind, 'answer': value}
