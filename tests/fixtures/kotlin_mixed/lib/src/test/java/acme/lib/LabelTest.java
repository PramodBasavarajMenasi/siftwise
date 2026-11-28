package acme.lib;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class LabelTest {
    @Test
    void brackets() {
        assertEquals("[a]", Label.of("a"));
    }
}
