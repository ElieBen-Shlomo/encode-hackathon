"""Build supervised fine-tuning examples from evaluated-correct agent traces.

The output has one JSON object per model action. Each object contains the full
chat history up to that action and the action itself as the final assistant turn.
Golden workbooks are used only to select correct trajectories; their values are
never included in model messages or output examples.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

RESEARCH_DIR = Path(__file__).resolve().parents[1]
if str(RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(RESEARCH_DIR))

from evaluate import score
from sb import DEFAULT_DATASET, load_dataset, read_jsonl

try:  # Supports both `python training/build_agent_sft_data.py` and module imports.
    from training.splits import DEFAULT_COUNTS, make_split, read_split, write_split
except ModuleNotFoundError:  # pragma: no cover - direct-script import path
    from splits import DEFAULT_COUNTS, make_split, read_split, write_split


PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)[^\s'\"<>]+")
GOLDEN_RE = re.compile(r"golden", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--submissions-dir", required=True, help="Existing agent run containing predictions.jsonl and traces/")
    p.add_argument("--out-dir", required=True, help="Directory for split.json, examples.jsonl, and report.json")
    p.add_argument("--dataset-dir", default=str(DEFAULT_DATASET))
    p.add_argument("--results", help="evaluate.py --out JSON; defaults to <submissions-dir>/results.json when present")
    p.add_argument("--split", choices=("train", "validation", "test", "all"), default="train")
    p.add_argument("--split-file", help="Existing split.json; created in --out-dir when omitted")
    p.add_argument("--seed", type=int, default=20260905)
    p.add_argument("--no-recalc", action="store_true", help="Skip LibreOffice while selecting passed workbooks")
    p.add_argument("--dry-run", action="store_true", help="Inspect candidates and write only report.json")
    return p.parse_args()


def sanitize_text(text: str) -> str:
    """Remove host paths while preserving the environment-variable interface the agent uses."""
    text = text.replace("IN_XLSX=", "IN_XLSX=<IN_XLSX>").replace("OUT_XLSX=", "OUT_XLSX=<OUT_XLSX>")
    return PATH_RE.sub("<LOCAL_PATH>", text)


def valid_action(text: object) -> bool:
    if not isinstance(text, str):
        return False
    try:
        action = json.loads(text.strip())
    except json.JSONDecodeError:
        return False
    return isinstance(action, dict) and isinstance(action.get("tool"), str) and isinstance(action.get("args"), dict)


def trace_examples(task_id: str, records: Iterable[dict]) -> tuple[list[dict], Counter]:
    """Recreate action turns using the prompts recorded by the harness."""
    examples: list[dict] = []
    stats = Counter()
    history: list[dict] = []
    for record in records:
        model = str(record.get("model") or "")
        prompt, response = record.get("prompt"), record.get("response")
        if not prompt or response is None or model.endswith(":critic"):
            continue
        if not isinstance(prompt, str) or not isinstance(response, str):
            stats["non_text"] += 1
            continue
        if GOLDEN_RE.search(prompt) or GOLDEN_RE.search(response):
            stats["golden_rejected"] += 1
            continue
        if not valid_action(response):
            stats["invalid_action"] += 1
            continue
        messages = history + [{"role": "user", "content": sanitize_text(prompt)}]
        target = sanitize_text(response)
        examples.append({"id": task_id, "turn": len(examples) + 1, "messages": messages + [{"role": "assistant", "content": target}]})
        history = messages + [{"role": "assistant", "content": target}]
        stats["actions"] += 1
    return examples, stats


def passed_task_ids(submissions_dir: Path, dataset_dir: Path, *, recalc: bool, results_path: Path | None = None) -> tuple[set[str], dict, str]:
    if results_path and results_path.exists():
        raw = json.loads(results_path.read_text(encoding="utf-8"))
        results = raw.get("items")
        if not isinstance(results, list):
            raise ValueError(f"evaluation results have no items array: {results_path}")
        by_id = {str(result["id"]): result for result in results if "id" in result}
        return {task_id for task_id, result in by_id.items() if result.get("pass") is True}, by_id, str(results_path.resolve())
    predictions_path = submissions_dir / "predictions.jsonl"
    predictions = read_jsonl(predictions_path)
    dataset = {task["id"]: task for task in load_dataset(dataset_dir)}
    selected = [dataset[prediction["id"]] for prediction in predictions if prediction.get("id") in dataset]
    _, results = score(predictions, selected, recalc=recalc, predictions_path=predictions_path)
    return ({result["id"] for result in results if result.get("pass")},
            {result["id"]: result for result in results}, "fresh evaluation")


def main() -> None:
    args = parse_args()
    submissions_dir, out_dir, dataset_dir = Path(args.submissions_dir), Path(args.out_dir), Path(args.dataset_dir)
    if not (submissions_dir / "predictions.jsonl").exists():
        raise FileNotFoundError(f"predictions.jsonl not found in {submissions_dir}")
    all_ids = [task["id"] for task in load_dataset(dataset_dir)]
    split_path = Path(args.split_file) if args.split_file else out_dir / "split.json"
    if split_path.exists():
        split = read_split(split_path)
    else:
        split = make_split(all_ids, seed=args.seed, counts=DEFAULT_COUNTS)
        if not args.dry_run:
            write_split(split_path, split, seed=args.seed)
    wanted = set(all_ids if args.split == "all" else split[args.split])
    default_results = submissions_dir / "results.json"
    results_path = Path(args.results) if args.results else (default_results if default_results.exists() else None)
    passed, scoring, evaluation_source = passed_task_ids(
        submissions_dir, dataset_dir, recalc=not args.no_recalc, results_path=results_path,
    )
    eligible = passed & wanted

    examples: list[dict] = []
    trace_stats = Counter()
    missing_traces: list[str] = []
    for task_id in sorted(eligible):
        trace_path = submissions_dir / "traces" / f"{task_id}.jsonl"
        if not trace_path.exists():
            missing_traces.append(task_id)
            continue
        task_examples, stats = trace_examples(task_id, read_jsonl(trace_path))
        examples.extend(task_examples)
        trace_stats.update(stats)

    report = {
        "submissions_dir": str(submissions_dir.resolve()),
        "dataset_dir": str(dataset_dir.resolve()),
        "requested_split": args.split,
        "evaluation_source": evaluation_source,
        "tasks_in_split": len(wanted),
        "passed_tasks": len(passed),
        "eligible_passed_tasks": len(eligible),
        "missing_traces": missing_traces,
        "examples": len(examples),
        "trace_stats": dict(trace_stats),
        "scoring_statuses": dict(Counter(result["status"] for result in scoring.values())),
    }
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{args.split}.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in examples), encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
