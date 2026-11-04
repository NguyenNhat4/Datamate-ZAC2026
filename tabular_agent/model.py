"""Native OpenAIModel transport to a provisioned loopback model server."""
import json
import os
from urllib.parse import urlparse

import httpx
from smolagents import OpenAIModel

from .runtime import QuestionStopped


def local_endpoint(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Use a loopback HTTP model endpoint without URL credentials")
    return url.rstrip("/")


def reject_redirect(response):
    if response.is_redirect:
        raise ValueError("Model endpoint redirects are disabled")


class LocalCodeModel(OpenAIModel):
    def __init__(self, model_id, base_url, *, seed=42, max_tokens=2048, context=32768, timeout=120):
        base_url = local_endpoint(base_url)
        self.deadline = None
        self.request_seconds = timeout
        self.context_capacity = context
        self.output_tokens = max_tokens
        self.tokenizer_supported = None
        self.context_counting = "not yet measured"
        self.http_client = httpx.Client(trust_env=False, follow_redirects=False,
                                       timeout=timeout, event_hooks={"response": [reject_redirect]})
        super().__init__(
            model_id=model_id, api_base=base_url,
            api_key=os.environ.get("LOCAL_MODEL_API_KEY") or "local",
            client_kwargs={"max_retries": 0, "http_client": self.http_client, "timeout": timeout},
            retry=False, temperature=0, seed=seed, max_tokens=max_tokens,
        )

    def generate(self, messages, **kwargs):
        timeout = self.request_seconds
        if self.deadline is not None:
            timeout = min(timeout, self.deadline.remaining())
        completion = self._prepare_completion_kwargs(
            messages=messages, model=self.model_id, custom_role_conversions=self.custom_role_conversions,
            convert_images_to_image_urls=True, **kwargs,
        )
        input_tokens = self.count_input_tokens(completion["messages"], timeout)
        if input_tokens + self.output_tokens + 128 > self.context_capacity:
            raise QuestionStopped("Context budget exhausted; reduce observations or start a larger context")
        if self.deadline is not None:
            timeout = min(timeout, self.deadline.remaining())
        return super().generate(messages, timeout=timeout, **kwargs)

    def count_input_tokens(self, messages, timeout):
        """Use llama.cpp's actual template/tokenizer when available, without inference."""
        if self.tokenizer_supported is not False:
            root = str(self.client.base_url).rstrip("/").removesuffix("/v1")
            headers = {"Authorization": f"Bearer {self.client.api_key}"}
            template = self.http_client.post(root + "/apply-template", json={"messages": messages},
                                             headers=headers, timeout=timeout)
            if template.status_code in {404, 405}:
                self.tokenizer_supported = False
            else:
                template.raise_for_status()
                prompt = template.json()["prompt"]
                tokens = self.http_client.post(root + "/tokenize", json={
                    "content": prompt, "add_special": True, "parse_special": True,
                }, headers=headers, timeout=timeout)
                tokens.raise_for_status()
                values = tokens.json()["tokens"]
                if not isinstance(prompt, str) or not isinstance(values, list):
                    raise ValueError("Invalid model-server tokenization response")
                self.tokenizer_supported = True
                self.context_counting = "server chat template and tokenizer"
                return len(values)
        # Other compatible servers may not expose tokenization. This conservative
        # fallback is explicit in run metadata and can abstain earlier than needed.
        self.context_counting = "conservative UTF-8 bytes (server tokenizer unavailable)"
        return len(json.dumps(messages, ensure_ascii=False).encode())

    def check(self):
        available = [model.id for model in self.client.models.list().data]
        if not self.model_id:
            if len(available) != 1:
                raise ValueError(f"Select one served model; available: {available}")
            self.model_id = available[0]
        if self.model_id not in available:
            raise ValueError(f"Model {self.model_id!r} is not served; available: {available}")
        return {"model": self.model_id, "base_url": str(self.client.base_url),
                "seed": self.kwargs.get("seed"), "temperature": 0,
                "max_tokens": self.output_tokens, "context": self.context_capacity,
                "context_counting": self.context_counting}
