"""Week 5 safety gates on the team's real trader and broker classes."""
import asyncio

import pytest

from arena.broker import _AdaptedBrokerBot
from arena.trader import _AdaptedTraderBot
from shared.messages import PlaceOrder, PortfolioUpdate, SessionEvent, CancelOrder, OrderAck
from team.broker import MyBroker, TeamBrokerBot
from team.mean_reversion_trader import MeanReversionTrader
from team.momentum_trader import MomentumTrader
from team.venue import TeamBot
from tests.test_event_layer import setup
from tests.test_momentum_trader import trend
from tests.test_broker import make, run, event, places


@pytest.mark.parametrize('cls', [MomentumTrader, MeanReversionTrader])
@pytest.mark.parametrize('market_wide', [False, True])
def test_traders_pause_until_explicit_resume_then_accept_orders(cls, market_wide):
    t, m, p, c = setup(cls)
    t.on_tick(m, p)
    data = {} if market_wide else {'symbol': 'AAPL'}
    t.on_event('MARKET_HALT' if market_wide else 'TRADING_HALT', '', data)
    c.t += 10000  # a guessed timer must not lift a halt
    assert t._place('AAPL', 'buy', 5, 195, c.t, 'test', taker=False) is None
    assert sum(t.rejects.values()) == 0
    t.on_event('MARKET_RESUMED' if market_wide else 'TRADING_RESUMED', '', data)
    assert t._place('AAPL', 'buy', 5, 195, c.t, 'test', taker=False) is not None


def test_one_symbol_resume_does_not_lift_market_halt():
    t, m, p, c = setup()
    t.on_tick(m, p)
    t.on_event('MARKET_HALT', '', {})
    t.on_event('TRADING_RESUMED', '', {'symbol': 'AAPL'})
    assert t._place('AAPL', 'buy', 5, 195, c.t, 'test', taker=False) is None


@pytest.mark.parametrize('market_wide', [False, True])
def test_broker_resumes_quoting_after_halt(market_wide):
    b, sent, now = make()
    data = {} if market_wide else {'symbol': 'AAPL'}
    run(b._on_session_event(event('MARKET_HALT' if market_wide else 'TRADING_HALT', **data)))
    now[0] += 10000
    run(b.quote_symbol('AAPL'))
    assert not places(sent)
    run(b._on_session_event(event('MARKET_RESUMED' if market_wide else 'TRADING_RESUMED', **data)))
    run(b.quote_symbol('AAPL'))
    assert places(sent)
    assert b._errors == 0


def test_trader_live_send_blocks_signal_created_before_halt(monkeypatch):
    t, m, p, c = setup()
    t.on_tick(m, p)
    signal = t._place('AAPL', 'buy', 5, 195, c.t, 'test', taker=False)
    bot = TeamBot(t)
    sent = []
    async def send(self, msg):
        sent.append(msg)
    monkeypatch.setattr(_AdaptedTraderBot, '_send', send)
    t.on_event('TRADING_HALT', '', {'symbol': 'AAPL'})
    run(bot._place_order(signal))
    assert not sent
    assert t.orders.live('AAPL', 'buy') is None
    t.on_event('TRADING_RESUMED', '', {'symbol': 'AAPL'})
    signal = t._place('AAPL', 'buy', 5, 195, c.t, 'test', taker=False)
    run(bot._place_order(signal))
    assert len(sent) == 1


def live_broker(monkeypatch, cash=400000, positions=None):
    b = TeamBrokerBot(MyBroker(), 'ws://test')
    b._session_open = True
    b.state.exchange_prices.update({'AAPL': 100., 'AMD': 100.})
    now = [100.]
    b._clock = lambda: now[0]
    b._on_portfolio(PortfolioUpdate(team_id='me', cash=cash,
                                   positions=positions or {}, realized_pnl=0,
                                   net_worth=cash, unrealized_pnl=0, total_fees_paid=0))
    sent = []
    async def send(self, msg):
        sent.append(msg)
    monkeypatch.setattr(_AdaptedBrokerBot, '_send', send)
    return b, sent, now


def test_halt_received_while_broker_waits_for_rate_limit(monkeypatch):
    b, sent, now = live_broker(monkeypatch)
    b._bucket.take(now[0])
    while b._bucket.take(now[0]):
        pass
    async def sleep(seconds):
        now[0] += seconds
        await b._on_session_event(event('TRADING_HALT', symbol='AAPL'))
    b._sleep = sleep
    run(b._send(PlaceOrder(order_type='limit', team_id='me', symbol='AAPL', side='buy', price=100, quantity=5)))
    assert not sent
    assert len(b._risk_orders) == 0


