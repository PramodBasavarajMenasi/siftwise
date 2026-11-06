package acme.app;

import static org.junit.jupiter.api.Assertions.assertEquals;

import acme.core.Money;
import org.junit.jupiter.api.Test;

class CartTest {
    @Test
    void total() {
        assertEquals(new Money(5), new Cart().add(new Money(2)).add(new Money(3)).total());
    }
}
