"""Model-written code runs in a subprocess and must not be able to damage the inputs."""

import hashlib
import sys

from conftest import AGENT

sys.path.insert(0, str(AGENT))
import sandbox  # noqa: E402


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def test_script_cannot_modify_the_input_workbook(tasks, init_copy, tmp_path):
    src = init_copy(tasks["12307"])
    before = _sha(src)
    out = tmp_path / "out.xlsx"
    code = """
import sys, openpyxl
wb = openpyxl.load_workbook(sys.argv[1]); wb.active["A1"] = "VANDALISED"; wb.save(sys.argv[1])   # tries to write the input
wb2 = openpyxl.load_workbook(sys.argv[2]); wb2.active["I12"] = 7; wb2.save(sys.argv[2])
"""
    ok, log = sandbox.run_code(code, str(src), str(out), timeout=60)
    assert ok, log
    assert _sha(src) == before, "the dataset init workbook was modified by model code"
    import openpyxl
    assert openpyxl.load_workbook(out).active["I12"].value == 7


def test_timeout_is_enforced(tasks, init_copy, tmp_path):
    src = init_copy(tasks["12307"])
    ok, log = sandbox.run_code("import time; time.sleep(30)", str(src), str(tmp_path / "o.xlsx"), timeout=2)
    assert ok is False and "TIMEOUT" in log


def test_deleting_output_is_a_failure(tasks, init_copy, tmp_path):
    src = init_copy(tasks["12307"])
    out = tmp_path / "o.xlsx"
    ok, log = sandbox.run_code("import os, sys; os.remove(sys.argv[2])", str(src), str(out), timeout=30)
    assert ok is False and "deleted OUT_XLSX" in log


def test_relative_paths_are_resolved(tasks, init_copy, tmp_path, monkeypatch):
    src = init_copy(tasks["12307"])
    monkeypatch.chdir(tmp_path)
    ok, log = sandbox.run_code("import sys, openpyxl; openpyxl.load_workbook(sys.argv[2]).save(sys.argv[2])",
                               str(src.name), "rel_out.xlsx", timeout=60)
    assert ok, log
    assert (tmp_path / "rel_out.xlsx").exists()


def test_api_keys_are_stripped_from_the_environment(tasks, init_copy, tmp_path, monkeypatch):
    monkeypatch.setenv("TINKER_API_KEY", "secret-should-not-leak")
    src = init_copy(tasks["12307"])
    ok, log = sandbox.run_code("import os; print('KEY=' + os.environ.get('TINKER_API_KEY', 'absent'))",
                               str(src), str(tmp_path / "o.xlsx"), timeout=30)
    assert ok and "KEY=absent" in log
