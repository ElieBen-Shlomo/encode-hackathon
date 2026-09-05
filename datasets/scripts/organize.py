"""Materialize the train/test split into browsable folders.

Reads datasets/splits/{train,test}.txt and creates:
    datasets/400/train/<id>/  ->  research/data/.../spreadsheet/<id>/
    datasets/400/test/<id>/   ->  (symlinks, so no files are duplicated)

so a teammate can see at a glance what's train vs test. The manifests are the source of
truth; these folders are generated (gitignored). Re-run any time; it rebuilds cleanly.

    python3 datasets/scripts/organize.py
"""

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "research/data/spreadsheetbench_verified_400/spreadsheet"
SPLITS = ROOT / "datasets/splits"
OUT = ROOT / "datasets/400"


def main() -> None:
    if not SRC.exists():
        sys.exit(f"missing {SRC.relative_to(ROOT)} — run `uv run research/data/download.py` first")

    for split in ("train", "test"):
        manifest = SPLITS / f"{split}.txt"
        if not manifest.exists():
            sys.exit(f"missing {manifest.relative_to(ROOT)} — run make_split.py first")
        ids = manifest.read_text().split()

        dest = OUT / split
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        made = 0
        for task_id in ids:
            target = SRC / task_id
            if not target.exists():
                print(f"  warn: no folder for {task_id}", file=sys.stderr)
                continue
            link = dest / task_id
            link.symlink_to(os.path.relpath(target, link.parent))
            made += 1
        print(f"{split:5}: {made} tasks -> {dest.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
