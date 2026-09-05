# Teacher-script generation — handoff / progress

Session handoff written 2026-09-05 when the generation fleet was stopped mid-run to switch
models for cost. Read README.md first for what this folder is; this file is where the work
stands and exactly how to continue it.

## State

- Infrastructure done and tested end-to-end: `verify.py` (sandbox run → LibreOffice recalc →
  grade vs golden, shipped scorer semantics), `build_sft.py` (harness-format trajectories,
  refuses test ids), `README.md`.
- **23 of 279 train scripts verified passing** (see `manifest.jsonl`). 24 script files exist;
  `118-50` fails at 9974/9998 cells (Sheet-Level, its authoring agent was killed mid-iteration
  — regenerate it).
- Remaining: the other 255 train ids. Compute the exact list any time:

      cd research
      python3 -c "
      import json
      done = {json.loads(l)['id'] for l in open('teacher/manifest.jsonl') if json.loads(l)['passed']}
      ids = [i for i in open('../datasets/splits/train.txt').read().split() if i not in done]
      print(len(ids)); print(','.join(ids))"

## How the fleet ran (reproduce this)

Parallel subagents, ~12 task ids each, each agent looping per id: inspect with
`.venv/bin/python ../agent/show.py <id>` → write `teacher/scripts/<id>.py` → check with
`.venv/bin/python teacher/verify.py <id>` (exit 0 = pass) → fix logic and re-verify, ≤5
attempts, then move on. Use the venv python directly (NOT `uv run` — concurrent uv syncs
race). 12 agents at a time was fine for the machine; LibreOffice bursts are short.

Agent prompt used (worked well; reuse verbatim, swapping the id list):

    Author verified "teacher" reference scripts for SpreadsheetBench tasks in the repo at
    /home/lorcan/code/github.com/ElieBen-Shlomo/encode-hackathon.

    Your task ids (do ALL of them, one at a time): <IDS>

    Per id:
    1. Inspect: `cd .../research && .venv/bin/python ../agent/show.py <id>` — prints the
       instruction, the init-workbook digest, and the golden answer cells. Golden values are
       ground truth for verification and for disambiguating messy forum instructions — but the
       script must NOT hardcode them.
    2. Write a Python script to research/teacher/scripts/<id>.py (overwrite on retries).
    3. Verify: `cd .../research && .venv/bin/python teacher/verify.py <id>` — exit 0 = PASS.
       The JSON verdict lists per-cell mismatches (expected vs actual) and hardcode_hits.
    4. On failure, fix the LOGIC and re-verify — up to 5 verify runs per task. If still
       failing, keep your best general attempt on disk and move on to the next id.

    Script contract (strict):
    - Read env vars IN_XLSX (input, chmod 444 — never write it) and OUT_XLSX. OUT_XLSX already
      exists as a copy of the input: load it with openpyxl, edit, save.
    - Python stdlib + openpyxl only.
    - Write final VALUES computed in Python. Only write a formula string if the instruction
      explicitly demands a formula in that cell (grading recalculates either way).
    - If you need computed values of existing formulas, read them from a second load with
      `data_only=True`; NEVER save a data_only workbook (it erases all formulas).
    - Dates/times must be datetime objects, never strings. Numbers as numbers (the grader
      rounds to 2 decimal places).
    - Edit only what the instruction requires; preserve all other content.
    - GENERAL LOGIC ONLY. Hardcoding golden values is forbidden; hardcode_hits must stay empty
      unless the flagged string appears verbatim in the instruction text itself. A failing
      general script beats a passing hardcoded one — hardcoded scripts poison the fine-tune.
    - Style: clean, direct, ~10-60 lines; this becomes training data for a smaller model.

    You may inspect workbooks read-only (`.venv/bin/python -c "import openpyxl; ..."`). Never
    modify anything under research/data/. Do not run git. Do not create or modify any file
    outside research/teacher/scripts/.

    Notes: instructions are messy Excel-forum posts — the golden cells tell you the intended
    transformation; implement it generally. The digest truncates; read the real workbook when
    in doubt. Only the graded range is scored, but honor the instruction's full intent when
    cheap.

    Final report (nothing else): one line per id — `<id> PASS attempts=N` or
    `<id> FAIL attempts=N correct=X/Y note=<short reason>`.

## After all scripts exist

    cd research
    .venv/bin/python teacher/verify.py --all     # rebuild manifest.jsonl (full sweep)
    uv run teacher/build_sft.py                  # passing train scripts -> teacher/sft/train.jsonl

Then spot-check a sample of scripts for hardcoding the detector can't catch (numeric
literals); the manifest's hardcode_hits only covers distinctive golden strings.

## Context that matters

- Branch: this work lives on `feat/teacher-scripts` (based on main). The separate
  `fix/critical-medium-review` branch (pushed) has the harness/sandbox/Dockerfile fixes and
  no longer touches the given evaluator files (evaluate.py, sb.py are as shipped).
- DATA_PLAN.md rules bind: train split only (279 ids), the test split is sealed. verify.py
  warns on test ids; build_sft.py refuses them.
- One task (60-7) names four sheets in its answer field; only the active sheet is graded by
  the shipped scorer, but the script should honor all four.
- Scripts are authored by Claude — SUBMISSION.md's training-data section must disclose it.
