"""
Tests for team.books: the seat's own books against the venue's.

The venue's own accounting is the oracle: engine Portfolio objects for the
arithmetic, and a real ExchangeServer for the message stream a seat sees.

Run with:  PYTHONPATH=engine:. python -m pytest tests/test_books.py -v
"""

import asyncio
import json
import logging
import random
from types import SimpleNamespace

import pytest

import broker.config as broker_config
import exchange.config as config
from exchange.ipo import IPOOffering
from exchange.server import ExchangeServer, Portfolio
from shared.messages import (Leaderboard, OrderAck, SeatRequest, SessionEvent,
                             TradeExecution, parse_message)
from shared.orderbook import Trade
from team.books import Bookkeeper, Books, VenueState, Verdict
from team.broker import MyBroker, TeamBrokerBot
from team.venue import TeamBot

TAKER = 0.0015
MAKER = 0.0010
SYMBOLS = ("AAA", "BBB", "CCC")


def random_fills(rng: random.Random, n: int):
    """Buys, sells, shorts, covers and flips, as taker or maker, some of
    them against a teammate (no rebate)."""
    for _ in range(n):
        side = rng.choice(("buy", "sell"))
        qty = rng.choice((1, 3, 7, 10, 25, 60))
        price = round(rng.uniform(20, 200), 2)
        notional = price * qty
        taker = rng.random() < 0.4
        teammate = rng.random() < 0.2
        fee = round(notional * TAKER, 8) if taker else 0.0
        rebate = 0.0 if taker or teammate else round(notional * MAKER, 8)
        yield side, rng.choice(SYMBOLS), qty, price, fee, rebate


def apply_to_portfolio(p: Portfolio, side, symbol, qty, price, fee, rebate):
    if side == "buy":
        p.apply_buy(symbol, price, qty, fee, rebate)
    else:
        p.apply_sell(symbol, price, qty, fee, rebate)


def venue_of(p: Portfolio) -> VenueState:
    return VenueState(
        source="oracle", cash=p.cash,
        positions={s: q for s, q in p.positions.items() if q},
        realized=p.realized_pnl, fees=p.total_fees_paid,
        rebates=p.total_rebates_earned, carry=p.total_carry_paid,
        net_worth=None, avg_cost=dict(p.avg_cost))


def assert_books_match(books: Books, p: Portfolio) -> None:
    assert books.cash == pytest.approx(p.cash, abs=1e-6)
    assert books.positions == {s: q for s, q in p.positions.items() if q}
    assert books.avg_cost.keys() == books.positions.keys()
    for s in books.positions:
        assert books.avg_cost[s] == pytest.approx(p.avg_cost[s], abs=1e-6)
    assert books.realized == pytest.approx(p.realized_pnl, abs=1e-6)
    assert books.fees == pytest.approx(p.total_fees_paid, abs=1e-6)
    assert books.rebates == pytest.approx(p.total_rebates_earned, abs=1e-6)


@pytest.mark.parametrize("seed", range(8))
def test_fills_match_the_venue_portfolio(seed):
    rng = random.Random(seed)
    venue = Portfolio(team_id="me", role="trader", level=1)
    books = Books.seed(venue_of(venue))
    for fill in random_fills(rng, 400):
        apply_to_portfolio(venue, *fill)
        books.apply_fill(*fill)
    assert_books_match(books, venue)
    assert books.fills == 400
    assert books.entry_known


@pytest.mark.parametrize("seed", range(4))
def test_books_seeded_mid_session_keep_matching(seed):
    rng = random.Random(100 + seed)
    venue = Portfolio(team_id="me", role="trader", level=1)
    for fill in random_fills(rng, 150):
        apply_to_portfolio(venue, *fill)
    books = Books.seed(venue_of(venue))
    for fill in random_fills(rng, 150):
        apply_to_portfolio(venue, *fill)
        books.apply_fill(*fill)
    assert_books_match(books, venue)


