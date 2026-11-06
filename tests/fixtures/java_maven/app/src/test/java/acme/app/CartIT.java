package acme.app;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CartIT {
    @Test
    void emptyCart() {
        assertEquals(0, new Cart().total().cents());
    }
}
