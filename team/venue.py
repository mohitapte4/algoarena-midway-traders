"""
Midway Traders — venue plumbing for the strategy seats.

Two pieces, neither of which edits arena/ or engine/:

OwnOrders
    This seat's orders from the moment they are sent until they fill, are
    cancelled, or are refused. The seat rests passive quotes (it is a maker
    by default), so it must know what it already has on the book: shares
    offered cannot be offered again (a sell larger than the position is
    rejected while shorts are locked), an order may not cross the seat's own
    resting order (self-trade prevention cancels it and counts a reject), and
    a cancel sent just as an order fills comes back ORDER_NOT_FOUND.

    The venue acknowledges an order with its id but sends nothing when a
    cancel succeeds, so a cancelled order is forgotten after CANCEL_GRACE_SEC
    unless the venue says it was not found (it filled first). Fills carry no
    order id; they are matched to the oldest order on that symbol and side
    whose limit allows the price.

TeamBot
    Replaces the engine's trader bot for our seats:
      * no close-out after SESSION_CLOSED: the venue rejects every order once
        the session is closed, so the engine's flatten sent one doomed market
        order per held symbol (10 rejects on 2026-10-09) and never traded;
      * reads order acks and errors off the socket and hands them to the
        trader (the engine only logs them);
      * sends the cancels the trader asks for, between ticks;
      * tells the trader which listed symbols are not equities (the index
        future is quoted weeks before it may be traded);
      * passes SESSION_OPEN to the trader, which the engine adapter does not:
        a seat that rejoins after the opening cross sees no AUCTION_RESULT;
      * reconnects with exponential backoff (team.reconnect) instead of a
        fixed 3 s, and a trader hook that raises is logged rather than
        dropping the connection;
      * keeps the seat's own books from every message it receives
        (team.books) and logs their reconciliation with the venue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass

from arena.trader import _AdaptedTraderBot
import trader.config as _config  # noqa: E402  (engine module)
from shared.messages import (  # noqa: E402  (engine module)
    BookSnapshot,
    CancelOrder,
    ErrorMsg,
    OrderAck,
    SessionEvent,
    TradeExecution,
    parse_message,
)
from trader.trader import MarketData, _print_startup  # noqa: E402  (engine module)

from team.books import Bookkeeper
from team.reconnect import run_with_backoff

logger = logging.getLogger(__name__)

CANCEL_GRACE_SEC = 0.5     # a cancel not refused by then is taken as done
ACK_TIMEOUT_SEC = 5.0      # an order neither acked nor filled by then is gone
EPS = 1e-9
# Venue errors that answer something other than an order, e.g. an IPO
# indication: they must not be matched to (and drop) a pending order.
NOT_ORDER_ERRORS = frozenset({"IPO_REJECTED"})
# A cancel that lost the race with a fill. The venue answers it without
# recording a reject, so it reconciles the order but spends no budget.
CANCEL_RACE = "ORDER_NOT_FOUND"


@dataclass(eq=False)          # identity: two identical orders are still two
class Order:
    symbol: str
    side: str
    price: float
    quantity: int             # still open
    sent_at: float
    purpose: str              # what placed it: "quote", "ride entry", ...
    taker: bool               # priced to take liquidity on arrival
    order_id: str | None = None
    cancel_sent_at: float | None = None


class OwnOrders:
    """This seat's live orders. Pure bookkeeping: the caller supplies time."""

    def __init__(self) -> None:
        self._orders: list[Order] = []
        self._last_fill: dict[tuple[str, str], float] = {}

    def add(self, order: Order) -> None:
        self._orders.append(order)

    def clear(self) -> None:
        """Forget everything (after a reconnect the venue state is unknown)."""
        self._orders.clear()

    # ── venue messages ──────────────────────────────────────────────────

    def on_ack(self, order_id: str, symbol: str, side: str, price: float) -> None:
        for o in self._orders:
            if (o.order_id is None and o.symbol == symbol and o.side == side
                    and abs(o.price - price) < 1e-6):
                o.order_id = order_id
                return
        # Already fully filled before the ack arrived: nothing to track.

    def on_fill(self, symbol: str, side: str, quantity: int, price: float,
                now: float) -> Order | None:
        """Apply a fill to the order it belongs to; return that order."""
        self._last_fill[(symbol, side)] = now
        for o in self._orders:
            if o.symbol != symbol or o.side != side:
                continue
            if side == "buy" and price > o.price + EPS:
                continue
            if side == "sell" and price < o.price - EPS:
                continue
            o.quantity -= quantity
            if o.quantity <= 0:
                self._orders.remove(o)
            return o
        return None

    def on_reject(self, code: str, message: str) -> Order | None:
        """Drop the order a venue error refers to; return it if found."""
        if code in NOT_ORDER_ERRORS:
            return None
        if code == "STP_CANCEL":
            # "Your resting buy 1 AAPL @ 196.5 was cancelled by self-trade ..."
            m = re.search(r"resting (buy|sell) \d+ (\S+) @ ([\d.]+)", message)
            if m:
                side, symbol, price = m.group(1), m.group(2), float(m.group(3))
                for o in self._orders:
                    if (o.side == side and o.symbol == symbol
                            and abs(o.price - price) < 1e-6):
                        self._orders.remove(o)
                        return o
            return None
        if code == "ORDER_NOT_FOUND":
            # A cancel lost the race with a fill (or the venue already
            # dropped the order): either way it is no longer on the book.
            m = re.search(r"Order '([^']+)'", message)
            for o in self._orders:
                if m and o.order_id == m.group(1):
                    self._orders.remove(o)
                    return o
            return None
        # Any other error refuses a NEW order, which is never acked: the
        # oldest unacked order is the one it answers.
        for o in self._orders:
            if o.order_id is None:
                self._orders.remove(o)
                return o
        return None

    def expire(self, now: float) -> list[Order]:
        """Forget cancelled orders past the grace period and lost orders."""
        gone = [o for o in self._orders
                if (o.cancel_sent_at is not None
                    and now - o.cancel_sent_at >= CANCEL_GRACE_SEC)
                or (o.order_id is None and now - o.sent_at >= ACK_TIMEOUT_SEC)]
        for o in gone:
            self._orders.remove(o)
        return gone

    # ── queries ─────────────────────────────────────────────────────────

    def live(self, symbol: str, side: str) -> Order | None:
        for o in self._orders:
            if o.symbol == symbol and o.side == side:
                return o
        return None

    def open_quantity(self, symbol: str, side: str) -> int:
        """Shares on order, counting orders whose cancel is not yet confirmed."""
        return sum(o.quantity for o in self._orders
                   if o.symbol == symbol and o.side == side)

    def open_buy_notional(self) -> float:
        return sum(o.quantity * o.price for o in self._orders if o.side == "buy")

    def recently_filled(self, symbol: str, side: str, now: float) -> bool:
        last = self._last_fill.get((symbol, side))
        return last is not None and now - last < CANCEL_GRACE_SEC

    def __iter__(self):
        return iter(list(self._orders))

    def __len__(self) -> int:
        return len(self._orders)


