"""Model helpers that involve actual parsing."""

from grocery.models import parse_price


def test_parse_price_variants():
    assert parse_price("11.70") == 11.7
    assert parse_price("CHF 11.70") == 11.7
    assert parse_price("1,95") == 1.95


def test_parse_price_rejects_garbage():
    assert parse_price(None) is None
    assert parse_price("") is None
    assert parse_price("Aktion") is None
