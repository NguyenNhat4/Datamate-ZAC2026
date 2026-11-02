"""Coarse, non-exclusive trace signals for post-inference evaluation."""
import json


def trace_signals(trace, answer):
    signals = set()
    for event in trace.get('events', []):
        if event.get('model_error'):
            signals.add('model_request_error')
        if event.get('stopped') in {'repeated_action', 'repeated_review'}:
            signals.add('repeated_action_stop')
        try:
            action = json.loads(event.get('response', '{}')).get('action')
        except (ValueError, TypeError, AttributeError):
            signals.add('malformed_model_output')
            action = None
        observation = event.get('observation') or {}
        error = str(observation.get('error', ''))
        validation = str(observation.get('result_validation_error', ''))
        if validation:
            signals.add('result_or_evidence_invalid')
            if 'disagrees' in validation:
                signals.add('verification_disagreement')
        if action in {'python', 'verify'} and observation.get('ok') is False and error:
            if not error.startswith('Identical action'):
                signals.add('execution_or_contract_error')
        if 'timed out' in error.lower():
            signals.add('worker_timeout')
    if answer.get('type') == 'abstain':
        reason = str(answer.get('reason', '')).lower()
        if 'time budget' in reason:
            signals.add('question_timeout')
        if 'budget exhausted' in reason:
            signals.add('agent_budget_exhausted')
    if trace.get('error'):
        signals.add('agent_exception')
    return sorted(signals)
