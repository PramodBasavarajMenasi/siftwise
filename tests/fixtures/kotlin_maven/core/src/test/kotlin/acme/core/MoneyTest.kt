package acme.core

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class MoneyTest {
    @Test
    fun `adds two amounts`() {
        assertEquals(Money(3), Money(1) + Money(2))
    }

    @Test
    fun zero() {
        assertEquals(0L, Money.ZERO.cents)
    }
}

// A second test class in the file, named after Rates.kt.
class RatesTest {
    @Test
    fun euro() {
        assertEquals(0.5, rate("EUR"))
    }
}
