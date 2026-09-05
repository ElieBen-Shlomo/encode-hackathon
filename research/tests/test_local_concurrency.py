"""What 400 tasks in flight do to the local machine: per-kind pools actually bound the work, cheap reads never
queue behind subprocess waits, finished event loops are released, and the shared render cache survives
concurrent eviction (a race there used to surface as a task submitted untouched)."""

import asyncio
import gc
import threading
import time
import weakref

import pytest

import harness
import workbook


@pytest.fixture(autouse=True)
def _restore_local_limits():
    saved = dict(harness._LIMITS)
    yield
    harness._LIMITS.clear()
    harness._LIMITS.update(saved)
    harness._reset_pools()


def test_each_kind_runs_on_its_own_pool_sized_to_its_limit():
    """The limit bounds what runs (not what is queued), and a burst of slow sandbox jobs does not delay a read."""
    harness.set_local_limits(libreoffice=2, sandbox=3, reads=2)
    lock, running, peak = threading.Lock(), {"n": 0}, {"n": 0}

    def slow():
        with lock:
            running["n"] += 1
            peak["n"] = max(peak["n"], running["n"])
        time.sleep(0.25)
        with lock:
            running["n"] -= 1

    async def go():
        heavy = [asyncio.create_task(harness.bounded("sandbox", slow)) for _ in range(9)]
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        await harness.bounded("reads", lambda: None)
        waited = time.perf_counter() - started
        await asyncio.gather(*heavy)
        return waited

    waited = asyncio.run(go())
    assert peak["n"] == 3, peak
    assert waited < 0.15, f"a read queued {waited:.2f}s behind sandbox work"


def test_semaphores_do_not_pin_finished_event_loops():
    harness.set_local_limits(reads=1)

    async def touch():
        sem = harness._sem("reads")
        await sem.acquire()
        waiter = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)            # the waiter blocks, binding the semaphore to this loop
        sem.release()
        await waiter
        sem.release()
        return weakref.ref(asyncio.get_running_loop())

    ref = asyncio.run(touch())
    gc.collect()
    assert ref() is None, "a closed event loop is still reachable through the semaphore cache"


def test_load_info_cache_survives_concurrent_eviction(monkeypatch):
    monkeypatch.setattr(workbook, "_load_info_uncached", lambda path, recalc=True: object())
    monkeypatch.setattr(workbook.os.path, "getmtime", lambda p: 0)
    monkeypatch.setattr(workbook, "_INFO_CACHE", {})
    errors = []

    def hammer(t):
        for i in range(3000):
            try:
                workbook.load_info(f"/x/{t}-{i}.xlsx")
            except Exception as exc:
                errors.append(repr(exc))

    threads = [threading.Thread(target=hammer, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors, errors[:3]
    assert len(workbook._INFO_CACHE) <= workbook._INFO_CACHE_MAX
