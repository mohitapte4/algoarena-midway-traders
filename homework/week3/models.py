from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, PositiveFloat

NonEmptyStr = Annotated[str, Field(min_length=1)]
class ErrorMsg(Enum):
    UNION_TAG_INVALID = "union_tag_invalid"
    MISSING = "missing"
    INT_PARSING = "int_parsing"
    GREATER_THAN = "greater_than"


class PlaceOrder(BaseModel):
    type: Literal["place_order"] = "place_order"
    symbol: NonEmptyStr
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    price: PositiveFloat | None = None     # None => market order

class OrderAck(BaseModel):
    type: Literal["order_ack"] = "order_ack"
    order_id: NonEmptyStr
    team_id: NonEmptyStr
    symbol: NonEmptyStr
    side: Literal["buy", "sell"]
    price: PositiveFloat
    quantity: int = Field(ge=0)

class BookSnapshot(BaseModel):
    type: Literal["book_snapshot"] = "book_snapshot"
    symbol: NonEmptyStr
    bids: list[list[float]]   # [[price, qty], ...] best (highest) first
    asks: list[list[float]]   # [[price, qty], ...] best (lowest) first
    mid_price: float = Field(gt=0.0)
    spread: float = Field(ge=0.0)   # 0.0 when the book is one-sided

class Handshake(BaseModel):
    type: Literal["handshake"] = "handshake"
    team_id: NonEmptyStr
    role: Literal["broker", "trader", "observer", "teacher"]
    level: int = Field(ge=1)
    token: str = ""           # empty for local play

Message = Annotated[Union[Handshake, PlaceOrder, OrderAck, BookSnapshot],
                    Field(discriminator="type")]