def test_flip_reopens_at_the_fill_price():
    venue = Portfolio(team_id="me", role="trader", level=1)
    books = Books.seed(venue_of(venue))
    for fill in (("buy", "AAA", 10, 50.0, 0.0, 0.5),
                 ("sell", "AAA", 25, 60.0, 2.25, 0.0)):
        apply_to_portfolio(venue, *fill)
        books.apply_fill(*fill)
    assert books.positions == {"AAA": -15}
    assert books.avg_cost == {"AAA": 60.0}
    assert books.realized == pytest.approx(100.0)
    assert_books_match(books, venue)


def test_ipo_and_dividend_match_the_venue():
    venue = Portfolio(team_id="me", role="trader", level=1)
    books = Books.seed(venue_of(venue))
    books.apply_fill("sell", "AAA", 30, 40.0, 1.8, 0.0)
    venue.apply_sell("AAA", 40.0, 30, 1.8, 0.0)
    # server.py _price_ipo and _pay_dividend, by hand
    venue.cash -= 50 * 12.0
    venue.positions["NEW"] = 50
    venue.avg_cost["NEW"] = 12.0
    venue.cash += venue.positions["AAA"] * 0.75
    books.apply_ipo("NEW", 50, 12.0)
    books.apply_dividend("AAA", 0.75)
    assert_books_match(books, venue)


def test_futures_fills_and_settlements_match_the_venue_portfolio():
    rng = random.Random(7)
    venue = Portfolio(team_id="me", role="trader", level=1)
    books = Books.seed(venue_of(venue))
    for n, (side, _, qty, _, fee, rebate) in enumerate(random_fills(rng, 300)):
        price = round(rng.uniform(280, 300), 2)
        venue.apply_futures_fill("ARENA10", price, qty, side, fee, rebate)
        books.apply_fill(side, "ARENA10", qty, price, fee, rebate, future=True)
        if n % 40 == 39:
            mark = round(rng.uniform(280, 300), 4)
            venue.settle_future("ARENA10", mark)
            books.settle_future("ARENA10", mark)
    assert_books_match(books, venue)
    assert venue.realized_pnl != 0


def test_a_fill_on_an_unpriced_position_loses_the_entry():
    seeded = VenueState(source="portfolio update", cash=1000.0,
                        positions={"AAA": 10, "BBB": 5}, realized=0.0,
                        fees=0.0, rebates=0.0, carry=0.0, net_worth=None)
    books = Books.seed(seeded)
    assert books.unpriced == {"AAA", "BBB"}

    board = VenueState(source="leaderboard t5", cash=1000.0,
                       positions={"AAA": 10, "BBB": 5}, realized=0.0,
                       fees=0.0, rebates=0.0, carry=0.0, net_worth=None,
                       avg_cost={"AAA": 20.0, "BBB": 30.0}, tick=5)
    books.apply_fill("sell", "BBB", 5, 31.0, 0.0, 0.0)
    assert books.learn_entries(board) == ["AAA"]
    assert books.avg_cost["AAA"] == 20.0
    assert not books.entry_known


# ── a seat on a real venue ──────────────────────────────────────────────────

