"""Deterministic, task-level dataset splits for LoRA experiments."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


DEFAULT_COUNTS = {"train": 280, "validation": 60, "test": 60}


def make_split(task_ids: list[str], *, seed: int = 20260905, counts: dict[str, int] | None = None) -> dict[str, list[str]]:
    """Split unique task IDs deterministically without depending on input ordering."""
    counts = counts or DEFAULT_COUNTS
    unique = sorted(set(map(str, task_ids)))
    expected = sum(counts.values())
    if len(unique) != expected:
        raise ValueError(f"split counts total {expected}, but dataset has {len(unique)} unique task IDs")
    ordered = sorted(unique, key=lambda task_id: hashlib.sha256(f"{seed}:{task_id}".encode()).hexdigest())
    result: dict[str, list[str]] = {}
    offset = 0
    for name in ("train", "validation", "test"):
        count = counts[name]
        result[name] = sorted(ordered[offset:offset + count])
        offset += count
    return result


def write_split(path: Path, split: dict[str, list[str]], *, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seed": seed, "splits": split}, indent=2) + "\n", encoding="utf-8")


def read_split(path: Path) -> dict[str, list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    split = raw.get("splits", raw)
    if set(split) != {"train", "validation", "test"}:
        raise ValueError("split file must contain train, validation, and test")
    ids = [task_id for group in split.values() for task_id in group]
    if len(ids) != len(set(ids)):
        raise ValueError("a task ID appears in more than one split")
    return {name: [str(task_id) for task_id in ids] for name, ids in split.items()}
