"""
Every wire model survives a JSON round trip unchanged:

    m == Model.model_validate_json(m.model_dump_json())

Run with:  pytest test_roundtrip.py -v
"""

import pytest
from models import BookSnapshot, Handshake, OrderAck, PlaceOrder
from pydantic import BaseModel

SAMPLES: list[BaseModel] = [
    Handshake(team_id="trader-1", role="trader", level=1),
    Handshake(team_id="broker-1", role="broker", level=3, token="tok"),
    Handshake(team_id="obs-1", role="observer", level=1),
    PlaceOrder(symbol="AAPL", side="buy", quantity=10, price=187.25),
    PlaceOrder(symbol="MSFT", side="sell", quantity=1),  # market order, price=None
    OrderAck(order_id="ord-42", team_id="trader-1", symbol="AAPL", side="buy", price=187.25, quantity=10),
    OrderAck(order_id="ord-43", team_id="trader-1", symbol="MSFT", side="sell", price=410.0, quantity=0),
    BookSnapshot(
        symbol="AAPL",
        bids=[[187.20, 300], [187.10, 150]],
        asks=[[187.30, 200], [187.40, 50]],
        mid_price=187.25,
        spread=0.10,
    ),
    BookSnapshot(symbol="EMPTY", bids=[], asks=[], mid_price=100.0, spread=0.0),
]


@pytest.mark.parametrize("m", SAMPLES, ids=lambda m: f"{type(m).__name__}")
def test_round_trip(m: BaseModel):
    model = type(m)
    assert m == model.model_validate_json(m.model_dump_json())


def test_every_model_is_covered():
    covered = {type(m) for m in SAMPLES}
    assert covered == {Handshake, PlaceOrder, OrderAck, BookSnapshot}


def test_book_levels_come_back_as_float_lists():
    # Levels are list[float], so integer quantities are stored as floats and
    # must still be [price, qty] lists of floats after the round trip.
    snap = SAMPLES[7]
    back = BookSnapshot.model_validate_json(snap.model_dump_json())
    assert back.bids == [[187.20, 300.0], [187.10, 150.0]]
    assert all(isinstance(x, float) for level in back.bids + back.asks for x in level)
    assert back.mid_price == 187.25
