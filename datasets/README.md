# datasets

Training/eval data for the SpreadsheetBench task. Everything here builds toward one thing:
`(prompt -> verified openpyxl script)` examples in the **harness's format** (see `agent/harness.py`),
filtered by the real scorer so labels are never trusted unverified.

## Layout

```
datasets/
  raw/            downloaded, untouched (gitignored, large)
    all_data_912_v0.1/                     full SpreadsheetBench: 912 tasks, 3 instances each
    spreadsheetbench_912_v0.1.tar.gz
    vals_finance_agent/finance_agent_benchmark.csv   Vals Finance Agent (SEC-filing QA)
  splits/         shared manifests (committed — the whole team MUST use the same ones)
    extra_912_ids.txt      512 ids = 912 minus the verified 400 (leakage-safe training pool)
    train/dev/heldout.txt  3-way split of the verified 400 (after make_split.py)
    eval_base_ids.txt      base ids in dev+heldout, to exclude their extra-912 siblings
  processed/      built artifacts: SFT jsonl, etc. (gitignored, reproducible via scripts)
  scripts/
    fetch.py         reproducible download of raw/ (idempotent, sha-checked)
    dedup_912.py     write extra_912_ids.txt (912 minus 400)
    sb912.py         loader for the 912 layout (input/answer, 3 instances/task)
    make_split.py    3-way stratified + base-id-blocked split of the 400
```

## Run order

```sh
python3 datasets/scripts/fetch.py         # 1. get raw data (already done once)
python3 datasets/scripts/dedup_912.py     # 2. extra pool = 912 - 400  -> splits/extra_912_ids.txt
python3 datasets/scripts/make_split.py    # 3. train/dev/heldout of the 400 (agree on sizes first!)
# 4. build SFT: rejection-sample the harness on train + extras, keep only scorer-passing traces (TODO)
```

## Data sources

| Source | What | Format fit | Use |
|---|---|---|---|
| verified 400 (`research/data/...`) | the shipped task + scorer | exact | split into train/dev/held-out |
| full 912 minus 400 (512 tasks, 1529 instances) | more real forum tasks, richer in Sheet-Level | same task shape | extra training via rejection sampling |
| Vals Finance Agent (185 Q) | SEC-filing research QA, rubric-graded | **wrong shape** (QA, not cell manipulation) | reference only; do NOT dump into SFT |

## Leakage rules (non-negotiable)

1. **held-out is sealed** — never train on it, never let AutoResearch's inner loop see it.
2. **extras are leakage-safe by construction** (912−400 can't contain a held-out task), *except*
   base-id siblings: drop any extra whose base id is in `eval_base_ids.txt`.
3. **the split is shared** — everyone uses the committed `splits/*.txt` (same seed), or held-out
   means nothing.
4. **never trust an unverified label** — every training example's answer must pass the scorer
   (LibreOffice recalc + compare) or it's discarded.

## Note on the hidden test set

The judges' evaluation data is proprietary and its format is unknown — it may or may not match
SpreadsheetBench. We do **not** optimise for a guessed distribution. We build to the one concrete
spec we have (the shipped format + scorer), keep the model general, and trust the held-out number.
