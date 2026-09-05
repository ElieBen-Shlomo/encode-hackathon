"""Disqualification guard for the inference path.

Submission rules: a prompt that contains golden values, or a step that looks up the
answer, is a disqualification. Golden answers live on disk as `*_golden.xlsx` (verified
400) and `*_answer.xlsx` (the 912 extras, under datasets/raw/). The old mental guard
"never open anything with 'golden' in the path" does NOT cover the `*_answer.xlsx`
extras, so this makes it explicit.

Guards by FILENAME, not directory: `*_init.xlsx` / `*_input.xlsx` always pass, so
training-data generation that runs the harness on an extra's *input* file (which lives
under datasets/raw/) is unaffected — only the answer/golden files are refused.
"""

from pathlib import Path


def is_answer_file(path) -> bool:
    name = Path(path).name.lower()
    return name.endswith("_answer.xlsx") or "golden" in str(path).lower()


def assert_safe_input(path) -> None:
    """Raise if `path` looks like a golden/answer file. Call before feeding any workbook
    to the model — the prompt digest and the sandboxed script's IN_XLSX."""
    if is_answer_file(path):
        raise RuntimeError(
            f"DQ GUARD: refusing to feed a golden/answer file to the model: {path!r}. "
            "Inference may only read *_init.xlsx / *_input.xlsx."
        )
