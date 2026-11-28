package acme.lib;

public final class Invoice {
    private final Price price;

    public Invoice(long cents) {
        this.price = Discounts.discount(new Price(cents));
    }

    public long total() {
        return price.taxed().getCents();
    }
}
