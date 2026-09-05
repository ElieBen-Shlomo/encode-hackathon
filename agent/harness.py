"""Multi-turn, verification-first local tool agent for SpreadsheetBench tasks."""

import json
import shutil
import tempfile
import time
from pathlib import Path

import openpyxl

from digest import digest, verification_snapshot
from sandbox import run_bash, run_python
from skills import selected_skills
from sb import recalculate
from verify import answer_range_coverage, diff_workbooks, error_cells_in_answer_range, formula_cells, format_verification
from workbook_tools import assert_blank, assert_sorted, inspect_range

TRACE_TEXT_CAP = 20_000

AGENT_SYSTEM = """You are an expert spreadsheet engineer operating a local workbook tool agent.
Solve the user instruction by inspecting and editing the workbook, then finish with a valid output workbook.

Reply with EXACTLY one JSON object and no prose, Markdown, or <think> tags. Valid actions are:
{"tool":"inspect_workbook","args":{}}
{"tool":"inspect_range","args":{"sheet":"Sheet1","range":"A1:E20","styles":false}}
{"tool":"assert_sorted","args":{"sheet":"Sheet1","range":"A2:E100","keys":["A","C"]}}
{"tool":"assert_blank","args":{"sheet":"Sheet1","range":"I295:M295"}}
{"tool":"run_python","args":{"mode":"inspect","code":"read-only Python script"}}
{"tool":"run_python","args":{"mode":"edit","expected_changes":["Sheet1!Q2:Q701"],"code":"complete Python script"}}
{"tool":"run_bash","args":{"mode":"inspect","command":"read-only Bash command"}}
{"tool":"run_bash","args":{"mode":"edit","expected_changes":["Sheet1!A1:E1102"],"command":"Bash command"}}
{"tool":"recalculate_workbook","args":{}}
{"tool":"finish","args":{"summary":"short completion note"}}

Python and Bash run locally in a persistent task workspace. They receive IN_XLSX and OUT_XLSX environment variables and Python also receives them as argv[1] and argv[2]. OUT_XLSX begins as a copy of IN_XLSX; edit and save OUT_XLSX, never the input. Use openpyxl for workbook edits. Dates must be real datetime objects, not text. Preserve unrelated workbook content.

Use mode=inspect for diagnostics that do not change OUT_XLSX. Use mode=edit only when changing it, and declare the target ranges in expected_changes. For iterative rules, first run an inspect simulation that prints intermediate state before editing. “At least”, “no less than”, and equivalent boundaries are inclusive: use >= and a small float tolerance when appropriate.

Only grading cell values matter, not whether they came from a formula. Prefer computing the answer in Python and writing the literal result into graded cells; write a live formula only when the instruction explicitly asks for one. This avoids two common failure classes: functions newer than Excel 2007 need the exact `_xlfn.` prefix openpyxl requires when written directly — `_xlfn.XLOOKUP`, `_xlfn.UNIQUE`, `_xlfn.LET`, `_xlfn.TEXTSPLIT`, `_xlfn.TEXTJOIN`, `_xlfn.CHOOSECOLS`, `_xlfn._xlws.FILTER` — classic functions (SUM, SUMIFS, INDEX, MATCH, VLOOKUP, LOOKUP, AGGREGATE) need none; and array-style formulas (whole-column INDEX/MATCH tricks, LOOKUP(2,1/(...)), DATEVALUE over a range) only evaluate correctly with CSE array entry, which is easy to get wrong. If you do write a formula, verify its recalculated value is not an error string before finishing.

Do not access golden workbooks, make network requests, or use web lookup. After every successful edit, independently re-read the instruction and inspect deterministic verification plus the grading-focused snapshot. The verification includes a formula-error check and a per-column answer-range coverage report — a column reported as fully unchanged since the initial workbook usually means you missed part of the task; confirm it should genuinely stay blank before finishing. Repair any issue before finishing. The harness recalculates formula cells in the graded range automatically and blocks finish while a graded cell still evaluates to an error."""


