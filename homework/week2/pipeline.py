"""Week 2: asyncio producer/consumer pipeline, backpressure, and blocking-call stall.

Run:  python homework/week2/pipeline.py
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass


@dataclass
class Tick:
    seq: int
    symbol: str
    price: float
    ts: float


# ── Pipeline ────────────────────────────────────────────────────────────────

async def producer(q: asyncio.Queue, n_ticks: int, n_consumers: int, stats: dict) -> None:
    price = 100.0
    for i in range(n_ticks):
        price += random.gauss(0, 0.05)
        await q.put(Tick(seq=i, symbol="AAPL", price=price, ts=time.perf_counter()))
        stats["produced"] += 1
        stats["peak_depth"] = max(stats["peak_depth"], q.qsize())
    # One sentinel per consumer: each consumer exits after reading exactly one None.
    for _ in range(n_consumers):
        await q.put(None)


async def consumer(q: asyncio.Queue, seen: list[int], work_s: float) -> None:
    
    while True:
        item = await q.get()
        try:
            if item is None:
                return
            if work_s:
                await asyncio.sleep(work_s)  # simulated processing (e.g. strategy update)
            seen.append(item.seq)
        finally:
            q.task_done()


async def main(n_consumers: int = 3, n_ticks: int = 1000, *, maxsize: int = 100, work_s: float = 0.0) -> dict:
    """Run the pipeline; return {"produced": int, "consumed": int, "elapsed_s": float}."""
    q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    stats = {"produced": 0, "peak_depth": 0}
    seen: list[int] = []

    t0 = time.perf_counter()
    await asyncio.gather(
        producer(q, n_ticks, n_consumers, stats),
        *(consumer(q, seen, work_s) for _ in range(n_consumers)),
    )
    elapsed = time.perf_counter() - t0

    assert q.empty(), "queue not drained"
    assert stats["produced"] == n_ticks
    assert len(seen) == n_ticks, f"lost {n_ticks - len(seen)} ticks"
    assert len(set(seen)) == len(seen), "a tick was processed twice"
    assert set(seen) == set(range(n_ticks)), "consumed set != produced set"

    return {
        "produced": stats["produced"],
        "consumed": len(seen),
        "elapsed_s": elapsed,
        "peak_depth": stats["peak_depth"],
    }


# ── Backpressure ────────────────────────────────────────────────────────────

def backpressure_report(n_ticks: int = 10_000, n_consumers: int = 3) -> None:
    print(f"\n## Backpressure ({n_ticks:,} ticks, {n_consumers} consumers)\n")
    print("| maxsize | work per tick | elapsed (s) | ticks/s | peak depth |")
    print("|--------:|--------------:|------------:|--------:|-----------:|")
    for work_s in (0.0, 0.001):
        for maxsize in (1, 10_000):
            r = asyncio.run(main(n_consumers, n_ticks, maxsize=maxsize, work_s=work_s))
            work = f"{work_s * 1000:g} ms" if work_s else "none"
            print(f"| {maxsize:>7,} | {work:>13} | {r['elapsed_s']:>11.3f} "
                  f"| {n_ticks / r['elapsed_s']:>7,.0f} | {r['peak_depth']:>10,} |")


# ── Blocking-call stall ─────────────────────────────────────────────────────

async def heartbeat(interval: float, stop: asyncio.Event, gaps: list[float]) -> None:
    """Wake every `interval` seconds and record how late each wakeup actually was."""
    last = time.perf_counter()
    while not stop.is_set():
        await asyncio.sleep(interval)
        now = time.perf_counter()
        gaps.append(now - last)
        last = now


async def blocking_bad() -> None:
    time.sleep(2)  # WRONG: blocks the event loop thread


async def nonblocking_sleep() -> None:
    await asyncio.sleep(2)


async def offloaded() -> None:
    await asyncio.to_thread(time.sleep, 2)  # blocking work on a worker thread


async def measure_stall(blocker, interval: float = 0.01) -> tuple[float, int]:
    stop = asyncio.Event()
    gaps: list[float] = []
    hb = asyncio.create_task(heartbeat(interval, stop, gaps))
    await asyncio.sleep(0.2)  # let the heartbeat settle
    await blocker()
    await asyncio.sleep(0.2)
    stop.set()
    await hb
    max_gap = max(gaps)
    missed = sum(max(0, round(g / interval) - 1) for g in gaps)
    return max_gap, missed


async def pipeline_with_blocker() -> float:
    async def block_soon():
        await asyncio.sleep(0.05)
        await blocking_bad()

    r, _ = await asyncio.gather(main(3, 1000, work_s=0.001), block_soon())
    return r["elapsed_s"]


def stall_report() -> None:
    print("\n## Blocking-call stall (heartbeat every 10 ms, 2 s of 'work')\n")
    print("| variant | max heartbeat gap (ms) | missed beats |")
    print("|---------|-----------------------:|-------------:|")
    for label, blocker in [
        ("`await asyncio.sleep(2)`", nonblocking_sleep),
        ("`time.sleep(2)` in coroutine", blocking_bad),
        ("`await asyncio.to_thread(time.sleep, 2)`", offloaded),
    ]:
        max_gap, missed = asyncio.run(measure_stall(blocker))
        print(f"| {label} | {max_gap * 1000:,.1f} | {missed} |")

    clean = asyncio.run(main(3, 1000, work_s=0.001))["elapsed_s"]
    stalled = asyncio.run(pipeline_with_blocker())
    print(f"\nPipeline (1,000 ticks, 1 ms work): {clean:.3f} s alone, "
          f"{stalled:.3f} s with a `time.sleep(2)` task alongside.")


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def debug_demo() -> list[str]:
    """Run the blocking coroutine with asyncio debug mode and capture the slow-callback warning."""
    handler = _ListHandler()
    log = logging.getLogger("asyncio")
    old_level, old_propagate = log.level, log.propagate
    log.setLevel(logging.WARNING)
    log.propagate = False
    log.addHandler(handler)
    try:
        asyncio.run(blocking_bad(), debug=True)
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
        log.propagate = old_propagate
    print("\n## asyncio debug mode output\n")
    for msg in handler.records:
        print(f"    {msg}")
    return handler.records


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)  # keep asyncio's own warnings out of stdout
    print(asyncio.run(main()))
    backpressure_report()
    stall_report()
    debug_demo()
