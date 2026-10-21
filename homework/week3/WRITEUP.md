# Week 3: pydantic message schemas

Run `pytest` for the round-trip tests (`test_roundtrip.py`) and the dispatch and negative tests (`test_message_adapter.py`). With the arena exchange running (`make exchange`), run `python client.py` to stream its books for 30 s; set `EXCHANGE_URL` to point it at another venue.

## Schema versioning

Schema versioning means treating the models as a wire contract between processes that upgrade at different times (the exchange, brokers and every team's bots), so each change must keep old and new peers talking. To add a field without breaking older clients, give it a default, as the arena did with `queue_ahead: int = 0` on `OrderAck`. Old senders that omit it still validate, and old receivers ignore it, because pydantic's default `extra="ignore"` drops unknown keys.

Renaming or removing a field, tightening a constraint or adding a required field breaks old clients, and new message kinds get a new `type` tag that old readers skip. The discriminated union reads the tag once, validates the payload against exactly one model, and returns a typed object whose errors name the exact tag and field, such as `("place_order", "quantity")`. With `isinstance` on dicts every payload is just a `dict`, so the code has to check `d["type"]` by hand and trust the keys and types, and mistakes surface later as a `KeyError` or a silently wrong value.

## Test coverage

`pytest -v --cov=. --cov-report=term-missing` (needs `pytest-cov`), Python 3.13 on macOS:

```
test_message_adapter.py::test_dispatch_by_type_tag[-Handshake] PASSED    [  5%]
test_message_adapter.py::test_dispatch_by_type_tag[-PlaceOrder] PASSED   [ 10%]
test_message_adapter.py::test_dispatch_by_type_tag[-OrderAck] PASSED     [ 15%]
test_message_adapter.py::test_dispatch_by_type_tag[-BookSnapshot] PASSED [ 21%]
test_message_adapter.py::test_unknown_type_tag_is_rejected PASSED        [ 26%]
test_message_adapter.py::test_missing_field_is_rejected PASSED           [ 31%]
test_message_adapter.py::test_wrong_type_is_rejected PASSED              [ 36%]
test_message_adapter.py::test_zero_quantity_is_rejected PASSED           [ 42%]
test_roundtrip.py::test_round_trip[Handshake0] PASSED                    [ 47%]
test_roundtrip.py::test_round_trip[Handshake1] PASSED                    [ 52%]
test_roundtrip.py::test_round_trip[Handshake2] PASSED                    [ 57%]
test_roundtrip.py::test_round_trip[PlaceOrder0] PASSED                   [ 63%]
test_roundtrip.py::test_round_trip[PlaceOrder1] PASSED                   [ 68%]
test_roundtrip.py::test_round_trip[OrderAck0] PASSED                     [ 73%]
test_roundtrip.py::test_round_trip[OrderAck1] PASSED                     [ 78%]
test_roundtrip.py::test_round_trip[BookSnapshot0] PASSED                 [ 84%]
test_roundtrip.py::test_round_trip[BookSnapshot1] PASSED                 [ 89%]
test_roundtrip.py::test_every_model_is_covered PASSED                    [ 94%]
test_roundtrip.py::test_book_levels_come_back_as_float_lists PASSED      [100%]

Name                      Stmts   Miss  Cover   Missing
-------------------------------------------------------
client.py                    27     27     0%   9-43
models.py                    37      0   100%
test_message_adapter.py      28      0   100%
test_roundtrip.py            17      0   100%
-------------------------------------------------------
TOTAL                       109     27    75%
============================== 19 passed in 0.11s ==============================
```

`models.py` is fully covered. `client.py` has no unit tests; it is checked by the live run below.

## Client against the exchange

Setup: the arena exchange (`python -m exchange.server`) running locally on port 8799 with auth off, the session opened with the `open_session` teacher command, and a seat resting 7 limit orders a side on AAPL and MSFT. The orders came from a throwaway script, because the team broker's Yahoo Finance feed returned no prices from this machine, so it never quoted. Then:

```
EXCHANGE_URL=ws://localhost:8799 python client.py
```

The client exited cleanly after 30 s, having parsed 659 book snapshots with 0 rejected frames. Frames of other kinds (portfolio updates, leaderboards, session events) were skipped as tags outside the union. One AAPL snapshot, trimmed to the top 5 of its 7 levels:

```
{
  "type": "book_snapshot",
  "symbol": "AAPL",
  "bids": [
    [
      219.65,
      10.0
    ],
    [
      219.6,
      20.0
    ],
    [
      219.55,
      30.0
    ],
    [
      219.5,
      40.0
    ],
    [
      219.45,
      50.0
    ]
  ],
  "asks": [
    [
      219.75,
      10.0
    ],
    [
      219.8,
      20.0
    ],
    [
      219.85,
      30.0
    ],
    [
      219.9,
      40.0
    ],
    [
      219.95,
      50.0
    ]
  ],
  "mid_price": 219.7,
  "spread": 0.09999999999999432
}
```
