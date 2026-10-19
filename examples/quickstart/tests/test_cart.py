from shop.cart import total


def test_total() -> None:
    assert total(["apple", "milk"]) == 170


def test_bulk_discount() -> None:
    assert total(["apple"] * 10) == 450
