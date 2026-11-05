"""Scripted model responses for mechanism checks, never model-quality evidence."""
from smolagents import Model
from smolagents.models import ChatMessage, MessageRole


def response(code, status="stop"):
    return ChatMessage(role=MessageRole.ASSISTANT, content=f"<code>\n{code}\n</code>",
                       raw={"choices": [{"finish_reason": status}]})


class ScriptedModel(Model):
    def __init__(self, responses):
        super().__init__(model_id="scripted-fixture")
        self.responses = list(responses)
        self.calls = 0
        self.messages = []

    def generate(self, messages, **kwargs):
        self.messages.append(str(messages))
        if self.calls >= len(self.responses):
            raise AssertionError("Unexpected extra model generation")
        result = self.responses[self.calls]
        self.calls += 1
        return result

    def check(self):
        return {"model": "scripted-fixture"}


def calculation(**changes):
    record = {"sources": ["F1"], "required_columns": {"F1": ["amount"]}, "metric": "sum", "period": None,
              "grain": "line", "unit": "bottles", "rules": ["current rules"],
              "row_count": 1, "checks": {"rows": 1}}
    return {**record, **changes}
