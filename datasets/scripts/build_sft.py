"""Rejection-sample verified (prompt -> script) SFT data with a strong teacher.

The teacher BLIND-solves each task exactly like inference (a plan + one ```python block,
traceback-repair only, never shown the answer), then we GATE on the real scorer
(evaluate.score_task, so it matches the judges' grader cell-for-cell after recalculation):

  - train_400 : the script's recalculated output must match the VERIFIED golden.
  - extras    : one script must pass ALL 3 test-case instances (3-for-3). A task whose
                golden is ambiguous/wrong won't be solvable by a single coherent script
                across all three, so this throws those out automatically.

Only the final verified script is kept, so every SFT target is a correct, general solution
(blind solving means it can't be a hardcoded answer). Any model may generate; the FINAL
fine-tuned model is Qwen.

Output: datasets/processed/sft/<name>/{sft.jsonl, cache/<id>.json, run.log}
sft.jsonl is rebuilt from the caches, so --resume after an interrupt is exact.

The teacher samples through TINKER (any base model Tinker offers; the FINAL fine-tuned
model is Qwen). Strong teachers available: Qwen/Qwen3.5-397B-A17B, Qwen/Qwen3-235B-A22B-
Instruct-2507, deepseek-ai/DeepSeek-V3.1, moonshotai/Kimi-K2.6, openai/gpt-oss-120b.

    # smoke test the wiring (no key; mock's no-op script just fails the gate):
    research/.venv/bin/python datasets/scripts/build_sft.py --name smoke --teacher-model mock --limit 3 --source train_400

    # real run (strong Tinker teacher; samples>1 needs temperature>0 for diversity):
    research/.venv/bin/python datasets/scripts/build_sft.py --name v1 \
        --teacher-model Qwen/Qwen3.5-397B-A17B --source both --samples 4 --temperature 0.7 --concurrency 6
"""

import argparse
import asyncio
import collections
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "agent", ROOT / "research", ROOT / "datasets/scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evaluate import score_task
from harness import CODE_SYSTEM, REPAIR_PROMPT, build_prompt, extract_code
from sandbox import run_code
from sb import load_dataset
from sb912 import load_extra_912

SPLITS = ROOT / "datasets/splits"
EXEC_TIMEOUT = 120


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# ---- teacher: samples via Tinker (or "mock" for keyless smoke tests) -------------------

class Teacher:
    def __init__(self, base_model: str, model_path: str | None, temperature: float, max_tokens: int,
                 project_id: str | None = None):
        self.base_model = base_model
        self._mock = None
        if base_model == "mock":
            from models import MockModel
            self._mock = MockModel()
            return
        import tinker
        from tinker import types
        from tinker_cookbook import renderers
        from tinker_cookbook.model_info import get_recommended_renderer_name
        from tinker_cookbook.tokenizer_utils import get_tokenizer
        # the default project is read-only; sampling creates a session, so we need a writable project
        self._sampler = tinker.ServiceClient(project_id=project_id).create_sampling_client(
            base_model=base_model, model_path=model_path)
        self._renderer = renderers.get_renderer(get_recommended_renderer_name(base_model), get_tokenizer(base_model))
        self._params = types.SamplingParams(max_tokens=max_tokens, temperature=temperature,
                                            stop=self._renderer.get_stop_sequences())

    async def complete(self, system: str, prompt: str) -> str:
        if self._mock is not None:
            text, _, _ = await self._mock.complete(system, prompt)
            return text
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        model_input = self._renderer.build_generation_prompt(messages)
        resp = await self._sampler.sample_async(prompt=model_input, num_samples=1, sampling_params=self._params)
        content = self._renderer.parse_response(resp.sequences[0].tokens)[0]["content"]
        if not isinstance(content, str):  # thinking renderers return parts; keep text, drop thinking
            content = "".join(p.get("text", "") for p in content if p.get("type") == "text")
        return content


# ---- exec + score (blocking; called via asyncio.to_thread) ----------------------------

def _exec_only(code: str, in_xlsx: str) -> tuple[bool, str]:
    """Does the script run without a traceback on this input? (no correctness check)"""
    with tempfile.TemporaryDirectory() as td:
        return run_code(code, in_xlsx, str(Path(td) / "out.xlsx"), EXEC_TIMEOUT)


def _run_and_score(code: str, in_xlsx: str, golden_xlsx: str, task: dict) -> bool:
    """Run the script on in_xlsx, recalc, compare graded cells to golden_xlsx. Matches evaluate.py."""
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.xlsx")
        ok, _ = run_code(code, in_xlsx, out, EXEC_TIMEOUT)
        if not ok:
            return False
        result = score_task({**task, "golden_xlsx": golden_xlsx}, out, True, td)
        return bool(result.get("pass"))


def gate(task: dict, code: str, source: str) -> tuple[bool, str]:
    """train_400: 1-for-1 vs verified golden. extras: 3-for-3 across instances."""
    if source == "train_400":
        passed = _run_and_score(code, task["init_xlsx"], task["golden_xlsx"], task)
        return passed, f"{int(passed)}/1"
    n = sum(_run_and_score(code, inp, ans, {**task, "init_xlsx": inp}) for inp, ans in task["instances"])
    total = len(task["instances"])
    return n == total and total > 0, f"{n}/{total}"


# ---- blind solve loop (mirrors the inference harness) ---------------------------------

