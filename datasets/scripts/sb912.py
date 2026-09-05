"""Loader for the full SpreadsheetBench 912 (datasets/raw/all_data_912_v0.1).

The 912 layout differs from the shipped verified 400: each task has up to 3 test-case
instances, named N_<id>_input.xlsx / N_<id>_answer.xlsx (not init/golden). Use this to
consume the 512 "extra" tasks (912 minus 400) as additional training data.

    from sb912 import load_912, load_extra_912
    for task in load_extra_912():        # only the 512 not in the verified 400
        for inp, ans in task["instances"]:
            ...  # inp/ans are absolute paths to *_input.xlsx / *_answer.xlsx
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIR912 = ROOT / "datasets/raw/all_data_912_v0.1"
EXTRA_IDS = ROOT / "datasets/splits/extra_912_ids.txt"

_INST = re.compile(r"^(\d+)_.*_(input|answer)\.xlsx$")


def _instances(folder: Path) -> list[tuple[str, str]]:
    """Return [(input_xlsx, answer_xlsx), ...] paired by instance number, sorted."""
    inputs, answers = {}, {}
    for f in folder.glob("*.xlsx"):
        m = _INST.match(f.name)
        if not m:
            continue
        (inputs if m.group(2) == "input" else answers)[m.group(1)] = str(f)
    return [(inputs[n], answers[n]) for n in sorted(inputs) if n in answers]


def load_912(dataset_dir: Path = DIR912) -> list[dict]:
    dataset_dir = Path(dataset_dir)
    tasks = json.loads((dataset_dir / "dataset.json").read_text())
    for t in tasks:
        t["id"] = str(t["id"])
        t["instances"] = _instances(dataset_dir / t["spreadsheet_path"])
    return tasks


def load_extra_912(dataset_dir: Path = DIR912) -> list[dict]:
    extra = set(EXTRA_IDS.read_text().split())
    return [t for t in load_912(dataset_dir) if t["id"] in extra]


if __name__ == "__main__":
    tasks = load_extra_912()
    n_inst = sum(len(t["instances"]) for t in tasks)
    print(f"extra tasks: {len(tasks)}  total instances (train examples): {n_inst}")
    print("example:", tasks[0]["id"], "->", len(tasks[0]["instances"]), "instances")
