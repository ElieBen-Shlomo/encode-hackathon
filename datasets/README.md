# datasets

Training/eval data for the SpreadsheetBench task. Everything builds toward one thing:
`(prompt -> verified openpyxl script)` examples in the **harness's format** (`agent/harness.py`),
filtered by the real scorer so labels are never trusted unverified.

## Layout

```
datasets/
  raw/            downloaded, untouched (gitignored, large)
    all_data_912_v0.1/                     full SpreadsheetBench: 912 tasks, 3 instances each
    vals_finance_agent/finance_agent_benchmark.csv   Vals Finance Agent (SEC-filing QA)
  splits/         shared manifests (committed — the whole team MUST use the same ones)
    extra_912_ids.txt      512 ids = 912 minus the verified 400
    train_400.txt          verified-400 tasks to fine-tune on
    heldout_400.txt        verified-400 tasks held out as our INSTRUMENT
    heldout_base_ids.txt   base ids in held-out, to drop their siblings from training
  processed/      built artifacts: sft/<name>/sft.jsonl (gitignored, reproducible)
  scripts/
    fetch.py         reproducible sha-checked download of raw/
    dedup_912.py     write extra_912_ids.txt (912 minus 400)
    sb912.py         loader for the 912 layout (input/answer, 3 instances/task)
    make_split.py    verified 400 -> train_400 + heldout_400 (stratified, base-id-blocked)
    build_sft.py     teacher (via Tinker) blind-solves + 3-for-3 gate -> verified SFT jsonl
```

## Run order

```sh
python3 datasets/scripts/fetch.py         # 1. raw data (once)
python3 datasets/scripts/dedup_912.py     # 2. extra pool = 912 - 400
python3 datasets/scripts/make_split.py    # 3. train_400 + heldout_400 (seeded, shared)
research/.venv/bin/python datasets/scripts/build_sft.py --name v1 \
    --teacher-model Qwen/Qwen3.5-397B-A17B --source both --samples 4 --temperature 0.7   # 4. SFT data
# 5. fine-tune Qwen on datasets/processed/sft/v1/sft.jsonl (Tinker)
```

## Data plan (blended)

The `evaluate.py --all` number is only a required artifact — **the judges rank on their own
hidden data** — so we don't keep the 400 pristine for reporting. We train on our best data and
hold a slice out purely as an honest internal signal.

| Data | Role |
|---|---|
| `train_400` (verified, trustworthy) | fine-tune the model |
| gated-512 (3-for-3 passers only) | more training volume, quality-filtered |
| synthetic (execution-verified) | top-up, esp. Sheet-Level |
| `heldout_400` (verified) | **instrument** — pick the best model/harness; touch rarely |

`build_sft.py` generates the training data: a strong teacher (any Tinker model; the FINAL model
is Qwen) **blind-solves** each task like inference (never sees the answer, so it can't hardcode),
then the **gate** keeps only correct scripts — `train_400` must match the verified golden, and each
extra must have **one script pass all 3 test-case instances** (3-for-3). That 3-for-3 gate is what
makes the unvetted 512 safe: a task with an ambiguous/wrong golden won't be solvable by a single
script across all three, so it's dropped automatically.

## Leakage rules (non-negotiable)

1. **held-out is the instrument** — never train on it; AutoResearch dev comes from gated extras, not here.
2. **drop base-id siblings of held-out** from training (`heldout_base_ids.txt`), so a near-variant
   can't leak into the instrument.
3. **the split is shared** — everyone uses the committed `splits/*.txt` (same seed).
4. **never trust an unverified label** — every SFT example's script must pass the real scorer
   (`evaluate.score_task`, LibreOffice recalc + compare) or it's discarded.

## Notes

- Tinker sampling/training needs a **writable project** (`TINKER_PROJECT_ID` in `.env` or `--project-id`);
  the default project is read-only.
- Vals CSV is SEC-filing QA — wrong shape for a code-gen harness. Reference only; not for SFT.
- The hidden test format is unknown; we build to the shipped format + scorer and keep the model general.