def build_messages(task: dict) -> list[dict]:
    skills = selected_skills(task["instruction"])
    return [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": (
            f"## Instruction\n{task['instruction']}\n\n"
            f"## Initial workbook digest\n{digest(task['init_xlsx'], task)}\n\n"
            f"## Graded answer range\nSheet: {task.get('answer_sheet') or 'active sheet'}\n"
            f"Cells: {task['answer_position']}\n\n"
            + (f"## Relevant spreadsheet playbooks\n{skills}\n\n" if skills else "")
            + "Start by choosing one tool action."
        )},
    ]


def parse_action(text: str) -> dict:
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
    action = json.loads(candidate)
    if not isinstance(action, dict) or not isinstance(action.get("tool"), str) or not isinstance(action.get("args"), dict):
        raise ValueError('action must be {"tool": string, "args": object}')
    return action


def _cap(value):
    if isinstance(value, str) and len(value) > TRACE_TEXT_CAP:
        return value[:TRACE_TEXT_CAP] + f"...[truncated, {len(value)} chars total]"
    return value


class TaskRun:
    def __init__(self, task: dict, out_dir: Path):
        self.task, self.out_dir = task, out_dir
        self.out_xlsx = out_dir / "outputs" / f"{task['id']}.xlsx"
        self.trace_path = out_dir / "traces" / f"{task['id']}.jsonl"
        self.step = 0

    def trace(self, **fields) -> None:
        from writers import append_jsonl
        self.step += 1
        record = {"step": self.step, "model": None, "prompt": None, "response": None,
                  "input_tokens": None, "output_tokens": None, "latency_ms": None, "error": None}
        record.update({key: _cap(value) for key, value in fields.items()})
        append_jsonl(self.trace_path, record)

    def prediction(self, status: str) -> None:
        from writers import append_jsonl
        append_jsonl(self.out_dir / "predictions.jsonl", {
            "id": self.task["id"], "output": f"outputs/{self.task['id']}.xlsx", "status": status,
        })


async def complete(model, run: TaskRun, messages: list[dict]) -> str | None:
    started = time.time()
    try:
        text, tokens_in, tokens_out = await model.complete(messages)
        run.trace(model=model.name, prompt=messages[-1]["content"], response=text, input_tokens=tokens_in,
                  output_tokens=tokens_out, latency_ms=int((time.time() - started) * 1000))
        return text
    except Exception as exc:
        run.trace(model=model.name, prompt=messages[-1]["content"], latency_ms=int((time.time() - started) * 1000),
                  error=f"{type(exc).__name__}: {exc}"[:500])
        return None


def tool_result(messages: list[dict], tool: str, result: str) -> None:
    messages.append({"role": "user", "content": f"## Tool result: {tool}\n{_cap(result)}\n\nChoose the next JSON action."})


def review_result(messages: list[dict], result: str) -> None:
    messages.append({"role": "user", "content": (
        "## Required independent review\nA workbook edit succeeded. Re-read the instruction and inspect the "
        "verification below. For iterative tasks, manually check the first several transitions. Issue a repair "
        "edit if anything is wrong; otherwise call finish.\n\n"
        f"{_cap(result)}\n\nChoose exactly one JSON action."
    )})


def output_is_readable(path: Path) -> bool:
    try:
        openpyxl.load_workbook(path, read_only=True).close()
        return True
    except Exception:
        return False