async def blind_solve(teacher: Teacher, prompt: str, in_xlsx: str, samples: int, max_repairs: int):
    """Return (code, completion) for a script that EXECUTES on in_xlsx, else (None, None).
    Never sees the answer — only tracebacks drive repair, exactly like inference."""
    for _ in range(samples):
        cur = prompt
        for _attempt in range(1 + max_repairs):
            text = await teacher.complete(CODE_SYSTEM, cur)
            code = extract_code(text)
            if code is None:
                cur = "Your reply had no ```python block. " + REPAIR_PROMPT.format(code="(none)", error="no code block found")
                continue
            ok, log = await asyncio.to_thread(_exec_only, code, in_xlsx)
            if ok:
                return code, text
            cur = REPAIR_PROMPT.format(code=code, error=log)
    return None, None


# ---- orchestration -------------------------------------------------------------------

def load_tasks(source: str, limit: int | None) -> list[tuple[str, dict]]:
    tasks: list[tuple[str, dict]] = []
    if source in ("train_400", "both"):
        train_ids = set((SPLITS / "train_400.txt").read_text().split())
        for t in load_dataset():
            if t["id"] in train_ids and t["golden_xlsx"]:
                tasks.append(("train_400", t))
    if source in ("extras", "both"):
        heldout_bases = set((SPLITS / "heldout_base_ids.txt").read_text().split()) if (SPLITS / "heldout_base_ids.txt").exists() else set()
        for t in load_extra_912():
            if t["id"].split("-")[0] in heldout_bases:   # no base-id sibling of a held-out task
                continue
            if len(t["instances"]) >= 1:
                tasks.append(("extra", t))
    if limit:
        tasks = tasks[:limit]
    return tasks


async def process(source: str, task: dict, teacher: Teacher, args, out_dir: Path, sem: asyncio.Semaphore) -> dict:
    cache = out_dir / "cache" / f"{task['id']}.json"
    if args.resume and cache.exists():
        return json.loads(cache.read_text())

    async with sem:
        started = time.time()
        result = {"id": task["id"], "source": source, "type": task["instruction_type"], "passed": False}
        try:
            if source == "extra":
                task["init_xlsx"] = task["instances"][0][0]   # digest/exec use the first input
            prompt = build_prompt(task)
            code, completion = await blind_solve(teacher, prompt, task["init_xlsx"], args.samples, args.max_repairs)
            if code is None:
                result["reason"] = "no executable script"
            else:
                passed, detail = await asyncio.to_thread(gate, task, code, source)
                result.update(passed=passed, instances=detail)
                if passed:
                    result["sft"] = {"system": CODE_SYSTEM, "prompt": prompt, "completion": completion, "code": code}
                else:
                    result["reason"] = f"gate {detail}"
        except Exception as e:
            result["reason"] = f"{type(e).__name__}: {e}"[:200]
        result["secs"] = round(time.time() - started, 1)

    cache.write_text(json.dumps(result))
    mark = "PASS" if result["passed"] else "  . "
    with open(out_dir / "run.log", "a") as f:
        f.write(f"{mark} {task['id']:<8} {source:<9} {result.get('instances', ''):>5}  {result.get('reason', '')}\n")
    print(f"{mark} {task['id']:<8} {source:<9} {result.get('instances',''):>5}  {result.get('reason','')}")
    return result


def rebuild_sft(out_dir: Path) -> int:
    lines, n = [], 0
    for c in sorted((out_dir / "cache").glob("*.json")):
        r = json.loads(c.read_text())
        if r.get("passed") and "sft" in r:
            lines.append(json.dumps({"id": r["id"], "source": r["source"], "type": r["type"], **r["sft"]}))
            n += 1
    (out_dir / "sft.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))
    return n


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True, help="output subdir under datasets/processed/sft/")
    p.add_argument("--teacher-model", required=True, help='"mock" or a Tinker base model, e.g. Qwen/Qwen3.5-397B-A17B')
    p.add_argument("--teacher-model-path", help="tinker://... checkpoint to sample from (omit for the base model)")
    p.add_argument("--project-id", help="writable Tinker project id (or set TINKER_PROJECT_ID); the default project is read-only")
    p.add_argument("--source", choices=["train_400", "extras", "both"], default="both")
    p.add_argument("--samples", type=int, default=1, help="blind attempts per task (needs --temperature>0 to differ)")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=8192, help="sheet-level tasks need long replies")
    p.add_argument("--max-repairs", type=int, default=2)
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--limit", type=int)
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()

    if args.samples > 1 and args.temperature == 0.0:
        print("warning: --samples>1 with temperature 0 gives identical attempts; set --temperature>0", file=sys.stderr)

    load_env()  # TINKER_API_KEY, TINKER_PROJECT_ID
    project_id = args.project_id or os.environ.get("TINKER_PROJECT_ID")
    out_dir = ROOT / "datasets/processed/sft" / args.name
    (out_dir / "cache").mkdir(parents=True, exist_ok=True)
    tasks = load_tasks(args.source, args.limit)
    teacher = Teacher(args.teacher_model, args.teacher_model_path, args.temperature, args.max_tokens, project_id)
    sem = asyncio.Semaphore(args.concurrency)
    print(f"teacher={args.teacher_model} source={args.source} tasks={len(tasks)} "
          f"samples={args.samples} temp={args.temperature} -> {out_dir.relative_to(ROOT)}")

    results = await asyncio.gather(*(process(s, t, teacher, args, out_dir, sem) for s, t in tasks))
    kept = rebuild_sft(out_dir)

    by = collections.Counter((r["source"], r["type"], r["passed"]) for r in results)
    print("\n=== yield ===")
    for source in ("train_400", "extra"):
        for typ in ("Cell-Level Manipulation", "Sheet-Level Manipulation"):
            passed = by[(source, typ, True)]
            total = passed + by[(source, typ, False)]
            if total:
                print(f"  {source:<9} {typ[:5]:<5}  {passed:3}/{total:<3} passed")
    print(f"\nSFT examples written: {kept}  ->  {(out_dir / 'sft.jsonl').relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
