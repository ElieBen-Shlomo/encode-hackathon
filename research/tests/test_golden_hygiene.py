"""Golden files may be opened by the evaluator and nothing else. Two guards:
a static scan of the pipeline code, and a runtime trap during a mock pipeline run."""

import asyncio
import re
import sys
from pathlib import Path

import openpyxl
import pytest

from conftest import AGENT, RESEARCH

ALLOWED_TO_MENTION_GOLDEN = {
    RESEARCH / "evaluate.py",           # the scorer
    RESEARCH / "sb.py",                 # load_dataset records the path; scorer helpers
    AGENT / "show.py",                  # pre-existing dev inspector, excluded from the Docker image
    RESEARCH / "experiments" / "teacher_prepare.py",   # only to pop the field before anything sees it
    RESEARCH / "tests" / "test_golden_hygiene.py",
    RESEARCH / "tests" / "test_scorer.py",
    RESEARCH / "tests" / "conftest.py",                 # pops the field so no test can see a golden path
}
SCAN_DIRS = [AGENT, RESEARCH / "eval", RESEARCH / "experiments", RESEARCH / "tests"]
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
            if f in ALLOWED_TO_MENTION_GOLDEN or ".venv" in f.parts or "__pycache__" in f.parts:
                continue
            for i, line in enumerate(_code_lines(f), 1):
                if CODE_PATTERN.search(line):
                    offenders.append(f"{f.relative_to(RESEARCH.parent)}:{i}: {line.strip()[:100]}")
    assert not offenders, "\n".join(offenders)


def test_show_py_is_excluded_from_the_docker_image():
    dockerignore = (RESEARCH.parent / ".dockerignore").read_text()
    assert "agent/show.py" in dockerignore


def test_pipeline_run_never_opens_a_golden_path(tasks, tmp_path, monkeypatch):
    """Trap every workbook open and file copy during a mock solve of two tasks."""
    sys.path.insert(0, str(AGENT))
    import shutil
    import harness
    import models

    opened = []
    real_load = openpyxl.load_workbook
    real_copy = shutil.copy

    def spy_load(path, *a, **kw):
        opened.append(str(path)); return real_load(path, *a, **kw)

    def spy_copy(src, dst, *a, **kw):
        opened.append(str(src)); return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(openpyxl, "load_workbook", spy_load)
    monkeypatch.setattr(shutil, "copy", spy_copy)
    (tmp_path / "outputs").mkdir(); (tmp_path / "traces").mkdir()
    model = models.MockModel()
    for tid in ("12307", "17-35"):
        for mode in ("values", "agent"):
            cfg = harness.SolveConfig(mode=mode, digest="grid", reasoning="low")
            status = asyncio.run(harness.solve_task(model, tasks[tid], tmp_path, cfg))
            assert status == "ok"
    assert opened, "the spy saw no file access; the test is not exercising the pipeline"
    bad = [p for p in opened if "golden" in p.lower()]
    assert not bad, bad
