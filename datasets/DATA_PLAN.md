# DATA PLAN — read before touching training/eval data

Single source of truth for what data is used for what. Splits are committed in
`datasets/splits/` (seed 0, shared — do not re-split).

## The split (verified 400)

| File | Count | Use |
|---|---|---|
| `train_400.txt` | 279 (192 Cell / 87 Sheet) | **TRAINING** |
| `heldout_400.txt` | 121 (83 Cell / 38 Sheet) | **TEST — our instrument** (never train/tune on it) |

## What to use for what

| Purpose | Data | Notes |
|---|---|---|
| **Train** (fine-tune Qwen) | `train_400` + gated-512 + synthetic | every example verified by the real scorer |
| **Dev** (iterate / AutoResearch) | a slice of gated-512 | drawn from extras, **not** the 400 → keeps held-out clean |
| **Test** (honest signal) | `heldout_400` | touch rarely; pick the best model/harness here |
| **Required artifact** | `evaluate.py --all` on the 400 | contaminated (279 trained) — **declare it**; not ranked (judges use hidden data) |

- **gated-512** = the 512 extras (`extra_912_ids.txt`), keep a task only if **one script passes all 3 instances** (3-for-3), minus any base-id sibling of a held-out task.
- **synthetic** = execution-verified only. Amount / generator = TBD (open decision below).

## Rules (non-negotiable)

1. `heldout_400` is **sealed** — never train on it, never let AutoResearch tune against it.
2. **Never trust an unverified label** — every training script must pass the real scorer
   (`train_400`: verified golden · extras: 3-for-3 · synthetic: execution-verified).
3. **Everyone uses the committed splits** (seed 0). Don't regenerate them.
4. **Drop base-id siblings of held-out** from all training (`heldout_base_ids.txt`).

## Pipeline

`fetch → dedup → make_split → build_sft (teacher blind-solve + 3-for-3 gate) → fine-tune Qwen (Tinker) → eval on heldout_400`

- `build_sft.py` produces `datasets/processed/sft/<name>/sft.jsonl`.
- Final model = **Qwen on Tinker**. The teacher is an *offline* data-gen step (can be any model).

## OPEN DECISIONS — lock these now

1. **Teacher for data gen** — Opus (Anthropic API, strongest, if the rules allow a non-Tinker model for *data prep*) vs strongest Tinker model (`Qwen/Qwen3.5-397B-A17B` / `deepseek-ai/DeepSeek-V3.1`). Final model is Qwen either way.
2. **Synthetic** — how much, what generator, finance-flavored vs general. Not built yet.
3. **Split sizes** — currently 279/121 (70/30). Confirm or change `--heldout`.

## Ownership (4 people, 2 groups — suggested)

- **Group A — harness + AutoResearch**: `agent/` harness, digest, DQ guard; AutoResearch vs the gated-extras dev set.
- **Group B — data + fine-tune**: run `build_sft`, build synthetic, Tinker fine-tune, own the checkpoints.

Sync points: shared split (this doc), the harness I/O contract, and the checkpoint handoff.
