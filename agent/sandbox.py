"""Local execution tools for the agent: model-written Python and Bash in a per-task workspace.

Two safety properties hold for everything run here:
- the child never sees API keys or tokens from our environment;
- the child only ever sees a COPY of the init workbook (`stage_input`), so model code cannot
  modify the dataset. OUT_XLSX starts as a copy of the init and is the only file it should write.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TAIL = 4000  # chars of stdout+stderr kept for the model


def stage_input(in_xlsx: str, work_dir: Path) -> str:
    """Copy the init workbook into the workspace once and return that path: the child works on the copy."""
    dst = Path(work_dir) / "in.xlsx"
    if not dst.exists():
        shutil.copy(in_xlsx, dst)
    return str(dst)


def _env(in_xlsx: str, out_xlsx: str) -> dict:
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "TOKEN" not in k and "SECRET" not in k}
    env["IN_XLSX"] = str(Path(in_xlsx).resolve())
    env["OUT_XLSX"] = str(Path(out_xlsx).resolve())
    return env


def _run(command: list[str], *, work_dir: Path, in_xlsx: str, out_xlsx: str, timeout: int) -> tuple[bool, str]:
    try:
        process = subprocess.run(command, cwd=work_dir, env=_env(in_xlsx, out_xlsx), capture_output=True,
                                 text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT: command exceeded {timeout}s"
    output = (process.stdout + process.stderr)[-TAIL:]
    if process.returncode:
        return False, output or f"command exited with code {process.returncode}"
    if not Path(out_xlsx).exists():
        return False, output + "\nERROR: command deleted OUT_XLSX"
    return True, output or "command completed successfully"


def run_python(code: str, *, work_dir: Path, in_xlsx: str, out_xlsx: str, turn: int, timeout: int) -> tuple[bool, str]:
    """Write and execute one model-generated Python script in the task workspace (argv[1]=IN, argv[2]=OUT)."""
    script = Path(work_dir) / f"turn_{turn:02d}.py"
    script.write_text(code, encoding="utf-8")
    in_copy = stage_input(in_xlsx, Path(work_dir))
    out_abs = str(Path(out_xlsx).resolve())
    return _run([sys.executable, str(script), in_copy, out_abs], work_dir=work_dir,
                in_xlsx=in_copy, out_xlsx=out_abs, timeout=timeout)


def run_bash(command: str, *, work_dir: Path, in_xlsx: str, out_xlsx: str, timeout: int) -> tuple[bool, str]:
    """Execute one model-generated Bash command in the persistent task workspace."""
    in_copy = stage_input(in_xlsx, Path(work_dir))
    out_abs = str(Path(out_xlsx).resolve())
    return _run(["/bin/bash", "-lc", command], work_dir=work_dir, in_xlsx=in_copy, out_xlsx=out_abs, timeout=timeout)


def run_code(code: str, in_xlsx: str, out_xlsx: str, timeout: int = 120) -> tuple[bool, str]:
    """Single-shot convenience: seed OUT_XLSX with the init, run one script in a throwaway workspace."""
    in_xlsx, out_xlsx = str(Path(in_xlsx).resolve()), str(Path(out_xlsx).resolve())
    shutil.copy(in_xlsx, out_xlsx)
    with tempfile.TemporaryDirectory() as td:
        return run_python(code, work_dir=Path(td), in_xlsx=in_xlsx, out_xlsx=out_xlsx, turn=1, timeout=timeout)
