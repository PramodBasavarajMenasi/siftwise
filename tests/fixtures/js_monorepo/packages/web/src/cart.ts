import { add, type Money } from '@shop/core';
import { newId } from '@shared/ids';
export const total = (items: Money[]) => items.reduce(add, { cents: 0 });
export const cartId = newId;
