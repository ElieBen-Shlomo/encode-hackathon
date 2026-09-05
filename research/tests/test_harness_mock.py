"""End-to-end plumbing with the mock model through agent/run.py: every task gets a prediction line and an
output workbook, traces follow the submission contract, and the edit -> review -> finish protocol holds."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import harness
import models
from conftest import AGENT, MOCK_IDS, RESEARCH

TRACE_FIELDS = {"step", "model", "prompt", "response", "input_tokens", "output_tokens", "latency_ms", "error"}


def run_pipeline(out_dir: Path, mode: str, extra=()):
    cmd = [sys.executable, str(AGENT / "run.py"), "--dataset-dir", str(RESEARCH / "data" / "spreadsheetbench_verified_400"),
           "--out-dir", str(out_dir), "--ids", ",".join(MOCK_IDS), "--mode", mode, "--model", "mock", "--concurrency", "3", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=900)


def read_jsonl(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_null_mode_copies_init_for_every_task(dataset_dir, tmp_path):
    proc = run_pipeline(tmp_path, "null")
    assert proc.returncode == 0, proc.stderr[-2000:]
    preds = read_jsonl(tmp_path / "predictions.jsonl")
    assert sorted(p["id"] for p in preds) == sorted(MOCK_IDS) and all(p["status"] == "ok" for p in preds)
    for tid in MOCK_IDS:
        assert (tmp_path / "outputs" / f"{tid}.xlsx").exists()
        rec = read_jsonl(tmp_path / "traces" / f"{tid}.jsonl")[0]
        assert TRACE_FIELDS <= set(rec) and rec["model"] == "null"


def test_agent_mode_with_mock_follows_edit_review_finish(dataset_dir, tmp_path):
    proc = run_pipeline(tmp_path, "agent")
    assert proc.returncode == 0, proc.stderr[-2000:]
    preds = read_jsonl(tmp_path / "predictions.jsonl")
    assert sorted(p["id"] for p in preds) == sorted(MOCK_IDS)
    assert all(p["status"] == "ok" for p in preds), preds
    for tid in MOCK_IDS:
        assert (tmp_path / "outputs" / f"{tid}.xlsx").exists()
        trace = read_jsonl(tmp_path / "traces" / f"{tid}.jsonl")
        for rec in trace:
            assert TRACE_FIELDS <= set(rec), f"trace record missing {TRACE_FIELDS - set(rec)}"
        assert [r["step"] for r in trace] == list(range(1, len(trace) + 1))
        model_calls = [r for r in trace if r.get("model") == "mock" and not r.get("tool")]
        assert model_calls[0]["prompt"].startswith("## Instruction")
        tools = [r["tool"] for r in trace if r.get("tool")]
        assert tools[0] == "run_python" and tools[-1] == "finish"
        edit = next(r for r in trace if r.get("tool") == "run_python")
        assert "## Deterministic verification" in edit["tool_output"]
        assert "## Graded answer cells" in edit["tool_output"]
        # the review turn was posed to the model before it finished
        assert any("Required independent review" in (r.get("prompt") or "") for r in model_calls)
    assert (tmp_path / "run.log").read_text()


def test_resume_does_not_duplicate_prediction_lines(dataset_dir, tmp_path):
    assert run_pipeline(tmp_path, "null").returncode == 0
    preds = read_jsonl(tmp_path / "predictions.jsonl")
    (tmp_path / "predictions.jsonl").write_text("".join(json.dumps(p) + "\n" for p in preds[:-1]))
    proc = run_pipeline(tmp_path, "null", extra=["--resume"])
    assert proc.returncode == 0, proc.stderr[-2000:]
    preds2 = read_jsonl(tmp_path / "predictions.jsonl")
    assert sorted(p["id"] for p in preds2) == sorted(MOCK_IDS) and len(preds2) == len(MOCK_IDS)


def test_parse_action_accepts_fenced_json_and_rejects_bad_shapes():
    assert harness.parse_action('{"tool":"finish","args":{}}') == {"tool": "finish", "args": {}}
    assert harness.parse_action('```json\n{"tool":"inspect_workbook","args":{}}\n```')["tool"] == "inspect_workbook"
    for bad in ('["finish"]', '{"tool": 3, "args": {}}', '{"tool": "x"}', "not json"):
        with pytest.raises(Exception):
            harness.parse_action(bad)


def test_trace_truncates_long_fields_with_note(out_dir):
    run = harness.TaskRun({"id": "t1", "init_xlsx": "x"}, out_dir)
    run.trace(model="m", prompt="x" * 30000, response="ok")
    rec = read_jsonl(out_dir / "traces" / "t1.jsonl")[0]
    assert len(rec["prompt"]) < 30000 and "truncated, 30000 chars total" in rec["prompt"] and rec["response"] == "ok"


def test_model_failure_still_writes_prediction_and_output(tasks, out_dir):
    class Broken:
        name = "broken"

        async def complete(self, messages):
            raise RuntimeError("endpoint down")

    status = asyncio.run(harness.solve_task(Broken(), tasks["12307"], out_dir, max_turns=3))
    assert status == "error: model call failed"
    preds = read_jsonl(out_dir / "predictions.jsonl")
    assert preds[0]["id"] == "12307" and preds[0]["status"].startswith("error")
    assert (out_dir / "outputs" / "12307.xlsx").exists()          # init copied as the output
    rec = read_jsonl(out_dir / "traces" / "12307.jsonl")[0]
    assert rec["error"].startswith("RuntimeError")


def test_finish_is_blocked_while_review_is_pending(tasks, out_dir):
    """A model that tries to finish right after an edit, skipping the review turn, is refused once."""
    class Impatient:
        name = "impatient"

        def __init__(self):
            self.turn = 0

        async def complete(self, messages):
            self.turn += 1
            if self.turn == 1:
                code = "import os, openpyxl\nwb = openpyxl.load_workbook(os.environ['OUT_XLSX']); wb.save(os.environ['OUT_XLSX'])"
                return json.dumps({"tool": "run_python", "args": {"mode": "edit", "expected_changes": [], "code": code}}), 0, 0
            return json.dumps({"tool": "finish", "args": {"summary": "done"}}), 0, 0

    status = asyncio.run(harness.solve_task(Impatient(), tasks["12307"], out_dir, max_turns=6))
    assert status == "ok"
    trace = read_jsonl(out_dir / "traces" / "12307.jsonl")
    tools = [r.get("tool") for r in trace if r.get("tool")]
    assert tools == ["run_python", "finish"]  # the review turn consumed the first finish; no "finish blocked" needed


def test_invalid_action_is_reported_and_loop_continues(tasks, out_dir):
    class Garbled:
        name = "garbled"

        def __init__(self):
            self.turn = 0

        async def complete(self, messages):
            self.turn += 1
            return ("this is not json", 0, 0) if self.turn == 1 else (json.dumps({"tool": "finish", "args": {}}), 0, 0)

    status = asyncio.run(harness.solve_task(Garbled(), tasks["12307"], out_dir, max_turns=4))
    assert status == "ok"
    trace = read_jsonl(out_dir / "traces" / "12307.jsonl")
    assert any(r.get("tool") == "action_parser" and r.get("error") == "invalid action" for r in trace)
