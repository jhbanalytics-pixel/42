/* The accounts words and the zero guard, as pure functions. The six shapes
   below are the probes of the Opus review of the Today branch: each one made
   the first version change a value or hide a card for the wrong figure. */
import {expect, test} from 'bun:test';
import {accountsCard, accountsFigure, accountsWords, countedCreators} from '../accounts.js';

const fig = (value, unit) => ({value, unit, query_id: 'q_' + String(unit).replace(/\W+/g, '_')});

test('probe 1: a 7-day creators count in a stored line is never relabelled as a 3-day accounts count', () => {
  const card = {
    count_line: '31 creators in 7 days, 12 posts in 3 days',
    numbers: [fig(12, 'creators in 3 days'), fig(40, 'posts in 3 days')],
  };
  const out = accountsCard(card);
  expect(out.count_line).toBeNull();
  expect(JSON.stringify(out)).not.toContain('accounts posting in 7 days');
  expect(out.numbers.map((n) => [n.value, n.unit])).toEqual([[12, 'accounts posting, last 3 days'], [40, 'posts in 3 days']]);
});

test('probe 2: a stored line is never spliced onto the measured accounts beside a different posts figure', () => {
  const card = {
    count_line: '12 creators and 20 posts in 3 days',
    numbers: [fig(17, 'creators in 3 days'), fig(40, 'posts in 3 days')],
  };
  const out = accountsCard(card);
  expect(out.count_line).toBeNull();
  expect(JSON.stringify(out)).not.toContain('17 accounts posting and 20 posts');
});

test('a stored line is dropped only when it carries a number and a figure was measured; the stored card is not changed', () => {
  const numbers = [fig(17, 'creators in 3 days')];
  expect(accountsCard({count_line: 'Rising across the week', numbers}).count_line).toBe('Rising across the week');
  expect(accountsCard({count_line: '5 creators, 3 days', numbers: []}).count_line).toBe('5 creators, 3 days');
  const stored = {count_line: '1 creator, 3 days', numbers: [fig(1, 'creators in 3 days')]};
  const before = JSON.stringify(stored);
  expect(accountsCard(stored).count_line).toBeNull();
  expect(JSON.stringify(stored)).toBe(before);
});

test('probe 3: a zero in the reach figure with another unit does not hide a card that measured 17 accounts', () => {
  expect(countedCreators({reach: fig(0, 'views in 3 days'), numbers: [fig(17, 'creators in 3 days')]})).toBe(17);
  expect(countedCreators({reach: fig(0, 'creators in 7 days'), numbers: [fig(17, 'creators in 3 days')]})).toBe(17);
});

test('probe 4: a 7-day zero placed before the 3-day figure does not hide the card', () => {
  expect(countedCreators({numbers: [fig(0, 'creators in 7 days'), fig(9, 'creators in 3 days')]})).toBe(9);
  expect(countedCreators({numbers: [fig(0, 'accounts posting, last 7 days'), fig(9, 'accounts posting, last 3 days')]})).toBe(9);
});

test('probe 5: an empty or blank string value is not a zero', () => {
  expect(countedCreators({reach: {value: '', unit: 'creators in 3 days'}, numbers: []})).toBeNull();
  expect(countedCreators({numbers: [{value: ' ', unit: 'creators in 3 days'}]})).toBeNull();
});

test('probe 6: false and a string zero are not a zero either, and a measured number zero is', () => {
  expect(countedCreators({numbers: [{value: false, unit: 'creators in 3 days'}]})).toBeNull();
  expect(countedCreators({numbers: [{value: '0', unit: 'creators in 3 days'}]})).toBeNull();
  expect(countedCreators({numbers: [fig(0, 'creators in 3 days')]})).toBe(0);
  expect(countedCreators({reach: fig(0, 'accounts posting, last 3 days'), numbers: []})).toBe(0);
  expect(countedCreators(null)).toBeNull();
});

test('only the words of a figure change, never its value, and the one-account wording is singular', () => {
  expect(accountsFigure(fig(1, 'creators in 3 days')).unit).toBe('account posting, last 3 days');
  expect(accountsFigure(fig(31, 'creators in 7 days')).unit).toBe('accounts posting, last 7 days');
  expect(accountsFigure(fig(31, 'creators in 7 days')).value).toBe(31);
  expect(accountsWords('posted by 31 creators in 3 days')).toBe('posted by 31 accounts in 3 days');
});
