package acme.lib

data class Price(val cents: Long) {
    fun taxed(): Price = Price(Tax.apply(cents))
}
