package acme.web;

import acme.core.*;
import java.util.ArrayList;
import java.util.List;

/* A Cart holds Money; "Receipt" in this comment or a string isn't a reference. */
public class Cart {
    private final List<Money> items = new ArrayList<>();

    public Cart add(Money money) {
        items.add(money);
        return this;
    }

    public Money total() {
        Money sum = Money.zero();
        for (Money m : items) {
            sum = sum.plus(m);
        }
        return sum;
    }
}
