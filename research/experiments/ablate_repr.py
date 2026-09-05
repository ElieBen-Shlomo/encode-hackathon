"""Representation-study runner: run configs on an id list, score, attribute, append the scoreboard.

    cd research
    uv run experiments/ablate_repr.py --model tinker \
        --config mode=values,digest=tsv,reasoning=xhigh \
        --config mode=values,digest=grid,reasoning=low \
        --config mode=agent,digest=schema,reasoning=low,budget=6000

    uv run experiments/ablate_repr.py --score-only --config mode=values,digest=tsv,reasoning=xhigh

Each config becomes private/runs/<name>/ with predictions.jsonl, outputs/, traces/, run.log,
results.json, attribution.json, summary.json. One row per config is appended to
experiments/scoreboard.md. Runs live under research/private/ (gitignored); the scoreboard is committed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
sys.path.insert(0, str(HERE))
from attribute import BUCKETS, attribute  # noqa: E402

DEFAULTS = {"mode": "values", "digest": "tsv", "reasoning": "xhigh", "budget": None, "max_tokens": None}
SCOREBOARD_HEADER = (
    "| run | when | n | pass % | cell acc % | cell-lvl % | sheet-lvl % | in tok p50/p90 | out tok mean | "
    "latency p50 s | s/task | calls/task | length hits | statuses | "
    + " | ".join(BUCKETS) + " |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|" + "---|" * len(BUCKETS) + "\n"
)


def parse_config(spec: str) -> dict:
    cfg = dict(DEFAULTS)
    for part in spec.split(","):
        if not part.strip():
            continue
        k, _, v = part.partition("=")
        k = k.strip().replace("-", "_")
        v = v.strip()
        if k not in cfg:
            raise SystemExit(f"unknown config key {k!r} in {spec!r}; keys: {sorted(cfg)}")
        cfg[k] = None if v in ("", "None", "none") else (int(v) if k in ("budget", "max_tokens") else v)
    return cfg


def run_name(cfg: dict, tag: str | None, ids_stem: str = "dev100", model: str = "tinker") -> str:
    """dev100 on the default Tinker model gets the short name; anything else is suffixed so runs never collide."""
    name = f"{cfg['mode']}-{cfg['digest']}-{cfg['reasoning']}"
    if cfg["budget"]:
        name += f"-b{cfg['budget']}"
    if cfg["max_tokens"]:
        name += f"-m{cfg['max_tokens']}"
    if ids_stem != "dev100":
        name += f"-{ids_stem}"
    if model != "tinker":
        name += "-" + re.sub(r"[^A-Za-z0-9.-]+", "_", model)
    if tag:
        name += f"-{tag}"
    return name


def read_ids(path: Path) -> list[str]:
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def run_pipeline(cfg: dict, out_dir: Path, args: argparse.Namespace, ids_file: Path) -> int:
    cmd = ["uv", "run", str(RESEARCH.parent / "agent" / "run.py"),
           "--dataset-dir", str(args.dataset_dir), "--out-dir", str(out_dir), "--ids", f"@{ids_file}",
           "--mode", cfg["mode"], "--model", args.model, "--digest", cfg["digest"], "--reasoning", cfg["reasoning"],
           "--concurrency", str(args.concurrency)]
    if cfg["budget"]:
        cmd += ["--budget", str(cfg["budget"])]
    if cfg["max_tokens"]:
        cmd += ["--max-tokens", str(cfg["max_tokens"])]
    if args.project_id:
        cmd += ["--project-id", args.project_id]
    if args.base_model:
        cmd += ["--base-model", args.base_model]
    if args.resume:
        cmd += ["--resume"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ablate_cmd.txt").write_text(" ".join(cmd) + "\n", encoding="utf-8")
    print(f"[{time.strftime('%H:%M:%S')}] run  {out_dir.name}", flush=True)
    if args.dry_run:
        print("   ", " ".join(cmd))
        return 0
    with (out_dir / "ablate_stdout.log").open("w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, cwd=str(RESEARCH), stdout=log, stderr=subprocess.STDOUT, text=True)
    return proc.returncode


def score(out_dir: Path, ids: list[str], args: argparse.Namespace) -> dict:
    cmd = ["uv", "run", "evaluate.py", "--predictions", str(out_dir / "predictions.jsonl"),
           "--dataset-dir", str(args.dataset_dir), "--ids", ",".join(ids), "--out", str(out_dir / "results.json"), "--quiet"]
    print(f"[{time.strftime('%H:%M:%S')}] score {out_dir.name}", flush=True)
    subprocess.run(cmd, cwd=str(RESEARCH), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return json.loads((out_dir / "results.json").read_text(encoding="utf-8"))["summary"]


def seconds_per_task(out_dir: Path) -> list[float]:
    secs = []
    log = out_dir / "run.log"
    if not log.exists():
        return secs
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.search(r"\s(\d+\.\d)s\s*$", line)
        if m:
            secs.append(float(m.group(1)))
    return secs


def pct(x) -> str:
    return "" if x is None else f"{100 * x:.1f}"


def p50p90(values: list) -> str:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return ""
    q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
    return f"{q(0.5)}/{q(0.9)}"


def summarise(out_dir: Path, summary: dict, attr: dict, ids: list[str]) -> dict:
    per = attr["per_task"]
    statuses = {}
    for line in (out_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            s = json.loads(line).get("status", "")
            key = "ok" if s == "ok" else s.split(":")[1].strip()[:24] if ":" in s else s[:24]
            statuses[key] = statuses.get(key, 0) + 1
    lat = [v["latency_ms"] / 1000 for v in per.values() if v.get("latency_ms")]
    secs = seconds_per_task(out_dir)
    row = {
        "run": out_dir.name,
        "when": datetime.datetime.now().strftime("%d %H:%M"),
        "n": summary.get("items"),
        "pass": summary.get("pass_rate"),
        "cell_acc": summary.get("cell_accuracy"),
        "cell_level": summary.get("pass_rate_cell_level"),
        "sheet_level": summary.get("pass_rate_sheet_level"),
        "in_tokens_p50p90": p50p90([v.get("first_input_tokens") for v in per.values()]),
        "out_tokens_mean": round(statistics.mean([v.get("output_tokens") or 0 for v in per.values()]), 0) if per else None,
        "latency_p50_s": round(statistics.median(lat), 1) if lat else None,
        "s_per_task": round(statistics.mean(secs), 1) if secs else None,
        "calls_per_task": round(statistics.mean([v.get("n_calls") or 0 for v in per.values()]), 2) if per else None,
        "length_hits": sum(v.get("length_hits") or 0 for v in per.values()),
        "statuses": statuses,
        "buckets": attr["counts"],
        "ids_file_n": len(ids),
    }
    (out_dir / "summary.json").write_text(json.dumps(row, indent=1), encoding="utf-8")
    return row


def scoreboard_row(row: dict) -> str:
    st = " ".join(f"{k}:{v}" for k, v in sorted(row["statuses"].items()))
    cells = [row["run"], row["when"], str(row["n"]), pct(row["pass"]), pct(row["cell_acc"]), pct(row["cell_level"]),
             pct(row["sheet_level"]), row["in_tokens_p50p90"], str(row["out_tokens_mean"]), str(row["latency_p50_s"]),
             str(row["s_per_task"]), str(row["calls_per_task"]), str(row["length_hits"]), st]
    cells += [str(row["buckets"].get(b, 0)) for b in BUCKETS]
    return "| " + " | ".join(cells) + " |\n"


def append_scoreboard(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# Representation study scoreboard\n\nOne row per run. Buckets are failure attribution "
                        "(see experiments/attribute.py). Runs live in research/private/runs/.\n\n" + SCOREBOARD_HEADER,
                        encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write(scoreboard_row(row))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", action="append", required=True, help="k=v,k=v with keys mode,digest,reasoning,budget,max_tokens")
    p.add_argument("--ids-file", default=str(RESEARCH / "eval" / "splits" / "dev100.txt"))
    p.add_argument("--dataset-dir", default=str(RESEARCH / "data" / "spreadsheetbench_verified_400"))
    p.add_argument("--runs-dir", default=str(RESEARCH / "private" / "runs"))
    p.add_argument("--scoreboard", default=str(HERE / "scoreboard.md"))
    p.add_argument("--model", default="tinker")
    p.add_argument("--base-model", default=None)
    p.add_argument("--project-id", default=None)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--tag", default=None)
    p.add_argument("--score-only", action="store_true", help="skip running; score and attribute existing run dirs")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--skip-if-scored", action="store_true",
                   help="skip a config entirely when its run dir already has a results.json covering every id (unattended re-runs)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    ids_file = Path(args.ids_file).resolve()
    ids = read_ids(ids_file)
    for spec in args.config:
        cfg = parse_config(spec)
        out_dir = Path(args.runs_dir) / run_name(cfg, args.tag, ids_file.stem, args.model)
        if args.skip_if_scored and (out_dir / "results.json").exists():
            done = {i["id"] for i in json.loads((out_dir / "results.json").read_text(encoding="utf-8"))["items"]}
            if set(ids) <= done:
                print(f"[{time.strftime('%H:%M:%S')}] skip {out_dir.name} (already scored)", flush=True)
                continue
        if not args.score_only:
            rc = run_pipeline(cfg, out_dir, args, ids_file)
            if args.dry_run:
                continue
            if rc != 0:
                print(f"    pipeline exited {rc}; see {out_dir / 'ablate_stdout.log'}", flush=True)
        if not (out_dir / "predictions.jsonl").exists():
            print(f"    no predictions.jsonl in {out_dir}; skipping score", flush=True)
            continue
        summary = score(out_dir, ids, args)
        attr = attribute(out_dir)
        row = summarise(out_dir, summary, attr, ids)
        append_scoreboard(Path(args.scoreboard), row)
        lat = f"{row['latency_p50_s']}s" if row["latency_p50_s"] is not None else "n/a"
        print(f"    pass {pct(row['pass'])}%  cell_acc {pct(row['cell_acc'])}%  in_tok {row['in_tokens_p50p90']}  "
              f"lat_p50 {lat}  s/task {row['s_per_task']}  buckets {row['buckets']}", flush=True)


if __name__ == "__main__":
    main()
