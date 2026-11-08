"""Capture smolagents' per-question memory for local inspection."""


def memory_callback(emit):
    """Create a callback that records completed framework action steps.

    Args:
        emit: Callback receiving one event dictionary, or None to disable output.

    Returns:
        A record(step, agent) callback compatible with smolagents step callbacks.
        It reads the step without modifying framework memory. Events can include
        complete model messages, observations and business data.
    """
    def record(step, agent):
        """Emit the recorded model context and outcome of one action step.

        Args:
            step: Framework step exposing dict() with model messages, code,
                observations, errors, token usage and final-answer status.
            agent: Agent supplied by the framework callback; unused.

        Returns:
            None. Sends a memory_step event to the captured emit callback when set.
            Exceptions from step serialization or the callback propagate.
        """
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
