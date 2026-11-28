package acme.lib

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class PriceTest {
    @Test
    fun taxed() {
        assertEquals(Price(110), Price(100).taxed())
    }
}
