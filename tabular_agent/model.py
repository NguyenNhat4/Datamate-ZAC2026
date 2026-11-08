"""Configure local or OpenAI-hosted smolagents models."""
from urllib.parse import urlsplit
import ipaddress
import json
import os
from pathlib import Path
import re
from dotenv import dotenv_values
import httpx
from smolagents import OpenAIModel
from .contracts import validate_answer

DEFAULT_OPENAI_MODEL = 'gpt-6-luna'


def normalize_final_json(content):
    """Convert an explicit typed JSON final answer into a CodeAgent action.

    Args:
        content: Model response content. Conversion requires a string of at
            most 16,000 characters, no opening code tag, and a trailing
            "Final answer:" JSON fence containing a valid typed answer.

    Returns:
        A string calling final_answer inside code tags if conversion succeeds;
        otherwise the original content unchanged, including non-string input.
    """
    if not isinstance(content, str) or '<code>' in content or len(content) > 16000:
        return content
    match = re.search(r'(?is)\bfinal\s+answer\s*:\s*```json\s*(\{.*?\})\s*```\s*(?:</code>)?\s*$', content)
    if not match:
        return content
    try:
        value = json.loads(match.group(1), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        answer = validate_answer(value)
    except (ValueError, TypeError, KeyError):
        return content
    return f'Thought: Submit the typed final answer.\n<code>\nfinal_answer({answer!r})\n</code>'


class LocalModel(OpenAIModel):
    def generate(self, messages, **kwargs):
        """Request a model response and reject incomplete generated actions.

        Args:
            messages: Framework chat messages forwarded to OpenAIModel.generate.
            **kwargs: Generation options, including optional tools_to_call_from.

        Returns:
            ChatMessage from the parent model. Without tool-call mode, explicit
            final JSON may be rewritten into a final_answer code action.

        Raises:
            ValueError: The response did not finish with stop, or tool_calls with
                actual tool calls. Provider request errors also propagate.
        """
        response = super().generate(messages, **kwargs)
        raw = response.raw
        choices = raw.get('choices', []) if isinstance(raw, dict) else getattr(raw, 'choices', [])
        finish = (choices[0].get('finish_reason') if isinstance(choices[0], dict)
                  else getattr(choices[0], 'finish_reason', None)) if choices else None
        if finish != 'stop' and not (finish == 'tool_calls' and response.tool_calls):
            raise ValueError(f'Incomplete model response ({finish!r}); generated code was not executed')
        if not kwargs.get('tools_to_call_from'):
            response.content = normalize_final_json(response.content)
        return response


class OpenAIAPIModel(LocalModel):
    # Avoid sending stop sequences, which are unsupported by some reasoning models.
    @property
    def supports_stop_parameter(self):
        """Report whether the hosted model adapter accepts stop sequences.

        Returns:
            False so framework requests omit the stop parameter.
        """
        return False

    def generate(self, messages, **kwargs):
        """Generate through the hosted API while redacting keys from error messages.

        Args:
            messages: Framework chat messages sent to the model.
            **kwargs: Generation options forwarded to LocalModel.generate.

        Returns:
            Validated-completion ChatMessage, with final JSON normalized when eligible.

        Raises:
            RuntimeError: Generation failed. The error text replaces the configured
                API key and recognized sk-prefixed tokens with [redacted].
        """
        try:
            return super().generate(messages, **kwargs)
        except Exception as error:
            message = str(error)
            key = self.client_kwargs.get('api_key')
            if key:
                message = message.replace(key, '[redacted]')
            message = re.sub(r'sk-[A-Za-z0-9_-]+', '[redacted]', message)
            raise RuntimeError(f'{type(error).__name__}: {message}') from None


def _setting(name):
    """Resolve a text setting from the environment or the working directory's .env.

    Args:
        name: Environment variable name to look up.

    Returns:
        Stripped value from a truthy environment entry, otherwise .env, or an
        empty string when unavailable. Reads .env without updating os.environ.
    """
    value = os.environ.get(name)
    if value:
        return value.strip()
    path = Path('.env')
    return (dotenv_values(path).get(name) or '').strip() if path.is_file() else ''


def openai_api_key():
    """Read the hosted API credential without displaying it.

    Returns:
        Stripped OPENAI_API_KEY from the environment, otherwise local .env,
        or an empty string when absent. The caller must keep this value private.
    """
    return _setting('OPENAI_API_KEY')


def openai_model_name():
    """Resolve the default hosted model name from local configuration.

    Returns:
        OPENAI_MODEL from the environment or .env, falling back to
        DEFAULT_OPENAI_MODEL when the resolved setting is empty.
    """
    return _setting('OPENAI_MODEL') or DEFAULT_OPENAI_MODEL


def create_model(model_id, base_url, *, max_tokens=2048, timeout=120, seed=42):
    """Configure an OpenAI-compatible model client for a local HTTP endpoint.

    Args:
        model_id: Model alias served by the existing local server.
        base_url: HTTP base URL using localhost or a loopback IP, without
            credentials, query parameters or a fragment.
        max_tokens: Maximum output-token budget sent to the server.
        timeout: HTTP request timeout in seconds.
        seed: Seed passed to the server; determinism depends on that server.

    Returns:
        LocalModel with proxies, redirects and request retries disabled. No
        inference is requested here. Close its client when it is no longer used.

    Raises:
        ValueError: The URL is not an allowed local HTTP endpoint.
    """
    parsed = urlsplit(base_url)
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname == 'localhost'
    if parsed.scheme != 'http' or not local or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Inference requires a local HTTP model endpoint')
    def reject_redirect(response):
        """Reject a redirect response from the local model server.

        Args:
            response: httpx response passed to the client's response hook.

        Returns:
            None when the response is not a redirect.

        Raises:
            ValueError: The server attempts to redirect the request.
        """
        if response.is_redirect:
            raise ValueError('Local model redirects are not allowed')
    client = httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                          event_hooks={'response': [reject_redirect]})
    return LocalModel(model_id=model_id, api_base=base_url, api_key='local',
                       max_tokens=max_tokens, seed=seed, retry=False,
                       client_kwargs={'http_client': client, 'max_retries': 0})


def create_openai_model(model_id, *, max_tokens=2048, timeout=120):
    """Configure a hosted model client using local API credentials.

    Args:
        model_id: Hosted model name to request.
        max_tokens: Maximum completion-token budget sent to the API.
        timeout: HTTP request timeout in seconds.

    Returns:
        OpenAIAPIModel fixed to https://api.openai.com/v1 with storage, proxies,
        redirects and retries disabled. No inference is requested here. Close
        its client when it is no longer used.

    Raises:
        ValueError: OPENAI_API_KEY is absent from both the environment and .env.
    """
    key = openai_api_key()
    if not key:
        raise ValueError('Set OPENAI_API_KEY in the environment or the local .env file')

    def reject_redirect(response):
        """Reject a redirect response from the hosted API.

        Args:
            response: httpx response passed to the client's response hook.

        Returns:
            None when the response is not a redirect.

        Raises:
            ValueError: The API attempts to redirect the request.
        """
        if response.is_redirect:
            raise ValueError('OpenAI API redirects are not allowed')

    client = httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                          event_hooks={'response': [reject_redirect]})
    return OpenAIAPIModel(model_id=model_id, api_base='https://api.openai.com/v1', api_key=key,
                          max_completion_tokens=max_tokens, store=False, retry=False,
                          client_kwargs={'http_client': client, 'max_retries': 0})
