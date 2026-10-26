"""Item prices, in cents."""

PRICES = {"apple": 50, "bread": 250, "milk": 120}


def price(item: str) -> int:
    return PRICES[item]
