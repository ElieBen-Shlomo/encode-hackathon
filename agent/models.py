"""Chat-model adapters used by the local tool agent and the single-shot values solver.

Every adapter implements

    text, input_tokens, output_tokens = await model.complete(messages, effort=None)

where `messages` is a chat list of {"role", "content"} dicts. After each call `model.last_info`
holds backend details for the trace (renderer, stop reason, reasoning text, truncation note).

mock            deterministic: tool actions for the agent loop, {"cells": []} for the values prompt
TinkerModel     Qwen through Tinker. Built either from a prebuilt sampler + renderer + params
                (research/baseline/agent_predict.py) or from base_model / reasoning level
                (agent/run.py). A reply cut off by max_tokens is never returned as the answer.
OpenRouterModel compatibility adapter for the OpenRouter entrypoint.

get_model("mock" | "tinker" | "tinker:<base>" | <openrouter id>, **kw) builds one.
"""

import json
import os
import re

DEFAULT_BASE_MODEL = "Qwen/Qwen3.8-27B"

# Thinking effort -> tinker_cookbook renderer for the Qwen3.8 family (most to least reasoning: xhigh > medium > low > off).
RENDERER_BY_EFFORT = {
    "off": "qwen3_8_disable_thinking",
    "low": "qwen3_8_low_reasoning",
    "medium": "qwen3_8_medium_reasoning",
    "xhigh": "qwen3_8_xhigh_reasoning",
}
EFFORTS = tuple(RENDERER_BY_EFFORT)
DEFAULT_MAX_TOKENS = {"off": 8192, "low": 16384, "medium": 16384, "xhigh": 16384}  # off matches the reference baseline

MOCK_VALUES_REPLY = '{"cells": []}'
MOCK_TOOL_CODE = "import os, openpyxl\nwb = openpyxl.load_workbook(os.environ['OUT_XLSX'])\nwb.save(os.environ['OUT_XLSX'])\n"


