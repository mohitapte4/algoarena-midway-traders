"""Book-validation regression tests.
AI: Codex assisted validation/tests.
"""
import pytest
from pydantic import ValidationError
from models import BookSnapshot


@pytest.mark.parametrize('level', [[101], [101, 5, 2], [-1, 5], [101, -1], [101, 0], [float('nan'), 5], [float('inf'), 5]])
def test_malformed_book_level_rejected(level):
    with pytest.raises(ValidationError):
        BookSnapshot(symbol='AAPL', bids=[level], asks=[], mid_price=101, spread=0)

