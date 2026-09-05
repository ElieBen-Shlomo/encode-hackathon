"""Per-task orchestration: prompt -> model writes Python -> sandboxed exec ->
traceback repair retries -> fallback. Every model call and every exec step gets a
trace line; every task always ends with an output workbook and a predictions line.
"""

import re
import shutil
import time
from pathlib import Path

from digest import digest
from sandbox import run_code

MAX_ATTEMPTS = 3       # 1 initial + 2 traceback repairs
EXEC_TIMEOUT = 120
TRACE_TEXT_CAP = 20000  # per SUBMISSION.md: truncate long fields and say so

CODE_SYSTEM = """You are an expert spreadsheet engineer. You get a user instruction from an \
Excel forum post, a digest of the workbook, and the answer range that will be graded. Write a \
complete Python script that applies the instruction to the workbook.

Contract:
- The script receives IN_XLSX as sys.argv[1] and OUT_XLSX as sys.argv[2] (also as env vars).
- OUT_XLSX already contains a copy of IN_XLSX. Load OUT_XLSX with openpyxl, mutate ONLY what \
the instruction requires, and save back to OUT_XLSX. Never rebuild the workbook from scratch: \
other sheets and cells must survive untouched.
- The digest may omit rows for brevity, but your script reads the real file: never hardcode \
values copied from the digest when they can be computed from the data; operate on the full data.
- Prefer computing plain values in Python over writing Excel formulas. If you must write a \
formula, modern functions need the _xlfn. prefix (e.g. _xlfn.XLOOKUP); classic ones (SUM, \
INDEX, VLOOKUP...) do not.
- Grading compares values after recalculation: numbers rounded to 2 decimals, dates must be \
real datetime objects (not strings), empty cells stay empty (None). Booleans stay booleans.
- The answer must land exactly in the graded range on the graded sheet.
- openpyxl is available. No network access. Only write to OUT_XLSX.

Reply with a short plan (2-4 sentences), then ONE ```python code block with the full script."""

REPAIR_PROMPT = """Your script failed. Fix it and reply with the corrected FULL script in one \
```python block.

## Your script
```python
{code}
```

## Output / traceback
{error}
"""


def build_prompt(task: dict) -> str:
    return (
        f"## Instruction\n{task['instruction']}\n\n"
        f"## Workbook digest\n{digest(task['init_xlsx'], task)}\n\n"
        f"## Graded answer range\nSheet: {task.get('answer_sheet') or 'active sheet'}\n"
        f"Cells: {task['answer_position']}\n"
    )


def extract_code(text: str) -> str | None:
    blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", text, flags=re.S)
    return blocks[-1].strip() if blocks else None


def _cap(s):
    if isinstance(s, str) and len(s) > TRACE_TEXT_CAP:
        return s[:TRACE_TEXT_CAP] + f"...[truncated, {len(s)} chars total]"
    return s


class TaskRun:
    """Collects trace lines for one task and writes the output files."""

    def __init__(self, task: dict, out_dir: Path):
        self.task = task
        self.out_dir = out_dir
        self.out_xlsx = out_dir / "outputs" / f"{task['id']}.xlsx"
        self.step = 0
        self.trace_path = out_dir / "traces" / f"{task['id']}.jsonl"

    def trace(self, **fields):
        from writers import append_jsonl
        self.step += 1
        record = {"step": self.step, "model": None, "prompt": None, "response": None,
                  "input_tokens": None, "output_tokens": None, "latency_ms": None, "error": None}
        record.update({k: _cap(v) for k, v in fields.items()})
        append_jsonl(self.trace_path, record)

    def predict_line(self, status: str):
        from writers import append_jsonl
        append_jsonl(self.out_dir / "predictions.jsonl",
                     {"id": self.task["id"], "output": f"outputs/{self.task['id']}.xlsx", "status": status})


async def timed_complete(model, run: TaskRun, system: str, prompt: str) -> str | None:
    """One model call with its trace line. Returns None on API failure."""
    started = time.time()
    try:
        text, tok_in, tok_out = await model.complete(system, prompt)
        run.trace(model=model.name, prompt=prompt, response=text, input_tokens=tok_in,
                  output_tokens=tok_out, latency_ms=int((time.time() - started) * 1000))
        return text
    except Exception as e:
        run.trace(model=model.name, prompt=prompt, latency_ms=int((time.time() - started) * 1000),
                  error=f"{type(e).__name__}: {e}"[:500])
        return None


async def solve_task(model, task: dict, out_dir: Path) -> str:
    """Code-gen loop with repair retries. Guarantees an output workbook and returns the status."""
    run = TaskRun(task, out_dir)
    try:
        prompt = build_prompt(task)
    except Exception as e:
        shutil.copy(task["init_xlsx"], run.out_xlsx)
        run.trace(error=f"digest failed: {type(e).__name__}: {e}"[:500])
        run.predict_line(f"error: digest failed: {e}"[:200])
        return "error: digest"

    status = "error: no attempts"
    for attempt in range(MAX_ATTEMPTS):
        text = await timed_complete(model, run, CODE_SYSTEM, prompt)
        if text is None:
            status = "error: model call failed"
            break
        code = extract_code(text)
        if code is None:
            prompt = "Your reply had no ```python code block. " + REPAIR_PROMPT.format(
                code="(none)", error="no code block found in your reply")
            status = "error: no code block"
            continue
        ok, exec_log = run_code(code, task["init_xlsx"], str(run.out_xlsx), EXEC_TIMEOUT)
        run.trace(model=model.name, tool="python_sandbox", tool_input=code, tool_output=exec_log,
                  error=None if ok else "exec failed")
        if ok:
            run.predict_line("ok")
            return "ok"
        prompt = REPAIR_PROMPT.format(code=code, error=exec_log)
        status = "error: exec failed after retries"

    # all attempts failed: init workbook is the output, per SUBMISSION.md
    shutil.copy(task["init_xlsx"], run.out_xlsx)
    run.predict_line(status[:200])
    return status
