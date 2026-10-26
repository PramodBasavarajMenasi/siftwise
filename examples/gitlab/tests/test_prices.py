import pytest

from shop.prices import price


def test_known_item() -> None:
    assert price("bread") == 250


def test_unknown_item() -> None:
    with pytest.raises(KeyError):
        price("caviar")
