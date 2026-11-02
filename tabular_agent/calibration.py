"""Fit a small abstention policy from scored validation traces, never test labels."""
from pathlib import Path
import json

POLICIES = ('answer_all', 'verified_only', 'no_errors', 'verified_no_errors')
ERROR_SIGNALS = {'model_request_error', 'repeated_action_stop', 'malformed_model_output',
                 'result_or_evidence_invalid', 'verification_disagreement',
                 'execution_or_contract_error', 'worker_timeout', 'question_timeout', 'agent_exception'}
SETTINGS = ('schema_mode', 'verification', 'max_steps', 'max_executions', 'question_timeout',
            'exec_timeout', 'model_timeout', 'max_tokens', 'context', 'seed', 'gpu_layers',
            'cache_type_k', 'cache_type_v')


def permits(policy, trace, signals):
    if policy not in POLICIES:
        raise ValueError('Unknown calibrated policy')
    verified = bool(trace.get('independent_result_matched'))
    clean = not ERROR_SIGNALS.intersection(signals)
    return (policy == 'answer_all' or
            (policy == 'verified_only' and verified) or
            (policy == 'no_errors' and clean) or
            (policy == 'verified_no_errors' and verified and clean))


def fit_policy(config, report, logs, *, min_cases=20, min_answered=10):
    """Choose from a fixed policy family by validation PA, retaining coverage on ties."""
    training_split = (Path(__file__).resolve().parent.parent / 'data' / 'train').resolve()
    if Path(config['arguments'].get('split', '')).resolve() != training_split:
        raise ValueError('Calibration is restricted to the repository training split')
    if config['arguments'].get('partition') != 'validation':
        raise ValueError('Calibration requires a scored validation-partition run')
    if config['arguments'].get('abstention_policy'):
        raise ValueError('Fit on an uncalibrated run, not an already filtered run')
    if report.get('status') != 'completed' or report.get('evaluator') != 'internal-v1-not-official':
        raise ValueError('Calibration requires a complete scored benchmark')
    details = report.get('questions', [])
    if min_cases < 20 or min_answered < 10:
        raise ValueError('At least 20 questions and 10 answered questions are required')
    if len(details) < min_cases:
        raise ValueError(f'Not enough validation questions: {len(details)} < {min_cases}')
    answered = sum(d.get('outcome') in {'correct', 'wrong'} for d in details)
    if answered < min_answered:
        raise ValueError(f'Not enough answered validation questions: {answered} < {min_answered}')
    rows = []
    for detail in details:
        item = logs.get(detail['id'])
        if item is None:
            raise ValueError('Validation logs are incomplete')
        if detail['outcome'] not in {'correct', 'wrong', 'abstain'}:
            raise ValueError('Unknown evaluation outcome')
        rows.append((detail['outcome'], item, item.get('diagnostic_signals', [])))
    results = []
    for policy in POLICIES:
        correct = sum(outcome == 'correct' and permits(policy, trace, signals) for outcome, trace, signals in rows)
        wrong = sum(outcome == 'wrong' and permits(policy, trace, signals) for outcome, trace, signals in rows)
        count = len(rows)
        results.append({'policy': policy, 'correct': correct, 'wrong': wrong,
                        'pa': (correct - wrong) / count, 'coverage': (correct + wrong) / count})
    best = max(results, key=lambda row: (row['pa'], row['coverage'], -POLICIES.index(row['policy'])))
    model = config['model']
    if not model.get('weight_sha256_provided'):
        raise ValueError('Calibration requires an explicitly recorded model-weight SHA-256')
    return {'version': 1, 'policy': best['policy'], 'agent_source_sha256': config['agent_source_sha256'],
            'model': model['model'], 'weight_sha256': model['weight_sha256_provided'],
            'settings': {key: config['arguments'][key] for key in SETTINGS},
            'validation_questions_sha256': config['selected_questions_sha256'],
            'validation_count': len(details), 'validation_answered': answered, 'candidates': results,
            'note': 'Selected on validation, not a held-out performance estimate.'}


def load_run(path):
    root = Path(path)
    config = json.loads((root / 'config.json').read_text())
    report = json.loads((root / 'report.json').read_text())
    logs = {}
    for path in (root / 'logs').glob('*.json'):
        item = json.loads(path.read_text())
        if item['id'] in logs:
            raise ValueError('Duplicate log ID')
        logs[item['id']] = item
    return config, report, logs


def check_compatible(policy, config):
    if policy.get('version') != 1 or policy.get('policy') not in POLICIES:
        raise ValueError('Invalid calibration policy')
    if policy.get('agent_source_sha256') != config['agent_source_sha256']:
        raise ValueError('Calibrated policy does not match the current agent source')
    if (policy.get('model') != config['model']['model'] or
            policy.get('weight_sha256') != config['model'].get('weight_sha256_provided')):
        raise ValueError('Calibrated policy does not match the current model weights')
    if policy.get('settings') != {key: config['arguments'][key] for key in SETTINGS}:
        raise ValueError('Calibrated policy does not match current inference settings')
