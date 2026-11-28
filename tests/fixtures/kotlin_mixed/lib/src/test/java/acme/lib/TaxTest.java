package acme.lib;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class TaxTest {
    @Test
    void tenPercent() {
        assertEquals(110, Tax.apply(100));
    }
}
