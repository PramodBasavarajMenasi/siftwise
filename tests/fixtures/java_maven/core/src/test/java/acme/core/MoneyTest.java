package acme.core;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class MoneyTest {
    @Test
    void plus() {
        assertEquals(new Money(3), new Money(1).plus(new Money(2)));
    }
}
