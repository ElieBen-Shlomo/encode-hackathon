"""Golden files may be opened by the evaluator and nothing else. Two guards: a static scan of the
pipeline code, and a runtime trap on every workbook open and file copy during a mock agent run."""

import asyncio
import re
import shutil
from pathlib import Path

import openpyxl

import harness
import models
from conftest import AGENT, RESEARCH

ALLOWED_TO_MENTION_GOLDEN = {
    RESEARCH / "evaluate.py",                 # the scorer
    RESEARCH / "sb.py",                       # load_dataset records the path; scorer helpers
    AGENT / "show.py",                        # dev inspector; not part of the pipeline
    RESEARCH / "tests" / "conftest.py",       # pops the field so no test can see a golden path
    RESEARCH / "tests" / "test_golden_hygiene.py",
    RESEARCH / "tests" / "test_scorer.py",
    RESEARCH / "experiments" / "teacher_prepare.py",   # only to pop the field before anything sees it
}
SCAN_DIRS = [AGENT, RESEARCH / "baseline", RESEARCH / "eval", RESEARCH / "experiments", RESEARCH / "tests"]
CODE_PATTERN = re.compile(r"golden_xlsx|load_answer_values|\*golden\*|golden\.xlsx", re.I)


def _code_lines(path: Path):
    """Source lines with comments and docstrings stripped, so prose about the rule does not trip the scan."""
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', "", text)
    for line in text.splitlines():
        yield re.sub(r"#.*$", "", line)


def test_no_pipeline_code_touches_golden_files():
    offenders = []
    for d in SCAN_DIRS:
        for f in d.rglob("*.py"):
            if f in ALLOWED_TO_MENTION_GOLDEN or "__pycache__" in f.parts or ".venv" in f.parts:
                continue
            for i, line in enumerate(_code_lines(f), 1):
                if CODE_PATTERN.search(line):
                    offenders.append(f"{f.relative_to(RESEARCH.parent)}:{i}: {line.strip()[:100]}")
    assert not offenders, "\n".join(offenders)


def test_agent_prompt_forbids_golden_access():
    assert "Do not access golden workbooks" in harness.AGENT_SYSTEM


def test_mock_agent_run_never_opens_a_golden_path(tasks, out_dir, monkeypatch):
    opened = []
    real_load, real_copy = openpyxl.load_workbook, shutil.copy

    def spy_load(path, *a, **kw):
        opened.append(str(path)); return real_load(path, *a, **kw)

    def spy_copy(src, dst, *a, **kw):
        opened.append(str(src)); return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(openpyxl, "load_workbook", spy_load)
    monkeypatch.setattr(shutil, "copy", spy_copy)
    for tid in ("12307", "560-12"):
        assert asyncio.run(harness.solve_task(models.MockModel(), tasks[tid], out_dir, max_turns=6)) == "ok"
    assert opened, "the spy saw no file access; the test is not exercising the pipeline"
    bad = [p for p in opened if "golden" in p.lower()]
    assert not bad, bad


def test_show_py_is_excluded_from_the_docker_image():
    assert "agent/show.py" in (RESEARCH.parent / ".dockerignore").read_text()
