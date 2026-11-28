package acme.app

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class BannerTest {
    @Test
    fun text() {
        assertEquals("welcome", Banner.text())
    }
}