class TokenBucket:
    """At most `rate` messages per second, bursts up to `burst`.

    The venue meters orders AND cancels at team scope (20 per exchange tick,
    shared with our broker), so each seat keeps itself well inside that.
    """

    def __init__(self, rate: float, burst: float) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens = burst
        self._at: float | None = None

    def _refill(self, now: float) -> None:
        if self._at is not None:
            self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
        self._at = now

    def available(self, now: float) -> bool:
        self._refill(now)
        return self._tokens >= 1.0

    def take(self, now: float) -> bool:
        self._refill(now)
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


# ── the live bot ────────────────────────────────────────────────────────────

class _Tap:
    """Wraps the WebSocket: each raw message is shown to `peek` first."""

    def __init__(self, ws, peek) -> None:
        self._ws = ws
        self._peek = peek

    def __getattr__(self, name):
        return getattr(self._ws, name)

    async def __aiter__(self):
        async for raw in self._ws:
            self._peek(raw)
            yield raw


class TeamBot(_AdaptedTraderBot):
    """The engine's trader bot with order acks, rejects and cancels wired in."""

    def __init__(self, owner) -> None:
        super().__init__(owner)
        self._books = Bookkeeper(_config.TEAM_ID)

    async def run(self) -> None:
        """Connect and trade, reconnecting with backoff whenever the link ends."""
        _print_startup()
        await run_with_backoff(self._serve, f"trader {_config.TEAM_ID}")

    async def _serve(self) -> None:
        """One connection: trade until the venue closes it or it drops."""
        try:
            await self.connect()
            self._session_open = False
            self.market = MarketData()
            # listen() returns on a clean close; the trading loop never
            # ends by itself, so it must be cancelled with the connection.
            listener = asyncio.create_task(self.listen())
            trading = asyncio.create_task(self.trading_loop())
            try:
                await listener
            finally:
                trading.cancel()
                await asyncio.wait({trading})   # a cancel aimed at us still propagates
        finally:
            if self._ws:
                with contextlib.suppress(Exception):
                    await self._ws.close()

    async def connect(self) -> None:
        await super().connect()
        self._owner.on_connect()
        try:
            self._books.on_connect()
        except Exception:
            logger.exception("Books could not take the connect")

    async def listen(self) -> None:
        if not isinstance(self._ws, _Tap):
            self._ws = _Tap(self._ws, self._peek)
        await super().listen()

    def _peek(self, raw) -> None:
        try:
            msg = parse_message(json.loads(raw))
        except (KeyError, ValueError) as exc:   # ValidationError is a ValueError
            # listen() parses the same message and warns about it.
            logger.debug("Skipped unreadable venue message: %s", exc)
            return
        try:
            self._books.on_message(msg)
        except Exception:
            logger.exception("Books could not take %s", type(msg).__name__)
        try:
            if isinstance(msg, OrderAck) and msg.team_id == _config.TEAM_ID:
                self._owner.on_ack(msg.order_id, msg.symbol, msg.side, msg.price)
            elif isinstance(msg, ErrorMsg):
                self._owner.on_reject(msg.code, msg.message)
            elif isinstance(msg, BookSnapshot) and msg.asset_type != "equity":
                # e.g. the index future, listed before it may be traded
                self._owner.untradeable.add(msg.symbol)
        except Exception:
            logger.exception("Trader could not take %s", type(msg).__name__)

    def _on_trade(self, msg: TradeExecution) -> None:
        try:
            super()._on_trade(msg)
        except Exception:
            logger.exception("Fill handler raised; connection kept")

    async def _on_session_event(self, msg: SessionEvent) -> None:
        try:
            await super()._on_session_event(msg)
            # The adapter keeps SESSION_OPEN from on_event, but it is the
            # only open a late joiner sees and it carries the week's flags.
            if msg.event == "SESSION_OPEN":
                self._owner.on_event(msg.event, msg.message, msg.data or {})
        except Exception:
            logger.exception("Event handler raised on %s; connection kept",
                             msg.event)

    async def trading_loop(self) -> None:
        pump = asyncio.create_task(self._cancel_pump())
        try:
            await super().trading_loop()
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    async def _cancel_pump(self) -> None:
        while True:
            await asyncio.sleep(_config.TICK_INTERVAL_SEC / 2)
            for order_id, symbol in self._owner.take_cancels():
                await self._send(CancelOrder(team_id=_config.TEAM_ID,
                                             order_id=order_id, symbol=symbol))

    async def flatten_all_positions(self) -> None:
        """Do nothing: once SESSION_CLOSED arrives every order is rejected."""
        logger.info("SESSION closed: positions are marked at the close, "
                    "no close-out orders sent")
