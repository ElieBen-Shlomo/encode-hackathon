"""Model backends behind one stateless interface:

    text, input_tokens, output_tokens = await model.complete(system, prompt)

`mock` needs no API key and returns a canned no-op script — lets the whole pipeline,
traces and Docker image be tested end to end before keys exist.

Model specs accepted by get_model():
    mock                                     no API key, canned no-op script
    tinker:<base-model>                      sample the base model
    tinker:<base-model>@tinker://<path>      sample a fine-tuned checkpoint
    <anything else>                          an OpenRouter model id
Tinker reads TINKER_API_KEY, and TINKER_PROJECT_ID when the org needs one.
"""

import os

MOCK_REPLY = """Here is the script:

```python
import sys
import openpyxl

wb = openpyxl.load_workbook(sys.argv[2])
wb.save(sys.argv[2])
```
"""


class MockModel:
    name = "mock"

    async def complete(self, system: str, prompt: str) -> tuple[str, int, int]:
        return MOCK_REPLY, 0, 0


class OpenRouterModel:
    """Plain-text completion via Pydantic AI + OpenRouter, temperature 0."""

    def __init__(self, model: str):
        from pydantic_ai import Agent
        from pydantic_ai.models.openrouter import OpenRouterModelSettings

        self.name = f"openrouter:{model}"
        self._settings = OpenRouterModelSettings(temperature=0)
        self._agent_cls = Agent
        self._model = model

    async def complete(self, system: str, prompt: str) -> tuple[str, int, int]:
        agent = self._agent_cls(model=f"openrouter:{self._model}", system_prompt=system,
                                model_settings=self._settings)
        result = await agent.run(prompt)
        usage = result.usage() if callable(result.usage) else result.usage
        return result.output, usage.input_tokens, usage.output_tokens


class TinkerModel:
    """Sampling via Tinker: a base model, or a fine-tuned sampler checkpoint.

    The tokenizer and renderer are built once here rather than per call — they are the
    expensive part, and the harness calls complete() up to MAX_ATTEMPTS times per task.
    """

    def __init__(self, base_model: str, model_path: str | None = None, max_tokens: int = 8192):
        import tinker
        from tinker import types
        from tinker_cookbook import renderers
        from tinker_cookbook.model_info import get_recommended_renderer_name
        from tinker_cookbook.tokenizer_utils import get_tokenizer

        self.name = f"tinker:{model_path or base_model}"
        client = tinker.ServiceClient(project_id=os.environ.get("TINKER_PROJECT_ID") or None)
        self._sampler = client.create_sampling_client(base_model=base_model, model_path=model_path)
        self._renderer = renderers.get_renderer(
            get_recommended_renderer_name(base_model), get_tokenizer(base_model)
        )
        self._params = types.SamplingParams(
            max_tokens=max_tokens, temperature=0, stop=self._renderer.get_stop_sequences()
        )

    async def complete(self, system: str, prompt: str) -> tuple[str, int, int]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        model_input = self._renderer.build_generation_prompt(messages)
        response = await self._sampler.sample_async(
            prompt=model_input, num_samples=1, sampling_params=self._params
        )
        tokens = response.sequences[0].tokens
        content = self._renderer.parse_response(tokens)[0]["content"]
        if not isinstance(content, str):  # thinking renderers return parts; keep text, drop thinking
            content = "".join(part.get("text", "") for part in content if part.get("type") == "text")
        return content, model_input.length, len(tokens)


def get_model(spec: str):
    if spec == "mock":
        return MockModel()
    if spec.startswith("tinker:"):
        base_model, _, model_path = spec[len("tinker:"):].partition("@")
        return TinkerModel(base_model, model_path or None)
    return OpenRouterModel(spec)
