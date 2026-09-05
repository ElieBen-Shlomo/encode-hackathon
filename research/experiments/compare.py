"""Paired bootstrap comparison of two runs on their shared task ids.

    uv run experiments/compare.py private/runs/<champion> private/runs/<candidate> [--n 10000]

Prints pass rates with 95% CIs, the paired difference with its CI, P(candidate > champion), and the
ids that flipped. With n=100 an unpaired gap under 8-10 points is noise; pairing roughly halves it.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def passes(run_dir: Path) -> dict[str, bool]:
    res = json.loads((Path(run_dir) / "results.json").read_text(encoding="utf-8"))
    return {i["id"]: bool(i.get("pass", False)) for i in res["items"]}


def bootstrap(a: list[int], b: list[int], n: int, seed: int) -> dict:
    rng = random.Random(seed)
    k = len(a)
    diffs, ra, rb = [], [], []
    for _ in range(n):
        idx = [rng.randrange(k) for _ in range(k)]
        sa = sum(a[i] for i in idx) / k
        sb = sum(b[i] for i in idx) / k
        ra.append(sa)
        rb.append(sb)
        diffs.append(sb - sa)
    diffs.sort(); ra.sort(); rb.sort()
    lo, hi = int(0.025 * n), int(0.975 * n) - 1
    return {
        "a_rate": sum(a) / k, "a_ci": (ra[lo], ra[hi]),
        "b_rate": sum(b) / k, "b_ci": (rb[lo], rb[hi]),
        "diff": (sum(b) - sum(a)) / k, "diff_ci": (diffs[lo], diffs[hi]),
        "p_b_gt_a": sum(d > 0 for d in diffs) / n,
        "p_b_ge_a": sum(d >= 0 for d in diffs) / n,
        "n_tasks": k,
    }


def compare(run_a: Path, run_b: Path, n: int = 10_000, seed: int = 0) -> dict:
    pa, pb = passes(run_a), passes(run_b)
    ids = sorted(set(pa) & set(pb))
    a = [int(pa[i]) for i in ids]
    b = [int(pb[i]) for i in ids]
    out = bootstrap(a, b, n, seed)
    out["flipped_to_pass"] = [i for i in ids if pb[i] and not pa[i]]
    out["flipped_to_fail"] = [i for i in ids if pa[i] and not pb[i]]
    out["a"], out["b"] = str(run_a), str(run_b)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("run_a", help="champion run dir (has results.json)")
    p.add_argument("run_b", help="candidate run dir")
    p.add_argument("--n", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()
    r = compare(Path(args.run_a), Path(args.run_b), args.n, args.seed)
    pct = lambda x: f"{100 * x:5.1f}"
    print(f"shared tasks: {r['n_tasks']}")
    print(f"A {Path(args.run_a).name:<40} {pct(r['a_rate'])}%  CI [{pct(r['a_ci'][0])}, {pct(r['a_ci'][1])}]")
    print(f"B {Path(args.run_b).name:<40} {pct(r['b_rate'])}%  CI [{pct(r['b_ci'][0])}, {pct(r['b_ci'][1])}]")
    print(f"B - A = {pct(r['diff'])} points  CI [{pct(r['diff_ci'][0])}, {pct(r['diff_ci'][1])}]   "
          f"P(B > A) = {r['p_b_gt_a']:.3f}   P(B >= A) = {r['p_b_ge_a']:.3f}")
    print(f"flipped to pass ({len(r['flipped_to_pass'])}): {', '.join(r['flipped_to_pass'])}")
    print(f"flipped to fail ({len(r['flipped_to_fail'])}): {', '.join(r['flipped_to_fail'])}")


if __name__ == "__main__":
    main()
