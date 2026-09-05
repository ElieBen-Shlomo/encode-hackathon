"""Pipeline entrypoint. This is what the Docker container runs.

Judge invocation (SUBMISSION.md):

    docker build -t <team> .
    docker run --rm -e OPENROUTER_API_KEY=... -v <dataset dir>:/data:ro -v <empty dir>:/out <team>

Reads dataset.json + init workbooks from --dataset-dir, writes predictions.jsonl,
outputs/<id>.xlsx, traces/<id>.jsonl and run.log to --out-dir.

Modes:
    null   copy the init workbook as the output, no model calls. Proves the container,
           mounts, and /out layout end-to-end without an API key.
    agent  code-writing pipeline (harness.py). --model mock needs no API key.
"""

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent, HERE.parent / "research"):
    if (candidate / "sb.py").exists():
        sys.path.insert(0, str(candidate))
        break

from sb import load_dataset
from writers import append_jsonl, log


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset-dir", default="/data")
    p.add_argument("--out-dir", default="/out")
    p.add_argument("--ids", help="comma-separated task ids (default: all)")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--mode", choices=["null", "agent"], default="null")
    p.add_argument("--model", default="mock", help='"mock", or an OpenRouter model id')
    p.add_argument("--resume", action="store_true", help="skip ids already in predictions.jsonl")
    return p.parse_args()


def selected_tasks(dataset_dir: Path, ids_arg: str | None) -> list[dict]:
    tasks = load_dataset(dataset_dir)
    if not ids_arg:
        return tasks
    ids = {i.strip() for i in ids_arg.split(",") if i.strip()}
    return [t for t in tasks if t["id"] in ids]


def prepare_out_dir(out_dir: Path, resume: bool) -> set[str]:
    """Create outputs/ and traces/, return ids already done when resuming."""
    for sub in ("outputs", "traces"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    predictions = out_dir / "predictions.jsonl"
    if resume and predictions.exists():
        done = {json.loads(line)["id"] for line in predictions.read_text().splitlines() if line.strip()}
        return done
    for sub in ("outputs", "traces"):
        shutil.rmtree(out_dir / sub)
        (out_dir / sub).mkdir(parents=True)
    for name in ("predictions.jsonl", "run.log"):
        (out_dir / name).write_text("", encoding="utf-8")
    return set()


def run_null(task: dict, out_dir: Path) -> str:
    """Identity prediction: the init workbook is the output. No model calls."""
    started = time.time()
    out = out_dir / "outputs" / f"{task['id']}.xlsx"
    shutil.copy(task["init_xlsx"], out)
    append_jsonl(out_dir / "traces" / f"{task['id']}.jsonl", {
        "step": 1, "model": "null", "prompt": None, "response": None,
        "input_tokens": 0, "output_tokens": 0,
        "latency_ms": int((time.time() - started) * 1000),
        "error": "null mode: no model call, init workbook copied as output",
    })
    append_jsonl(out_dir / "predictions.jsonl",
                 {"id": task["id"], "output": f"outputs/{task['id']}.xlsx", "status": "ok"})
    return "ok"


async def run_agent(tasks: list[dict], out_dir: Path, model_spec: str, concurrency: int) -> None:
    from harness import solve_task
    from models import get_model

    model = get_model(model_spec)
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(task: dict) -> None:
        async with semaphore:
            try:
                status = await solve_task(model, task, out_dir)
            except Exception as e:  # last-ditch: never lose a task
                shutil.copy(task["init_xlsx"], out_dir / "outputs" / f"{task['id']}.xlsx")
                append_jsonl(out_dir / "predictions.jsonl",
                             {"id": task["id"], "output": f"outputs/{task['id']}.xlsx",
                              "status": f"error: harness crash: {e}"[:200]})
                status = f"error: harness crash: {e}"[:80]
        log(out_dir, f"{task['id']:<8} {status}")

    await asyncio.gather(*(run_one(task) for task in tasks))


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    done = prepare_out_dir(out_dir, args.resume)
    tasks = [t for t in selected_tasks(Path(args.dataset_dir), args.ids) if t["id"] not in done]
    log(out_dir, f"mode {args.mode}  model {args.model}  tasks {len(tasks)}  skipped(done) {len(done)}")

    if args.mode == "agent":
        asyncio.run(run_agent(tasks, out_dir, args.model, args.concurrency))
        return

    for task in tasks:
        status = run_null(task, out_dir)
        log(out_dir, f"{task['id']:<8} {status}")


if __name__ == "__main__":
    main()
