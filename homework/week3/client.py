"""
Connect, send a Handshake, then for 30 s parse every inbound frame through
the Message union and pretty-print each BookSnapshot as JSON, trimmed to the
top 5 levels; mid_price and spread come from the snapshot itself.

Run with:  python client.py    (EXCHANGE_URL defaults to the local arena, ws://localhost:8765)
"""

import asyncio
import os

from models import BookSnapshot, ErrorMsg, Handshake, Message
from pydantic import TypeAdapter, ValidationError
from websockets.asyncio.client import connect

URL = os.environ.get("EXCHANGE_URL", "ws://localhost:8765")
adapter = TypeAdapter(Message)


async def main() -> None:
    hello = Handshake(team_id=os.environ.get("TEAM_ID", "hw3-observer"), role="observer",
                      level=1, token=os.environ.get("ARENA_TOKEN", ""))
    async with connect(URL) as ws:
        await ws.send(hello.model_dump_json())
        try:
            async with asyncio.timeout(30):
                async for frame in ws:
                    try:
                        msg = adapter.validate_json(frame)
                    except ValidationError as e:
                        # The venue sends many message kinds; skip tags outside our union.
                        if e.errors()[0]["type"] != ErrorMsg.UNION_TAG_INVALID.value:
                            print(f"rejected frame: {e.errors()[0]['msg']}")
                        continue
                    if isinstance(msg, BookSnapshot):
                        top5 = msg.model_copy(update={"bids": msg.bids[:5], "asks": msg.asks[:5]})
                        print(top5.model_dump_json(indent=2))
        except TimeoutError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
