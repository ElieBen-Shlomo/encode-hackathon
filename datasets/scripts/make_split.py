"""Split the verified 400 into train + a held-out INSTRUMENT.

The evaluate.py --all number is only a required artifact — the judges rank on their own
hidden data — so we don't keep the 400 pristine for reporting. We hold a slice out for
ONE reason: an honest internal signal to tell whether a fine-tune/harness change actually
helped (and to give AutoResearch a target). That signal must be trustworthy, so it comes
from the human-verified 400, not the unvetted extras.

  - train_400    : fine-tune on these (verified, trustworthy) + gated-512 + synthetic
  - heldout_400  : our instrument — touch rarely, use to pick the best model/harness
  - heldout_base_ids.txt : base ids in held-out, so gated-512 / synthetic can drop any
                           sibling that shares a base id (no leakage into the instrument)

Base-id-blocked (82-1, 82-2 stay together) and stratified by instruction_type, seeded so
the whole team shares the identical held-out.

    python3 datasets/scripts/make_split.py                 # default 0.70/0.30, seed 0
    python3 datasets/scripts/make_split.py --heldout 0.25 --seed 0
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


def make_split(heldout: float, seed: int) -> dict[str, list[str]]:
    tasks = json.loads(D400.read_text())
    rng = random.Random(seed)
    out = {"train_400": [], "heldout_400": []}

    by_type = collections.defaultdict(list)
    for t in tasks:
        by_type[t["instruction_type"]].append(str(t["id"]))
    for _type, ids in sorted(by_type.items()):
        groups = collections.defaultdict(list)
        for i in ids:
            groups[base_id(i)].append(i)
        glist = list(groups.values())
        rng.shuffle(glist)
        target_heldout = heldout * len(ids)
        n_heldout = 0
        for g in glist:  # fill held-out to target, keeping base-id groups intact
            if n_heldout < target_heldout:
                out["heldout_400"].extend(g)
                n_heldout += len(g)
            else:
                out["train_400"].extend(g)
    return {k: sorted(v) for k, v in out.items()}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--heldout", type=float, default=0.30)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    split = make_split(args.heldout, args.seed)
    SPLITS.mkdir(parents=True, exist_ok=True)
    for name, ids in split.items():
        (SPLITS / f"{name}.txt").write_text("\n".join(ids) + "\n")
    heldout_bases = sorted({base_id(i) for i in split["heldout_400"]})
    (SPLITS / "heldout_base_ids.txt").write_text("\n".join(heldout_bases) + "\n")

    types = {str(t["id"]): t["instruction_type"] for t in json.loads(D400.read_text())}
    print(f"seed={args.seed} heldout_frac={args.heldout}")
    for name, ids in split.items():
        by = collections.Counter(types[i] for i in ids)
        print(f"  {name:12} {len(ids):3}  Cell={by['Cell-Level Manipulation']:3}  Sheet={by['Sheet-Level Manipulation']:3}")
    print("wrote train_400.txt, heldout_400.txt, heldout_base_ids.txt to datasets/splits/")
    print("next: gated-512 + synthetic feed train_400; AutoResearch dev drawn from gated extras")


if __name__ == "__main__":
    main()