class Seat:
    """What a real ExchangeServer sends one seat, fed to its Bookkeeper, or
    to the seat's own connect and message hooks when they are given.

    `drop` decides which messages never arrive, `edit` may rewrite one on
    the way."""

    def __init__(self, srv: ExchangeServer, seat: str = "me", *,
                 keeper: Bookkeeper | None = None, on_connect=None,
                 on_message=None) -> None:
        self.srv, self.seat = srv, seat
        self.keeper = keeper or Bookkeeper(seat)
        self.on_connect = on_connect or self.keeper.on_connect
        self.on_message = on_message or self.keeper.on_message
        self.inbox: list = []
        self.drop = lambda msg: False
        self.edit = lambda msg: msg

    async def send(self, payload: str) -> None:
        self.inbox.append(parse_message(json.loads(payload)))

    def connect(self) -> None:
        self.srv.clients[self.seat] = self
        self.srv.ws_to_team[self] = self.seat
        self.on_connect()
        # server.py sends every book and the leaderboard to a new connection
        self.inbox.extend(self.srv._make_snapshot(s, b)
                          for s, b in self.srv.books.items())
        self.inbox.append(self.srv._build_leaderboard())
        self.deliver()

    def disconnect(self) -> None:
        self.deliver()
        del self.srv.clients[self.seat]

    def deliver(self) -> None:
        for msg in self.inbox:
            if not self.drop(msg):
                self.on_message(self.edit(msg))
        self.inbox.clear()

    def run(self, coro) -> None:
        asyncio.run(coro)
        self.deliver()

    def trade(self, side: str, symbol: str, qty: int, price: float, *,
              taker: bool, against: str = "them") -> None:
        buyer, seller = (self.seat, against) if side == "buy" else (against, self.seat)
        aggressor = side if taker else ("sell" if side == "buy" else "buy")
        self.run(self.srv._process_trade(Trade(
            trade_id=f"t{len(self.srv.trade_log)}", symbol=symbol, price=price,
            quantity=qty, buyer_id=buyer, seller_id=seller,
            aggressor=aggressor, fee=0.0, timestamp=0.0)))

    def ticks(self, n: int) -> None:
        for _ in range(n):
            self.srv.tick += 1
            asyncio.run(self.srv._apply_carry_and_maintenance())
        self.inbox.append(self.srv._build_leaderboard())
        self.deliver()

    def close(self):
        self.run(self.srv.close_session(persist=False))
        return self.keeper.recons[-1]


@pytest.fixture
def venue(monkeypatch):
    monkeypatch.setattr(config, "MAKER_TAKER_ENABLED", True)
    monkeypatch.setattr(config, "TAKER_FEE_RATE", TAKER)
    monkeypatch.setattr(config, "MAKER_REBATE_RATE", MAKER)
    srv = ExchangeServer()
    for team, role in (("me", "trader"), ("them", "broker")):
        srv.portfolios[team] = Portfolio(team_id=team, role=role, level=1)
    return srv


def a_session(seat: Seat) -> None:
    """Long and short, taker and maker, a flip, carry and idle-cash interest."""
    seat.connect()
    seat.trade("buy", "AAPL", 40, 210.0, taker=True)
    seat.trade("sell", "AAPL", 15, 214.5, taker=False)
    seat.ticks(30)
    seat.trade("sell", "MSFT", 20, 410.0, taker=False)      # short
    seat.ticks(10)                                          # borrow fees
    seat.trade("buy", "MSFT", 35, 405.25, taker=True)       # cover and flip
    seat.trade("sell", "AAPL", 25, 208.0, taker=True)
    seat.ticks(40)


def test_clean_session_reconciles(venue):
    seat = Seat(venue)
    a_session(seat)
    recon = seat.close()
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())
    assert recon.row("pos MSFT").venue == 15
    assert recon.row("realized").ok
    assert recon.row("fees").ok and recon.row("fees").own > 0
    assert recon.row("rebates").ok and recon.row("rebates").own > 0
    assert recon.row("carry (venue's)").venue > 0
    assert recon.row("interest (est)").venue > 0       # credited, never itemized


def test_dropped_fill_breaks_on_its_symbol(venue):
    seat = Seat(venue)
    seat.drop = lambda m: isinstance(m, TradeExecution) and m.symbol == "MSFT" \
        and m.quantity == 35
    a_session(seat)
    recon = seat.close()
    assert recon.verdict is Verdict.BREAK
    assert broken_positions(recon) == ["pos MSFT"]
    assert recon.row("pos MSFT").own == -20
    at_close = seat.keeper.recons[-2]
    assert "SESSION_CLOSED" in at_close.title
    assert broken_positions(at_close) == ["pos MSFT"]


def broken_positions(recon) -> list[str]:
    return [r.item for r in recon.rows
            if r.ok is False and r.item.startswith("pos ")]


def test_ipo_allocation_reconciles(venue):
    seat = Seat(venue)
    seat.connect()
    deal = IPOOffering(symbol="NEWCO", name="NewCo", shares=100, range_lo=10,
                       range_hi=12, open_tick=0, close_tick=1, list_tick=2,
                       state="open")
    deal.subscribe("me", 60, 12)
    seat.run(venue._price_ipo(deal))
    assert seat.keeper.books.positions["NEWCO"] == 60
    seat.ticks(5)
    recon = seat.close()
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())
    assert recon.row("pos NEWCO").venue == 60


