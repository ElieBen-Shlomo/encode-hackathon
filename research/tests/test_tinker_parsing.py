"""TinkerModel's reply handling against a fake sampler and fake renderers: no network, no key.

The Qwen3.8 renderers prefill the think tag, so a completion cut off by max_tokens is thinking
text with no answer. That must never be returned as the reply."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from conftest import AGENT

sys.path.insert(0, str(AGENT))
import models  # noqa: E402


class FakeRenderer:
    def __init__(self, parts, is_clean=True):
        self.parts, self.is_clean = parts, is_clean

    def build_generation_prompt(self, messages):
        return SimpleNamespace(length=123, messages=messages)

    def get_stop_sequences(self):
        return ["<|im_end|>"]

    def parse_response(self, tokens):
        return ({"role": "assistant", "content": self.parts}, SimpleNamespace(is_clean=self.is_clean))


class FakeSampler:
    def __init__(self, tokens, stop_reason):
        self.tokens, self.stop_reason, self.calls = tokens, stop_reason, []

    async def sample_async(self, prompt, num_samples, sampling_params):
        self.calls.append(sampling_params)
        return SimpleNamespace(sequences=[SimpleNamespace(tokens=self.tokens, stop_reason=self.stop_reason)])


MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "prompt"}]


def make_model(renderer, sampler, reasoning="low", max_tokens=None):
    m = object.__new__(models.TinkerModel)
    m.base_model, m.model_path, m.reasoning, m.max_tokens = "Qwen/Qwen3.8-27B", None, reasoning, max_tokens
    m.name, m.last_info, m.project_id, m._fixed_params = "tinker:test", {}, None, None
    m._types = SimpleNamespace(SamplingParams=lambda **kw: SimpleNamespace(**kw))
    m._sampler, m._tokenizer = sampler, SimpleNamespace(decode=lambda toks: "raw")
    m._renderers = {e: renderer for e in models.EFFORTS}
    m._renderer_names = dict(models.RENDERER_BY_EFFORT)
    m._default_effort = reasoning if reasoning in models.EFFORTS else "medium"
    return m


def test_clean_reply_returns_text_and_keeps_reasoning_for_the_trace():
    parts = [{"type": "thinking", "thinking": "let me count"}, {"type": "text", "text": '{"cells": []}'}]
    m = make_model(FakeRenderer(parts), FakeSampler(list(range(40)), "stop"))
    text, n_in, n_out = asyncio.run(m.complete(MSGS))
    assert text == '{"cells": []}' and (n_in, n_out) == (123, 40)
    assert m.last_info["reasoning"] == "let me count"
    assert m.last_info["renderer"] == "qwen3_8_low_reasoning" and m.last_info["parse_error"] is None


def test_truncated_reply_is_never_returned_as_the_answer():
    parts = [{"type": "thinking", "thinking": "We need answer user's request. Need compute..."}]
    m = make_model(FakeRenderer(parts, is_clean=False), FakeSampler(list(range(8192)), "length"), max_tokens=8192)
    text, _, n_out = asyncio.run(m.complete(MSGS))
    assert text == ""                                   # nothing for the JSON parser to misread
    assert n_out == 8192
    assert "truncated at max_tokens=8192" in m.last_info["parse_error"]
    assert m.last_info["stop_reason"] == "length"
    assert "We need answer" in m.last_info["reasoning"]  # the thinking still reaches the trace


def test_length_stop_reason_alone_is_enough_to_flag_truncation():
    parts = [{"type": "text", "text": '{"cells": [{"cell": "A1", "va'}]  # JSON cut mid-way
    m = make_model(FakeRenderer(parts, is_clean=True), FakeSampler([1] * 100, "length"), max_tokens=100)
    text, _, _ = asyncio.run(m.complete(MSGS))
    assert text == "" and "truncated" in m.last_info["parse_error"]


def test_effort_override_selects_renderer_and_default_caps():
    m = make_model(FakeRenderer([{"type": "text", "text": "ok"}]), FakeSampler([1], "stop"), reasoning="low")
    asyncio.run(m.complete(MSGS, effort="xhigh"))
    assert m.last_info["effort"] == "xhigh" and m.last_info["max_tokens"] == models.DEFAULT_MAX_TOKENS["xhigh"]
    asyncio.run(m.complete(MSGS, effort="off"))
    assert m.last_info["max_tokens"] == 8192  # matches the shipped baseline's cap
    assert m._sampler.calls[-1].temperature == 0


def test_prebuilt_constructor_used_by_agent_predict_keeps_fixed_params_and_flags_truncation():
    """research/baseline/agent_predict.py builds TinkerModel(sampler, renderer, params, name) positionally."""
    params = SimpleNamespace(max_tokens=50, temperature=0)
    sampler = FakeSampler([1] * 50, "length")
    m = models.TinkerModel(sampler, FakeRenderer([{"type": "text", "text": "cut off"}]), params, "tinker:Qwen/Qwen3.8-27B")
    text, n_in, n_out = asyncio.run(m.complete(MSGS))
    assert sampler.calls[-1] is params and n_out == 50
    assert text == "" and "truncated at max_tokens=50" in m.last_info["parse_error"]
    m2 = models.TinkerModel(FakeSampler([1, 2, 3], "stop"), FakeRenderer([{"type": "text", "text": "ok"}]), params, "n")
    assert asyncio.run(m2.complete(MSGS))[0] == "ok"


def test_get_model_spec_parsing_without_network(monkeypatch):
    seen = {}
    monkeypatch.setattr(models, "TinkerModel", lambda **kw: seen.update(kw) or "T")
    assert models.get_model("mock").name == "mock"
    assert models.get_model("tinker", reasoning="medium") == "T" and seen["base_model"] == models.DEFAULT_BASE_MODEL
    assert models.get_model("tinker:Qwen/Qwen3-8B") == "T" and seen["base_model"] == "Qwen/Qwen3-8B"


def test_renderer_ladder_names_match_cookbook_family():
    assert list(models.RENDERER_BY_EFFORT) == ["off", "low", "medium", "xhigh"]
    assert all(v.startswith("qwen3_8_") for v in models.RENDERER_BY_EFFORT.values())
