"""Per-task solving. Two solvers share one workbook view (`--digest`), one trace format and one
fallback rule (every task ends with an output workbook and a predictions line):

  agent   multi-turn, verification-first local tool agent: the model inspects and edits the workbook
          through inspect_workbook / inspect_range / assert_* / run_python / run_bash /
          recalculate_workbook; every declared edit is diffed against the previous state, formula
          cells in the graded range are recalculated, a review turn is required, and an optional
          critic model must approve before finish.
  values  single call: the model returns JSON cell values, written with the baseline writer.
"""

import asyncio
import json
import os
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
from digest import verification_snapshot
from sandbox import run_bash, run_python
from sb import answer_cells, recalculate
from serialize import render
from skills import selected_skills
from verify import diff_workbooks, formula_cells, format_verification
from workbook_tools import assert_blank, assert_sorted, inspect_range

TRACE_TEXT_CAP = 20_000

# Local CPU work is bounded separately from API concurrency: at --concurrency 400 the model calls fly, but
# 400 simultaneous LibreOffice recalculations or sandbox interpreters would thrash the machine.
_LIMITS = {"libreoffice": max(2, os.cpu_count() or 4), "sandbox": 2 * max(2, os.cpu_count() or 4)}
_SEMAPHORES: dict[tuple, asyncio.Semaphore] = {}


def set_local_limits(libreoffice: int | None = None, sandbox: int | None = None) -> None:
    if libreoffice:
        _LIMITS["libreoffice"] = libreoffice
    if sandbox:
        _LIMITS["sandbox"] = sandbox
    _SEMAPHORES.clear()


def _sem(kind: str) -> asyncio.Semaphore:
    key = (kind, id(asyncio.get_running_loop()))
    if key not in _SEMAPHORES:
        _SEMAPHORES[key] = asyncio.Semaphore(_LIMITS[kind])
    return _SEMAPHORES[key]


async def bounded(kind: str, fn, *args, **kwargs):
    """Run blocking local work in a thread, holding the per-kind semaphore."""
    async with _sem(kind):
        return await asyncio.to_thread(fn, *args, **kwargs)

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

