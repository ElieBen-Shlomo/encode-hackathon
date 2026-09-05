"""Reproducible download of the external datasets into datasets/raw/.

Idempotent: skips anything already present, checksum-verifies the tarball, extracts it.

    python3 datasets/scripts/fetch.py
"""

import hashlib
import sys
import tarfile
import urllib.request
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / "raw"

SB912_URL = "https://huggingface.co/datasets/KAKA22/SpreadsheetBench/resolve/main/spreadsheetbench_912_v0.1.tar.gz?download=true"
SB912_TAR = RAW / "spreadsheetbench_912_v0.1.tar.gz"
SB912_SHA = "9cf7228b54f1edcdd4b372eb736774adf29cb4f804c9920229bac6c154833399"
SB912_DIR = RAW / "all_data_912_v0.1"

VALS_URL = "https://huggingface.co/datasets/vals-ai/finance_agent_benchmark/resolve/main/finance_agent_benchmark.csv?download=true"
VALS_CSV = RAW / "vals_finance_agent" / "finance_agent_benchmark.csv"
VALS_SHA = "bb33b8dbf21cf4b4981011057bdbf043cc90ebc1ae1f562b4f680dfacff89b3d"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download(url: str, dest: Path, sha: str | None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        print(f"downloading {dest.name}")
        urllib.request.urlretrieve(url, dest)
    if sha:
        got = _sha256(dest)
        if got != sha:
            sys.exit(f"checksum mismatch for {dest}: {got}")
        print(f"{dest.name}: OK ({got[:12]}...)")


def main() -> None:
    _download(SB912_URL, SB912_TAR, SB912_SHA)
    if not (SB912_DIR / "dataset.json").exists():
        print("extracting 912")
        with tarfile.open(SB912_TAR) as tar:
            tar.extractall(RAW, filter="data")
    print(f"912 present: {SB912_DIR.relative_to(RAW.parent)}")

    _download(VALS_URL, VALS_CSV, VALS_SHA)
    print(f"vals present: {VALS_CSV.relative_to(RAW.parent)}")


if __name__ == "__main__":
    main()
