"""The numbers we report: failure buckets, paired comparison, probe grading."""

import datetime
import sys

import pytest

from conftest import RESEARCH

sys.path.insert(0, str(RESEARCH / "experiments"))
sys.path.insert(0, str(RESEARCH / "eval"))
import attribute  # noqa: E402
import compare  # noqa: E402
import probes  # noqa: E402

STATS_OK = {"n_calls": 1, "n_call_errors": 0, "length_hits": 0, "meta": {"answer_range_in_window": True}}


def graded(mismatches, correct=0, cells=1):
    return {"status": "graded", "pass": not mismatches, "mismatches": mismatches, "correct": correct, "cells": cells}


@pytest.mark.parametrize("item,stats,expected", [
    (graded([]), STATS_OK, "pass"),
    ({"status": "missing_output"}, STATS_OK, "infra"),
    (graded([{"expected": 5, "actual": 3}]), {**STATS_OK, "n_calls": 2, "n_call_errors": 2}, "infra"),
    (graded([{"expected": 5, "actual": 3}]), {**STATS_OK, "length_hits": 1}, "infra"),       # every call truncated
    (graded([{"expected": 45322.0, "actual": "2024-01-31"}]), STATS_OK, "format"),           # date as text
    (graded([{"expected": 42, "actual": "42"}]), STATS_OK, "format"),                        # would pass the grader anyway
    (graded([{"expected": 100.0, "actual": 100.004}]), STATS_OK, "format"),                  # rounding near-miss
    (graded([{"expected": 10000, "actual": 10040}]), STATS_OK, "reasoning"),                 # 0.4% off is a wrong value
    (graded([{"expected": 5, "actual": "#NAME?"}]), STATS_OK, "error_value"),
    (graded([{"expected": 5, "actual": None}, {"expected": 6, "actual": None}]), STATS_OK, "coverage"),
    (graded([{"expected": 5, "actual": 3}]), {**STATS_OK, "meta": {"answer_range_in_window": False}}, "truncation"),
    (graded([{"expected": 5, "actual": 3}]), STATS_OK, "reasoning"),
])
def test_buckets(item, stats, expected):
    assert attribute.bucket(item, stats) == expected


def test_format_mismatch_does_not_accept_large_relative_errors():
    assert attribute._is_format_mismatch(1_000_000, 1_004_000) is False
    assert attribute._is_format_mismatch(1.0, 1.01) is True
    assert attribute._is_format_mismatch("abc", None) is False


def test_bootstrap_identical_vectors_have_zero_difference():
    a = [1, 0, 1, 1, 0, 1, 0, 1, 1, 1]
    r = compare.bootstrap(a, list(a), n=2000, seed=0)
    assert r["diff"] == 0 and r["diff_ci"] == (0, 0) and r["p_b_gt_a"] == 0.0 and r["p_b_ge_a"] == 1.0
    assert r["a_rate"] == 0.7


def test_bootstrap_detects_a_clear_improvement():
    a = [0] * 50 + [1] * 50
    b = [0] * 30 + [1] * 70
    r = compare.bootstrap(a, b, n=4000, seed=1)
    assert 0.15 < r["diff"] < 0.25 and r["p_b_gt_a"] > 0.95 and r["diff_ci"][0] > 0


@pytest.mark.parametrize("qtype,expected,actual,ok", [
    ("cell_value", 42, "42", True),
    ("cell_value", 42, "42.00", True),
    ("cell_value", 42, "142", False),                   # numeric substring must not pass
    ("cell_value", 42, "the value is 42", False),
    ("cell_value", "Nizghi, Righat", "nizghi, righat", True),
    ("cell_value", "Nizghi, Righat", "Righat", False),
    ("cell_value", datetime.datetime(2024, 1, 31), "2024-01-31", True),
    ("header", "No of countries", "No of countries", True),
    ("sheet_names", ["Sheet1", "Data"], "Data, Sheet1", True),
    ("sheet_names", ["Sheet1", "Data"], ["Sheet1", "Data"], True),
    ("sheet_names", ["Sheet1", "Data"], '["Sheet1", "Data"]', True),
    ("sheet_names", ["Sheet1", "Data"], "Sheet1", False),
    ("date_columns", ["A", "H"], "A;H", True),
    ("nonempty_count", 311, "311", True),
    ("nonempty_count", 311, "310", False),
    ("formula_text", "=_xlfn.XLOOKUP(A1,B:B,C:C)", "XLOOKUP(A1, B:B, C:C)", True),
    ("is_formula", "formula", "Formula", True),
    ("last_row", 20, "20.0", True),
])
def test_probe_grader(qtype, expected, actual, ok):
    assert probes.grade(qtype, expected, actual) is ok


def test_probe_generator_answers_grade_as_correct(tasks):
    """Every generated question must accept its own expected answer, for a mixed set of workbooks."""
    import random
    from workbook import load_info
    for tid in ("12307", "15380", "17-35", "516-46", "49300"):
        info = load_info(tasks[tid]["init_xlsx"], recalc=False)
        for q in probes.make_questions(tasks[tid], info, random.Random(tid), 20):
            exp = q["expected"]
            ans = ", ".join(map(str, exp)) if isinstance(exp, list) else exp.strftime("%Y-%m-%d") if isinstance(exp, datetime.datetime) else str(exp)
            assert probes.grade(q["type"], exp, ans), (tid, q["type"], exp, ans)
