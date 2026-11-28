package acme.core

data class Money(val cents: Long) {
    companion object {
        val ZERO = Money(0)
    }
}

// `a + b` calls this without naming it: every file of the package (and its importers) links.
operator fun Money.plus(other: Money): Money = Money(cents + other.cents)