def test_dividend_on_a_short_reconciles(venue):
    seat = Seat(venue)
    seat.connect()
    seat.trade("sell", "AAPL", 50, 200.0, taker=True)
    seat.run(venue._pay_dividend(SimpleNamespace(symbol="AAPL",
                                                 amount_per_share=2.0)))
    seat.ticks(5)
    recon = seat.close()
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())


def test_futures_and_a_settlement_reconcile(venue):
    seat = Seat(venue)
    seat.connect()
    seat.trade("buy", "ARENA10", 700, 290.0, taker=True)
    seat.trade("sell", "ARENA10", 100, 291.5, taker=False)
    seat.trade("buy", "AAPL", 10, 200.0, taker=True)
    # 4-dp rounding of this mark costs 600 contracts $0.03
    venue.ref_prices["ARENA10"] = 295.123449
    seat.run(venue._settle_futures())
    seat.ticks(5)
    recon = seat.close()
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())
    assert recon.row("pos ARENA10").venue == 600
    assert recon.row("realized").venue == pytest.approx(
        100 * 1.5 + 600 * 5.123449, abs=0.01)
    assert recon.row("realized").ok


def test_liquidation_of_this_seat_reconciles(venue, caplog):
    """A futures blow-up with cash still positive: the venue closes both
    positions at a penalty, sends no fills, and pays no interest after."""
    seat = Seat(venue)
    seat.connect()
    seat.trade("buy", "AAPL", 100, 200.0, taker=True)
    seat.trade("buy", "ARENA10", 500, 290.0, taker=True)
    seat.ticks(30)
    venue.ref_prices["ARENA10"] = 80.0
    with caplog.at_level(logging.INFO, logger="team.books"):
        seat.ticks(1)                   # the maintenance check liquidates
    assert venue.portfolios["me"].liquidated
    assert "BOOKS  liquidated: proceeds" in caplog.text
    seat.ticks(200)
    recon = seat.close()
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())
    assert recon.row("positions held").venue == 0
    assert recon.row("realized").ok
    assert "the venue closed every position" in recon.row("liquidated").note


def test_liquidation_of_another_seat_changes_nothing(venue):
    seat = Seat(venue)
    seat.connect()
    seat.trade("buy", "AAPL", 50, 200.0, taker=True)
    seat.run(venue._liquidate(venue.portfolios["them"], 0.0))
    seat.trade("buy", "MSFT", 5, 400.0, taker=False, against="other")
    seat.ticks(5)
    recon = seat.close()
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())
    assert recon.row("pos AAPL").own == 50
    assert not any(r.item == "liquidated" for r in recon.rows)


class Teacher:
    """The teacher relay's socket: SEAT_RESULT, with each bot's share of
    the cost, goes only here."""

    def __init__(self) -> None:
        self.events: list = []

    async def send(self, payload: str) -> None:
        self.events.append(parse_message(json.loads(payload)))


def test_a_seat_bought_mid_session_reconciles(venue, monkeypatch, tmp_path):
    roster = tmp_path / "teams.json"
    roster.write_text(json.dumps({"Acme": {
        "traders": ["acme_trader_1", "acme_trader_2"],
        "brokers": ["acme_broker"],
        "capital": {"acme_trader_1": 100_000, "acme_trader_2": 100_000,
                    "acme_broker": 100_000}}}))
    monkeypatch.setattr(config, "ROSTER_PATH", str(roster))
    monkeypatch.setitem(venue.scenario.flags, "purchase_window", True)
    for bot, role, cash in (("acme_trader_1", "trader", 100_000.0),
                            ("acme_trader_2", "trader", 137_421.37),
                            ("acme_broker", "broker", -5_000.0)):
        venue.portfolios[bot] = Portfolio(team_id=bot, role=role, level=1,
                                          cash=cash)
    seat = Seat(venue, "acme_trader_1")
    seat.connect()
    seat.trade("buy", "AAPL", 100, 200.0, taker=True)
    seat.ticks(10)
    before = seat.keeper.books.cash

    teacher = Teacher()
    venue.teacher_ws.add(teacher)
    seat.run(venue._handle_seat_request(
        teacher, SeatRequest(team="Acme", kind="trader", capital=75_000),
        "teacher"))
    result = teacher.events[-1]
    assert result.data["ok"], result.message
    assert before - seat.keeper.books.cash == pytest.approx(
        result.data["split"]["acme_trader_1"], abs=0.01)
    seat.ticks(5)
    recon = seat.close()
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())


