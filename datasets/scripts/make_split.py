"""Split the verified 400 into train + test.

Two-way split, seeded so the whole team shares the identical split:
  - train (fine-tune Qwen + generate teacher scripts)
  - test  (the sealed instrument — measure only, never train/tune on it)

Stratified by instruction_type (275 Cell / 125 Sheet -> same mix in each split) and
base-id-blocked (variants like 82-1 / 82-2 stay in the same split, no leakage).

    python3 datasets/scripts/make_split.py                 # default 0.30 test, seed 0
    python3 datasets/scripts/make_split.py --test 0.25 --seed 0
"""

import argparse
import collections
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
D400 = ROOT / "research/data/spreadsheetbench_verified_400/dataset.json"
SPLITS = ROOT / "datasets/splits"


def base_id(task_id: str) -> str:
    return task_id.split("-")[0]


def make_split(test_frac: float, seed: int) -> dict[str, list[str]]:
    tasks = json.loads(D400.read_text())
    rng = random.Random(seed)
    out = {"train": [], "test": []}

    by_type = collections.defaultdict(list)
    for t in tasks:
        by_type[t["instruction_type"]].append(str(t["id"]))
    for _type, ids in sorted(by_type.items()):
        groups = collections.defaultdict(list)
        for i in ids:
            groups[base_id(i)].append(i)
        glist = list(groups.values())
        rng.shuffle(glist)
        target_test, n_test = test_frac * len(ids), 0
        for g in glist:  # fill test to the target, keeping base-id groups intact
            if n_test < target_test:
                out["test"].extend(g)
                n_test += len(g)
            else:
                out["train"].extend(g)
    return {k: sorted(v) for k, v in out.items()}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--test", type=float, default=0.30, help="fraction of the 400 held out as test")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    split = make_split(args.test, args.seed)
    SPLITS.mkdir(parents=True, exist_ok=True)
    for name, ids in split.items():
        (SPLITS / f"{name}.txt").write_text("\n".join(ids) + "\n")

    types = {str(t["id"]): t["instruction_type"] for t in json.loads(D400.read_text())}
    print(f"seed={args.seed} test_frac={args.test}")
    for name, ids in split.items():
        by = collections.Counter(types[i] for i in ids)
        print(f"  {name:5} {len(ids):3}  Cell={by['Cell-Level Manipulation']:3}  Sheet={by['Sheet-Level Manipulation']:3}")
    print("wrote datasets/splits/train.txt and test.txt")


if __name__ == "__main__":
    main()
