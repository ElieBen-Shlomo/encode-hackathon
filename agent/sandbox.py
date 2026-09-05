"""Execute model-written Python in a subprocess. Only ever runs inside our Docker container
(or locally during development) — the judge's machine sees nothing but /out.

Contract with the model's script: IN_XLSX and OUT_XLSX come as argv[1]/argv[2] and env vars.
OUT_XLSX is pre-seeded with a copy of IN_XLSX, so the natural move is load OUT, mutate, save.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TAIL = 4000  # chars of stdout+stderr kept for the repair prompt


def clean_env(tmp_home: str) -> dict:
    """Subprocess env without API keys."""
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "TOKEN" not in k}
    env["HOME"] = tmp_home
    return env


def run_code(code: str, in_xlsx: str, out_xlsx: str, timeout: int = 120) -> tuple[bool, str]:
    """Run the script, return (ok, combined output tail). OUT_XLSX must exist afterwards."""
    # absolute: the script runs with cwd set to its own temp dir, so relative paths
    # from a relative --out-dir would not resolve there
    in_xlsx, out_xlsx = str(Path(in_xlsx).resolve()), str(Path(out_xlsx).resolve())
    shutil.copy(in_xlsx, out_xlsx)
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "transform.py"
        script.write_text(code, encoding="utf-8")
        try:
            p = subprocess.run(
                [sys.executable, str(script), str(in_xlsx), str(out_xlsx)],
                env={**clean_env(td), "IN_XLSX": str(in_xlsx), "OUT_XLSX": str(out_xlsx)},
                capture_output=True, text=True, timeout=timeout, cwd=td,
            )
        except subprocess.TimeoutExpired:
            return False, f"TIMEOUT: script exceeded {timeout}s"
        output = (p.stdout + p.stderr)[-TAIL:]
        if p.returncode != 0:
            return False, output
        if not Path(out_xlsx).exists():
            return False, output + "\nERROR: script deleted OUT_XLSX"
        return True, output