def test_reconnect_with_books_intact(venue, caplog):
    seat = Seat(venue)
    seat.connect()
    seat.trade("buy", "AAPL", 10, 200.0, taker=False)
    seat.disconnect()
    seat.ticks(20)
    with caplog.at_level(logging.INFO, logger="team.books"):
        seat.connect()
    assert "BOOKS  reconnect check PASS: books survived the drop" in caplog.text
    assert seat.keeper.recons[-1].verdict is Verdict.PASS


def test_allocation_missed_while_disconnected_is_rebased(venue):
    seat = Seat(venue)
    seat.connect()
    seat.trade("buy", "AAPL", 10, 200.0, taker=False)
    deal = IPOOffering(symbol="NEWCO", name="NewCo", shares=100, range_lo=10,
                       range_hi=12, open_tick=0, close_tick=1, list_tick=2,
                       state="open")
    deal.subscribe("me", 40, 12)
    seat.disconnect()
    asyncio.run(venue._price_ipo(deal))           # no socket: nothing sent
    seat.connect()
    check = seat.keeper.recons[-1]
    assert check.verdict is Verdict.BREAK
    assert check.row("pos NEWCO").ok is False
    seat.trade("sell", "AAPL", 4, 205.0, taker=True)
    seat.ticks(5)
    recon = seat.close()
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())
    assert "1 rebases" in recon.title


def test_leaderboard_without_cash_is_not_a_break(venue):
    seat = Seat(venue)

    def no_cash(msg):
        if isinstance(msg, Leaderboard) and seat.keeper.recons:
            for row in msg.traders:
                row.pop("cash", None)
        return msg

    seat.connect()
    seat.trade("buy", "AAPL", 10, 200.0, taker=True)
    seat.ticks(5)
    seat.edit = no_cash
    recon = seat.close()
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())
    assert recon.row("cash").ok is None
    assert recon.row("cash").venue is None


def test_starting_grant_at_the_open_reconciles(venue, monkeypatch):
    monkeypatch.setattr(config, "RECORD_SESSIONS", False)

    async def no_report():
        pass

    monkeypatch.setattr(venue, "_write_eod_report", no_report)
    seat = Seat(venue)
    seat.connect()
    seat.run(venue.open_session())
    granted = venue.portfolios["me"].positions.get("AAPL", 0)
    assert granted > 0
    seat.ticks(1)                   # the leaderboard gives the grant's entry
    seat.trade("sell", "AAPL", granted + 5, 200.0, taker=False)
    seat.ticks(5)
    recon = seat.close()
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())
    assert recon.row("pos AAPL").venue == -5
    assert recon.row("realized").ok
    pnl = next(r for r in recon.rows if r.item.startswith("P&L since"))
    assert pnl.item == "P&L since t1"             # measured after the grant
    assert abs(pnl.venue) < 5_000                 # the grant is worth ~$58k


def test_unbooked_credit_breaks_on_cash(venue):
    seat = Seat(venue)
    seat.drop = lambda m: isinstance(m, SessionEvent) and m.event == "DIVIDEND"
    seat.connect()
    seat.trade("buy", "AAPL", 50, 200.0, taker=True)
    seat.run(venue._pay_dividend(SimpleNamespace(symbol="AAPL",
                                                 amount_per_share=2.0)))
    seat.ticks(5)
    recon = seat.close()
    assert recon.verdict is Verdict.BREAK
    assert recon.row("cash").ok is False
    assert recon.row("cash").diff == pytest.approx(100.0, abs=1.0)


