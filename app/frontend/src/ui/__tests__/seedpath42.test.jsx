import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {SeedPathResult, suggestionTerms, normaliseTerm, validSeedPathPayload} from '../../seedpath.jsx';

const fig = (value, unit) => ({value, unit, query_id: 'q_seed_path', run_id: 'r1', result_hash: 'sha256:x'});
const points = (counts) => counts.map((posts, i) => ({date: '2026-09-0' + (i + 1), posts}));
const platform = (id, name, first, counts, before = false) => ({
  platform: id, name, first_seen: fig(first, 'first sighting day'), before_window: before,
  posts_all_time: fig(counts.reduce((a, b) => a + b, 0), 'posts'), posts_in_window: fig(counts.reduce((a, b) => a + b, 0), 'posts'),
  daily: {unit: 'matching posts a day', query_id: 'q_seed_path', run_id: 'r1', points: points(counts)},
});
const data = {
  keyword: 'amapiano', market: 'ZA', status: 'ok', message: null, query_id: 'q_seed_path', run_id: 'r1',
  window: {from: '2026-09-01', to: '2026-09-03', days: 3}, matched_posts: fig(5, 'posts'),
  platforms: [platform('tiktok', 'TikTok', '2026-09-01', [2, 0, 1]), platform('x', 'X', '2026-09-03', [0, 0, 2])],
  side_words: [{word: 'braai', posts: fig(3, 'posts')}, {word: '#soweto', posts: fig(2, 'posts')}], side_words_min_posts: 2,
  topics: [{item_id: 'abc', title: '#fixture_za_step', kind: 'hashtag', state_word: 'Emerging', held_back: false, posts: fig(1, 'posts')}],
  truncated: false, notes: ['Matched as a whole word or hashtag in post text and video transcripts, so it may include unrelated uses of the same word.'],
};

test('the trace reads as a sentence with every number carrying its query', () => {
  expect(validSeedPathPayload(data, 'amapiano', 'za')).toBe(true);
  const html = renderToStaticMarkup(<SeedPathResult data={data} market="za" onTrace={() => {}} />);
  expect(html).toContain('amapiano');
  const words = html.replace(/<[^>]+>/g, '');
  expect(words).toContain('Recorded on 2 platforms. TikTok saw it first, on');
  expect(words).toContain('5 matching posts were first seen');
  const numbers = html.match(/class="sp-fig"[^>]*>/g) || [];
  expect(numbers.length).toBe(2 + 2 * 2 + 2 + 1);
  expect(numbers.every((n) => n.includes('data-query-id="q_seed_path"'))).toBe(true);
});

test('side words trace themselves and topics link to the 42 topic page in the market', () => {
  const traced = [];
  const html = renderToStaticMarkup(<SeedPathResult data={data} market="za" onTrace={(w, m) => traced.push([w, m])} />);
  expect(html).toContain('>braai</button>');
  expect(html).toContain('>#soweto</button>');
  expect(html).toContain('href="#/t/abc?market=ZA"');
  expect(html).toContain('Hashtag, Emerging');
  expect(html).toContain('Ask about this');
  expect(html).toContain('href="#/ask?q=');
});

test('the chart legend is a closed details element under the chart', () => {
  const html = renderToStaticMarkup(<SeedPathResult data={data} market="za" onTrace={() => {}} />);
  expect(html).toMatch(/<details class="sp-how"><summary>How to read this chart<\/summary>/);
  expect(html).not.toMatch(/<details[^>]* open/);
});

test('empty words and topics say so in plain words', () => {
  const html = renderToStaticMarkup(<SeedPathResult data={{...data, side_words: [], topics: []}} market="ke" onTrace={() => {}} />);
  expect(html).toContain('No other word appears in 2 or more of these posts yet.');
  expect(html).toContain('None of these posts belongs to a topic 42 is following in Kenya today.');
});

test('suggestions are only real Discover items, never creators, never invented', () => {
  expect(suggestionTerms(null)).toEqual([]);
  expect(suggestionTerms({items: []})).toEqual([]);
  expect(suggestionTerms({items: [{title: '#a', kind: 'hashtag'}, {title: 'Someone', kind: 'creator'}, {title: '#a', kind: 'hashtag'}, {title: 'b topic', kind: 'topic'}]})).toEqual(['#a', 'b topic']);
});

test('the term is matched as the API matches it', () => {
  expect(normaliseTerm('  #AmaPiano  ')).toBe('amapiano');
  expect(normaliseTerm('Log   Drum')).toBe('log drum');
});

test('the page copy carries no dashes or pipeline words', () => {
  const html = renderToStaticMarkup(<SeedPathResult data={data} market="za" onTrace={() => {}} />);
  const words = html.replace(/<[^>]+>/g, ' ');
  expect(words).not.toMatch(/[–—]|--|\b(lane|route|series|run id|contract|estate)\b/i);
});
