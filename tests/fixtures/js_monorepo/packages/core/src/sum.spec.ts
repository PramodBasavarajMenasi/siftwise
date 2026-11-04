import { add } from './money';
test('add', () => expect(add({ cents: 1 }, { cents: 2 }).cents).toBe(3));
