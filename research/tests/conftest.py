"""Shared fixtures. Fast tests only: no model calls, no credits, and nothing here opens a golden file.

Run:  cd research && uv run --with pytest pytest tests -q
Tests that need LibreOffice are marked `libreoffice` and skip when soffice is not installed.
"""

import shutil
import sys
from pathlib import Path

import pytest

RESEARCH = Path(__file__).resolve().parent.parent
AGENT = RESEARCH.parent / "agent"
for p in (RESEARCH, AGENT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import sb  # noqa: E402

DATASET = RESEARCH / "data" / "spreadsheetbench_verified_400"

# Deliberately awkward tasks: defined names, array formula + big range, 4 ranges + 4 sheets, whole-column
# with quoted sheets, Arabic and Chinese sheet names, leading non-breaking space, tiny sheet, formulas w/o cache.
EDGE_IDS = ["15380", "17-35", "41-47", "283-32", "516-46", "560-12", "49300", "12307", "24-23"]


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
    def _copy(task: dict) -> Path:
        dst = tmp_path / f"{task['id']}_init.xlsx"
        shutil.copy(task["init_xlsx"], dst)
        return dst
    return _copy
