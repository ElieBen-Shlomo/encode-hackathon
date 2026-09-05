"""Pipeline entrypoint. This is what the Docker container runs.

Judge invocation (SUBMISSION.md):

    docker build -t <team> .
    docker run --rm -e TINKER_API_KEY=... -e TINKER_PROJECT_ID=... -v <dataset dir>:/data:ro -v <empty dir>:/out <team>

Reads dataset.json + init workbooks from --dataset-dir, writes predictions.jsonl,
outputs/<id>.xlsx, traces/<id>.jsonl and run.log to --out-dir.

Modes:
    agent   multi-turn local tool agent (harness.py).     [default]
    values  one call, JSON cell values (the baseline strategy through the same harness).
    null    copy the init workbook as the output, no model calls. Proves the container,
            mounts, and /out layout end-to-end without an API key.

Model (--model):
    tinker            base model --base-model (default Qwen/Qwen3.8-27B) via Tinker  [default]
    tinker:<base>     another Tinker base model
    mock              canned replies, no API key
    <openrouter id>   OpenRouter fallback

View (--digest) and thinking level (--reasoning) are the knobs of the representation study.
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

from baseline.common import load_env
from models import DEFAULT_BASE_MODEL, EFFORTS
from sb import load_dataset
from serialize import available as available_digests
from writers import append_jsonl, log


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset-dir", default="/data")
    p.add_argument("--out-dir", default="/out")
    p.add_argument("--ids", help="comma-separated task ids, or @file with one id per line (default: all)")
    p.add_argument("--concurrency", type=int, default=400, help="tasks in flight (API calls); local work is bounded separately")
    p.add_argument("--lo-concurrency", type=int, default=None, help="simultaneous LibreOffice recalculations (default: CPU count)")
    p.add_argument("--sandbox-concurrency", type=int, default=None, help="simultaneous Python/Bash tool processes (default: 2x CPU count)")
    p.add_argument("--retries", type=int, default=6, help="attempts per model call on throttling or transient errors")
    p.add_argument("--mode", choices=["agent", "values", "null"], default="agent")
    p.add_argument("--model", default="tinker", help='"tinker", "tinker:<base>", "mock", or an OpenRouter model id')
    p.add_argument("--base-model", default=DEFAULT_BASE_MODEL, help="Tinker base model")
    p.add_argument("--model-path", help="tinker://... sampler checkpoint (omit to sample the base model)")
    p.add_argument("--project-id", help="Tinker project id (else TINKER_PROJECT_ID env, else the org default project)")
    p.add_argument("--reasoning", choices=[*EFFORTS, "adaptive"], default="low",
                   help="thinking level (renderer) for the Tinker backend")
    p.add_argument("--max-turns", type=int, default=20, help="agent mode: maximum model turns per task")
    p.add_argument("--tool-timeout", type=int, default=120, help="agent mode: seconds per Python/Bash tool call")
    p.add_argument("--max-tokens", type=int, default=32768, help="completion cap per model turn")
    p.add_argument("--digest", default="grid", help=f"workbook view, one of {available_digests()}")
    p.add_argument("--budget", type=int, help="prompt token budget for views that support it")
    p.add_argument("--resume", action="store_true", help="skip ids already in predictions.jsonl")
    return p.parse_args()


def parse_ids(ids_arg: str | None) -> set[str] | None:
    if not ids_arg:
        return None
    if ids_arg.startswith("@"):
        text = Path(ids_arg[1:]).read_text(encoding="utf-8")
        return {line.strip() for line in text.splitlines() if line.strip()}
    return {i.strip() for i in ids_arg.split(",") if i.strip()}


def selected_tasks(dataset_dir: Path, ids: set[str] | None) -> list[dict]:
    tasks = load_dataset(dataset_dir)
    return tasks if ids is None else [t for t in tasks if t["id"] in ids]


def prepare_out_dir(out_dir: Path, resume: bool) -> set[str]:
    """Create outputs/ and traces/, return ids already done when resuming."""
    for sub in ("outputs", "traces"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    predictions = out_dir / "predictions.jsonl"
    if resume and predictions.exists():
        done = set()
        for line in predictions.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue  # half-written line from a killed run
        # tasks that were in flight left partial traces/outputs: remove them so the re-run starts clean
        for sub, suffix in (("traces", ".jsonl"), ("outputs", ".xlsx")):
            for f in (out_dir / sub).glob(f"*{suffix}"):
                if f.name[: -len(suffix)] not in done:
                    f.unlink()
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


async def run_model(tasks: list[dict], out_dir: Path, args: argparse.Namespace) -> None:
    from harness import SolveConfig, set_local_limits, solve_task
    from models import get_model

    set_local_limits(args.lo_concurrency, args.sandbox_concurrency)
    model = get_model(args.model, base_model=args.base_model, model_path=args.model_path,
                      project_id=args.project_id, reasoning=args.reasoning, max_tokens=args.max_tokens, retries=args.retries)
    cfg = SolveConfig(mode=args.mode, digest=args.digest, budget_tokens=args.budget, reasoning=args.reasoning,
                      max_turns=args.max_turns, tool_timeout=args.tool_timeout)
    semaphore = asyncio.Semaphore(args.concurrency)

    async def run_one(task: dict) -> None:
        async with semaphore:
            started = time.time()
            try:
                status = await solve_task(model, task, out_dir, cfg)
            except Exception as e:  # last-ditch: never lose a task
                shutil.copy(task["init_xlsx"], out_dir / "outputs" / f"{task['id']}.xlsx")
                append_jsonl(out_dir / "predictions.jsonl",
                             {"id": task["id"], "output": f"outputs/{task['id']}.xlsx",
                              "status": f"error: harness crash: {e}"[:200]})
                status = f"error: harness crash: {e}"[:80]
        log(out_dir, f"{task['id']:<8} {status:<40} {time.time() - started:6.1f}s")

    await asyncio.gather(*(run_one(task) for task in tasks))


def main() -> None:
    load_env()
    args = parse_args()
    out_dir = Path(args.out_dir).resolve()
    done = prepare_out_dir(out_dir, args.resume)
    tasks = [t for t in selected_tasks(Path(args.dataset_dir), parse_ids(args.ids)) if t["id"] not in done]
    config = {k: v for k, v in vars(args).items() if k != "ids"}
    config["n_tasks"] = len(tasks)
    (out_dir / "run_config.json").write_text(json.dumps(config, indent=2, default=str), encoding="utf-8")
    log(out_dir, f"mode {args.mode}  model {args.model}  digest {args.digest}  reasoning {args.reasoning}  "
                 f"budget {args.budget}  tasks {len(tasks)}  skipped(done) {len(done)}")

    if args.mode == "null":
        for task in tasks:
            log(out_dir, f"{task['id']:<8} {run_null(task, out_dir)}")
        return
    asyncio.run(run_model(tasks, out_dir, args))


if __name__ == "__main__":
    main()
