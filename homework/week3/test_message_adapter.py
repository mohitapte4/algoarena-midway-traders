"""
Inbound payloads parsed through pydantic.TypeAdapter(Message): a valid one is
routed to the right class by its `type` tag, and a malformed one must raise
ValidationError with the expected error code and location.

Run with:  pytest test_message_adapter.py -v
"""

import pytest
from models import (BookSnapshot, ErrorMsg, Handshake, Message, OrderAck,
                    PlaceOrder)
from pydantic import TypeAdapter, ValidationError

adapter = TypeAdapter(Message)

@pytest.mark.parametrize(
    "raw, expected_cls",
    [
        ('{"type": "handshake", "team_id": "t1", "role": "trader", "level": 1}', Handshake),
        ('{"type": "place_order", "symbol": "AAPL", "side": "buy", "quantity": 5}', PlaceOrder),
        ('{"type": "order_ack", "order_id": "o1", "team_id": "t1", "symbol": "AAPL", "side": "buy",'
         ' "price": 101.5, "quantity": 5}', OrderAck),
        ('{"type": "book_snapshot", "symbol": "AAPL", "bids": [[101.4, 100]],'
         ' "asks": [[101.6, 200]], "mid_price": 101.5, "spread": 0.2}', BookSnapshot),
    ],
    ids=lambda v: v.__name__ if isinstance(v, type) else "",
)
def test_dispatch_by_type_tag(raw: str, expected_cls: type):
    assert type(adapter.validate_json(raw)) is expected_cls


def assert_single_error(exc: pytest.ExceptionInfo, kind: ErrorMsg, loc: tuple):
    errors = exc.value.errors()
    assert len(errors) == 1
    assert errors[0]["type"] == kind.value
    assert errors[0]["loc"] == loc


def test_unknown_type_tag_is_rejected():
    with pytest.raises(ValidationError) as exc:
        adapter.validate_json('{"type": "cancel_order", "order_id": "o1"}')
    assert_single_error(exc, ErrorMsg.UNION_TAG_INVALID, ())


def test_missing_field_is_rejected():
    with pytest.raises(ValidationError) as exc:
        adapter.validate_json('{"type": "place_order", "side": "buy", "quantity": 5}')
    assert_single_error(exc, ErrorMsg.MISSING, ("place_order", "symbol"))


def test_wrong_type_is_rejected():
    with pytest.raises(ValidationError) as exc:
        adapter.validate_json(
            '{"type": "place_order", "symbol": "AAPL", "side": "buy", "quantity": "ten"}'
        )
    assert_single_error(exc, ErrorMsg.INT_PARSING, ("place_order", "quantity"))


def test_zero_quantity_is_rejected():
    with pytest.raises(ValidationError) as exc:
        adapter.validate_json(
            '{"type": "place_order", "symbol": "AAPL", "side": "buy", "quantity": 0}'
        )
    assert_single_error(exc, ErrorMsg.GREATER_THAN, ("place_order", "quantity"))
