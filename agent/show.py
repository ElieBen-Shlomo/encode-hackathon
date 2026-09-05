"""Inspect a task in the terminal: instruction, init workbook digest, and what the
golden expects in the graded cells.

    uv run ../agent/show.py 51-12            # from research/
    uv run ../agent/show.py 51-12 --golden   # also digest the golden workbook
"""

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent, HERE.parent / "research"):
    if (candidate / "sb.py").exists():
        sys.path.insert(0, str(candidate))
        break

from digest import digest
from sb import DEFAULT_DATASET, load_answer_values, load_dataset


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("task_id")
    p.add_argument("--dataset-dir", default=str(DEFAULT_DATASET))
    p.add_argument("--golden", action="store_true", help="also show the golden workbook digest")
    args = p.parse_args()

    task = next((t for t in load_dataset(args.dataset_dir) if t["id"] == args.task_id), None)
    if task is None:
        sys.exit(f"no task {args.task_id!r}")

    print(f"=== {task['id']}  [{task['instruction_type']}] ===")
    print(f"answer: sheet={task.get('answer_sheet') or '(active)'}  cells={task['answer_position']}\n")
    print("--- instruction ---")
    print(task["instruction"], "\n")
    print("--- init workbook ---")
    print(digest(task["init_xlsx"], task), "\n")

    if task["golden_xlsx"]:
        gold = load_answer_values(task["golden_xlsx"], task)
        nonempty = {k: v for k, v in gold.items() if v is not None}
        print(f"--- golden answer cells ({len(gold)} graded, {len(nonempty)} non-empty) ---")
        for (sheet, coord), v in list(nonempty.items())[:40]:
            print(f"  {sheet}!{coord} = {v!r}")
        if len(nonempty) > 40:
            print(f"  ... {len(nonempty) - 40} more")
        if args.golden:
            print("\n--- golden workbook ---")
            print(digest(task["golden_xlsx"], task))


if __name__ == "__main__":
    main()
