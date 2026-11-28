package acme.app

import acme.core.Money
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class CartTest {
    @Test
    fun total() {
        assertEquals(Money(3), Cart(listOf(Money(1), Money(2))).total())
    }

    @Test
    fun `in euro`() {
        assertEquals(1.5, Cart(listOf(Money(3))).inEuro())
    }
}
