"""Prepare isolated per-task folders for a blind "teacher" solve of the 400 by a strong model.

    uv run experiments/teacher_prepare.py --out private/fable400 [--ids-file eval/splits/dev100.txt]

For every task writes private/<out>/tasks/<id>.json (instruction, graded range, paths) and copies the init
workbook to private/<out>/work/<id>/init.xlsx. The solver only ever receives that folder: it never sees the
dataset directory, so it cannot open a golden file. Nothing here reads golden files either.

Afterwards: outputs go to private/<out>/outputs/<id>.xlsx, reasoning logs to private/<out>/traces/<id>.md.
`teacher_collect.py` builds predictions.jsonl (copying the init for any task without an output) and only then
is evaluate.py run.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

RESEARCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RESEARCH))

import openpyxl  # noqa: E402

from sb import DEFAULT_DATASET, answer_cells, load_dataset  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(RESEARCH / "private" / "fable400"))
    p.add_argument("--dataset-dir", default=str(DEFAULT_DATASET))
    p.add_argument("--ids-file", default=None)
    args = p.parse_args()

    out = Path(args.out)
    for sub in ("tasks", "work", "outputs", "traces"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    ids = None
    if args.ids_file:
        ids = {l.strip() for l in Path(args.ids_file).read_text().splitlines() if l.strip()}

    n = 0
    for t in load_dataset(args.dataset_dir):
        if ids is not None and t["id"] not in ids:
            continue
        t.pop("golden_xlsx", None)  # never travels further than this line
        work = out / "work" / t["id"]
        work.mkdir(parents=True, exist_ok=True)
        init_copy = work / "init.xlsx"
        if not init_copy.exists():
            shutil.copy(t["init_xlsx"], init_copy)
        try:
            wb = openpyxl.load_workbook(init_copy)
            n_cells = len(answer_cells(t, wb))
            sheets = wb.sheetnames
            active = wb.active.title
        except Exception as e:
            n_cells, sheets, active = None, [], None
        spec = {
            "id": t["id"],
            "instruction_type": t["instruction_type"],
            "instruction": t["instruction"],
            "answer_position": t["answer_position"],
            "answer_sheet": t.get("answer_sheet"),
            "graded_sheet_hint": t.get("answer_sheet") or f"the active sheet ({active!r})",
            "n_graded_cells": n_cells,
            "sheets": sheets,
            "active_sheet": active,
            "init_xlsx": str(init_copy),
            "output_xlsx": str(out / "outputs" / f"{t['id']}.xlsx"),
            "trace_md": str(out / "traces" / f"{t['id']}.md"),
            "work_dir": str(work),
        }
        (out / "tasks" / f"{t['id']}.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
    print(f"{n} tasks prepared under {out}")


if __name__ == "__main__":
    main()
