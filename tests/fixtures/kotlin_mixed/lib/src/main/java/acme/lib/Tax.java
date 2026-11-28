package acme.lib;

public final class Tax {
    private Tax() {}

    public static long apply(long cents) {
        return cents + cents / 10;
    }
}
