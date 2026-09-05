"""The grader's semantics are the contract everything else is built on. Pin them."""

import datetime
import shutil

import pytest

import evaluate
import sb

ORACLE_IDS = ["12307", "15380", "17-35", "41-47", "283-32", "516-46", "560-12", "49300", "12864", "13-1"]


@pytest.mark.parametrize("gold,pred,expected", [
    (42, "42", True),            # numeric strings coerce
    (42, 42.004, True),          # 2-dp rounding
    (42, 42.006, False),
    (True, 1, False),            # bool is not a number
    (True, True, True),
    ("", None, True),            # empty string equals empty cell
    (0, None, False),            # zero is not empty
    ("abc", "ABC", False),       # text is exact
    (datetime.datetime(2024, 1, 31), 45322.0, True),          # date equals its whole-day Excel serial
    (datetime.datetime(2024, 1, 31, 11, 0), 45322.0, True),   # time of day rounds to the NEAREST day...
    (datetime.datetime(2024, 1, 31, 15, 0), 45323.0, True),   # ...so an afternoon timestamp grades as the next day
    (datetime.datetime(2024, 1, 31), "2024-01-31", False),    # date as text never matches
    (datetime.time(13, 5), "13:05", True),
])
def test_values_equal_contract(gold, pred, expected):
    assert sb.values_equal(gold, pred) is expected


def test_oracle_passes_on_subset(dataset_dir):
    """Golden scored against itself must pass. Only evaluate.py touches goldens, here included."""
    tasks = [t for t in sb.load_dataset(dataset_dir) if t["id"] in set(ORACLE_IDS)]
    assert len(tasks) == len(ORACLE_IDS)
    summary, items = evaluate.score([], tasks, recalc=False, oracle=True)
    assert summary["pass_rate"] == 1.0, [i for i in items if not i.get("pass")]


def test_init_as_prediction_fails(dataset_dir, tmp_path):
    """A prediction equal to the untouched init must fail (the graded cells are empty in this init)."""
    task = next(t for t in sb.load_dataset(dataset_dir) if t["id"] == "12307")
    (tmp_path / "outputs").mkdir()
    shutil.copy(task["init_xlsx"], tmp_path / "outputs" / "12307.xlsx")
    (tmp_path / "predictions.jsonl").write_text("")
    preds = [{"id": "12307", "output": "outputs/12307.xlsx", "status": "ok"}]
    summary, items = evaluate.score(preds, [task], recalc=False, predictions_path=tmp_path / "predictions.jsonl")
    assert items[0]["status"] == "graded" and items[0]["pass"] is False and summary["pass_rate"] == 0.0


def test_missing_prediction_counts_as_fail(dataset_dir):
    task = next(t for t in sb.load_dataset(dataset_dir) if t["id"] == "12307")
    summary, items = evaluate.score([], [task], recalc=False)
    assert items[0]["status"] == "missing" and summary["missing"] == 1 and summary["pass_rate"] == 0.0


def test_summary_splits_by_instruction_type():
    items = [
        {"id": "a", "type": "Cell-Level Manipulation", "status": "graded", "pass": True, "cells": 2, "correct": 2, "mismatches": []},
        {"id": "b", "type": "Sheet-Level Manipulation", "status": "graded", "pass": False, "cells": 4, "correct": 1, "mismatches": [{}]},
        {"id": "c", "type": "Cell-Level Manipulation", "status": "missing", "cells": 3, "correct": 0},
    ]
    s = evaluate.summarise(items)
    assert s["items"] == 3 and s["graded"] == 2 and s["missing"] == 1
    assert s["pass_rate"] == round(1 / 3, 4) and s["cell_accuracy"] == round(3 / 9, 4)
    assert s["pass_rate_cell_level"] == 0.5 and s["pass_rate_sheet_level"] == 0.0


@pytest.mark.libreoffice
def test_recalculate_roundtrip(tasks, init_copy, tmp_path):
    src = init_copy(tasks["17-35"])  # has an array formula and cross-cell formulas
    out = sb.recalculate(src, tmp_path / "recalc")
    assert out.exists()
    import openpyxl
    wb = openpyxl.load_workbook(out, data_only=True)
    assert wb["FILTER 5b"]["G2"].value not in (None, "")  # the SORT(UNIQUE()) result now has a cached value