def test_seeded_from_a_dividend_update_books_it_once(venue):
    """With no usable leaderboard the first PortfolioUpdate seeds the books,
    and the venue sends a dividend's update before the DIVIDEND event."""
    venue.portfolios["me"].positions["AAPL"] = -30
    venue.portfolios["me"].avg_cost["AAPL"] = 210.0
    seat = Seat(venue)

    def hide_seat_until_seeded(msg):
        if isinstance(msg, Leaderboard) and seat.keeper.books is None:
            return msg.model_copy(update={"traders": []})
        return msg

    seat.edit = hide_seat_until_seeded
    seat.connect()
    seat.run(venue._pay_dividend(SimpleNamespace(symbol="AAPL",
                                                 amount_per_share=2.0)))
    assert seat.keeper.books.cash == pytest.approx(venue.portfolios["me"].cash)
    seat.ticks(5)
    recon = seat.close()
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())


# ── the seats ───────────────────────────────────────────────────────────────

class Owner:
    """The trader behind a TeamBot, reduced to what its socket hooks call."""

    def __init__(self) -> None:
        self.untradeable: set[str] = set()
        self.acks: list[str] = []

    def on_connect(self) -> None:
        pass

    def on_ack(self, order_id, symbol, side, price) -> None:
        self.acks.append(order_id)

    def on_reject(self, code, message) -> None:
        pass


@pytest.fixture
def trader_bot(monkeypatch):
    import trader.config as trader_config
    from trader.trader import TraderBot

    async def no_socket(self):
        pass

    monkeypatch.setattr(trader_config, "TEAM_ID", "me")
    monkeypatch.setattr(TraderBot, "connect", no_socket)
    owner = Owner()
    return TeamBot(owner), owner


def test_a_trader_seat_reconciles_its_own_books(venue, trader_bot):
    bot, _ = trader_bot
    seat = Seat(venue, keeper=bot._books,
                on_connect=lambda: asyncio.run(bot.connect()),
                on_message=lambda m: bot._peek(m.model_dump_json()))
    a_session(seat)
    seat.disconnect()
    seat.connect()
    assert seat.keeper.recons[-1].verdict is Verdict.PASS     # the reconnect
    recon = seat.close()
    assert "close" in recon.title
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())


def test_a_failing_bookkeeper_leaves_the_trader_its_messages(trader_bot, caplog):
    bot, owner = trader_bot

    def broken(msg):
        raise RuntimeError("books bug")

    bot._books.on_message = broken
    ack = OrderAck(order_id="o1", team_id="me", symbol="AAPL", side="buy",
                   price=100.0, quantity=5)
    with caplog.at_level(logging.ERROR, logger="team.venue"):
        bot._peek(ack.model_dump_json())
    assert owner.acks == ["o1"]
    assert "Books could not take OrderAck" in caplog.text


def test_the_broker_seat_reconciles_its_own_books(venue, monkeypatch):
    seat_id = broker_config.TEAM_ID
    venue.portfolios[seat_id] = Portfolio(team_id=seat_id, role="broker",
                                          level=1)
    bot = TeamBrokerBot(MyBroker(), "ws://test")

    async def idle(*args):
        pass

    for name in ("connect_to_exchange", "listen_to_exchange", "requote_loop"):
        monkeypatch.setattr(bot, name, idle)
    seat = Seat(venue, seat_id, keeper=bot._books,
                on_connect=lambda: asyncio.run(bot._serve()),
                on_message=lambda m: asyncio.run(bot._peek(m.model_dump_json())))
    seat.connect()
    seat.trade("sell", "AAPL", 30, 220.0, taker=False)
    seat.trade("buy", "AAPL", 10, 219.0, taker=False)
    seat.disconnect()
    seat.ticks(10)
    seat.connect()
    assert "reconnect check" in seat.keeper.recons[-1].title
    assert seat.keeper.recons[-1].verdict is Verdict.PASS
    recon = seat.close()
    assert "close" in recon.title
    assert recon.verdict is Verdict.PASS, "\n".join(recon.format())
    assert recon.row("pos AAPL").venue == -20
