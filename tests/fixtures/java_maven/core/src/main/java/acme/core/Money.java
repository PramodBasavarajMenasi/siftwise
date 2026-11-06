package acme.core;

/** Money in cents. */
public record Money(long cents) {
    public static Money zero() {
        return new Money(0);
    }

    public Money plus(Money other) {
        return new Money(cents + other.cents);
    }
}
