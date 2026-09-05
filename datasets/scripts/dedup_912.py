"""Dedup the full SpreadsheetBench 912 against the shipped verified 400.

The verified 400 (research/data/spreadsheetbench_verified_400) is a strict subset of
the full 912 (datasets/raw/all_data_912_v0.1). This script confirms that and writes the
"extra" pool = 912 minus 400 as an id manifest. We keep the raw download untouched and
express the dedup as a list, so it stays reproducible and re-download can't reintroduce
duplicates.

Note the 912 uses a different on-disk layout from the verified 400:
    <id>/N_<id>_input.xlsx  and  N_<id>_answer.xlsx     (N = 1,2,3 test-case instances)
vs the verified 400's single  1_<id>_init.xlsx / 1_<id>_golden.xlsx.

    python3 datasets/scripts/dedup_912.py
"""

import collections
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
D400 = ROOT / "research/data/spreadsheetbench_verified_400/dataset.json"
D912 = ROOT / "datasets/raw/all_data_912_v0.1/dataset.json"
OUT = ROOT / "datasets/splits/extra_912_ids.txt"


def main() -> None:
    ids400 = {str(t["id"]) for t in json.loads(D400.read_text())}
    d912 = json.loads(D912.read_text())
    ids912 = {str(t["id"]) for t in d912}

    missing = ids400 - ids912
    if missing:
        raise SystemExit(f"expected 400 to be a subset of 912, but {len(missing)} are missing: {sorted(missing)[:5]}")

    extra = sorted(ids912 - ids400)
    by_type = collections.Counter(t["instruction_type"] for t in d912 if str(t["id"]) in set(extra))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(extra) + "\n")

    print(f"400 (verified, shipped): {len(ids400)}")
    print(f"912 (full download):     {len(ids912)}")
    print(f"extra = 912 - 400:       {len(extra)}  {dict(by_type)}")
    print(f"wrote manifest: {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
