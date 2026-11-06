package acme.web;

import static acme.core.Rates.EUR;

public sealed interface Receipt permits Receipt.Line {
    String render();

    record Line(Cart cart) implements Receipt {
        @Override
        public String render() {
            String header = """
                RECEIPT "{currency}"
                """;
            char quote = '\'';
            return header + acme.core.text.Strings.pad(cart.total().cents() + " " + EUR, 10) + quote;
        }
    }
}