def test_broker_requires_portfolio_before_live_orders(monkeypatch):
    b, sent, now = live_broker(monkeypatch)
    b._risk_ready = False
    run(b._send(PlaceOrder(order_type='limit', team_id='me', symbol='AAPL', side='buy', price=100, quantity=5)))
    assert not sent


def test_broker_reserves_unacked_orders_across_symbols(monkeypatch):
    b, sent, now = live_broker(monkeypatch, cash=10000)
    # Gross budget 2000. The first pending order consumes 1500.
    for symbol in ('AAPL', 'AMD'):
        run(b._send(PlaceOrder(order_type='limit', team_id='me', symbol=symbol, side='buy', price=100, quantity=15)))
    assert len(sent) == 1
    assert sent[0].symbol == 'AAPL'


def test_broker_equity_floor_still_allows_exits(monkeypatch):
    b, sent, now = live_broker(monkeypatch, cash=10000)
    b._on_portfolio(PortfolioUpdate(team_id='me', cash=6000, positions={'AAPL': 10},
                                   net_worth=7000, realized_pnl=-3000, unrealized_pnl=0, total_fees_paid=0))
    run(b._send(PlaceOrder(order_type='limit', team_id='me', symbol='AMD', side='buy', price=100, quantity=1)))
    run(b._send(PlaceOrder(order_type='limit', team_id='me', symbol='AAPL', side='sell', price=100, quantity=5)))
    assert [(m.side, m.symbol) for m in sent] == [('sell', 'AAPL')]


def test_broker_does_not_borrow_to_cover_short(monkeypatch):
    b, sent, now = live_broker(monkeypatch, cash=100, positions={'AAPL': -5})
    run(b._send(PlaceOrder(order_type='limit', team_id='me', symbol='AAPL', side='buy', price=100, quantity=5)))
    assert not sent


@pytest.mark.parametrize('cls', [MomentumTrader, MeanReversionTrader])
def test_scenario_limit_counts_pending_orders(cls):
    t, m, p, c = setup(cls)
    t.on_tick(m, p)
    t.on_event('SESSION_OPEN', '', {'scenario': {'position_limit': 30}})
    s = t._place('AAPL', 'buy', 100, 195, c.t, 'test', taker=False)
    assert s.quantity == 10
    assert t._place('AAPL', 'buy', 1, 195, c.t, 'test', taker=False) is None


def test_broker_cap_counts_unacked_orders(monkeypatch):
    b, sent, now = live_broker(monkeypatch)
    run(b._on_session_event(event('SESSION_OPEN', scenario={'position_limit':30})))
    for _ in range(2):
        run(b._send(PlaceOrder(order_type='limit', team_id='me', symbol='AAPL', side='buy', price=100, quantity=6)))
    assert len(sent) == 1


def test_cancelled_order_keeps_reservation_through_grace(monkeypatch):
    import broker.config as config
    b, sent, now = live_broker(monkeypatch, cash=10000)
    order = PlaceOrder(order_type='limit', team_id=config.TEAM_ID, symbol='AAPL', side='buy', price=100, quantity=15)
    run(b._send(order))
    b._on_order_ack(OrderAck(team_id=config.TEAM_ID, order_id='o1', symbol='AAPL',
                             side='buy', price=100, quantity=15))
    run(b._send(CancelOrder(team_id=config.TEAM_ID, order_id='o1', symbol='AAPL')))
    other = PlaceOrder(order_type='limit', team_id=config.TEAM_ID, symbol='AMD', side='buy', price=100, quantity=15)
    run(b._send(other))
    assert len(places(sent)) == 1
    now[0] += 1
    run(b._send(other))
    assert len(places(sent)) == 2


