"""Shared fixtures for the fast test suite: no model calls, no credits, no golden files opened.

Run from research/:   uv run --with pytest pytest tests -q
Tests marked `libreoffice` skip when soffice is not installed. Tests marked xfail(strict=True)
document behaviour the current code does not have yet; they turn red the day it is fixed, so
remove the marker then.
"""

import shutil
import sys
from pathlib import Path

import pytest

RESEARCH = Path(__file__).resolve().parent.parent
AGENT = RESEARCH.parent / "agent"
BASELINE = RESEARCH / "baseline"
for p in (RESEARCH, AGENT, BASELINE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import sb  # noqa: E402

DATASET = RESEARCH / "data" / "spreadsheetbench_verified_400"

# Deliberately awkward tasks: defined names, array formula + big range, 4 ranges + 4 sheets, whole-column
# with quoted sheets, Arabic and Chinese sheet names, leading non-breaking space, tiny sheet, formulas w/o cache.
EDGE_IDS = ["15380", "17-35", "41-47", "283-32", "516-46", "560-12", "49300", "12307", "24-23"]
# Small tasks whose graded cells are empty in the init and hold no formulas: safe for mock agent runs.
MOCK_IDS = ["12307", "560-12", "15380"]


def pytest_configure(config):
    config.addinivalue_line("markers", "libreoffice: needs a soffice binary (LibreOffice) on this machine")


def pytest_collection_modifyitems(config, items):
    if sb.soffice_path():
        return
    skip = pytest.mark.skip(reason="LibreOffice (soffice) not installed")
    for item in items:
        if "libreoffice" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def dataset_dir() -> Path:
    if not (DATASET / "dataset.json").exists():
        pytest.skip("dataset not downloaded: uv run data/download.py")
    return DATASET


@pytest.fixture(scope="session")
def tasks(dataset_dir) -> dict:
    """Tasks by id with the golden path removed, so no test can reach a golden file by accident."""
    out = {}
    for t in sb.load_dataset(dataset_dir):
        t.pop("golden_xlsx", None)
        out[t["id"]] = t
    return out


@pytest.fixture
def init_copy(tmp_path):
    """Copy a task's init workbook into tmp so tests never write next to the dataset."""
    def _copy(task: dict, name: str | None = None) -> Path:
        dst = tmp_path / (name or f"{task['id']}_init.xlsx")
        shutil.copy(task["init_xlsx"], dst)
        return dst
    return _copy


@pytest.fixture
def out_dir(tmp_path) -> Path:
    (tmp_path / "outputs").mkdir()
    (tmp_path / "traces").mkdir()
    return tmp_path
