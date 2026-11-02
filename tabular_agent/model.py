"""Local-only JSON action transport. No SDK, proxy, redirects, or downloads."""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Model endpoint redirects are disabled')


class LocalModel:
    def __init__(self, backend='ollama', base_url=None, model=None, timeout=120,
                 seed=42, max_tokens=2048, context=32768):
        self.backend = backend
        self.base_url = (base_url or ('http://127.0.0.1:11434' if backend == 'ollama' else 'http://127.0.0.1:8000/v1')).rstrip('/')
        parsed = urlparse(self.base_url)
        if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Only localhost model endpoints without URL credentials are allowed')
        self.model, self.timeout, self.seed = model, timeout, seed
        self.max_tokens, self.context = max_tokens, context
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.metadata = {}

    def request(self, path, payload=None, timeout=None):
        headers = {'Content-Type': 'application/json'}
        if os.environ.get('LOCAL_MODEL_API_KEY'):
            headers['Authorization'] = 'Bearer ' + os.environ['LOCAL_MODEL_API_KEY']
        req = Request(self.base_url + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as response:
                raw = response.read(2 * 1024**2 + 1)
            if len(raw) > 2 * 1024**2:
                raise RuntimeError('Model response exceeds 2 MiB')
            return json.loads(raw)
        except HTTPError as exc:
            raise RuntimeError(f'Local model HTTP {exc.code}; check model name, context and server configuration') from exc
        except TimeoutError as exc:
            raise RuntimeError('Local model generation exceeded the request time budget') from exc
        except URLError as exc:
            raise RuntimeError(f'Local model unavailable at {self.base_url}; start your server first') from exc

    def check(self):
        payload = self.request('/api/tags' if self.backend == 'ollama' else '/models')
        names = ([x['name'] for x in payload.get('models', [])] if self.backend == 'ollama'
                 else [x['id'] for x in payload.get('data', [])])
        if not self.model:
            if len(names) != 1:
                raise RuntimeError(f'Set MODEL to an installed model; available: {names}')
            self.model = names[0]
        if self.model not in names:
            raise RuntimeError(f'Model {self.model!r} is not installed/served. Available: {names}')
        self.metadata = {'backend': self.backend, 'base_url': self.base_url, 'model': self.model,
                         'seed': self.seed, 'temperature': 0, 'max_tokens': self.max_tokens, 'context': self.context}
        if self.backend == 'ollama':
            details = self.request('/api/show', {'model': self.model})
            self.metadata['details'] = details.get('details', {})
            self.metadata['model_info'] = details.get('model_info', {})
            self.metadata['digest'] = next((x.get('digest') for x in payload['models'] if x['name'] == self.model), None)
        return self.metadata

    def complete(self, messages, timeout=None):
        common = {'model': self.model, 'messages': messages, 'stream': False}
        if self.backend == 'ollama':
            payload = {**common, 'format': 'json', 'options': {'temperature': 0, 'seed': self.seed,
                       'num_predict': self.max_tokens, 'num_ctx': self.context}}
            result = self.request('/api/chat', payload, timeout)
            usage = {k: result.get(k) for k in ('prompt_eval_count', 'eval_count', 'load_duration')}
            usage['finish_reason'] = result.get('done_reason')
            return result['message']['content'], usage
        payload = {**common, 'temperature': 0, 'seed': self.seed, 'max_tokens': self.max_tokens,
                   'response_format': {'type': 'json_object'}}
        result = self.request('/chat/completions', payload, timeout)
        choice = result['choices'][0]
        return choice['message']['content'], {**result.get('usage', {}), 'finish_reason': choice.get('finish_reason')}
