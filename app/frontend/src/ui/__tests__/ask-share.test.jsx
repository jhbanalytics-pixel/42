import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {allowanceText, shareableRequestHash, storedWindowText} from '../../askPresentation.js';
import {buildWorkbenchHash, parseWorkbenchRoute} from '../../workbenchRoute.js';
import {intelligenceFixture} from './fixtures/general-intelligence.js';

const id = '00000000-0000-4000-8000-000000000001';

test('a validated reply puts its request id in the address under general 42', () => {
  expect(shareableRequestHash(intelligenceFixture(), '')).toBe(`#/console?work=ask&request=${id}`);
});

test('no address is written for a lensed ask, a withheld reply or a malformed id', () => {
  expect(shareableRequestHash(intelligenceFixture(), 'bsa_pulse_lens')).toBeNull();
  const withheld = intelligenceFixture(); withheld.receipts[0].market = 'ng';
  expect(shareableRequestHash(withheld, '')).toBeNull();
  const upper = intelligenceFixture(); upper.request_id = 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA';
  expect(shareableRequestHash(upper, '')).toBeNull();
  expect(shareableRequestHash(null, '')).toBeNull();
});

test('choosing a lens from a shared request address stays on the live ask', async () => {
  const {lensRoutePath} = await import('../../chat.jsx');
  const shared = parseWorkbenchRoute(buildWorkbenchHash({work: 'ask', requestId: id}));
  const after = parseWorkbenchRoute('#' + lensRoutePath(shared, 'bsa_pulse_lens'));
  expect(after.requestId).toBeUndefined();
  expect(after.error).toBeUndefined();
  expect(after.work).toBe('ask');
  expect(after.clientLensId).toBe('bsa_pulse_lens');
});

test('the stored allowance reads as money', () => {
  expect(allowanceText(100000)).toBe('US$0.10');
  expect(allowanceText(500)).toBe('US$0.0005');
  expect(allowanceText(1234567)).toBe('US$1.234567');
  expect(allowanceText(0)).toBe('US$0.00');
  expect(allowanceText(2500000)).toBe('US$2.50');
});

/* Ask redesign, 23 Sept 2026: windows read as a reader says them, the day
   without a leading zero and the month and year named once. */
test('a stored request without its own window names the one the question resolved to', () => {
  const window = {start: '2026-08-25', end: '2026-09-07', closed: true};
  /* Round three, 24 Sept 2026: the two sides disagreed on this sentence. The
     Ask copy fix says only what the reply covers when no window was sent,
     since the page cannot tell whether the wording or the default chose it,
     so its words are kept; the dates read as the redesign sets them. */
  expect(storedWindowText(null, window)).toBe('Requested window: none was sent with the question. The reply covers 25 Aug to 7 Sept 2026.');
  expect(storedWindowText({start: '2026-08-01', end: '2026-08-31'}, window)).toBe('Requested window: 1 to 31 Aug 2026.');
  expect(storedWindowText(null, null)).toBe('Requested window: Unspecified.');
  /* A refused reply covers nothing, so it names only what the question
     resolved to. Round three, 24 Sept 2026: dates read as the redesign sets
     them. */
  expect(storedWindowText(null, window, {answered: false, fromPlan: true})).toBe('Requested window: none was sent with the question. It resolved to 25 Aug to 7 Sept 2026.');
});

/* A reply written before any plan carries the engine's default window: the
   fourteen days before as_of, not what the question resolved to. */
test('a stored failure with no plan names no resolved window', () => {
  const window = {start: '2026-09-09', end: '2026-09-22', closed: true};
  expect(storedWindowText(null, window, {answered: false, fromPlan: false})).toBe('Requested window: none was sent with the question.');
  expect(storedWindowText(null, window, {answered: false})).toBe('Requested window: none was sent with the question.');
  /* Round three, 24 Sept 2026: dates read as the redesign sets them. */
  expect(storedWindowText(null, window, {answered: false, fromPlan: true})).toBe('Requested window: none was sent with the question. It resolved to 9 to 22 Sept 2026.');
});

test('the stored view says a refused reply covers nothing', () => {
  const chat = readFileSync(new URL('../../chat.jsx', import.meta.url), 'utf8');
  expect(chat).toMatch(/storedWindowText\(value\.requested_window, resolved\?\.window \|\| null, \{answered: \['complete', 'partial'\]\.includes\(resolved\?\.status\), fromPlan: value\.window_from_plan\}\)/);
});
