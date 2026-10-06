/* Demo polish, 2 October 2026: an Ask figure read "312 TikTok posts tagged
   #fixture in 7 days. Query q_fixture_tt_tag_7d" on the page. The query
   still travels with the number, as its data attribute and hover title, but
   the sentence a reader sees is the figure alone. */
import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {FigureLine} from '../FigureLine.jsx';

test('a figure line shows the figure and carries its query without printing the id', () => {
  const html = renderToStaticMarkup(<FigureLine number={{value: 312, unit: 'TikTok posts tagged #fixture in 7 days', query_id: 'q_fixture_tt_tag_7d'}} />);
  const text = html.replace(/<[^>]+>/g, '');
  expect(text).toBe('312 TikTok posts tagged #fixture in 7 days.');
  expect(html).toContain('data-query-id="q_fixture_tt_tag_7d"');
  expect(html).toContain('title="From query q_fixture_tt_tag_7d"');
});

test('a figure line with no number renders nothing', () => {
  expect(renderToStaticMarkup(<FigureLine number={{value: 'x'}} />)).toBe('');
});
