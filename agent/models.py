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

import asyncio
import json
import os
import random
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
EFFORT_BY_RENDERER = {v: k for k, v in RENDERER_BY_EFFORT.items()}
LADDER = ("xhigh", "medium", "low", "off")   # step-down order when a reply is cut off by max_tokens


def next_lower_effort(effort: str | None) -> str | None:
    if effort in LADDER and LADDER.index(effort) + 1 < len(LADDER):
        return LADDER[LADDER.index(effort) + 1]
    return None
DEFAULT_MAX_TOKENS = {"off": 8192, "low": 32768, "medium": 32768, "xhigh": 32768}  # tokens are cheap; truncation is not

# Transient failures worth retrying: Tinker's typed errors first (status codes, connection/timeouts,
# RequestFailedError categories that blame the server), then a narrow message pattern as a fallback.
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
TRANSIENT_TYPE_NAMES = {"RateLimitError", "InternalServerError", "APIConnectionError", "APITimeoutError",
                        "TimeoutError", "ConnectionError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError"}
_TRANSIENT_RE = re.compile(r"\b(408|429|502|503|504)\b|rate.?limit|too many requests|timed? ?out|temporarily unavailable"
                           r"|service unavailable|overloaded|connection (reset|error|refused|aborted)|server error", re.I)


def _is_transient(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in TRANSIENT_STATUS
    category = getattr(exc, "category", None)          # tinker.RequestFailedError
    if category is not None:
        label = str(getattr(category, "name", category)).lower()
        return any(k in label for k in ("server", "infra", "transient", "timeout", "unavailable"))
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    if {c.__name__ for c in type(exc).__mro__} & TRANSIENT_TYPE_NAMES:
        return True
    return bool(_TRANSIENT_RE.search(str(exc)))


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

    Three ways to build it:
      TinkerModel(sampler, renderer, sampling_params, name)          prebuilt, one fixed renderer, no ladder
      TinkerModel(base_model=..., reasoning="medium", max_tokens=..) one renderer per thinking level,
                                                                     so `effort` can vary per call
      TinkerModel(sampler, base_model=..., reasoning=...)            same, reusing an existing sampling client

    With per-effort renderers a reply cut off by max_tokens is retried one thinking level lower
    (xhigh > medium > low > off) unless step_down=False; the trace records `stepped_down_from`.
    """

    def __init__(self, sampler=None, renderer=None, sampling_params=None, name: str | None = None, *,
                 base_model: str = DEFAULT_BASE_MODEL, model_path: str | None = None,
                 project_id: str | None = None, reasoning: str = "low", max_tokens: int | None = None,
                 temperature: float = 0, retries: int = 6, call_timeout: float | None = 900.0,
                 step_down: bool = True):
        self.last_info = {}
        self._fixed_params = None
        self._types = None
        self._tokenizer = None
        self.retries = retries
        self.call_timeout = call_timeout
        self.temperature = temperature
        self.step_down = step_down
        if sampler is not None and renderer is not None:
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
        self.name = name or f"tinker:{model_path or base_model}"
        self._types = types
        project_id = project_id or os.environ.get("TINKER_PROJECT_ID") or None
        self.project_id = project_id
        self._sampler = sampler if sampler is not None else tinker.ServiceClient(project_id=project_id).create_sampling_client(
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
        params = self._types.SamplingParams(max_tokens=max_tokens, temperature=self.temperature,
                                            stop=renderer.get_stop_sequences())
        return eff, renderer, params

    async def _sample(self, model_input, params):
        """One sampling call with a per-attempt deadline and exponential backoff on transient failures.

        Tinker's SDK retries and stuck-detects internally (up to hours); the deadline here bounds each attempt
        so a hung call cannot block a task indefinitely, and the attempt budget bounds the total.
        """
        attempts_allowed = max(1, int(self.retries or 1))
        delay = 2.0
        for attempt in range(1, attempts_allowed + 1):
            try:
                call = self._sampler.sample_async(prompt=model_input, num_samples=1, sampling_params=params)
                response = await (asyncio.wait_for(call, timeout=self.call_timeout) if self.call_timeout else call)
                return response, attempt
            except Exception as e:
                if attempt == attempts_allowed or not _is_transient(e):
                    raise
                await asyncio.sleep(min(60.0, delay) + random.uniform(0, 1))
                delay *= 2

    async def complete(self, messages: list[dict], effort: str | None = None) -> tuple[str, int, int]:
        eff, renderer, params = self._resolve(effort)
        content, n_in, n_out, info = await self._complete_once(messages, eff, renderer, params)
        stepped = []
        while self.step_down and info.get("parse_error") and "truncated" in info["parse_error"]:
            lower = next_lower_effort(eff)
            if lower is None or lower not in self._renderers:
                break
            stepped.append(eff)
            eff, renderer, params = self._resolve(lower)
            content, more_in, more_out, info = await self._complete_once(messages, eff, renderer, params)
            n_in, n_out = n_in + more_in, n_out + more_out
        if stepped:
            info["stepped_down_from"] = " > ".join(stepped)
        self.last_info = info
        return content, n_in, n_out

    async def _complete_once(self, messages: list[dict], eff: str, renderer, params) -> tuple[str, int, int, dict]:
        model_input = renderer.build_generation_prompt(messages)
        response, attempts = await self._sample(model_input, params)
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
        info = {
            "effort": eff,
            "renderer": self._renderer_names.get(eff),
            "max_tokens": max_tokens,
            "stop_reason": stop or None,
            "parse_error": parse_error,
            "reasoning": reasoning,
            "attempts": attempts if attempts > 1 else None,
        }
        return content, model_input.length, len(tokens), info


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
              project_id: str | None = None, reasoning: str = "low", max_tokens: int | None = None, retries: int = 6,
              call_timeout: float | None = 900.0):
    if spec == "mock":
        return MockModel()
    if spec == "tinker" or spec.startswith("tinker:"):
        if spec.startswith("tinker:") and len(spec) > len("tinker:"):
            base_model, _, path = spec[len("tinker:"):].partition("@")
            model_path = path or model_path
        return TinkerModel(base_model=base_model, model_path=model_path, project_id=project_id,
                           reasoning=reasoning, max_tokens=max_tokens, retries=retries, call_timeout=call_timeout)
    return OpenRouterModel(spec)
