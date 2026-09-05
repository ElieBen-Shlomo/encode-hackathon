"""Per-task solving. Two solvers share one workbook view (`--digest`), one trace format and one
fallback rule (every task ends with an output workbook and a predictions line):

  agent   multi-turn local tool agent: the model inspects and edits the workbook through
          inspect_workbook / run_python / run_bash / recalculate_workbook, then calls finish.
  values  single call: the model returns JSON cell values, written with the baseline writer.
"""

import asyncio
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent
for _candidate in (HERE.parent, HERE.parent / "research"):
    if (_candidate / "sb.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from baseline.common import FORMAT_HINT, parse_answer, write_output
from baseline.common import SYSTEM_PROMPT as VALUES_SYSTEM
from sandbox import run_bash, run_python, stage_input
from sb import answer_cells, recalculate
from serialize import render

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


@dataclass
class SolveConfig:
    mode: str = "agent"              # agent | values
    digest: str = "windowed"         # any name in serialize.available()
    budget_tokens: int | None = None
    reasoning: str = "medium"        # off | low | medium | xhigh | adaptive  (Tinker backend only)
    adaptive_small: int = 20         # adaptive: <= this many graded cells -> low, else medium
    max_turns: int = 20
    tool_timeout: int = 120


def effort_for(task: dict, cfg: SolveConfig) -> str | None:
    """Per-call thinking effort. None means the model's configured default."""
    if cfg.reasoning != "adaptive":
        return None
    try:
        n = len(answer_cells(task))
    except Exception:
        n = 10**6
    return "low" if n <= cfg.adaptive_small else "medium"


def render_workbook(path: str, task: dict, cfg: SolveConfig):
    return render(cfg.digest, path, task, cfg.budget_tokens)


def task_header(task: dict, workbook_text: str, title: str = "Workbook") -> str:
    return (f"## Instruction\n{task['instruction']}\n\n"
            f"## {title}\n{workbook_text}\n\n"
            f"## Answer range\nSheet: {task.get('answer_sheet') or 'active sheet'}\n"
            f"Cells: {task['answer_position']}\n")


def build_messages(task: dict, cfg: SolveConfig) -> tuple[list[dict], dict]:
    rendered = render_workbook(task["init_xlsx"], task, cfg)
    user = task_header(task, rendered.text, "Initial workbook digest") + "\nStart by choosing one tool action."
    return [{"role": "system", "content": AGENT_SYSTEM}, {"role": "user", "content": user}], rendered.meta


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
    """Collects trace lines for one task and writes the output files."""

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

    predict_line = prediction  # older name

    def fail(self, status: str, error: str | None = None) -> str:
        """Init workbook is the output, per SUBMISSION.md."""
        shutil.copy(self.task["init_xlsx"], self.out_xlsx)
        if error:
            self.trace(error=error[:500])
        self.prediction(status[:200])
        return status[:80]


async def complete(model, run: TaskRun, messages: list[dict], effort: str | None = None) -> str | None:
    """One model call with its trace line (backend details such as renderer, stop reason and reasoning
    text are merged in from model.last_info). Returns None on API failure."""
    started = time.time()
    try:
        text, tokens_in, tokens_out = await model.complete(messages, effort=effort)
        extra = {k: v for k, v in getattr(model, "last_info", {}).items() if v is not None}
        run.trace(model=model.name, prompt=messages[-1]["content"], response=text, input_tokens=tokens_in,
                  output_tokens=tokens_out, latency_ms=int((time.time() - started) * 1000), **extra)
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


async def solve_agent(model, task: dict, out_dir: Path, cfg: SolveConfig) -> str:
    """Multi-turn tool conversation; always writes a prediction record and an output workbook."""
    run = TaskRun(task, out_dir)
    shutil.copy(task["init_xlsx"], run.out_xlsx)
    try:
        messages, meta = await asyncio.to_thread(build_messages, task, cfg)
    except Exception as exc:
        return run.fail(f"error: digest failed: {exc}", error=f"digest failed: {type(exc).__name__}: {exc}")
    run.trace(tool="render", tool_input=json.dumps({"digest": cfg.digest, "budget_tokens": cfg.budget_tokens}),
              tool_output=json.dumps(meta, default=str))
    effort = effort_for(task, cfg)

    def digest_now() -> str:
        return render_workbook(str(run.out_xlsx), task, cfg).text

    status = "error: turn limit reached"
    with tempfile.TemporaryDirectory(prefix=f"spreadsheet-agent-{task['id']}-") as temp:
        work_dir = Path(temp)
        staged_in = stage_input(task["init_xlsx"], work_dir)   # the model's tools only ever see this copy
        for turn in range(1, cfg.max_turns + 1):
            text = await complete(model, run, messages, effort)
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
                    ok, result = True, await asyncio.to_thread(digest_now)
                except Exception as exc:
                    ok, result = False, f"{type(exc).__name__}: {exc}"
            elif tool == "run_python" and isinstance(args.get("code"), str):
                ok, result = await asyncio.to_thread(run_python, args["code"], work_dir=work_dir, in_xlsx=staged_in,
                                                     out_xlsx=str(run.out_xlsx), turn=turn, timeout=cfg.tool_timeout)
            elif tool == "run_bash" and isinstance(args.get("command"), str):
                ok, result = await asyncio.to_thread(run_bash, args["command"], work_dir=work_dir, in_xlsx=staged_in,
                                                     out_xlsx=str(run.out_xlsx), timeout=cfg.tool_timeout)
            elif tool == "recalculate_workbook":
                ok, result = await asyncio.to_thread(recalculate_output, run.out_xlsx, work_dir)
            else:
                ok, result = False, f"Unknown tool or invalid args: {tool}"

            if ok and tool in {"run_python", "run_bash", "recalculate_workbook"}:
                try:
                    result += "\n\n## Updated workbook digest\n" + await asyncio.to_thread(digest_now)
                except Exception as exc:
                    result += f"\n\nUnable to digest updated workbook: {type(exc).__name__}: {exc}"
            run.trace(model=model.name, tool=tool, tool_input=args, tool_output=result, error=None if ok else "tool failed")
            tool_result(messages, tool, result)
            status = "error: tool failed" if not ok else "error: turn limit reached"

    if not output_is_readable(run.out_xlsx):
        shutil.copy(task["init_xlsx"], run.out_xlsx)
        status = "error: output unreadable"
    run.prediction(status[:200])
    return status


async def solve_values(model, task: dict, out_dir: Path, cfg: SolveConfig) -> str:
    """The baseline strategy through the same view and trace: one call, JSON cell values, baseline writer."""
    run = TaskRun(task, out_dir)
    try:
        rendered = await asyncio.to_thread(render_workbook, task["init_xlsx"], task, cfg)
    except Exception as exc:
        return run.fail(f"error: render failed: {exc}", error=f"render failed: {type(exc).__name__}: {exc}")
    run.trace(tool="render", tool_input=json.dumps({"digest": cfg.digest, "budget_tokens": cfg.budget_tokens}),
              tool_output=json.dumps(rendered.meta, default=str))
    messages = [{"role": "system", "content": VALUES_SYSTEM},
                {"role": "user", "content": task_header(task, rendered.text) + FORMAT_HINT}]
    text = await complete(model, run, messages, effort_for(task, cfg))
    if text is None:
        return run.fail("error: model call failed")
    try:
        answer = parse_answer(text)
        write_output(task, answer, run.out_xlsx)
    except Exception as exc:
        return run.fail(f"error: {exc}", error=f"values write failed: {type(exc).__name__}: {exc}")
    run.prediction("ok")
    return "ok"


async def solve_task(model, task: dict, out_dir: Path, cfg: SolveConfig | None = None, *,
                     max_turns: int | None = None, tool_timeout: int | None = None) -> str:
    """Dispatch on cfg.mode. Keyword overrides keep research/baseline/agent_predict.py working unchanged."""
    cfg = cfg or SolveConfig()
    if max_turns is not None:
        cfg.max_turns = max_turns
    if tool_timeout is not None:
        cfg.tool_timeout = tool_timeout
    if cfg.mode == "values":
        return await solve_values(model, task, out_dir, cfg)
    return await solve_agent(model, task, out_dir, cfg)
