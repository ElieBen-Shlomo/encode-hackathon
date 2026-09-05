"""3-way split of the verified 400 into train / dev / held-out.

- stratified by instruction_type (275 Cell-Level / 125 Sheet-Level -> same mix in each split)
- blocked by base-id: tasks sharing a base id (e.g. 82-1, 82-2) stay in the SAME split,
  so a near-variant can't sit in train while its sibling is in held-out
- seeded, so the whole team gets the identical split (shared held-out = no leakage)

Also writes eval_base_ids.txt = base ids used in dev+held-out, so the extra-912 training
pool can drop any sibling that shares a base id with an eval task.

    python3 datasets/scripts/make_split.py                 # defaults: 0.50/0.25/0.25, seed 0
    python3 datasets/scripts/make_split.py --dev 0.2 --heldout 0.2 --seed 0
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


def make_split(train: float, dev: float, heldout: float, seed: int) -> dict[str, list[str]]:
    tasks = json.loads(D400.read_text())
    fracs = {"train": train, "dev": dev, "heldout": heldout}
    rng = random.Random(seed)
    out = {s: [] for s in fracs}

    # stratify by type; within a type, keep base-id groups intact
    by_type = collections.defaultdict(list)
    for t in tasks:
        by_type[t["instruction_type"]].append(str(t["id"]))
    for _type, ids in sorted(by_type.items()):
        groups = collections.defaultdict(list)
        for i in ids:
            groups[base_id(i)].append(i)
        glist = list(groups.values())
        rng.shuffle(glist)
        total = len(ids)
        target = {s: fracs[s] * total for s in fracs}
        assigned = {s: 0 for s in fracs}
        for g in glist:  # give each group to the split with the largest remaining deficit
            s = max(fracs, key=lambda s: target[s] - assigned[s])
            out[s].extend(g)
            assigned[s] += len(g)
    return {s: sorted(v) for s, v in out.items()}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--train", type=float, default=0.50)
    p.add_argument("--dev", type=float, default=0.25)
    p.add_argument("--heldout", type=float, default=0.25)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    split = make_split(args.train, args.dev, args.heldout, args.seed)
    SPLITS.mkdir(parents=True, exist_ok=True)
    for name, ids in split.items():
        (SPLITS / f"{name}.txt").write_text("\n".join(ids) + "\n")

    eval_bases = sorted({base_id(i) for i in split["dev"] + split["heldout"]})
    (SPLITS / "eval_base_ids.txt").write_text("\n".join(eval_bases) + "\n")

    types = {t["id"]: t["instruction_type"] for t in map(lambda x: {"id": str(x["id"]), **x}, json.loads(D400.read_text()))}
    print(f"seed={args.seed} fractions train/dev/heldout = {args.train}/{args.dev}/{args.heldout}")
    for name, ids in split.items():
        by = collections.Counter(types[i] for i in ids)
        print(f"  {name:8} {len(ids):3}  Cell={by['Cell-Level Manipulation']:3}  Sheet={by['Sheet-Level Manipulation']:3}")
    print(f"wrote {', '.join(f'{s}.txt' for s in split)} + eval_base_ids.txt to datasets/splits/")


if __name__ == "__main__":
    main()
