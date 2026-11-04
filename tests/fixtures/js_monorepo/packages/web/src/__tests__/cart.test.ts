import { total } from '../cart';
test('total', () => expect(total([]).cents).toBe(0));
