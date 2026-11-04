import { Price } from '@web/components/Price';
jest.mock('~/cart');
test('renders', () => expect(<Price cents={100} />).toBeTruthy());
