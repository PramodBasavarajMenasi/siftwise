# Quickstart example

A tiny project for the siftwise [Quickstart](../../README.md#quickstart): `shop/cart.py` uses
`shop/prices.py`, and each has its own test file. Change `cart.py` and siftwise selects
`tests/test_cart.py` only; change `prices.py` and it selects both (the cart imports prices).