def recalculate_output(out_xlsx: Path, work_dir: Path) -> tuple[bool, str]:
    try:
        recalculated = recalculate(str(out_xlsx), work_dir / "recalculated")
        shutil.copy(recalculated, out_xlsx)
        return True, "LibreOffice recalculation completed"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def ask_critic(critic, task: dict, evidence: str, run: TaskRun, *, strict_json: bool) -> tuple[bool, str]:
    prompt = (
        "You are an independent spreadsheet-workbook critic. Do not edit files and do not use external data. "
        "Check exact row/column placement, formulas, data types, sort order, inclusive boundaries, and unexpected changes. "
        "Reply with exactly one JSON object: {\"verdict\":\"approve\",\"checks\":[{\"name\":\"...\",\"status\":\"pass\",\"evidence\":\"...\"}],\"reason\":\"...\"} "
        "or {\"verdict\":\"repair\",\"checks\":[...],\"reason\":\"specific correction\"}.\n\n"
        f"## Instruction\n{task['instruction']}\n\n## Primary evidence\n{evidence}"
    )
    started = time.time()
    try:
        text, tokens_in, tokens_out = await critic.complete([{"role": "user", "content": prompt}])
        run.trace(model=critic.name, prompt=prompt, response=text, input_tokens=tokens_in, output_tokens=tokens_out,
                  latency_ms=int((time.time() - started) * 1000), tool="critic")
        verdict = json.loads(text.strip())
        if isinstance(verdict, dict) and verdict.get("verdict") == "approve":
            return True, str(verdict.get("reason") or "Critic approved")
        if isinstance(verdict, dict) and verdict.get("verdict") == "repair":
            return False, str(verdict.get("reason") or "Critic requested repair")
        return (not strict_json), "Critic returned an unrecognised verdict"
    except Exception as exc:
        run.trace(model=critic.name, prompt=prompt, tool="critic", error=f"{type(exc).__name__}: {exc}"[:500],
                  latency_ms=int((time.time() - started) * 1000))
        return (not strict_json), f"Critic unavailable or invalid JSON: {type(exc).__name__}: {exc}"


