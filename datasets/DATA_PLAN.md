# DATA PLAN — read before touching training/eval data

**Scope: we are using ONLY the verified 400 right now.** The full 912 extras and synthetic
data are deferred — ignore them for now.

## The split (verified 400)

The 400 is split **two ways**, seeded and shared so everyone uses the identical split:

| Split | Count | Use it for |
|---|---|---|
| **train** | 279 (192 Cell / 87 Sheet) | fine-tune Qwen · generate teacher scripts · debug / error-analysis |
| **test** | 121 (83 Cell / 38 Sheet) | measure ONLY — baseline, fine-tuned, harness changes. **Never train or tune on it.** |

- **Source of truth:** `datasets/splits/train.txt` and `datasets/splits/test.txt`
  (seed 0, stratified by instruction_type, base-id-blocked so variants like `82-1`/`82-2` stay together).
- **Browsable folders:** run `python datasets/scripts/organize.py` to get
  `datasets/400/train/<id>/` and `datasets/400/test/<id>/` (symlinks to the real files).

## When to use what

| If you're… | Use | Never touch |
|---|---|---|
| fine-tuning Qwen | `train` | `test` |
| generating teacher scripts (SFT data) | `train` | `test` |
| measuring baseline / fine-tuned / a harness change | `test` | — |
| debugging, reading failures, error-analysis | `train` | `test` |

## Rules (non-negotiable)

1. **`test` is sealed** — never train on it, never tune the harness against it. It's your only honest number.
2. **Everyone uses the committed split** (seed 0). Don't regenerate it.
3. **Never trust an unverified label** — a teacher script is kept only if it reproduces the golden (via the real scorer, LibreOffice recalc + compare).

## Pipeline (400-only)

1. **Baseline** — run Qwen through the harness on `test`, score. *(Needs the Tinker backend in the harness — `feat/tinker-agent-backend`.)*
2. **Fine-tune** — teacher writes scripts on `train` → keep only scorer-passing ones → fine-tune Qwen → re-score on `test`.
3. **Improve** — batch the failures from the traces, fix the general error modes in the harness (prompt / repair / digest).

## Deferred (not now)

- **The 512 extra tasks (912 − 400) and synthetic data** — more training volume, revisit later.
- **AutoResearch** — dropped. Harness tuning is manual error-analysis from the traces, not an autonomous loop.

## Ownership

- **Group A** — harness + error-analysis (prompt, digest, repair loop, failure batching).
- **Group B** — teacher data-gen (`train`) + Tinker fine-tune + checkpoints.
