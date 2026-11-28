package acme.app

import acme.core.text.shout

fun receipt(cart: Cart): String = "total ${cart.total().cents}".shout()
