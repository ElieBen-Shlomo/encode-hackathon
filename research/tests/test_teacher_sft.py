"""The teacher SFT builder must render trajectories with the workbook view the harness serves at inference
(the shipped SolveConfig: grid). Training on one view and answering on another is the mismatch the
representation study warned about. Reference workbooks are never opened here (needs_recalc is stubbed)."""

import json
import sys

from conftest import RESEARCH

sys.path.insert(0, str(RESEARCH / "teacher"))

import build_sft  # noqa: E402
import harness  # noqa: E402


def test_trajectory_uses_the_inference_view_and_protocol(tasks, monkeypatch):
    monkeypatch.setattr(build_sft, "needs_recalc", lambda out, task: False)
    task = tasks["12307"]
    script = (RESEARCH / "teacher" / "scripts" / "12307.py").read_text(encoding="utf-8")
    messages = build_sft.trajectory(task, script)
    assert messages is not None, "the verified teacher script no longer replays"
    assert messages[0] == {"role": "system", "content": harness.AGENT_SYSTEM}
    assert "Initial workbook digest" in messages[1]["content"]
    inference_view = harness.render_workbook(task["init_xlsx"], task, harness.SolveConfig()).text
    assert inference_view in messages[1]["content"]                      # grid, exactly as solve_task renders it
    tools = [json.loads(m["content"])["tool"] for m in messages if m["role"] == "assistant"]
    assert tools == ["run_python", "finish"]
    assert "## Updated workbook digest" in messages[3]["content"] and "=" in messages[3]["content"]
