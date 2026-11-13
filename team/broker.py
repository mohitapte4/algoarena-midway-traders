"""
Midway Traders — broker (market maker).

Posts two-sided quotes and earns the spread plus maker rebates on every
passive fill. Three decisions, three methods — override any of them.
Inventory management (skew) is the key to staying solvent: the exchange
charges margin interest and liquidates teams below maintenance.

Underneath, TeamBrokerBot sends only orders and cancels the venue accepts
(asks capped at the inventory, or at MAX_INVENTORY short once shorts are
allowed; no cancels for filled quotes; nothing after the close), stands
aside while a scheduled event or a shock moves the price, and leaves a new
listing alone for its first minute of price discovery. It also keeps the
seat's own books (team.books) and logs their reconciliation with the venue.

Week 7: one quote level a side and 4.5 messages a second per venue, so the
team stays inside 8 a tick; RATE_LIMITED is a pause, not an error; the quote
width comes from a rolling high-low range (O(1) per book update); and on a
FLASH the name's quotes are pulled and one maker order bets on the walk back
(team.flash). Message counts, quota rejections, the cancel bill and the flash
reactions are logged once a minute per venue.

Run:
    TEAM_ID=midway_traders_broker python -m team.broker
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass

from arena import Broker                # first: puts engine/ on sys.path
import broker.config as _config         # noqa: E402  (engine module)
from arena.broker import _AdaptedBrokerBot
from broker.broker import _print_startup
from broker.broker import BrokerState
from shared.messages import (BookSnapshot, CancelOrder, ErrorMsg, OrderAck, PlaceOrder,
                             PortfolioUpdate, SessionEvent, TradeExecution,
                             parse_message)
from team.books import Bookkeeper
from team.events import DEFAULT_RAMP_TICKS, EXCHANGE_TICK_SEC
from team.flash import FlashLog, entry_order, parse_flash
from team.hotpath import REPORT_EVERY_SEC, LatencyMeter, MessageMeter
from team.reconnect import run_with_backoff
from team.rolling import RollingRange
from team.venue import TokenBucket, OwnOrders, Order
from team.risk import TradingState

logger = logging.getLogger(__name__)

# Volatility-adjusted width. `history` holds one venue mark per book update;
# week 7 sends one every 0.2 s per name, so 25 points is the last ~5 seconds.
# Live, the high and low of those points are kept in O(1) (team.rolling).
LOOKBACK = 25
CALM_BPS = 10.0         # recent range below this: keep the engine default
WIDEN_FACTOR = 2.0      # quote width = 2 × the recent high-low range
MAX_WIDEN_BPS = 150.0   # never wider than 1.5% of the price
DEFAULT_WIDTH = 0.30    # engine BASE_SPREAD...
DEFAULT_CAP_BPS = 25.0  # ...capped at 25 bp of the price

REJECT_BUDGET = 8       # graded: under 10 rejects a session; stop quoting at 8
MAX_INVENTORY = 150     # per symbol, held + bid, or short + offered. The venue's
                        # limit (800 from week 4) is per team and counts resting
                        # orders: two trader seats at SEAT_MAX_SHARES (200) plus
                        # this stays under it.
EVENT_MAX_INVENTORY = 50  # per symbol once an event has fired in it: in Session
                        # 3 the winner's timed exit sold 110 NVDA into our bids
                        # in 3 s, and we held 145 shares of a falling name
MESSAGES_PER_SEC = 4.5  # orders + cancels. Week 7 meters 8 per exchange tick
                        # (1 s) for the whole team on a venue, burst 24; the
                        # trader seats take 1.5 each (base_trader), 7.5 in all
QUOTE_LEVELS = 1        # one level a side: a requote is at most 2 cancels and
                        # 2 orders (3 levels made it 12 messages and 6 cancel fees)
REQUOTE_BPS = 8.0       # requote once the centre moves 8 bp (engine: 5)
QUOTE_MAX_AGE_SEC = 300.0  # engine: 30. Every cancel is billed, and on Nov 6
                           # most requotes were this refresh of unmoved quotes
PAUSE_RAMPS = 3         # stay out of an event's symbol for three ramps
PRICE_EPS = 0.005       # half a cent: quotes sit on the penny grid
TICK = 0.01
LISTING_PAUSE_SEC = 60.0  # an IPO opens at the offer and moves ~3% a tick
# The venue sends an event and then the fills it set off. Pulling the quotes
# this long after the event lets the listener book those fills first, so no
# cancel goes to a quote that already traded (week-4 run: ORDER_NOT_FOUND on
# AMZN). Far inside the 60 ticks the session check gives the broker to act.
EVENT_CANCEL_GRACE_SEC = 0.05
_NOT_FOUND = re.compile(r"Order '([^']+)' not found")
_STP = re.compile(r"resting (buy|sell) \d+ (\S+) @ ([\d.]+)")
_HALTED = re.compile(r"^(\S+) trading is halted")
# Shorts the venue refused: the market-wide borrow pool (1,200 a symbol in
# week 7) or the short-sale rule. Both hold for the rest of the session.
_NO_BORROW = re.compile(r"of \d+ (\S+) shares already short")
_SSR = re.compile(r"^(\S+) is under the short-sale rule")


def volatility_spread(price, history) -> float | None:
    """Dollar width for a choppy market, or None when it is calm.

    A scheduled event ramps the price several percent in ~15 s; a quote left
    at the default width is bought or sold by every fast trader on the way
    (week 2: one broker lost $2,964 on AAPL asks in a single minute).
    Widening with the recent range keeps quotes out of reach while the price
    is moving and costs nothing when it is not.
    """
    if not price or price <= 0 or len(history) < LOOKBACK:
        return None
    recent = history[-LOOKBACK:]
    return range_spread(price, max(recent) - min(recent))


def range_spread(price, move: float) -> float | None:
    """volatility_spread from the recent high-low range alone."""
    if not price or price <= 0:
        return None
    if move < price * CALM_BPS / 10_000:
        return None
    default = min(DEFAULT_WIDTH, price * DEFAULT_CAP_BPS / 10_000)
    return max(default, min(WIDEN_FACTOR * move, price * MAX_WIDEN_BPS / 10_000))


def inventory_skew(position: int, limit: int, width: float) -> float:
    """Shift both quotes against the inventory: one full width at the limit.

    Long, the ask moves toward the touch so buyers take the shares; short,
    the bid does. In Session 3 the broker quoted symmetrically and held 145
    NVDA from the event to the close.
    """
    if limit <= 0:
        return 0.0
    return -max(-1.0, min(1.0, position / limit)) * width


class MyBroker(Broker):
    """Your market-making logic. An empty subclass already quotes."""

    def spread(self, symbol, price, history):
        # Level 3: widen the quotes when the market gets choppy.
        return volatility_spread(price, history)

    # Level 4 (skew) lives in TeamBrokerBot.quote_symbol: it needs the
    # quote width and the inventory limit, which the skew() hook is not given.

    # Optional extra hooks:
    #   def toxic(self, trader_id): ...                 # Level 5
    #   def on_fill(self, side, symbol, quantity, price): ...

    async def _run_venues(self) -> None:
        """One TeamBrokerBot per venue; the first one's Yahoo feed serves all."""
        urls = [u.strip() for u in _config.EXCHANGE_URLS if u.strip()]
        bots = [TeamBrokerBot(self, url) for url in urls] or [TeamBrokerBot(self)]
        for b in bots[1:]:
            b.state.yahoo_prices = bots[0].state.yahoo_prices
            b._own_yahoo_feed = False
        await asyncio.gather(*(b.run() for b in bots))


