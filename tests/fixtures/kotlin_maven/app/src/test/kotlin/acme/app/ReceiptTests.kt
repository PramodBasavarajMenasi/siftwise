package acme.app

import acme.core.Money
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class ReceiptTest {
    @Test
    fun shouts() {
        assertEquals("TOTAL 1!", receipt(Cart(listOf(Money(1)))))
    }
}
