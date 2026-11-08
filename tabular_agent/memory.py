"""Capture smolagents' per-question memory for local inspection."""


def memory_callback(emit):
    """Return a smolagents step callback; do not mutate the agent's memory."""
    def record(step, agent):
        if emit is None:
            return
        data = step.dict()
        emit({
            'stage': 'memory_step',
            'step': data['step_number'],
            'model_input_messages': data['model_input_messages'],
            'model_output': data['model_output'],
            'code_action': data['code_action'],
            'tool_calls': data.get('tool_calls'),
            'observations': data['observations'],
            'error': data['error'],
            'token_usage': data['token_usage'],
            'is_final_answer': data['is_final_answer'],
        })
    return record