Do not access golden workbooks, make network requests, or use web lookup. After every successful edit, independently re-read the instruction and inspect deterministic verification plus the grading-focused snapshot. Repair any issue before finishing. The harness recalculates formula cells in the graded range automatically."""


@dataclass
class SolveConfig:
    mode: str = "agent"              # agent | values
    digest: str = "grid"             # any name in serialize.available(); grid won the representation study
    budget_tokens: int | None = None
    reasoning: str = "low"           # off | low | medium | xhigh | adaptive  (Tinker backend only)
    adaptive_small: int = 20         # adaptive: <= this many graded cells -> low, else medium
    max_turns: int = 20
    tool_timeout: int = 120
    review_after_edit: bool = True
    auto_recalculate_formulas: bool = True
    verify_changes: bool = True
    max_critic_rounds: int = 2
    strict_critic_json: bool = True


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
    skills = selected_skills(task["instruction"])
    user = (task_header(task, rendered.text, "Initial workbook digest") + "\n"
            + (f"## Relevant spreadsheet playbooks\n{skills}\n\n" if skills else "")
            + "Start by choosing one tool action.")
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
        # adapters that predate the effort knob take (messages) only; pass effort only when one is set
        if effort is None:
            text, tokens_in, tokens_out = await model.complete(messages)
        else:
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


async def solve_agent(model, task: dict, out_dir: Path, cfg: SolveConfig, critic=None) -> str:
    """Verification-first tool conversation; always writes a prediction record and an output workbook."""
    run = TaskRun(task, out_dir)
    shutil.copy(task["init_xlsx"], run.out_xlsx)
    try:
        messages, meta = await bounded("libreoffice", build_messages, task, cfg)
    except Exception as exc:
        return run.fail(f"error: digest failed: {exc}", error=f"digest failed: {type(exc).__name__}: {exc}")
    run.trace(tool="render", tool_input=json.dumps({"digest": cfg.digest, "budget_tokens": cfg.budget_tokens}),
              tool_output=json.dumps(meta, default=str))
    effort = effort_for(task, cfg)

    def digest_now() -> str:
        return render_workbook(str(run.out_xlsx), task, cfg).text

    status, review_pending, repair_required = "error: turn limit reached", False, False
    edit_generation, critic_generation, critic_rounds, last_evidence = 0, -1, 0, "No edits were made."
    with tempfile.TemporaryDirectory(prefix=f"spreadsheet-agent-{task['id']}-") as temp:
        work_dir = Path(temp)
        for turn in range(1, cfg.max_turns + 1):
            is_review_turn = review_pending
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
                    if critic_rounds >= cfg.max_critic_rounds:
                        repair_required = True
                        tool_result(messages, "critic", "Critic repair limit reached; make a final explicit repair.")
                        continue
                    critic_rounds += 1
                    critic_generation = edit_generation
                    approved, reason = await ask_critic(critic, task, last_evidence, run, strict_json=cfg.strict_critic_json)
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

            # the sandbox stages a copy of the init for the child, so the dataset file is never exposed
            if tool == "inspect_workbook":
                try:
                    ok, result = True, await bounded("libreoffice", digest_now)
                except Exception as exc:
                    ok, result = False, f"{type(exc).__name__}: {exc}"
            elif tool == "inspect_range":
                ok, result = await asyncio.to_thread(inspect_range, str(run.out_xlsx), args.get("sheet", ""), args.get("range", ""),
                                                     styles=bool(args.get("styles", False)))
            elif tool == "assert_sorted":
                ok, result = await asyncio.to_thread(assert_sorted, str(run.out_xlsx), args.get("sheet", ""), args.get("range", ""), args.get("keys", []))
            elif tool == "assert_blank":
                ok, result = await asyncio.to_thread(assert_blank, str(run.out_xlsx), args.get("sheet", ""), args.get("range", ""))
            elif tool == "run_python" and isinstance(args.get("code"), str):
                ok, result = await bounded("sandbox", run_python, args["code"], work_dir=work_dir, in_xlsx=task["init_xlsx"],
                                                     out_xlsx=str(run.out_xlsx), turn=turn, timeout=cfg.tool_timeout)
            elif tool == "run_bash" and isinstance(args.get("command"), str):
                ok, result = await bounded("sandbox", run_bash, args["command"], work_dir=work_dir, in_xlsx=task["init_xlsx"],
                                                     out_xlsx=str(run.out_xlsx), timeout=cfg.tool_timeout)
            elif tool == "recalculate_workbook":
                ok, result = await bounded("libreoffice", recalculate_output, run.out_xlsx, work_dir)
            else:
                ok, result = False, f"Unknown tool or invalid args: {tool}"

            if ok and is_mutation:
                formulas = await asyncio.to_thread(formula_cells, str(run.out_xlsx), task)
                if formulas and cfg.auto_recalculate_formulas:
                    calc_ok, calc_result = await bounded("libreoffice", recalculate_output, run.out_xlsx, work_dir)
                    result += f"\n\nAutomatic formula recalculation: {calc_result}"
                    ok = ok and calc_ok
                diff = await asyncio.to_thread(diff_workbooks, str(before_edit), str(run.out_xlsx), task,
                                               args.get("expected_changes") if cfg.verify_changes else None)
                formulas = await asyncio.to_thread(formula_cells, str(run.out_xlsx), task)
                verification = format_verification(diff, formulas)
                try:
                    snapshot = await asyncio.to_thread(verification_snapshot, str(run.out_xlsx), task)
                except Exception as exc:
                    snapshot = f"Unable to build verification snapshot: {type(exc).__name__}: {exc}"
                last_evidence = f"## Last tool output\n{result}\n\n{verification}\n\n{snapshot}"
                result = last_evidence
                edit_generation += 1
                repair_required = False
                if cfg.review_after_edit:
                    review_pending = True

            run.trace(model=model.name, tool=tool, tool_input=args, tool_output=result, error=None if ok else "tool failed")
            if ok and is_mutation and cfg.review_after_edit:
                review_result(messages, result)
            else:
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
        rendered = await bounded("libreoffice", render_workbook, task["init_xlsx"], task, cfg)
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
                     max_turns: int | None = None, tool_timeout: int | None = None,
                     review_after_edit: bool | None = None, auto_recalculate_formulas: bool | None = None,
                     verify_changes: bool | None = None, critic=None, max_critic_rounds: int | None = None,
                     strict_critic_json: bool | None = None) -> str:
    """Dispatch on cfg.mode. The keyword overrides keep research/baseline/agent_predict.py working unchanged."""
    cfg = cfg or SolveConfig()
    for name, value in (("max_turns", max_turns), ("tool_timeout", tool_timeout), ("review_after_edit", review_after_edit),
                        ("auto_recalculate_formulas", auto_recalculate_formulas), ("verify_changes", verify_changes),
                        ("max_critic_rounds", max_critic_rounds), ("strict_critic_json", strict_critic_json)):
        if value is not None:
            setattr(cfg, name, value)
    if cfg.mode == "values":
        return await solve_values(model, task, out_dir, cfg)
    return await solve_agent(model, task, out_dir, cfg, critic=critic)
