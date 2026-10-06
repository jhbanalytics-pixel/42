/* countOf writes a count with its noun in the right number, so the topic page
   reads "1 post" and "2 posts" rather than "1 posts". */
import {describe, expect, test} from 'bun:test';
import {countOf} from '../../model.js';

describe('countOf agrees the noun with the count', () => {
  test('one takes the singular', () => {
    expect(countOf(1, 'post')).toBe('1 post');
    expect(countOf(1, 'source')).toBe('1 source');
  });

  test('zero and more take the plural', () => {
    expect(countOf(0, 'post')).toBe('0 posts');
    expect(countOf(2, 'post')).toBe('2 posts');
    expect(countOf(42, 'source')).toBe('42 sources');
  });

  test('an irregular plural can be named', () => {
    expect(countOf(3, 'reply', 'replies')).toBe('3 replies');
    expect(countOf(1, 'reply', 'replies')).toBe('1 reply');
  });
});