def _text_parts(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")


def _thinking_parts(content):
    if isinstance(content, str):
        return None
    parts = [str(p.get("thinking") or p.get("text") or "") for p in content
             if isinstance(p, dict) and p.get("type") == "thinking"]
    return "\n".join(parts) or None


class MockModel:
    """Deterministic stand-in for smoke tests: no API key, no network."""

    name = "mock"

    def __init__(self):
        self.last_info = {}

    async def complete(self, messages: list[dict], effort: str | None = None) -> tuple[str, int, int]:
        self.last_info = {"effort": effort, "stop_reason": "stop"}
        if any("Reply with JSON only" in str(m.get("content", "")) for m in messages):
            return MOCK_VALUES_REPLY, 0, 0                       # values solver prompt
        completed_edit = any(m.get("role") == "user" and "Required independent review" in str(m.get("content"))
                             for m in messages)
        tool_results = sum(m.get("role") == "user" and "Tool result" in str(m.get("content")) for m in messages)
        if completed_edit or tool_results >= 2:   # finish after the review turn (or if the edit path failed twice)
            return json.dumps({"tool": "finish", "args": {"summary": "mock completed"}}), 0, 0
        return json.dumps({"tool": "run_python", "args": {"mode": "edit", "expected_changes": [], "code": MOCK_TOOL_CODE}}), 0, 0


class TinkerModel:
    """Sample Qwen (a base model or a tinker:// sampler checkpoint) at temperature 0.

    Two ways to build it:
      TinkerModel(sampler, renderer, sampling_params, name)          prebuilt, one fixed renderer
      TinkerModel(base_model=..., reasoning="medium", max_tokens=..) one renderer per thinking level,
                                                                     so `effort` can vary per call
    """

    def __init__(self, sampler=None, renderer=None, sampling_params=None, name: str | None = None, *,
                 base_model: str = DEFAULT_BASE_MODEL, model_path: str | None = None,
                 project_id: str | None = None, reasoning: str = "medium", max_tokens: int | None = None):
        self.last_info = {}
        self._fixed_params = None
        self._types = None
        self._tokenizer = None
        if sampler is not None:
            self._sampler = sampler
            self._renderers = {"default": renderer}
            self._renderer_names = {"default": type(renderer).__name__}
            self._fixed_params = sampling_params
            self._default_effort = "default"
            self.name = name or "tinker"
            self.max_tokens = getattr(sampling_params, "max_tokens", None)
            self.reasoning = "default"
            return

        import tinker
        from tinker import types
        from tinker_cookbook import renderers
        from tinker_cookbook.model_info import get_recommended_renderer_name
        from tinker_cookbook.tokenizer_utils import get_tokenizer

        self.base_model = base_model
        self.model_path = model_path
        self.reasoning = reasoning
        self.max_tokens = max_tokens
        self.name = f"tinker:{model_path or base_model}"
        self._types = types
        project_id = project_id or os.environ.get("TINKER_PROJECT_ID") or None
        self.project_id = project_id
        self._sampler = tinker.ServiceClient(project_id=project_id).create_sampling_client(
            base_model=base_model, model_path=model_path)
        self._tokenizer = get_tokenizer(base_model)
        qwen38 = "qwen3.8" in base_model.lower()
        self._renderer_names, self._renderers = {}, {}
        for eff in (EFFORTS if qwen38 else ("default",)):
            rname = RENDERER_BY_EFFORT[eff] if qwen38 else get_recommended_renderer_name(base_model)
            self._renderer_names[eff] = rname
            self._renderers[eff] = renderers.get_renderer(rname, self._tokenizer)
        self._default_effort = (reasoning if reasoning in EFFORTS else "medium") if qwen38 else "default"

    def _resolve(self, effort: str | None):
        eff = effort if effort in self._renderers else self._default_effort
        renderer = self._renderers[eff]
        if self._fixed_params is not None or self._types is None:
            return eff, renderer, self._fixed_params
        max_tokens = self.max_tokens or DEFAULT_MAX_TOKENS.get(eff, 16384)
        params = self._types.SamplingParams(max_tokens=max_tokens, temperature=0, stop=renderer.get_stop_sequences())
        return eff, renderer, params

    async def complete(self, messages: list[dict], effort: str | None = None) -> tuple[str, int, int]:
        eff, renderer, params = self._resolve(effort)
        model_input = renderer.build_generation_prompt(messages)
        response = await self._sampler.sample_async(prompt=model_input, num_samples=1, sampling_params=params)
        seq = response.sequences[0]
        tokens = list(seq.tokens)
        stop = str(getattr(seq, "stop_reason", "") or "")
        max_tokens = getattr(params, "max_tokens", None)
        parse_error = reasoning = None
        termination = None
        try:
            parsed = renderer.parse_response(tokens)
            message, termination = parsed[0], (parsed[1] if len(parsed) > 1 else None)
            content = message.get("content", "")
            reasoning = _thinking_parts(content)          # keep the thinking for the trace
            content = _text_parts(content)
        except Exception as e:  # malformed tags: fall back to a raw decode when a tokenizer is available
            parse_error = f"{type(e).__name__}: {e}"[:200]
            raw = self._tokenizer.decode(tokens) if self._tokenizer is not None else ""
            content = re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.S).strip()
        # The Qwen3.8 renderers prefill the think tag, so a completion cut off by max_tokens is thinking
        # text with no answer (or a JSON cut mid-way): never hand that to a parser as if it were the reply.
        unclean = termination is not None and getattr(termination, "is_clean", True) is False
        if "length" in stop.lower() or unclean or (max_tokens and len(tokens) >= max_tokens):
            parse_error = ((parse_error or "") + f" truncated at max_tokens={max_tokens} (stop_reason={stop})").strip()
            reasoning = reasoning or content
            content = ""
        self.last_info = {
            "effort": eff,
            "renderer": self._renderer_names.get(eff),
            "max_tokens": max_tokens,
            "stop_reason": stop or None,
            "parse_error": parse_error,
            "reasoning": reasoning,
        }
        return content, model_input.length, len(tokens)


class OpenRouterModel:
    """Compatibility adapter for the OpenRouter entrypoint (Pydantic AI, temperature 0)."""

    def __init__(self, model: str):
        from pydantic_ai import Agent
        from pydantic_ai.models.openrouter import OpenRouterModelSettings

        self.name = f"openrouter:{model}"
        self._settings = OpenRouterModelSettings(temperature=0)
        self._agent_cls = Agent
        self._model = model
        self.last_info = {}

    async def complete(self, messages: list[dict], effort: str | None = None) -> tuple[str, int, int]:
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        prompt = "\n\n".join(str(m.get("content", "")) for m in messages if m.get("role") != "system")
        agent = self._agent_cls(model=f"openrouter:{self._model}", system_prompt=system, model_settings=self._settings)
        result = await agent.run(prompt)
        usage = result.usage() if callable(result.usage) else result.usage
        self.last_info = {"effort": None, "stop_reason": None}
        return result.output, usage.input_tokens, usage.output_tokens


def get_model(spec: str, *, base_model: str = DEFAULT_BASE_MODEL, model_path: str | None = None,
              project_id: str | None = None, reasoning: str = "medium", max_tokens: int | None = None):
    if spec == "mock":
        return MockModel()
    if spec == "tinker" or spec.startswith("tinker:"):
        if spec.startswith("tinker:") and len(spec) > len("tinker:"):
            base_model, _, path = spec[len("tinker:"):].partition("@")
            model_path = path or model_path
        return TinkerModel(base_model=base_model, model_path=model_path, project_id=project_id,
                           reasoning=reasoning, max_tokens=max_tokens)
    return OpenRouterModel(spec)
