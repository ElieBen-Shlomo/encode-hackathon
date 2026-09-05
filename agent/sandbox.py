"""Local execution tools for the Qwen agent."""

import os
import subprocess
import sys
from pathlib import Path

TAIL = 4000

# Model-written code must not see host secrets (TINKER_API_KEY, OPENROUTER_API_KEY, ...):
# a stray `env` would put them in tool output, which lands in committed traces.
KEEP_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


def _env(in_xlsx: str, out_xlsx: str) -> dict:
    env = {key: os.environ[key] for key in KEEP_ENV if key in os.environ}
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
    """Write and execute one model-generated Python script in the task workspace."""
    script = work_dir / f"turn_{turn:02d}.py"
    script.write_text(code, encoding="utf-8")
    return _run([sys.executable, str(script), str(in_xlsx), str(out_xlsx)], work_dir=work_dir,
                in_xlsx=in_xlsx, out_xlsx=out_xlsx, timeout=timeout)


def run_bash(command: str, *, work_dir: Path, in_xlsx: str, out_xlsx: str, timeout: int) -> tuple[bool, str]:
    """Execute one model-generated Bash command in the persistent task workspace.

    Non-login shell: profiles would re-source the host environment and clobber the curated PATH.
    """
    return _run(["/bin/bash", "-c", command], work_dir=work_dir, in_xlsx=in_xlsx,
                out_xlsx=out_xlsx, timeout=timeout)
