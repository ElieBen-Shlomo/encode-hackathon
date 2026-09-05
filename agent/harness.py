"""Multi-turn local tool agent for SpreadsheetBench tasks."""

import json
import shutil
import tempfile
import time
from pathlib import Path

import openpyxl

from digest import digest
from sandbox import run_bash, run_python
from sb import recalculate

TRACE_TEXT_CAP = 20_000

AGENT_SYSTEM = """You are an expert spreadsheet engineer operating a local workbook tool agent.
Solve the user instruction by inspecting and editing the workbook, then finish with a valid output workbook.

Reply with EXACTLY one JSON object and no prose, Markdown, or <think> tags. Valid actions are:
{"tool":"inspect_workbook","args":{}}
{"tool":"run_python","args":{"code":"complete Python script"}}
{"tool":"run_bash","args":{"command":"Bash command"}}
{"tool":"recalculate_workbook","args":{}}
{"tool":"finish","args":{"summary":"short completion note"}}

Python and Bash run locally in a persistent task workspace. They receive IN_XLSX and OUT_XLSX environment variables and Python also receives them as argv[1] and argv[2]. OUT_XLSX begins as a copy of IN_XLSX; edit and save OUT_XLSX, never the input. Use openpyxl for workbook edits. Dates must be real datetime objects, not text. Preserve unrelated workbook content.

Do not access golden workbooks, make network requests, or use web lookup. The workbook digest is incomplete; inspect the actual workbook with a tool when needed. After each tool result, choose the next JSON action. Only call finish after OUT_XLSX is ready for grading. After a successful edit, use the updated digest to verify it and call finish unless another concrete repair is needed; do not rewrite the same workbook merely to explain your work."""


def build_messages(task: dict) -> list[dict]:
    return [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": (
            f"## Instruction\n{task['instruction']}\n\n"
            f"## Initial workbook digest\n{digest(task['init_xlsx'], task)}\n\n"
            f"## Graded answer range\nSheet: {task.get('answer_sheet') or 'active sheet'}\n"
            f"Cells: {task['answer_position']}\n\nStart by choosing one tool action."
        )},
    ]


def parse_action(text: str) -> dict:
    """Accept one object, or an object inside a Markdown fence, and validate its tool shape."""
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
        self.task = task
        self.out_dir = out_dir
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


async def solve_task(model, task: dict, out_dir: Path, *, max_turns: int = 20, tool_timeout: int = 120) -> str:
    """Run a tool conversation and always write a prediction record and output workbook."""
    run = TaskRun(task, out_dir)
    shutil.copy(task["init_xlsx"], run.out_xlsx)
    try:
        messages = build_messages(task)
    except Exception as exc:
        run.trace(error=f"digest failed: {type(exc).__name__}: {exc}"[:500])
        run.prediction(f"error: digest failed: {exc}"[:200])
        return "error: digest"

    status = "error: turn limit reached"
    with tempfile.TemporaryDirectory(prefix=f"spreadsheet-agent-{task['id']}-") as temp:
        work_dir = Path(temp)
        for turn in range(1, max_turns + 1):
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

            tool, args = action["tool"], action["args"]
            if tool == "finish":
                if output_is_readable(run.out_xlsx):
                    run.trace(model=model.name, tool="finish", tool_input=args, tool_output="workbook accepted")
                    run.prediction("ok")
                    return "ok"
                result = "OUT_XLSX is unreadable. Repair it before calling finish."
                run.trace(model=model.name, tool="finish", tool_input=args, tool_output=result, error="unreadable output")
                tool_result(messages, tool, result)
                status = "error: unreadable output"
                continue

            if tool == "inspect_workbook":
                try:
                    ok, result = True, digest(str(run.out_xlsx), task)
                except Exception as exc:
                    ok, result = False, f"{type(exc).__name__}: {exc}"
            elif tool == "run_python" and isinstance(args.get("code"), str):
                ok, result = run_python(args["code"], work_dir=work_dir, in_xlsx=task["init_xlsx"],
                                        out_xlsx=str(run.out_xlsx), turn=turn, timeout=tool_timeout)
            elif tool == "run_bash" and isinstance(args.get("command"), str):
                ok, result = run_bash(args["command"], work_dir=work_dir, in_xlsx=task["init_xlsx"],
                                      out_xlsx=str(run.out_xlsx), timeout=tool_timeout)
            elif tool == "recalculate_workbook":
                ok, result = recalculate_output(run.out_xlsx, work_dir)
            else:
                ok, result = False, f"Unknown tool or invalid args: {tool}"

            if ok and tool in {"run_python", "run_bash", "recalculate_workbook"}:
                try:
                    result += "\n\n## Updated workbook digest\n" + digest(str(run.out_xlsx), task)
                except Exception as exc:
                    result += f"\n\nUnable to digest updated workbook: {type(exc).__name__}: {exc}"
            run.trace(model=model.name, tool=tool, tool_input=args, tool_output=result,
                      error=None if ok else "tool failed")
            tool_result(messages, tool, result)
            status = "error: tool failed" if not ok else "error: turn limit reached"

    if not output_is_readable(run.out_xlsx):
        shutil.copy(task["init_xlsx"], run.out_xlsx)
        status = "error: output unreadable"
    run.prediction(status[:200])
    return status
