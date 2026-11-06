package acme.web;

import static org.junit.jupiter.api.Assertions.assertTrue;

import acme.core.Money;
import org.junit.jupiter.api.Test;

class ReceiptTest {
    @Test
    void renders() {
        assertTrue(new Receipt.Line(new Cart().add(new Money(7))).render().contains("7 EUR"));
    }
}
