package acme.app

import acme.core.Money
import acme.core.plus
import acme.core.rate as exchangeRate

class Cart(private val items: List<Money>) {
    fun total(): Money = items.fold(Money.ZERO) { sum, m -> sum + m }

    fun inEuro(): Double = total().cents * exchangeRate("EUR")
}
