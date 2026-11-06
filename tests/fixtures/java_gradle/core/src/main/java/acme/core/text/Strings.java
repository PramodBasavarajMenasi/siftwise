package acme.core.text;

public final class Strings {
    private Strings() {}

    public static String pad(String s, int width) {
        return " ".repeat(Math.max(0, width - s.length())) + s;
    }
}
