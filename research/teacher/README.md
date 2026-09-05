# Teacher scripts (SFT data for the Qwen fine-tune)

One reference Python script per **train-split** task (`datasets/splits/train.txt`, 279 ids),
each verified to reproduce the golden workbook through the real scorer: the script runs in the
sandbox on a copy of the init workbook, the output is LibreOffice-recalculated, and every
graded cell must match the golden (`sb.load_answer_values` + `sb.values_equal`, the shipped
semantics). Only passing scripts become training data — DATA_PLAN rule: never trust an
unverified label. Scripts were authored by Claude (Fable 5); disclose that in SUBMISSION.md
under training data.

## Layout

    scripts/<id>.py     the teacher scripts (verified ones are the deliverable)
    manifest.jsonl      verification results, one line per script (rebuilt by verify.py --all)
    sft/train.jsonl     harness-format trajectories built from passing train scripts

## Commands (from `research/`)

    uv run teacher/verify.py 13-1        # verify one script: verdict JSON, exit 0 pass / 1 fail
    uv run teacher/verify.py --all       # sweep scripts/, rewrite manifest.jsonl
    uv run teacher/build_sft.py          # passing train scripts -> sft/train.jsonl

## Script contract

- Read `IN_XLSX` and `OUT_XLSX` from the environment. `OUT_XLSX` already exists as a copy of
  the input: load it with openpyxl, edit, save. Never write `IN_XLSX` (the verifier makes it
  read-only and will fail the script).
- Python stdlib + openpyxl only. No network.
- Write final **values** computed in Python; write a formula only when the instruction
  explicitly demands one (grading recalculates, so values always count).
- To read computed values of existing formulas, load a second copy with `data_only=True` —
  and never save that copy, it would destroy every formula.
- Dates and times are datetime objects, not strings. Preserve unrelated content.
- **General logic only**: derive answers from the workbook per the instruction. Hardcoding
  golden values is forbidden — `hardcode_hits` in the verdict flags distinctive golden strings
  found in the script; the only acceptable hits are values the instruction text itself states.

## Rules

- Train split only. `build_sft.py` refuses test-split ids; `verify.py --all` warns about them.
- The test split is sealed (DATA_PLAN): no script authoring, no peeking for debugging.
- Rerun `verify.py --all` after any script change; the manifest is always a full rebuild.
