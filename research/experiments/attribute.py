"""Failure attribution for one run: which bucket does each failing task fall in?

    uv run experiments/attribute.py private/runs/<run>          # prints counts, writes <run>/attribution.json

Buckets, first match wins:
  pass        graded and every cell matched
  infra       not graded (missing output, scorer error), or the model call itself failed
  truncation  the render meta says the graded range was not fully in the window
  format      every listed mismatch is the right value in the wrong type/format (date as text,
              "42" vs 42 after rounding, rounding near-miss, bool vs number)
  coverage    every listed mismatch has an empty actual: the solver wrote nothing there
  error_value the output contains Excel error strings (#NAME?, #VALUE!, ...)
  reasoning   everything else: the model computed something, and it was wrong

Reads results.json (from evaluate.py --out) and traces/<id>.jsonl (render meta, call errors).
Expected values from results.json stay inside research/private/; never copy them into prompts or traces.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from collections import Counter
from pathlib import Path

ERROR_STRINGS = {"#NAME?", "#REF!", "#VALUE!", "#DIV/0!", "#N/A", "#NUM!", "#NULL!", "#SPILL!"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$|^\d{1,2}/\d{1,2}/\d{2,4}$")
BUCKETS = ["pass", "infra", "truncation", "format", "coverage", "error_value", "reasoning"]


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def trace_stats(run_dir: Path, task_id: str) -> dict:
    rows = read_jsonl(run_dir / "traces" / f"{task_id}.jsonl")
    calls = [r for r in rows if r.get("model") and not r.get("tool")]
    render = next((r for r in rows if r.get("tool") == "render"), None)
    meta = {}
    if render and render.get("tool_output"):
        try:
            meta = json.loads(render["tool_output"])
        except Exception:
            meta = {}
    ok_calls = [c for c in calls if not c.get("error")]
    return {
        "meta": meta,
        "n_calls": len(calls),
        "n_call_errors": sum(1 for c in calls if c.get("error")),
        "first_input_tokens": (ok_calls[0].get("input_tokens") if ok_calls else None),
        "output_tokens": sum((c.get("output_tokens") or 0) for c in calls),
        "latency_ms": sum((c.get("latency_ms") or 0) for c in calls),
        "stop_reasons": [c.get("stop_reason") for c in calls if c.get("stop_reason")],
        "length_hits": sum(1 for c in calls if "length" in str(c.get("stop_reason", "")).lower()),
        "parse_errors": sum(1 for c in calls if c.get("parse_error")),
        "sandbox_fails": sum(1 for r in rows if r.get("tool") == "python_sandbox" and r.get("error")),
    }


def _num(v):
    try:
        if isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_format_mismatch(expected, actual) -> bool:
    if actual is None or actual == "":
        return False
    if isinstance(actual, str) and actual.strip() in ERROR_STRINGS:
        return False
    e, a = str(expected).strip(), str(actual).strip()
    if e == a:
        return True
    en, an = _num(expected), _num(actual)
    if en is not None and an is not None:
        return abs(en - an) <= 0.011  # rounding near-miss at the scorer's 2-dp boundary only; anything larger is a wrong value
    # date expected (serial or datetime) but a date-looking string came back
    if isinstance(actual, str) and DATE_RE.match(a) and (en is not None or isinstance(expected, (datetime.date, str))):
        return True
    if isinstance(expected, bool) or isinstance(actual, bool):
        return e.lower() in ("true", "false", "1", "0") and a.lower() in ("true", "false", "1", "0")
    return False


def bucket(item: dict, stats: dict) -> str:
    if item.get("status") != "graded":
        return "infra"
    if item.get("pass"):
        return "pass"
    if stats.get("n_calls", 0) and stats["n_calls"] == stats.get("n_call_errors", 0):
        return "infra"
    if stats.get("length_hits") and stats.get("n_calls") and stats["length_hits"] >= stats["n_calls"]:
        return "infra"  # every call hit max_tokens: no answer was ever produced
    mm = item.get("mismatches") or []
    if mm and all(_is_format_mismatch(m.get("expected"), m.get("actual")) for m in mm):
        return "format"
    if any(isinstance(m.get("actual"), str) and m["actual"].strip() in ERROR_STRINGS for m in mm):
        return "error_value"
    if mm and all(m.get("actual") in (None, "") for m in mm):
        return "coverage"
    # only after the value-level causes: the window hid part of the graded range and the answer is wrong
    meta = stats.get("meta") or {}
    if meta.get("answer_range_in_window") is False:
        return "truncation"
    return "reasoning"


def attribute(run_dir: Path) -> dict:
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    per_task = {}
    for item in results["items"]:
        stats = trace_stats(run_dir, item["id"])
        per_task[item["id"]] = {"bucket": bucket(item, stats), "status": item.get("status"),
                               "pass": item.get("pass", False), "type": item.get("type"),
                               "correct": item.get("correct"), "cells": item.get("cells"), **stats}
    counts = Counter(v["bucket"] for v in per_task.values())
    out = {"counts": {b: counts.get(b, 0) for b in BUCKETS}, "per_task": per_task}
    (run_dir / "attribution.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return out


def main() -> None:
    run_dir = Path(sys.argv[1])
    out = attribute(run_dir)
    print(json.dumps(out["counts"]))
    fails = [(tid, v) for tid, v in out["per_task"].items() if v["bucket"] not in ("pass",)]
    for tid, v in sorted(fails, key=lambda kv: kv[1]["bucket"])[:60]:
        print(f"{tid:<8} {v['bucket']:<11} calls={v['n_calls']} in={v['first_input_tokens']} out={v['output_tokens']} "
              f"lat={v['latency_ms'] / 1000:.0f}s stop={','.join(map(str, v['stop_reasons']))[:40]}")


if __name__ == "__main__":
    main()