def test_three_team_seats_against_exchange_through_halt_and_adverse_move(monkeypatch):
    """Real team strategy/order gates + real exchange fills and maintenance.

    In-process transport only: no token, hosted venue or live orders.
    """
    import json
    from collections import deque
    import exchange.config as ec
    import trader.config as tc
    import broker.config as bc
    from exchange.server import ExchangeServer, Portfolio
    from exchange.scenario import load_scenario
    from pathlib import Path
    from shared.messages import parse_message, ErrorMsg, TradeExecution, BookSnapshot

    scenario = load_scenario(str(Path(__file__).resolve().parents[1] / 'season/week05.json'))
    scenario.apply()
    monkeypatch.setattr(ec, 'SEASON_PERSIST', False)
    srv = ExchangeServer()
    srv.session_open = True
    srv.scenario = scenario
    now = [1000.]
    ids = ['midway_traders_trader_1', 'midway_traders_trader_2', 'midway_traders_broker']
    traders = [MomentumTrader(clock=lambda: now[0]), MeanReversionTrader(clock=lambda: now[0])]
    gateways = [TeamBot(t) for t in traders]
    broker = TeamBrokerBot(MyBroker(), 'ws://local-test')
    broker._clock = lambda: now[0]
    async def advance(seconds):
        now[0] += seconds
    broker._sleep = advance
    gateways.append(broker)
    monkeypatch.setattr(bc, 'TEAM_ID', ids[2])
    queues = [deque() for _ in ids]
    transmitted = [0, 0, 0]
    rejected = []

    class Incoming:
        def __init__(self, i): self.i = i
        async def send(self, raw): queues[self.i].append(raw)

    class Outgoing:
        def __init__(self, i): self.i = i
        async def send(self, raw):
            msg = parse_message(json.loads(raw))
            if isinstance(msg, PlaceOrder):
                transmitted[self.i] += 1
                await srv._handle_place_order(clients[self.i], msg, ids[self.i])
            elif isinstance(msg, CancelOrder):
                await srv._handle_cancel_order(clients[self.i], msg, ids[self.i])

    clients = [Incoming(i) for i in range(3)]
    for i, name in enumerate(ids):
        srv.portfolios[name] = Portfolio(team_id=name, role='broker' if i == 2 else 'trader',
                                         level=6, cash=400000 if i == 2 else 300000)
        srv.clients[name] = clients[i]
        gateways[i]._ws = Outgoing(i)
        gateways[i]._session_open = True

    async def drain():
        for i, queue in enumerate(queues):
            if i < 2: monkeypatch.setattr(tc, 'TEAM_ID', ids[i])
            while queue:
                raw = queue.popleft()
                msg = parse_message(json.loads(raw))
                if isinstance(msg, ErrorMsg): rejected.append(msg.code)
                if i < 2:
                    bot = gateways[i]
                    bot._peek(raw)
                    if isinstance(msg, SessionEvent): await bot._on_session_event(msg)
                    elif isinstance(msg, TradeExecution): bot._on_trade(msg)
                    elif isinstance(msg, BookSnapshot): bot.market.update_book(msg)
                    elif isinstance(msg, PortfolioUpdate): bot.portfolio.apply_server_update(msg)
                else:
                    await broker._peek(raw)
                    if isinstance(msg, SessionEvent): await broker._on_session_event(msg)
                    elif isinstance(msg, TradeExecution): broker._on_trade(msg)
                    elif isinstance(msg, PortfolioUpdate): broker._on_portfolio(msg)
                    elif isinstance(msg, OrderAck): broker._on_order_ack(msg)
                    elif isinstance(msg, BookSnapshot): broker.state.exchange_prices[msg.symbol] = msg.ref_price or msg.mid_price

    async def broadcast(name, **data):
        await srv._broadcast(SessionEvent(event=name, message='', data=data))
        await drain()

    async def simulate():
        await broadcast('SESSION_OPEN', scenario={'position_limit':800,'flags':{'shorts_allowed':True}})
        for tick in range(90):
            now[0] += 1
            srv._refill_quotas()
            # A repeatable 12% adverse repricing and explicit halt/resume.
            if tick == 20:
                srv.circuit_breaker.halt_symbol('AAPL', 'test', duration=300)
                await broadcast('TRADING_HALT', symbol='AAPL', duration=300)
                before = list(transmitted)
            if tick == 25:
                assert transmitted == before
                srv.circuit_breaker.resume_symbol('AAPL')
                await broadcast('TRADING_RESUMED', symbol='AAPL')
            px = 100.0 if tick < 20 else 88.0
            srv.ref_prices['AAPL'] = px
            book = srv.books['AAPL']
            book.cancel_team_orders('house')
            book.place_order('house', 'buy', px - .05, 10000)
            book.place_order('house', 'sell', px + .05, 10000)
            for i, name in enumerate(ids):
                p = srv.portfolios[name]
                await clients[i].send(srv._make_snapshot('AAPL', book).model_dump_json())
                await clients[i].send(PortfolioUpdate(team_id=name, cash=p.cash,
                    positions=dict(p.positions), net_worth=p.net_worth(srv.ref_prices),
                    realized_pnl=p.realized_pnl, unrealized_pnl=0,
                    total_fees_paid=p.total_fees_paid).model_dump_json())
            await drain()
            for i, trader in enumerate(traders):
                monkeypatch.setattr(tc, 'TEAM_ID', ids[i])
                signal = trader.on_tick(gateways[i].market, gateways[i].portfolio)
                if signal: await gateways[i]._place_order(signal)
                for oid, symbol in trader.take_cancels():
                    await gateways[i]._send(CancelOrder(team_id=ids[i], order_id=oid, symbol=symbol))
                await drain()
            await broker.quote_symbol('AAPL')
            await drain()
            await srv._apply_carry_and_maintenance()
            for name in ids:
                p = srv.portfolios[name]
                assert not p.liquidated
                assert abs(p.positions.get('AAPL', 0)) <= 800
        assert all(n > 0 for n in transmitted)
        assert all(after > earlier for after, earlier in zip(transmitted, before))
        assert 'SYMBOL_HALTED' not in rejected
        assert 'POSITION_LIMIT' not in rejected
        assert 'LIQUIDATED' not in rejected

    try:
        run(simulate())
    finally:
        scenario.restore()


