/* Visual QA, 5 October 2026: reader words for units, board titles and the
   record ids a gap's search can name. */
import {expect, test} from 'bun:test';
import {boardTitle, plainSearched, plainUnit, unitFor} from '../../readerUnits.js';

test('a count of one takes the singular of its leading noun only', () => {
  expect(unitFor(1, 'posts first seen in 7 days')).toBe('post first seen in 7 days');
  expect(unitFor(1, 'posts a day')).toBe('post a day');
  expect(unitFor(2, 'posts a day')).toBe('posts a day');
  expect(unitFor(0, 'posts')).toBe('posts');
  expect(unitFor(1, 'Creators')).toBe('Creator');
  expect(unitFor(1, 'share of posts')).toBe('share of posts');
  expect(unitFor(1, '')).toBe('');
});

test('a warehouse unit reads as words', () => {
  expect(plainUnit('located_posts')).toBe('posts with a known location');
  expect(plainUnit('located_creators')).toBe('creators with a known location');
  expect(plainUnit('reach_per_post')).toBe('reach per post');
  expect(plainUnit('posts tagged #fixture_za_step')).toBe('posts tagged #fixture_za_step');
});

test('a board title loses its <br> tags and keeps its parts', () => {
  expect(boardTitle('<br>Gratitude<br>Asake')).toBe('Gratitude · Asake');
  expect(boardTitle('TEA<BR />Rema')).toBe('TEA · Rema');
  expect(boardTitle('#amapiano')).toBe('#amapiano');
  expect(boardTitle('<br><br>')).toBe('');
});

test('record ids in a gap search become a count', () => {
  expect(plainSearched('cited posts obs1_7bfd7de8e71252031cc34ad053aee005, obs1_66faeb419bbd118deb6bbaecfed8f213, obs1_43aa2bbce7f6b8f603323811e6f31a95')).toBe('3 cited posts');
  expect(plainSearched('cited posts obs1_7c5bbc4782cb6b3b0567d5670c39d603')).toBe('1 cited post');
  expect(plainSearched('the what to watch next text')).toBe('the what to watch next text');
});
