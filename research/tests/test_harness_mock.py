"""End-to-end plumbing with the mock model: every task gets a line and a file, traces obey the
submission contract, and --resume neither duplicates nor appends to stale traces."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import AGENT, RESEARCH

IDS = ["12307", "560-12", "17-35"]
TRACE_FIELDS = {"step", "model", "prompt", "response", "input_tokens", "output_tokens", "latency_ms", "error"}


def run_pipeline(out_dir: Path, mode: str, extra=()):
    cmd = [sys.executable, str(AGENT / "run.py"), "--dataset-dir", str(RESEARCH / "data" / "spreadsheetbench_verified_400"),
           "--out-dir", str(out_dir), "--ids", ",".join(IDS), "--mode", mode, "--model", "mock",
           "--digest", "grid", "--reasoning", "low", "--concurrency", "3", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def read_jsonl(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.mark.parametrize("mode", ["values", "agent"])
def test_every_task_gets_output_and_prediction(dataset_dir, tmp_path, mode):
    out = tmp_path / mode
    proc = run_pipeline(out, mode)
    assert proc.returncode == 0, proc.stderr[-2000:]
    preds = read_jsonl(out / "predictions.jsonl")
    assert sorted(p["id"] for p in preds) == sorted(IDS)
    assert all(p["status"] == "ok" for p in preds), preds
    for tid in IDS:
        assert (out / "outputs" / f"{tid}.xlsx").exists()
        trace = read_jsonl(out / "traces" / f"{tid}.jsonl")
        assert trace, "no trace lines"
        for rec in trace:
            assert TRACE_FIELDS <= set(rec), f"trace record missing {TRACE_FIELDS - set(rec)}"
        assert trace[0]["tool"] == "render" and json.loads(trace[0]["tool_output"])["digest"] == "grid"
        model_calls = [r for r in trace if r.get("model") == "mock" and not r.get("tool")]
        assert model_calls and model_calls[0]["prompt"].startswith("## Instruction")
        assert [r["step"] for r in trace] == list(range(1, len(trace) + 1))
    assert (out / "run_config.json").exists() and (out / "run.log").read_text()


def test_resume_skips_done_and_removes_stale_traces(dataset_dir, tmp_path):
    out = tmp_path / "resume"
    assert run_pipeline(out, "values").returncode == 0
    preds = read_jsonl(out / "predictions.jsonl")
    last = preds[-1]["id"]
    # simulate a run killed mid-flight: the last task has a trace and output but no predictions line
    (out / "predictions.jsonl").write_text("".join(json.dumps(p) + "\n" for p in preds[:-1]), encoding="utf-8")
    stale = out / "traces" / f"{last}.jsonl"
    stale.write_text(stale.read_text() + json.dumps({"step": 99, "model": "stale"}) + "\n")
    proc = run_pipeline(out, "values", extra=["--resume"])
    assert proc.returncode == 0, proc.stderr[-2000:]
    preds2 = read_jsonl(out / "predictions.jsonl")
    assert sorted(p["id"] for p in preds2) == sorted(IDS) and len(preds2) == len(IDS)  # no duplicates
    trace = read_jsonl(stale)
    assert all(r.get("model") != "stale" for r in trace)  # stale trace was removed before re-running
    assert [r["step"] for r in trace] == list(range(1, len(trace) + 1))


def test_trace_truncates_long_fields_with_note(tmp_path):
    sys.path.insert(0, str(AGENT))
    import harness
    (tmp_path / "traces").mkdir()
    (tmp_path / "outputs").mkdir()
    run = harness.TaskRun({"id": "t1", "init_xlsx": "x"}, tmp_path)
    run.trace(model="m", prompt="x" * 30000, response="ok")
    rec = read_jsonl(tmp_path / "traces" / "t1.jsonl")[0]
    assert len(rec["prompt"]) < 30000 and "truncated, 30000 chars total" in rec["prompt"]
    assert rec["response"] == "ok"


def test_adaptive_effort_by_graded_size(tasks):
    sys.path.insert(0, str(AGENT))
    import harness
    cfg = harness.SolveConfig(reasoning="adaptive")
    assert harness.effort_for(tasks["12307"], cfg) == "low"      # 2 cells
    assert harness.effort_for(tasks["17-35"], cfg) == "medium"   # 1450 cells
    assert harness.effort_for(tasks["12307"], harness.SolveConfig(reasoning="low")) is None