@dataclass
class _Quote:
    """One acked quote and the shares of it still resting."""

    symbol: str
    side: str
    price: float
    open_qty: int


class _Tap:
    """Wraps the WebSocket: each raw message is awaited by `peek` first."""

    def __init__(self, ws, peek) -> None:
        self._ws = ws
        self._peek = peek

    def __getattr__(self, name):
        return getattr(self._ws, name)

    async def __aiter__(self):
        async for raw in self._ws:
            await self._peek(raw)
            yield raw


class TeamBrokerBot(_AdaptedBrokerBot):
    """The engine's broker with each of the venue's reject causes designed out.

    Asks never exceed the inventory while shorts are locked, cancels go only
    to quotes still resting, nothing is placed after the close, and a symbol
    with an event coming or firing goes quiet until its ramp has played out.
    Every error the venue still sends is counted; at REJECT_BUDGET the
    broker pulls its quotes and stops quoting.
    """

    def __init__(self, owner: Broker, exchange_url: str | None = None) -> None:
        super().__init__(owner, exchange_url)
        # Replaces the engine's buy_ids / sell_ids: only what is still resting.
        self._live: dict[str, _Quote] = {}
        # Shares an order took as a taker before its ack arrived.
        self._taken: dict[tuple[str, str], int] = {}
        self._shorts_allowed = False        # until the open says otherwise
        self._no_short: set[str] = set()    # refused a short: borrow or SSR
        # Sides the risk check would not let us quote at the last requote:
        # an empty side there is intended, not a quote that was taken.
        self._unquoted: dict[str, set[str]] = {}
        self.trading_state = TradingState(lambda: self._clock())
        self._risk_orders = OwnOrders()
        self._risk_ready = False
        self._initial_equity: float | None = None
        self._paused_until: dict[str, float] = {}   # symbol or "*" -> time
        # Each book's levels as last snapshotted: (bids, asks) of [price, qty]
        self._levels: dict[str, tuple[list, list]] = {}
        self._event_fired: set[str] = set()         # symbols, or "*"
        self._quoted_position: dict[str, int] = {}  # inventory at the last quote
        # The shares granted at the open: the skew leans only against what
        # we trade on top of them, as an idle team keeps them. Kept across
        # reconnects; a restarted process takes what it holds as its opening.
        self._opening: dict[str, int] | None = None
        self._errors = 0
        self._clock = time.monotonic
        self._sleep = asyncio.sleep
        self._bucket = TokenBucket(MESSAGES_PER_SEC, MESSAGES_PER_SEC)
        self._books = Bookkeeper(_config.TEAM_ID)
        self._pulls: set[asyncio.Task] = set()      # event cancels in their grace
        # the recent high and low of each mark, for the quote width, O(1)
        self._ranges: dict[str, RollingRange] = {}
        self._centre: dict[str, float] = {}         # last centre quoted around
        self.messages = MessageMeter()
        self.flash_log = FlashLog()
        self.message_time = LatencyMeter("message handling")
        self.quote_time = LatencyMeter("requote, including waits for a message slot")
        self._next_report: float | None = None

    # ── connection ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Quote this venue, reconnecting with backoff whenever the link ends."""
        _print_startup()
        self.start_yahoo_feeds()
        await run_with_backoff(self._serve, f"broker {_config.TEAM_ID} "
                                            f"{self.exchange_url}")

    async def _serve(self) -> None:
        """One connection: quote until the venue closes it or it drops.

        The venue cancels a seat's resting orders when it disconnects
        (server.py, cancel-on-disconnect), so each connection starts with
        nothing tracked; cancelling over the dead socket, as the engine
        does, would only lose track of what was left.
        """
        self._live.clear()
        self._risk_orders.clear()
        self._risk_ready = False
        self._taken.clear()
        self.state.resting_orders.clear()
        self.state.last_quote_prices.clear()
        try:
            await self.connect_to_exchange()
            try:
                self._books.on_connect()
            except Exception:
                logger.exception("Books could not take the connect")
            self._session_open = False
            listener = asyncio.create_task(self.listen_to_exchange())
            requoter = asyncio.create_task(self.requote_loop())
            try:
                await listener
            finally:
                # The venue sweeps our quotes as the link drops, so the event
                # cancels still waiting would go to orders it already pulled.
                pending = {requoter, *self._pulls}
                for task in pending:
                    task.cancel()
                await asyncio.wait(pending)   # a cancel aimed at us still propagates
        finally:
            if self._ws:
                with contextlib.suppress(Exception):
                    await self._ws.close()

    async def _send(self, msg) -> bool:
        """Send, pacing orders and cancels inside the team's message quota.

        A requote is a cancel and a replace of every level, and the venue
        refuses whatever goes past the quota (week-3 dry run: 69
        RATE_LIMITED in the first two seconds), so a send waits its turn.
        Orders still waiting when the session closes are dropped. Returns
        whether the message went out.
        """
        if isinstance(msg, (PlaceOrder, CancelOrder)):
            while not self._bucket.take(self._clock()):
                await self._sleep(1.0 / self._bucket.rate)
        # A requote waiting its turn can outlive the session (dry run: 3
        # SESSION_CLOSED); once it is closed only cancels are accepted.
        if isinstance(msg, PlaceOrder) and not self._session_open:
            return False
        if isinstance(msg, PlaceOrder):
            # Recheck after pacing: a halt/portfolio update can arrive while
            # waiting for the next message slot.
            if self.trading_state.blocked(msg.symbol):
                return False
            refusal = self._risk_refusal(msg)
            if refusal:
                logger.info("RISK %s %d %s @ %.2f not sent: %s", msg.side,
                            msg.quantity, msg.symbol, msg.price, refusal)
                return False
            self._risk_orders.add(Order(
                msg.symbol, msg.side, msg.price, msg.quantity,
                self._clock(), "broker", False))
        elif isinstance(msg, CancelOrder):
            for order in self._risk_orders:
                if order.order_id == msg.order_id:
                    order.cancel_sent_at = self._clock()
        await super()._send(msg)
        if isinstance(msg, PlaceOrder):
            self.messages.order()
        elif isinstance(msg, CancelOrder):
            self.messages.cancel()
        return True

    def _risk_refusal(self, msg: PlaceOrder) -> str | None:
        """Why this order must not go out, or None to send it.

        No borrowing; reserve pending orders and leave cash/equity buffers.
        Risk-reducing orders remain possible when the equity floor is hit.
        An authoritative portfolio and prices for every holding are required.

        The gross limit counts what the broker has traded, not the shares
        the venue granted at the open: the grant alone was 78% of equity on
        Nov 6, so a limit on the whole position refused every bid from
        Week 5 on (0 buys in Weeks 5 and 6).
        """
        if not self._risk_ready or not msg.price:
            return "no portfolio from the venue yet"
        self._risk_orders.expire(self._clock(), unacked=False)
        prices = self.state.exchange_prices
        if any(not prices.get(s) for s, q in self.state.positions.items() if q):
            return "no price for a holding"
        equity = self.state.cash + sum(q * prices[s]
                                      for s, q in self.state.positions.items() if q)
        if equity <= 0:
            return "no equity"
        if self._initial_equity is None:
            self._initial_equity = equity
        held = self.state.positions.get(msg.symbol, 0)
        buys = self._risk_orders.open_quantity(msg.symbol, "buy")
        sells = self._risk_orders.open_quantity(msg.symbol, "sell")
        cap = self.trading_state.cap(MAX_INVENTORY)
        projected = (held + buys + msg.quantity if msg.side == "buy"
                     else held - sells - msg.quantity)
        if abs(projected) > cap and abs(projected) >= abs(held):
            return f"inventory {projected:+d} past the {cap}-share cap"
        reducing = ((msg.side == "sell" and 0 < msg.quantity <= max(0, held - sells))
                    or (msg.side == "buy" and 0 < msg.quantity <= max(0, -held - buys)))
        notional = msg.quantity * msg.price
        if msg.side == "buy":
            # Includes all outstanding buys, not just this symbol's ladder.
            cash_room = self.state.cash - self._risk_orders.open_buy_notional() * 1.01
            if notional * 1.01 > cash_room - (0 if reducing else equity * 0.10):
                return "cash buffer"
        if reducing:
            return None
        if equity < (self._initial_equity or equity) * 0.80:
            return "equity 20% below the open"
        gross = self._traded_gross(msg)
        if gross > equity * 0.20:
            return f"traded gross {gross:,.0f} past 20% of equity {equity:,.0f}"
        return None

    def _traded_gross(self, msg: PlaceOrder) -> float:
        """Notional the broker would hold away from its opening grant if
        every resting order, and `msg`, filled on its worse side."""
        opening = self._opening or {}
        positions = self.state.positions
        symbols = ({s for s, q in positions.items() if q} | set(opening)
                   | {o.symbol for o in self._risk_orders} | {msg.symbol})
        gross = 0.0
        for s in symbols:
            traded = positions.get(s, 0) - opening.get(s, 0)
            buys = self._risk_orders.open_quantity(s, "buy")
            sells = self._risk_orders.open_quantity(s, "sell")
            if s == msg.symbol:
                if msg.side == "buy":
                    buys += msg.quantity
                else:
                    sells += msg.quantity
            worst = max(abs(traded + buys), abs(traded - sells))
            gross += worst * (self.state.exchange_prices.get(s) or msg.price)
        return gross

    # ── quoting ─────────────────────────────────────────────────────────

    def _max_inventory(self, symbol: str) -> int:
        """MAX_INVENTORY, or EVENT_MAX_INVENTORY once an event has fired in
        the name: informed flow follows an event for the rest of the block."""
        if symbol in self._event_fired or "*" in self._event_fired:
            return self.trading_state.cap(EVENT_MAX_INVENTORY)
        return self.trading_state.cap(MAX_INVENTORY)

    def _ask_room(self, symbol: str) -> int:
        """Shares we may offer: the inventory, plus the inventory limit short
        while shorts are allowed (the venue rejects a sell below 0
        otherwise, ignoring our resting sells)."""
        held = self.state.positions.get(symbol, 0)
        if self._shorts_allowed and symbol not in self._no_short:
            return max(held + self._max_inventory(symbol), 0)
        return max(held, 0)

    def _bid_room(self, symbol: str) -> int:
        """Shares we may still bid for without the team breaching its limit."""
        held = max(self.state.positions.get(symbol, 0), 0)
        return max(self._max_inventory(symbol) - held, 0)

    def _others_touch(self, symbol: str) -> tuple[float | None, float | None]:
        """Best bid and ask of everyone but us: a level counts only for the
        size beyond our own quotes resting at that price."""
        bids, asks = self._levels.get(symbol, ([], []))
        mine: dict[tuple[str, float], int] = {}
        for q in self._live.values():
            if q.symbol == symbol:
                key = (q.side, round(q.price, 2))
                mine[key] = mine.get(key, 0) + q.open_qty

        def first(levels, side):
            for price, qty in levels:
                if qty > mine.get((side, round(price, 2)), 0):
                    return price
            return None
        return first(bids, "buy"), first(asks, "sell")

    def _spread(self, symbol: str) -> float:
        """Quote width: from the rolling high-low range once LOOKBACK marks
        are in (O(1)); until then, and offline, the hook on the history."""
        rng = self._ranges.get(symbol)
        if rng is None or len(rng) < LOOKBACK:
            return self.state.compute_spread(symbol)
        price = (self.state.exchange_prices.get(symbol)
                 or self.state.yahoo_prices.get(symbol))
        width = range_spread(price, rng.high - rng.low)
        return BrokerState.compute_spread(self.state, symbol) if width is None else width

    @staticmethod
    def requote_threshold(centre: float) -> float:
        return max(_config.REQUOTE_THRESHOLD, abs(centre) * REQUOTE_BPS / 10_000.0)

    def _paused(self, symbol: str) -> bool:
        now = self._clock()
        return any(self._paused_until.get(k, 0.0) > now for k in (symbol, "*"))

    def needs_requote(self, symbol: str, centre: float) -> bool:
        """The engine's test, except a missing ask with nothing to sell is
        intended, not a side that was taken."""
        last = self.state.last_quote_prices.get(symbol)
        if last is None:
            return True
        resting = self.state.resting_orders.get(symbol, {})
        unquoted = self._unquoted.get(symbol, ())
        if (not resting.get("buy_qty") and self._bid_room(symbol) > 0
                and "buy" not in unquoted):
            return True
        if (not resting.get("sell_qty") and self._ask_room(symbol) > 0
                and "sell" not in unquoted):
            return True
        if abs(centre - last) >= self.requote_threshold(centre):
            return True
        # Fills that moved the inventory a quote size call for a new skew.
        moved = (self.state.positions.get(symbol, 0)
                 - self._quoted_position.get(symbol, 0))
        if abs(moved) >= _config.QUOTE_SIZE:
            return True
        age = time.time() - self.state.last_quote_time.get(symbol, 0.0)
        return age >= QUOTE_MAX_AGE_SEC

    async def quote_symbol(self, symbol: str) -> None:
        """The engine's ladder, with each ask capped at the inventory not
        already offered on the levels inside it."""
        if (self._errors >= REJECT_BUDGET or self._paused(symbol)
                or self.trading_state.blocked(symbol)):
            return
        centre = self.quote_centre(symbol)
        if not centre:
            return
        if not self.needs_requote(symbol, centre):
            return
        start = time.perf_counter()
        try:
            await self._requote(symbol, centre)
        finally:
            self.quote_time.add(time.perf_counter() - start)

    async def _requote(self, symbol: str, centre: float) -> None:
        spread = self._spread(symbol)
        position = self.state.positions.get(symbol, 0)
        traded = position - (self._opening or {}).get(symbol, 0)
        skew = inventory_skew(traded, self._max_inventory(symbol), spread)
        half = spread / 2
        bid_price = math.floor((centre - half + skew) * 100) / 100
        ask_price = math.ceil((centre + half + skew) * 100) / 100
        # Never cross the book: the mark can sit far from it (Session 3 NFLX:
        # mark 572, book 568), and a bid at the mark takes the offer.
        best_bid, best_ask = self._others_touch(symbol)
        if best_ask:
            bid_price = min(bid_price, round(best_ask - TICK, 2))
        if best_bid:
            ask_price = max(ask_price, round(best_bid + TICK, 2))
        if bid_price <= 0 or ask_price <= bid_price:
            return

        half_step = max(half, 0.01)
        unoffered = self._ask_room(symbol)
        unbid = self._bid_room(symbol)
        wanted: list[tuple[str, float, int]] = []
        for lvl in range(QUOTE_LEVELS):
            size = _config.QUOTE_SIZE * (lvl + 1)
            lvl_bid = math.floor((bid_price - lvl * half_step) * 100) / 100
            lvl_ask = math.ceil((ask_price + lvl * half_step) * 100) / 100
            if lvl_bid <= 0:
                continue
            bid_size = min(size, unbid)
            if bid_size > 0:
                wanted.append(("buy", lvl_bid, bid_size))
                unbid -= bid_size
            ask_size = min(size, unoffered)
            if ask_size > 0:
                wanted.append(("sell", lvl_ask, ask_size))
                unoffered -= ask_size

        # Every cancel is billed (week 7), so a quote already resting at the
        # wanted price, with no more shares than wanted, stays where it is.
        kept: dict[str, int] = {"buy": 0, "sell": 0}
        to_place = []
        live = {i: q for i, q in self._live.items() if q.symbol == symbol}
        for side, price, size in wanted:
            same = next((i for i, q in live.items() if q.side == side
                         and abs(q.price - price) < PRICE_EPS
                         and 0 < q.open_qty <= size), None)
            if same is None:
                to_place.append((side, price, size))
            else:
                kept[side] += live.pop(same).open_qty
        for order_id in live:
            del self._live[order_id]
            await self._send(CancelOrder(
                team_id=_config.TEAM_ID, order_id=order_id, symbol=symbol))
        if not self._session_open:
            return

        bid_total, ask_total = kept["buy"], kept["sell"]
        unquoted = set()
        for side, price, size in to_place:
            sent = await self._send(PlaceOrder(
                team_id=_config.TEAM_ID, symbol=symbol, side=side,
                order_type="limit", price=price, quantity=size,
            ))
            if not sent:
                unquoted.add(side)
            elif side == "buy":
                bid_total += size
            else:
                ask_total += size
        self._unquoted[symbol] = unquoted

        resting = self.state.resting_orders.setdefault(symbol, {})
        resting["buy_qty"] = bid_total
        resting["sell_qty"] = ask_total
        self.state.last_quote_prices[symbol] = centre
        self.state.last_quote_time[symbol] = time.time()
        self._quoted_position[symbol] = position
        self._centre[symbol] = centre
        logger.info("Quoted %s bid=%.2f x%d ask=%.2f x%d centre=%.4f "
                    "inventory %+d (traded %+d) skew %+.2f", symbol, bid_price,
                    bid_total, ask_price, ask_total, centre, position, traded, skew)

    async def cancel_quotes(self, symbol: str) -> None:
        """Cancel this symbol's quotes that are still resting, and no others:
        a cancel for a filled quote is an ORDER_NOT_FOUND."""
        mine = [i for i, q in self._live.items() if q.symbol == symbol]
        for order_id in mine:
            del self._live[order_id]
            await self._send(CancelOrder(
                team_id=_config.TEAM_ID, order_id=order_id, symbol=symbol))
        resting = self.state.resting_orders.setdefault(symbol, {})
        resting["buy_qty"] = resting["sell_qty"] = 0
        self.state.last_quote_prices.pop(symbol, None)

    async def flatten_all(self) -> None:
        """Pull the quotes and send no close-out orders.

        After SESSION_CLOSED the venue rejects every PlaceOrder, but it still
        accepts cancels and leaves the books in place, so cancelling is safe.
        """
        await self.cancel_all_quotes()
        logger.info("Session closed: quotes cancelled, no close-out orders "
                    "(the venue marks positions at the close)")
        self.session_summary()

    def session_summary(self) -> None:
        venue = self.exchange_url or "venue"
        logger.info("HOTPATH %s %s", venue, self.message_time.report())
        logger.info("HOTPATH %s %s", venue, self.quote_time.report())
        logger.info("MSGS   %s %s", venue, self.messages.report())
        logger.info("FLASH  %s %s", venue, self.flash_log.report())

    def _maybe_report(self) -> None:
        now = self._clock()
        if self._next_report is None:
            self._next_report = now + REPORT_EVERY_SEC
        elif now >= self._next_report:
            self._next_report = now + REPORT_EVERY_SEC
            self.session_summary()

    # ── venue messages ──────────────────────────────────────────────────

    async def listen_to_exchange(self) -> None:
        if not isinstance(self._ws, _Tap):
            self._ws = _Tap(self._ws, self._peek)
        await super().listen_to_exchange()

    async def _peek(self, raw) -> None:
        """Keep each book's touch and count the venue's errors; the engine
        loop keeps only the mark and logs errors. Timed (hot path)."""
        start = time.perf_counter()
        try:
            await self._handle(raw)
        finally:
            self.message_time.add(time.perf_counter() - start)
            self._maybe_report()

    async def _handle(self, raw) -> None:
        try:
            msg = parse_message(json.loads(raw))
        except (KeyError, ValueError):
            return                     # the engine loop logs these
        try:
            self._books.on_message(msg)
        except Exception:
            logger.exception("Books could not take %s", type(msg).__name__)
        if isinstance(msg, BookSnapshot):
            self._levels[msg.symbol] = (msg.bids, msg.asks)
            ref = msg.ref_price or msg.mid_price     # what the engine records
            if ref and ref > 0:
                rng = self._ranges.get(msg.symbol)
                if rng is None:
                    rng = self._ranges[msg.symbol] = RollingRange(LOOKBACK)
                rng.push(ref)
            return
        if not isinstance(msg, ErrorMsg):
            return
        try:
            await self._on_error(msg)
        except Exception:
            logger.exception("Could not handle venue error [%s]", msg.code)

    async def _on_error(self, msg: ErrorMsg) -> None:
        if msg.code == "RATE_LIMITED":
            self._on_rate_limited(msg.message)
            return
        if msg.code == "ORDER_NOT_FOUND":
            self.messages.not_found()
            self._risk_orders.on_reject(msg.code, msg.message)
            # A cancel that lost the race with a fill (our own seats fade
            # into these bids as an event fires): not a rejected order.
            found = _NOT_FOUND.search(msg.message)
            if found:
                self._live.pop(found.group(1), None)
            return
        # STP identifies the exact resting order. Other errors (notably a
        # throttled cancel) may not identify which request failed: keep the
        # reservation rather than freeing money for an order still live.
        if msg.code == "STP_CANCEL":
            self._risk_orders.on_reject(msg.code, msg.message)
        self._errors += 1
        halted = _HALTED.search(msg.message) if msg.code == "SYMBOL_HALTED" else None
        refused = (_NO_BORROW.search(msg.message) if msg.code == "BORROW_UNAVAILABLE"
                   else _SSR.search(msg.message) if msg.code == "SSR_RESTRICTED"
                   else None)
        if refused:
            # Retrying on every requote would spend the error budget.
            self._no_short.add(refused.group(1))
            logger.warning("No more shorts in %s this session: %s",
                           refused.group(1), msg.message)
        if halted:
            self.trading_state.halted_reject(halted.group(1), msg.message)
        if msg.code == "STP_CANCEL":
            # The venue names the quote it pulled by side, symbol and price.
            found = _STP.search(msg.message)
            if found:
                side, symbol, price = found.groups()
                pulled = next((i for i, q in self._live.items()
                               if (q.symbol, q.side) == (symbol, side)
                               and abs(q.price - float(price)) < PRICE_EPS),
                              None)
                self._live.pop(pulled, None)
        if self._errors == REJECT_BUDGET:
            logger.error("%d venue errors this session (last [%s] %s): "
                         "cancelling every quote and quoting nothing more",
                         self._errors, msg.code, msg.message)
            await self.cancel_all_quotes()

    def _on_rate_limited(self, message: str) -> None:
        """Over the team's quota: stop sending for a tick; not an error.

        Week 7 grades quota rejections as a share of messages (under 5%), so
        they do not count toward REJECT_BUDGET. A refused order is never
        acked: its cash reservation is released and its symbol requoted. The
        venue's order answer says "Slow your requoting"; a cancel's does not.
        """
        self.messages.limited()
        self._bucket.drain(self._clock())
        if "requot" in message.lower():
            order = self._risk_orders.on_reject("RATE_LIMITED", message)
            if order is not None:
                self.state.last_quote_prices.pop(order.symbol, None)
        logger.warning("QUOTA RATE_LIMITED (%d this session, %.1f%% of %d messages): "
                       "%s", self.messages.rate_limited,
                       self.messages.limited_share * 100, self.messages.messages, message)

    def _on_portfolio(self, msg: PortfolioUpdate) -> None:
        super()._on_portfolio(msg)
        self._risk_ready = True
        prices = self.state.exchange_prices
        if all(prices.get(s) for s, q in msg.positions.items() if q):
            equity = msg.cash + sum(q * prices[s] for s, q in msg.positions.items() if q)
            if self._initial_equity is None and equity > 0:
                self._initial_equity = equity
        if self._opening is None and msg.positions:
            self._opening = dict(msg.positions)
            logger.info("Opening position (not skewed away): %s", self._opening)

    def _on_order_ack(self, msg: OrderAck) -> None:
        if msg.team_id != _config.TEAM_ID:
            return
        self._risk_orders.on_ack(msg.order_id, msg.symbol, msg.side, msg.price)
        open_qty = msg.quantity - self._taken.pop((msg.symbol, msg.side), 0)
        if open_qty > 0:
            self._live[msg.order_id] = _Quote(msg.symbol, msg.side,
                                              msg.price, open_qty)

    def _on_trade(self, msg: TradeExecution) -> None:
        try:
            super()._on_trade(msg)
            if msg.buyer_id == _config.TEAM_ID:
                self._book_fill(msg.symbol, "buy", msg.price, msg.quantity,
                                taker=msg.aggressor == "buy")
            elif msg.seller_id == _config.TEAM_ID:
                self._book_fill(msg.symbol, "sell", msg.price, msg.quantity,
                                taker=msg.aggressor == "sell")
        except Exception:
            logger.exception("Could not book trade %s", msg.trade_id)

    def _book_fill(self, symbol: str, side: str, price: float, qty: int,
                   taker: bool) -> None:
        """Take a fill off the quotes it executed against.

        A resting quote fills at its own price, or better in the auction
        cross, and the venue fills our most aggressive quote first; so the
        quotes at or through the print, best first, are the ones that traded.
        A taker fill with no such quote belongs to an order not yet acked,
        and its ack subtracts it.
        """
        self._risk_orders.on_fill(symbol, side, qty, price, self._clock())
        sign = 1 if side == "buy" else -1
        hit = sorted((i for i, q in self._live.items()
                      if (q.symbol, q.side) == (symbol, side)
                      and sign * (q.price - price) > -PRICE_EPS),
                     key=lambda i: -sign * self._live[i].price)
        for order_id in hit:
            quote = self._live[order_id]
            take = min(qty, quote.open_qty)
            quote.open_qty -= take
            qty -= take
            if quote.open_qty == 0:
                del self._live[order_id]
            if qty == 0:
                return
        if taker:
            key = (symbol, side)
            self._taken[key] = self._taken.get(key, 0) + qty

    async def _on_session_event(self, msg: SessionEvent) -> None:
        try:
            self.trading_state.event(msg.event, msg.data or {})
            if msg.event in ("SESSION_PREOPEN", "SESSION_OPEN"):
                flags = (msg.data.get("scenario") or {}).get("flags") or {}
                self._shorts_allowed = flags.get("shorts_allowed") is True
            elif msg.event == "CALENDAR":
                self._on_calendar(msg.data)
            elif msg.event in ("CALENDAR_EVENT", "SHOCK"):
                self._on_fire(msg.event, msg.data)
            elif msg.event == "FLASH":
                self._on_flash(msg.data or {})
            elif msg.event == "IPO_LISTED" and msg.data.get("symbol"):
                # Nothing rests in the name yet, so there is nothing to cancel.
                self._paused_until[msg.data["symbol"]] = (
                    self._clock() + LISTING_PAUSE_SEC)
                logger.info("IPO %s listed: no quotes for %.0fs",
                            msg.data["symbol"], LISTING_PAUSE_SEC)
            await super()._on_session_event(msg)
        except Exception:
            logger.exception("Could not handle session event %s", msg.event)

    # ── events ──────────────────────────────────────────────────────────

    def _on_calendar(self, data: dict) -> None:
        """An imminent event: out until three default ramps after it fires.

        The venue warns `announce_lead` ticks ahead (twice that on the early
        feed), so the fire is at most that far away.
        """
        lead = float(data.get("announce_lead") or 120)
        if data.get("early"):
            lead *= 2
        for ev in data.get("imminent") or []:
            key = ("*" if ev.get("market_wide") or not ev.get("symbol")
                   else ev["symbol"])
            self._pause(key, (lead + PAUSE_RAMPS * DEFAULT_RAMP_TICKS)
                        * EXCHANGE_TICK_SEC, ev.get("kind", "event"))

    def _on_fire(self, event: str, data: dict) -> None:
        """A CALENDAR_EVENT or SHOCK: out until three ramps from now.

        This replaces a longer pause from the warning, which assumed the
        event would fire as late as possible: requoting three ramps after the
        fire is also the broker's order within 60 ticks of the event.
        """
        ramp = int(data.get("ramp_ticks") or DEFAULT_RAMP_TICKS)
        if data.get("market_wide"):
            keys = ["*"]
        else:
            keys = data.get("affected") or (
                [data["symbol"]] if data.get("symbol") else [])
        for key in keys:
            self._event_fired.add(key)
            self._pause(key, PAUSE_RAMPS * ramp * EXCHANGE_TICK_SEC, event,
                        replace=True)

    def _pause(self, key: str, seconds: float, reason: str, *,
               replace: bool = False) -> None:
        """Quote nothing in `key` from now on, and pull its quotes once the
        fills already on their way are booked (EVENT_CANCEL_GRACE_SEC)."""
        until = self._clock() + seconds
        if not replace:
            until = max(self._paused_until.get(key, 0.0), until)
        self._paused_until[key] = until
        symbols = list(self._quote_symbols) if key == "*" else [key]
        task = asyncio.create_task(self._pull_quotes(symbols))
        self._pulls.add(task)
        task.add_done_callback(self._pulls.discard)
        logger.info("EVENT %s: %s quotes pulled for %.0fs", reason, key, seconds)

    def _on_flash(self, data: dict) -> None:
        """A flash on this venue: pull the name's quotes and offer (or bid)
        once into the jump at half its size, as a maker.

        The quotes go first because a 2% jump fills every quote on the wrong
        side at the old price. The name stays paused until the flash has
        walked back; the requote after it cancels the flash order if it is
        still resting.
        """
        self.flash_log.seen += 1
        parsed = parse_flash(data)
        if parsed is None:
            logger.warning("FLASH unreadable event data %s", data)
            return
        symbol, pct, revert = parsed
        pre = self._centre.get(symbol) or self.state.exchange_prices.get(symbol)
        seconds = (revert + 1) * EXCHANGE_TICK_SEC
        self._paused_until[symbol] = max(self._paused_until.get(symbol, 0.0),
                                         self._clock() + seconds)
        logger.info("FLASH %s %+.2f%% on %s: quotes pulled for %.0fs, pre-flash "
                    "centre %s", symbol, pct * 100, self.exchange_url, seconds,
                    f"{pre:.2f}" if pre else "unknown")
        task = asyncio.create_task(self._flash_trade(symbol, pct, pre, self._clock()))
        self._pulls.add(task)
        task.add_done_callback(self._pulls.discard)

    async def _flash_trade(self, symbol: str, pct: float, pre: float | None,
                           t0: float) -> None:
        try:
            await asyncio.sleep(EVENT_CANCEL_GRACE_SEC)
            await self.cancel_quotes(symbol)
            if not pre or not self._session_open or self._errors >= REJECT_BUDGET:
                return
            bid, ask = self._others_touch(symbol)
            # Maker only: a taker fee of 1.0 can never be paid by the move.
            side, price, _ = entry_order(pct, pre, bid, ask, taker=1.0, rebate=0.0)
            room = self._ask_room(symbol) if side == "sell" else self._bid_room(symbol)
            qty = min(_config.QUOTE_SIZE, room)
            if qty < 1:
                logger.info("FLASH %s: no room to %s, quotes pulled only", symbol, side)
                return
            sent = await self._send(PlaceOrder(team_id=_config.TEAM_ID, symbol=symbol,
                                               side=side, order_type="limit",
                                               price=price, quantity=qty))
            if not sent:
                logger.info("FLASH %s: %s %d @ %.2f not sent", symbol, side, qty, price)
                return
            waited = self._clock() - t0
            self.flash_log.reacted(waited)
            logger.info("FLASH %s %s %d @ %.2f (maker) %.0f ms after the event",
                        symbol, side, qty, price, waited * 1000)
        except Exception:
            logger.exception("Could not trade the %s flash", symbol)

    async def _pull_quotes(self, symbols: list[str]) -> None:
        await asyncio.sleep(EVENT_CANCEL_GRACE_SEC)
        for symbol in symbols:
            try:
                await self.cancel_quotes(symbol)
            except Exception:
                logger.exception("Could not pull the quotes for %s", symbol)


if __name__ == "__main__":
    from team import logs
    logs.setup(always=True)
    MyBroker().run()
