"""
Midway Traders — reconnect policy shared by the trader seats and the broker.

The venue drops connections (a restart sends a clean close, a network blip
raises), and a seat is graded on time connected. run_with_backoff keeps one
connection's worth of work running forever: each time it ends, wait and
start it again. The wait doubles from `base` to `cap`, with jitter so our
bots do not all redial the same instant, and drops back to `base` once a
connection has stayed up `healthy_after` seconds.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable

import websockets.exceptions

logger = logging.getLogger(__name__)


def backoff_delay(attempt: int, *, base: float, cap: float, jitter: float,
                  r: float) -> float:
    """Seconds to wait before retry `attempt` (1 = first retry).

    `r` in [0, 1) picks the jitter: 0.5 gives the plain doubled delay,
    0 and 1 its -jitter and +jitter ends.
    """
    plain = min(cap, base * 2 ** (attempt - 1))
    return plain * (1 + jitter * (2 * r - 1))


async def run_with_backoff(
    serve: Callable[[], Awaitable[None]],
    name: str,
    *,
    base: float = 1.0,
    cap: float = 30.0,
    healthy_after: float = 30.0,
    jitter: float = 0.2,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    rand: Callable[[], float] = random.random,
) -> None:
    """Run `serve()` again after every end, until cancelled.

    `serve` is one connection: connect, work, return when the peer closes.
    Every exception except cancellation means "try again"; the unexpected
    ones are logged with their traceback.
    """
    attempt = 0
    while True:
        started = clock()
        try:
            await serve()
            reason = "connection closed cleanly"
        except websockets.exceptions.ConnectionClosed as exc:
            reason = f"connection dropped ({exc})"
        except websockets.exceptions.InvalidHandshake as exc:
            reason = f"handshake rejected ({exc})"
        except OSError as exc:            # includes TimeoutError
            reason = f"cannot connect ({type(exc).__name__}: {exc})"
        except Exception as exc:
            logger.exception("%s: unexpected error", name)
            reason = f"unexpected {type(exc).__name__}"
        attempt = 1 if clock() - started >= healthy_after else attempt + 1
        delay = backoff_delay(attempt, base=base, cap=cap, jitter=jitter, r=rand())
        logger.warning("%s: %s — reconnecting in %.1fs (attempt %d)",
                       name, reason, delay, attempt)
        await sleep(delay)
