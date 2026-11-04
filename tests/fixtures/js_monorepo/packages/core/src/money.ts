export type Money = { cents: number };
export const add = (a: Money, b: Money): Money => ({ cents: a.cents + b.cents });
