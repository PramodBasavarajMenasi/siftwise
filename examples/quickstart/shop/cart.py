"""A shopping cart: totals with a bulk discount."""

from shop.prices import price


def total(items: list[str]) -> int:
    cents = sum(price(item) for item in items)
    if len(items) >= 10:
        cents = cents * 9 // 10  # 10% off 10 items or more
    return cents
