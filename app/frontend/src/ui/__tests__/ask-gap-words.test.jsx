import {expect, test} from 'bun:test';
import {plainGapWhat} from '../../ask42.jsx';

/* A gap line is read by clients: no check rule code, item index or
   warehouse table name may show, while hashtags and handles stay as written. */
test('gap lines never show a rule code, an item index or a table name', () => {
  const shown = [
    plainGapWhat('watch_next item 0 removed: it forecast before 42 can score forecasts (K9)'),
    plainGapWhat('Cross-referenced item_daily, rising_topics, and cultural_map tables'),
  ];
  for (const line of shown){
    expect(line).not.toMatch(/\(K\d/);
    expect(line).not.toMatch(/\bitem \d/);
    expect(line).not.toMatch(/[a-z]_[a-z]/);
  }
  expect(shown[0]).toBe('A watch-next line was removed: it forecast before 42 can score forecasts');
});

test('hashtags and handles in a gap line keep their underscores', () => {
  expect(plainGapWhat('No #fixture_za_step posts from @some_user on Instagram')).toBe('No #fixture_za_step posts from @some_user on Instagram');
});
