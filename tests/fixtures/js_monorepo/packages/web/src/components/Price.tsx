import { formatPrice } from '@shop/core';
import './price.css';
export const Price = ({ cents }: { cents: number }) => <span title="Don't round">{formatPrice({ cents })}</span>;