def test_unknown_unacked_broker_order_keeps_budget_reserved(monkeypatch):
    b, sent, now = live_broker(monkeypatch, cash=10000)
    run(b._send(PlaceOrder(order_type='limit', team_id='me', symbol='AAPL',
                          side='buy', price=100, quantity=15)))
    now[0] += 30
    run(b._send(PlaceOrder(order_type='limit', team_id='me', symbol='AMD',
                          side='buy', price=100, quantity=15)))
    assert len(sent) == 1


@pytest.mark.parametrize('cls', [MomentumTrader, MeanReversionTrader])
def test_halt_lifts_after_venue_duration_when_resume_is_missed(cls):
    t, m, p, c = setup(cls)
    t.on_tick(m, p)
    t.on_event('TRADING_HALT', '', {'symbol': 'AAPL', 'duration': 300})
    t.on_connect()  # the seat dropped and missed TRADING_RESUMED
    c.t += 299
    assert t._place('AAPL', 'buy', 5, 195, c.t, 'test', taker=False) is None
    c.t += 5
    assert t._place('AAPL', 'buy', 5, 195, c.t, 'test', taker=False) is not None


def test_broker_halt_lifts_after_venue_duration(monkeypatch):
    b, sent, now = live_broker(monkeypatch)
    run(b._on_session_event(event('TRADING_HALT', symbol='AAPL', duration=300)))
    now[0] += 299
    run(b.quote_symbol('AAPL'))
    assert not places(sent)
    now[0] += 5
    run(b.quote_symbol('AAPL'))
    assert places(sent)


def test_teacher_lift_resumes_halted_symbols():
    t, m, p, c = setup()
    t.on_tick(m, p)
    t.on_event('TRADING_HALT', '', {'symbol': 'AAPL'})
    t.on_event('MARKET_RESUMED', '', {'symbols': ['AAPL']})
    assert t._place('AAPL', 'buy', 5, 195, c.t, 'test', taker=False) is not None


def test_symbol_halted_reject_blocks_until_the_venue_resume_time():
    t, m, p, c = setup()
    t.on_tick(m, p)
    t._place('AAPL', 'buy', 5, 195, c.t, 'test', taker=False)
    t.on_reject('SYMBOL_HALTED', 'AAPL trading is halted. Resume in 120s')
    assert t.rejects['SYMBOL_HALTED'] == 1
    c.t += 100
    assert t._place('AAPL', 'buy', 5, 195, c.t, 'test', taker=False) is None
    c.t += 25
    assert t._place('AAPL', 'buy', 5, 195, c.t, 'test', taker=False) is not None


def test_broker_symbol_halted_reject_stops_quoting_the_name(monkeypatch):
    from shared.messages import ErrorMsg
    b, sent, now = live_broker(monkeypatch)
    run(b._on_error(ErrorMsg(code='SYMBOL_HALTED',
                             message='AAPL trading is halted. Resume in 60s')))
    run(b.quote_symbol('AAPL'))
    assert not places(sent)
    now[0] += 65
    run(b.quote_symbol('AAPL'))
    assert places(sent)


@pytest.mark.parametrize('cls', [MomentumTrader, MeanReversionTrader])
def test_reaction_bid_waits_out_the_halt_its_event_caused(cls):
    from tests.test_event_layer import PX, fire, run as tick
    t, m, p, c = setup(cls)
    fire(t, 0.062)
    t.on_event('TRADING_HALT', '', {'symbol': 'AAPL', 'duration': 300})
    assert tick(t, m, p, c, 300) == []
    t.on_event('TRADING_RESUMED', '', {'symbol': 'AAPL'})
    out = tick(t, m, p, c, 10)
    assert [(s.side, s.quantity) for _, s in out] == [('buy', 5)]
    assert out[0][0] < 2
    assert out[0][1].price <= PX * 1.062
