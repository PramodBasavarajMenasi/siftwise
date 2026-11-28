package acme.lib;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class InvoiceTest {
    @Test
    void total() {
        assertEquals(82, new Invoice(100).total());
    }
}
