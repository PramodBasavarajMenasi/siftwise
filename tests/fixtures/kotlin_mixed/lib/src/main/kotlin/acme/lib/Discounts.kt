@file:JvmName("Discounts")

package acme.lib

fun discount(price: Price): Price = Price(price.cents - price.cents / 4)
