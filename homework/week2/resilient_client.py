"""Week 2: websocket client that reconnects with exponential backoff.

Run:  python homework/week2/resilient_client.py
(starts a local flaky server, so no exchange is needed)
"""

import asyncio
import contextlib
import json
import logging
import socket
import time
from collections import Counter

import websockets

logger = logging.getLogger("client")
stats: Counter = Counter()


def handle(raw) -> None:
    """Process one message. Must not raise: a bad message is logged and skipped."""
    try:
        msg = json.loads(raw)
        price = float(msg["price"])
        stats["good"] += 1
        logger.info("tick %s %s @ %.2f", msg.get("seq"), msg.get("symbol"), price)
    except Exception as e:
        stats["bad"] += 1
        logger.warning("skipping bad message %r: %s: %s", raw, type(e).__name__, e)


async def consume(url: str, max_delay: float = 30.0) -> None:
    delay = 1.0
    while True:
        try:
            async with websockets.connect(url, open_timeout=5) as ws:
                stats["connects"] += 1
                logger.info("connected to %s", url)
                delay = 1.0
                async for raw in ws:
                    handle(raw)
            # A clean close (code 1000) ends the `async for` without raising.
            stats["disconnects"] += 1
            logger.warning("server closed the connection cleanly")
        except websockets.exceptions.ConnectionClosed as e:
            stats["disconnects"] += 1
            logger.warning("connection dropped: %s", e)
        except asyncio.TimeoutError:
            logger.warning("connect timed out")
        except OSError as e:
            # Connection refused / host unreachable: not covered by the two cases above.
            logger.warning("cannot connect: %s", e)
        except websockets.exceptions.InvalidHandshake as e:
            logger.warning("handshake rejected: %s", e)
        stats["retries"] += 1
        logger.info("retrying in %.0f s", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


# ── Local demo ──────────────────────────────────────────────────────────────

async def flaky_handler(ws) -> None:
    """Send a few ticks, one garbage message, then drop the connection abnormally."""
    for seq in range(3):
        await ws.send(json.dumps({"seq": seq, "symbol": "AAPL", "price": 100 + seq}))
    await ws.send("not json")
    await ws.send(json.dumps({"seq": 3, "symbol": "AAPL"}))  # missing price
    await ws.send(json.dumps({"seq": 4, "symbol": "AAPL", "price": 104}))
    await ws.close(code=1011, reason="server restarting")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def demo() -> None:
    port = free_port()
    url = f"ws://127.0.0.1:{port}"
    t0 = time.perf_counter()

    async with websockets.serve(flaky_handler, "127.0.0.1", port):
        client = asyncio.create_task(consume(url))
        await asyncio.sleep(1.5)  # client connects, gets dropped, reconnects once

    logger.info("--- server down for 6 s (watch the backoff grow) ---")
    await asyncio.sleep(6)

    async with websockets.serve(flaky_handler, "127.0.0.1", port):
        logger.info("--- server back up ---")
        await asyncio.sleep(1)

    client.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await client
    print(f"\nsurvived {time.perf_counter() - t0:.1f} s: {dict(stats)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("websockets").setLevel(logging.ERROR)
    asyncio.run(demo())