async def solve_task(model, task: dict, out_dir: Path, *, max_turns: int = 20, tool_timeout: int = 120,
                     review_after_edit: bool = True, auto_recalculate_formulas: bool = True,
                     verify_changes: bool = True, critic=None, max_critic_rounds: int = 2,
                     strict_critic_json: bool = True) -> str:
    run = TaskRun(task, out_dir)
    shutil.copy(task["init_xlsx"], run.out_xlsx)
    try:
        messages = build_messages(task)
    except Exception as exc:
        run.trace(error=f"digest failed: {type(exc).__name__}: {exc}"[:500])
        run.prediction(f"error: digest failed: {exc}"[:200])
        return "error: digest"

    status, review_pending, repair_required = "error: turn limit reached", False, False
    edit_generation, critic_generation, critic_rounds, last_evidence = 0, -1, 0, "No edits were made."
    with tempfile.TemporaryDirectory(prefix=f"spreadsheet-agent-{task['id']}-") as temp:
        work_dir = Path(temp)
        for turn in range(1, max_turns + 1):
            is_review_turn = review_pending
            text = await complete(model, run, messages)
            if text is None:
                status = "error: model call failed"
                break
            messages.append({"role": "assistant", "content": text})
            try:
                action = parse_action(text)
            except Exception as exc:
                result = f"Invalid tool JSON: {type(exc).__name__}: {exc}. Reply with exactly one valid action object."
                run.trace(model=model.name, tool="action_parser", tool_input=text, tool_output=result, error="invalid action")
                tool_result(messages, "action_parser", result)
                status = "error: invalid action"
                continue
            if is_review_turn:
                review_pending = False

            tool, args = action["tool"], action["args"]
            if tool == "finish":
                if review_pending or repair_required:
                    result = "A required review/repair is still outstanding. Issue an edit tool call before finish."
                    run.trace(model=model.name, tool="finish", tool_input=args, tool_output=result, error="finish blocked")
                    tool_result(messages, tool, result)
                    status = "error: finish blocked"
                    continue
                if critic is not None and edit_generation and critic_generation != edit_generation:
                    if critic_rounds >= max_critic_rounds:
                        repair_required = True
                        tool_result(messages, "critic", "Critic repair limit reached; make a final explicit repair.")
                        continue
                    critic_rounds += 1
                    critic_generation = edit_generation
                    approved, reason = await ask_critic(critic, task, last_evidence, run, strict_json=strict_critic_json)
                    if not approved:
                        repair_required, review_pending = True, True
                        tool_result(messages, "critic", f"Critic requested repair: {reason}")
                        status = "error: critic requested repair"
                        continue
                if output_is_readable(run.out_xlsx):
                    run.trace(model=model.name, tool="finish", tool_input=args, tool_output="workbook accepted")
                    run.prediction("ok")
                    return "ok"
                result = "OUT_XLSX is unreadable. Repair it before calling finish."
                run.trace(model=model.name, tool="finish", tool_input=args, tool_output=result, error="unreadable output")
                tool_result(messages, tool, result)
                status = "error: unreadable output"
                continue

            mode = args.get("mode", "inspect")
            is_mutation = tool in {"run_python", "run_bash"} and mode == "edit"
            before_edit = work_dir / f"before_{turn:02d}.xlsx"
            if is_mutation:
                shutil.copy(run.out_xlsx, before_edit)

            if tool == "inspect_workbook":
                try:
                    ok, result = True, digest(str(run.out_xlsx), task)
                except Exception as exc:
                    ok, result = False, f"{type(exc).__name__}: {exc}"
            elif tool == "inspect_range":
                ok, result = inspect_range(str(run.out_xlsx), args.get("sheet", ""), args.get("range", ""), styles=bool(args.get("styles", False)))
            elif tool == "assert_sorted":
                ok, result = assert_sorted(str(run.out_xlsx), args.get("sheet", ""), args.get("range", ""), args.get("keys", []))
            elif tool == "assert_blank":
                ok, result = assert_blank(str(run.out_xlsx), args.get("sheet", ""), args.get("range", ""))
            elif tool == "run_python" and isinstance(args.get("code"), str):
                ok, result = run_python(args["code"], work_dir=work_dir, in_xlsx=task["init_xlsx"], out_xlsx=str(run.out_xlsx), turn=turn, timeout=tool_timeout)
            elif tool == "run_bash" and isinstance(args.get("command"), str):
                ok, result = run_bash(args["command"], work_dir=work_dir, in_xlsx=task["init_xlsx"], out_xlsx=str(run.out_xlsx), timeout=tool_timeout)
            elif tool == "recalculate_workbook":
                ok, result = recalculate_output(run.out_xlsx, work_dir)
            else:
                ok, result = False, f"Unknown tool or invalid args: {tool}"

            if ok and is_mutation:
                formulas = formula_cells(str(run.out_xlsx), task)
                if formulas and auto_recalculate_formulas:
                    calc_ok, calc_result = recalculate_output(run.out_xlsx, work_dir)
                    result += f"\n\nAutomatic formula recalculation: {calc_result}"
                    ok = ok and calc_ok
                diff = diff_workbooks(str(before_edit), str(run.out_xlsx), task, args.get("expected_changes") if verify_changes else None)
                formulas = formula_cells(str(run.out_xlsx), task)
                verification = format_verification(diff, formulas)
                error_cells = error_cells_in_answer_range(str(run.out_xlsx), task)
                coverage = answer_range_coverage(task["init_xlsx"], str(run.out_xlsx), task)
                if error_cells:
                    verification += (f"\n\n## GRADED CELLS CONTAIN FORMULA ERRORS\n{', '.join(error_cells[:20])}\n"
                                      "These cells will score as wrong. Repair before finishing.")
                # Warning only, not a hard block: a graded column can legitimately stay blank by
                # design, so forcing repair here risks a deadlock that burns every remaining turn.
                verification += f"\n\n## Answer range coverage\n{coverage}"
                try:
                    snapshot = verification_snapshot(str(run.out_xlsx), task)
                except Exception as exc:
                    snapshot = f"Unable to build verification snapshot: {type(exc).__name__}: {exc}"
                last_evidence = f"## Last tool output\n{result}\n\n{verification}\n\n{snapshot}"
                result = last_evidence
                edit_generation += 1
                repair_required = bool(error_cells)
                if review_after_edit:
                    review_pending = True

            run.trace(model=model.name, tool=tool, tool_input=args, tool_output=result, error=None if ok else "tool failed")
            if ok and is_mutation and review_after_edit:
                review_result(messages, result)
            else:
                tool_result(messages, tool, result)
            status = "error: tool failed" if not ok else "error: turn limit reached"

    if not output_is_readable(run.out_xlsx):
        shutil.copy(task["init_xlsx"], run.out_xlsx)
        status = "error: output unreadable"
    run.prediction(status[:200])
    return status
