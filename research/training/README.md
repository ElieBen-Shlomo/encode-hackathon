# Agent LoRA experiments

The training pipeline learns the local agent's tool-action protocol from agent
traces whose output workbooks pass `evaluate.py`. Golden workbooks select which
traces are eligible, but are never included in an SFT prompt or target.
Each example applies loss only to its next tool action; earlier actions remain
conversation context.

Create the 280/60/60 task split and inspect available training examples:

```bash
uv run training/build_agent_sft_data.py \
  --submissions-dir submissions/qwen-agent-critic \
  --out-dir training/artifacts/agent-sft \
  --split train --no-recalc
```

Run the same command with `--split validation` to make validation JSONL. The
builder only writes examples for tasks that passed evaluation; the report shows
how many traces and actions it found.

Validate an experiment without Tinker or an API key:

```bash
uv run training/train_lora.py \
  --data training/artifacts/agent-sft/train.jsonl \
  --validation-data training/artifacts/agent-sft/validation.jsonl \
  --out-dir training/runs/pilot \
  --dry-run
```

Train after reviewing the report. This sends model training work to Tinker:

```bash
uv sync --extra tinker
uv run training/train_lora.py \
  --data training/artifacts/agent-sft/train.jsonl \
  --validation-data training/artifacts/agent-sft/validation.jsonl \
  --out-dir training/runs/pilot
```

The last `sampler_path` in `training_state.json` plugs directly into the agent:

```bash
uv run baseline/agent_predict.py \
  --out-dir submissions/qwen-agent-lora-pilot \
  --model-path 'tinker://.../sampler_weights/spreadsheet-agent-epoch-2'
```
