"""Submission format and JSONL utilities."""
import datetime
import json
import math
import re


def read_jsonl(path):
    """Read UTF-8 JSON Lines records with unique string identifiers.

    Args:
        path: Filesystem path to a JSONL file; blank lines are ignored.

    Returns:
        List of dictionaries in file order, each with a unique string id.

    Raises:
        OSError: The file cannot be opened or read.
        ValueError: JSON is invalid, contains nonstandard numeric constants,
            or a record is not an object with a unique string id.
    """
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
    """Check a typed answer and return its submission fields.

    Args:
        answer: Dictionary with type and answer, or type and optional reason
            for abstain/unanswerable. Numbers must be finite; booleans are
            not numbers. Lists and table cells accept strings or numbers.

    Returns:
        New dictionary containing type and answer, or type and a string
        reason (defaulting to empty). Extra fields, including id, are removed;
        nested answer values are reused rather than copied.

    Raises:
        ValueError: The dictionary, declared type, date, reason or value is
            invalid. Table rows must match the nonempty columns list's width.
    """
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
