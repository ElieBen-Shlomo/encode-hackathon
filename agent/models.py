"""Model backends behind one stateless interface:

    text, input_tokens, output_tokens = await model.complete(system, prompt)

`mock` needs no API key and returns a canned no-op script — lets the whole pipeline,
traces and Docker image be tested end to end before keys exist.
"""

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


def get_model(spec: str):
    return MockModel() if spec == "mock" else OpenRouterModel(spec)
