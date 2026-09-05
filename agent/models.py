"""Chat-model adapters used by the local tool agent."""

import json


class MockModel:
    """Deterministic tool user for local artifact and harness smoke tests."""

    name = "mock"

    async def complete(self, messages: list[dict]) -> tuple[str, int, int]:
        completed_edit = any(
            m.get("role") == "user" and "Required independent review" in str(m.get("content"))
            for m in messages
        )
        if completed_edit:
            return json.dumps({"tool": "finish", "args": {"summary": "mock completed"}}), 0, 0
        code = "import os\nimport openpyxl\nout=os.environ['OUT_XLSX']\nwb=openpyxl.load_workbook(out)\nwb.save(out)\n"
        return json.dumps({"tool": "run_python", "args": {"mode": "edit", "expected_changes": [], "code": code}}), 0, 0


class TinkerModel:
    """Tinker Qwen adapter preserving multi-turn chat history through its renderer."""

    def __init__(self, sampler, renderer, sampling_params, name: str):
        self._sampler = sampler
        self._renderer = renderer
        self._params = sampling_params
        self.name = name

    async def complete(self, messages: list[dict]) -> tuple[str, int, int]:
        model_input = self._renderer.build_generation_prompt(messages)
        response = await self._sampler.sample_async(prompt=model_input, num_samples=1, sampling_params=self._params)
        tokens = response.sequences[0].tokens
        content = self._renderer.parse_response(tokens)[0]["content"]
        if not isinstance(content, str):
            content = "".join(part.get("text", "") for part in content if part.get("type") == "text")
        return content, model_input.length, len(tokens)


class OpenRouterModel:
    """Compatibility adapter for the existing Docker/OpenRouter agent entrypoint."""

    def __init__(self, model: str):
        from pydantic_ai import Agent
        from pydantic_ai.models.openrouter import OpenRouterModelSettings

        self.name = f"openrouter:{model}"
        self._settings = OpenRouterModelSettings(temperature=0)
        self._agent_cls = Agent
        self._model = model

    async def complete(self, messages: list[dict]) -> tuple[str, int, int]:
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        prompt = "\n\n".join(str(m.get("content", "")) for m in messages if m.get("role") != "system")
        agent = self._agent_cls(model=f"openrouter:{self._model}", system_prompt=system, model_settings=self._settings)
        result = await agent.run(prompt)
        usage = result.usage() if callable(result.usage) else result.usage
        return result.output, usage.input_tokens, usage.output_tokens


def get_model(spec: str):
    return MockModel() if spec == "mock" else OpenRouterModel(spec)
