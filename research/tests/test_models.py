"""agent/models.py: the mock's protocol and the Tinker adapter's reply handling, with a fake sampler and renderer.
No network, no key."""

import asyncio
import json
from types import SimpleNamespace

import pytest

import models


class FakeRenderer:
    def __init__(self, parts):
        self.parts = parts

    def build_generation_prompt(self, messages):
        return SimpleNamespace(length=123, messages=messages)

    def get_stop_sequences(self):
        return ["<|im_end|>"]

    def parse_response(self, tokens):
        return ({"role": "assistant", "content": self.parts}, SimpleNamespace(is_clean=True))


class FakeSampler:
    def __init__(self, tokens, stop_reason="stop"):
        self.tokens, self.stop_reason, self.calls = tokens, stop_reason, []

    async def sample_async(self, prompt, num_samples, sampling_params):
        self.calls.append((prompt, num_samples, sampling_params))
        return SimpleNamespace(sequences=[SimpleNamespace(tokens=self.tokens, stop_reason=self.stop_reason)])


MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "## Instruction\ndo it"}]


def test_mock_edits_first_then_finishes_after_the_review_turn():
    m = models.MockModel()
    first = json.loads(asyncio.run(m.complete(MSGS))[0])
    assert first["tool"] == "run_python" and first["args"]["mode"] == "edit"
    assert "expected_changes" in first["args"] and "OUT_XLSX" in first["args"]["code"]
    after_tool = MSGS + [{"role": "user", "content": "## Tool result: run_python\nok\n\nChoose the next JSON action."}]
    assert json.loads(asyncio.run(m.complete(after_tool))[0])["tool"] == "run_python"   # a plain tool result is not the review
    after_review = MSGS + [{"role": "user", "content": "## Required independent review\n..."}]
    assert json.loads(asyncio.run(m.complete(after_review))[0])["tool"] == "finish"


def test_tinker_adapter_returns_text_parts_and_token_counts():
    parts = [{"type": "thinking", "thinking": "let me count"}, {"type": "text", "text": '{"tool":"finish","args":{}}'}]
    params = SimpleNamespace(max_tokens=100, temperature=0)
    sampler = FakeSampler(list(range(40)))
    m = models.TinkerModel(sampler, FakeRenderer(parts), params, "tinker:Qwen/Qwen3.8-27B")
    text, n_in, n_out = asyncio.run(m.complete(MSGS))
    assert text == '{"tool":"finish","args":{}}'                 # thinking dropped from the reply
    assert (n_in, n_out) == (123, 40)
    assert sampler.calls[0][1] == 1 and sampler.calls[0][2] is params   # the configured params are used as-is
    assert m.name == "tinker:Qwen/Qwen3.8-27B"


def test_tinker_adapter_accepts_plain_string_content():
    m = models.TinkerModel(FakeSampler([1, 2]), FakeRenderer("plain"), SimpleNamespace(max_tokens=10), "n")
    assert asyncio.run(m.complete(MSGS))[0] == "plain"


def test_thinking_only_reply_yields_empty_text():
    """When the cap hits mid-thought there is no text part; the adapter returns an empty string, not the reasoning."""
    m = models.TinkerModel(FakeSampler([1] * 50, "length"), FakeRenderer([{"type": "thinking", "thinking": "We need..."}]),
                           SimpleNamespace(max_tokens=50), "n")
    assert asyncio.run(m.complete(MSGS))[0] == ""


def test_truncated_json_reply_is_not_returned_as_the_answer():
    parts = [{"type": "text", "text": '{"tool":"run_python","args":{"code":"import ope'}]   # cut mid-way
    m = models.TinkerModel(FakeSampler([1] * 100, "length"), FakeRenderer(parts), SimpleNamespace(max_tokens=100), "n")
    text, _, _ = asyncio.run(m.complete(MSGS))
    assert text == ""


def test_get_model_spec_parsing():
    assert isinstance(models.get_model("mock"), models.MockModel)
    m = models.get_model("deepseek/deepseek-v3.2")
    assert isinstance(m, models.OpenRouterModel) and m.name == "openrouter:deepseek/deepseek-v3.2"


def test_openrouter_adapter_flattens_messages_into_system_and_prompt(monkeypatch):
    """The adapter joins non-system turns into one prompt; check the flattening without a network call."""
    seen = {}

    class FakeAgent:
        def __init__(self, model, system_prompt, model_settings):
            seen["system"] = system_prompt

        async def run(self, prompt):
            seen["prompt"] = prompt
            return SimpleNamespace(output="reply", usage=lambda: SimpleNamespace(input_tokens=3, output_tokens=2))

    m = models.get_model("x/y")
    m._agent_cls = FakeAgent
    text, n_in, n_out = asyncio.run(m.complete([{"role": "system", "content": "S"}, {"role": "user", "content": "U1"},
                                                {"role": "assistant", "content": "A"}, {"role": "user", "content": "U2"}]))
    assert (text, n_in, n_out) == ("reply", 3, 2)
    assert seen["system"] == "S" and seen["prompt"] == "U1\n\nA\n\nU2"
