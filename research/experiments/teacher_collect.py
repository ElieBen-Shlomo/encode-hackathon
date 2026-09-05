"""Collect a teacher solve into predictions.jsonl, then (and only then) score it.

    uv run experiments/teacher_collect.py --out private/fable400 [--ids-file eval/splits/dev100.txt] [--score]

Every task in tasks/ gets one line: status "ok" when outputs/<id>.xlsx exists and opens, otherwise the init
workbook is copied to outputs/<id>.xlsx and the status records the reason. With --score, evaluate.py is run
on exactly those ids and results.json is written next to predictions.jsonl. Golden files are touched by
evaluate.py alone.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

RESEARCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RESEARCH))

import openpyxl  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(RESEARCH / "private" / "fable400"))
    p.add_argument("--ids-file", default=None)
    p.add_argument("--score", action="store_true")
    args = p.parse_args()
    out = Path(args.out)
    ids = None
    if args.ids_file:
        ids = {l.strip() for l in Path(args.ids_file).read_text().splitlines() if l.strip()}

    lines = []
    counts = {"ok": 0, "missing": 0, "unreadable": 0}
    for spec_path in sorted((out / "tasks").glob("*.json")):
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        tid = spec["id"]
        if ids is not None and tid not in ids:
            continue
        outp = Path(spec["output_xlsx"])
        status = "ok"
        if not outp.exists():
            shutil.copy(spec["init_xlsx"], outp)
            status = "error: no output produced, init copied"
            counts["missing"] += 1
        else:
            try:
                openpyxl.load_workbook(outp, read_only=True).close()
            except Exception as e:
                shutil.copy(spec["init_xlsx"], outp)
                status = f"error: output unreadable ({type(e).__name__}), init copied"
                counts["unreadable"] += 1
        if status == "ok":
            counts["ok"] += 1
        lines.append({"id": tid, "output": f"outputs/{tid}.xlsx", "status": status})
    (out / "predictions.jsonl").write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines), encoding="utf-8")
    print(f"{len(lines)} predictions: {counts}")

    if args.score and lines:
        cmd = ["uv", "run", "evaluate.py", "--predictions", str(out / "predictions.jsonl"),
               "--ids", ",".join(l["id"] for l in lines), "--out", str(out / "results.json")]
        print("scoring:", " ".join(cmd[:5]), "...")
        subprocess.run(cmd, cwd=str(RESEARCH), check=False)


if __name__ == "__main__":
    main()
