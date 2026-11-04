import { Money } from './money';
import rates from './rates.json';
export const formatPrice = (m: Money) => `${(m.cents / 100 / rates.eur).toFixed(2)} €`;
