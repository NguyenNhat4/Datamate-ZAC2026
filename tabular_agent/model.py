"""Configure local or OpenAI-hosted smolagents models."""
from urllib.parse import urlsplit
import ipaddress
import os
from pathlib import Path
import re
from dotenv import dotenv_values
import httpx
from smolagents import OpenAIModel

DEFAULT_OPENAI_MODEL = 'gpt-6-luna'


class LocalModel(OpenAIModel):
    def generate(self, messages, **kwargs):
        response = super().generate(messages, **kwargs)
        raw = response.raw
        choices = raw.get('choices', []) if isinstance(raw, dict) else getattr(raw, 'choices', [])
        finish = (choices[0].get('finish_reason') if isinstance(choices[0], dict)
                  else getattr(choices[0], 'finish_reason', None)) if choices else None
        if finish != 'stop':
            raise ValueError(f'Incomplete model response ({finish!r}); generated code was not executed')
        return response


class OpenAIAPIModel(LocalModel):
    # Avoid sending stop sequences, which are unsupported by some reasoning models.
    @property
    def supports_stop_parameter(self):
        return False

    def generate(self, messages, **kwargs):
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
    value = os.environ.get(name)
    if value:
        return value.strip()
    path = Path('.env')
    return (dotenv_values(path).get(name) or '').strip() if path.is_file() else ''


def openai_api_key():
    """Read the key without displaying it; environment takes priority over .env."""
    return _setting('OPENAI_API_KEY')


def openai_model_name():
    return _setting('OPENAI_MODEL') or DEFAULT_OPENAI_MODEL


def create_model(model_id, base_url, *, max_tokens=2048, timeout=120, seed=42):
    parsed = urlsplit(base_url)
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname == 'localhost'
    if parsed.scheme != 'http' or not local or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Inference requires a local HTTP model endpoint')
    def reject_redirect(response):
        if response.is_redirect:
            raise ValueError('Local model redirects are not allowed')
    client = httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                          event_hooks={'response': [reject_redirect]})
    return LocalModel(model_id=model_id, api_base=base_url, api_key='local',
                       max_tokens=max_tokens, seed=seed, retry=False,
                       client_kwargs={'http_client': client, 'max_retries': 0})


def create_openai_model(model_id, *, max_tokens=2048, timeout=120):
    """Use the OpenAI API with the key from the environment or local .env."""
    key = openai_api_key()
    if not key:
        raise ValueError('Set OPENAI_API_KEY in the environment or the local .env file')

    def reject_redirect(response):
        if response.is_redirect:
            raise ValueError('OpenAI API redirects are not allowed')

    client = httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                          event_hooks={'response': [reject_redirect]})
    return OpenAIAPIModel(model_id=model_id, api_base='https://api.openai.com/v1', api_key=key,
                          max_completion_tokens=max_tokens, store=False, retry=False,
                          client_kwargs={'http_client': client, 'max_retries': 0})
