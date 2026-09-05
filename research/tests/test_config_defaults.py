"""The shipped defaults are the configuration the representation study selected: code agent, grid view,
low thinking, 32k cap with the step-down ladder, 400 tasks in flight with local CPU work bounded separately."""

import asyncio
import importlib
import subprocess
import sys

import pytest
import yaml

import harness
from conftest import AGENT, RESEARCH


@pytest.fixture(autouse=True)
def _restore_local_limits():
    saved = dict(harness._LIMITS)
    yield
    harness._LIMITS.clear()
    harness._LIMITS.update(saved)
    harness._reset_pools()


def test_solve_config_defaults_match_the_study():
    cfg = harness.SolveConfig()
    assert cfg.mode == "agent" and cfg.digest == "grid" and cfg.reasoning == "low"


def test_run_py_defaults_match_the_study(monkeypatch):
    out = subprocess.run([sys.executable, str(AGENT / "run.py"), "--help"], capture_output=True, text=True, timeout=120).stdout
    # argparse prints defaults only when asked; parse them by instantiating the parser instead
    monkeypatch.setattr(sys, "argv", ["run.py"])
    run = importlib.import_module("run")
    args = run.parse_args()
    assert (args.mode, args.digest, args.reasoning, args.max_tokens, args.concurrency, args.retries) == \
           ("agent", "grid", "low", 32768, 400, 6)
    assert "--lo-concurrency" in out and "--sandbox-concurrency" in out and "--reads-concurrency" in out


def test_yaml_matches_the_study():
    cfg = yaml.safe_load((RESEARCH / "config" / "qwen.yaml").read_text())
    assert cfg["renderer"] == "qwen3_8_low_reasoning"
    assert cfg["max_tokens"] == 32768 and cfg["fallback_renderers"] == "auto"
    assert cfg["digest"] == "grid" and cfg["temperature"] == 0
    assert cfg["concurrency"] == 400 and cfg["retries"] == 6 and cfg["call_timeout"] == 900
    assert set(cfg) >= {"libreoffice_concurrency", "sandbox_concurrency", "reads_concurrency"}


def test_docker_image_installs_the_tinker_backend():
    """run.py defaults to --model tinker, so the image must install the optional tinker dependencies."""
    text = (RESEARCH.parent / "Dockerfile").read_text()
    assert "--extra tinker" in text or "--all-extras" in text
    assert "TINKER_API_KEY" in text


def test_local_limits_are_separate_from_api_concurrency():
    harness.set_local_limits(libreoffice=3, sandbox=5, reads=4)

    async def probe():
        return tuple(harness._sem(k)._value for k in ("libreoffice", "sandbox", "reads"))

    assert asyncio.run(probe()) == (3, 5, 4)
    harness.set_local_limits(libreoffice=None, sandbox=None)   # None keeps the previous value
    assert asyncio.run(probe()) == (3, 5, 4)
    with pytest.raises(ValueError):
        harness.set_local_limits(sandbox=0)                    # 0 is an error, not "keep the default"


def test_bounded_runs_work_in_a_thread_under_the_semaphore():
    harness.set_local_limits(libreoffice=1, sandbox=1)
    seen = []

    def work(x):
        seen.append(x)
        return x * 2

    async def go():
        return await asyncio.gather(*(harness.bounded("sandbox", work, i) for i in range(5)))

    assert asyncio.run(go()) == [0, 2, 4, 6, 8] and sorted(seen) == [0, 1, 2, 3, 4]
