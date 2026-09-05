"""Same baseline through Tinker: a base model, or your fine-tuned sampler checkpoint.

    uv sync --extra tinker
    uv run baseline/tinker_predict.py --out-dir submissions/qwen3-8b --ids 13-1,51-12
    uv run baseline/tinker_predict.py --out-dir submissions/mine \
        --model-path tinker://<run-id>/sampler_weights/final

Needs TINKER_API_KEY in .env. The base model picks the tokenizer and chat template. Writes the same
files as llm_predict.py. Inference defaults live in config/qwen.yaml; command-line flags override them.
A reply cut off by max_tokens is never parsed as an answer: it is retried one thinking level lower
(`fallback_renderers: auto` in the config steps xhigh -> medium -> low -> off) and recorded as an error
if every level truncates.
"""

import argparse
import asyncio
from pathlib import Path

import tinker
import yaml
from common import FORMAT_HINT, SYSTEM_PROMPT, load_env, parse_ids, run, selected_tasks
from tinker import types
from tinker_cookbook import renderers
from tinker_cookbook.model_info import get_recommended_renderer_name
from tinker_cookbook.tokenizer_utils import get_tokenizer

from sb import DEFAULT_DATASET

# Qwen3.8 thinking levels from most to least reasoning; a truncated reply retries one level down.
QWEN38_LADDER = ["qwen3_8_xhigh_reasoning", "qwen3_8_medium_reasoning", "qwen3_8_low_reasoning", "qwen3_8_disable_thinking"]


def default_renderer(base_model: str) -> str:
    """Start Qwen3.8 at medium thinking. The cookbook's recommended default is xhigh, which runs past any
    practical max_tokens while still thinking; medium answers most tasks and the ladder steps down from there."""
    if "qwen3.8" in base_model.lower():
        return "qwen3_8_medium_reasoning"
    return get_recommended_renderer_name(base_model)


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    if not isinstance(config, dict):
        raise ValueError(f"config must be a YAML mapping: {path}")
    return config


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", required=True)
    p.add_argument("--dataset-dir", default=str(DEFAULT_DATASET))
    p.add_argument("--ids", help="comma-separated task ids (default: all)")
    p.add_argument("--config", default="config/qwen.yaml", help="YAML inference settings file")
    p.add_argument("--base-model", help="e.g. Qwen/Qwen3-8B")
    p.add_argument("--model-path", help="tinker://... sampler checkpoint. Omit to sample the base model.")
    p.add_argument("--project-id", help="Tinker project ID for the sampling session (uses Tinker's default project if omitted)")
    p.add_argument("--concurrency", type=int, help="parallel requests")
    p.add_argument("--max-tokens", type=int, help="sheet-level tasks need long replies")
    p.add_argument("--temperature", type=float, help="sampling temperature")
    return p.parse_args()


async def main():
    load_env()
    args = parse_args()
    config = load_config(Path(args.config))
    base_model = args.base_model or config.get("base_model")
    if not base_model:
        raise ValueError("base_model must be set in the YAML config or passed as --base-model")
    project_id = args.project_id or config.get("project_id")
    concurrency = args.concurrency if args.concurrency is not None else config.get("concurrency", 4)
    max_tokens = args.max_tokens if args.max_tokens is not None else config.get("max_tokens", 8192)
    temperature = args.temperature if args.temperature is not None else config.get("temperature", 0)
    renderer_name = config.get("renderer") or default_renderer(base_model)
    print(f"Tinker project ID: {project_id or '<default project>'}", flush=True)
    sampler = tinker.ServiceClient(project_id=project_id).create_sampling_client(
        base_model=base_model, model_path=args.model_path
    )
    renderer = renderers.get_renderer(renderer_name, get_tokenizer(base_model))
    params = types.SamplingParams(max_tokens=max_tokens, temperature=temperature, stop=renderer.get_stop_sequences())

    # The Qwen3.8 renderers prefill the think tag, so when max_tokens is hit mid-thought the sampled text
    # is reasoning with no answer (or a JSON cut mid-way). Detect that from the stop reason instead of
    # handing it to the JSON parser, then step the thinking level down one notch per retry.
    fb = config.get("fallback_renderers", "auto")
    if fb == "auto":
        chain = QWEN38_LADDER[QWEN38_LADDER.index(renderer_name) + 1:] if renderer_name in QWEN38_LADDER else []
    elif fb:
        chain = list(fb) if isinstance(fb, list) else [fb]
    else:
        chain = []
    tokenizer = get_tokenizer(base_model)
    ladder = [(renderer_name, renderer)] + [(name, renderers.get_renderer(name, tokenizer)) for name in chain]
    print(f"renderer ladder on truncation: {' -> '.join(name for name, _ in ladder)}", flush=True)

    async def sample(rnd, messages):
        prm = types.SamplingParams(max_tokens=max_tokens, temperature=temperature, stop=rnd.get_stop_sequences())
        model_input = rnd.build_generation_prompt(messages)
        response = await sampler.sample_async(prompt=model_input, num_samples=1, sampling_params=prm)
        seq = response.sequences[0]
        tokens = seq.tokens
        truncated = "length" in str(getattr(seq, "stop_reason", "")).lower() or len(tokens) >= max_tokens
        content = rnd.parse_response(tokens)[0]["content"]
        if not isinstance(content, str):  # thinking renderers return parts; keep the text, drop the thinking
            content = "".join(part.get("text", "") for part in content if part.get("type") == "text")
        return content, model_input.length, len(tokens), truncated

    async def complete(prompt: str):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt + FORMAT_HINT}]
        n_in = n_out = 0
        for name, rnd in ladder:
            content, i, o, truncated = await sample(rnd, messages)
            n_in, n_out = n_in + i, n_out + o
            if not truncated:
                return content, n_in, n_out
        raise RuntimeError(f"reply truncated at max_tokens={max_tokens} with every renderer tried "
                           f"({' -> '.join(name for name, _ in ladder)}): raise max_tokens")

    tasks = selected_tasks(Path(args.dataset_dir), parse_ids(args.ids))
    await run(complete, args.model_path or base_model, tasks, Path(args.out_dir), concurrency)


if __name__ == "__main__":
    asyncio.run(main())
