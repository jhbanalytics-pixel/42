/* Evidence text is never cut mid-word without a mark. The brief evidence
   pack stores text as the first 280 characters of a post and quote_text as
   the whole post. A post whose stored full text is longer than its preview
   offers a "Read full post" disclosure that shows every word; the preview
   ends at a word with an ellipsis. A post cut at 280 characters with no full
   text says the full text is on the post. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());
const React = await import('react');
const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {EvidenceList} = await import('../TrendCard.jsx');

let host = null;
let root = null;
beforeEach(() => {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
});

const render = (items, props = {}) => flushSync(() => root.render(React.createElement(EvidenceList, {items, ...props})));
const words = (s) => s.split(/\s+/u).filter(Boolean);

const OPENING = 'Braai day in Soweto started early and the queue for the shisanyama wound round the block before nine. ';
const MIDDLE = 'Everyone had a view on whose marinade wins, and the aunties were not shy about saying so loudly to anyone nearby. ';
const QUOTE = 'Nothing beats my gogo\'s chakalaka recipe.';
const LATER = '\n\nSecond paragraph: we stayed until the lights came on and the last plate went home wrapped in foil for tomorrow.';
const FULL = OPENING + MIDDLE + OPENING + QUOTE + LATER;
const PREVIEW = [...FULL].slice(0, 280).join('');

const longItem = {id: 'ev_long', platform: 'tiktok', handle: '@braai_za', url: 'https://www.tiktok.com/@braai_za/video/1', text: PREVIEW, quote_text: FULL};

test('the fixture preview is cut mid-word and the quote sits past the cut', () => {
  expect(FULL.length).toBeGreaterThan(300);
  expect(FULL.indexOf(QUOTE)).toBeGreaterThan(280);
  expect(/\S/.test(FULL[280]) && /\S/.test(FULL[279])).toBe(true);
});

test('a long post offers a keyboard disclosure that reveals every word of the stored full text', () => {
  render([longItem]);
  const post = host.querySelector('[data-evidence-id="ev_long"]');
  const details = post.querySelector('details');
  expect(details).not.toBeNull();
  const summary = details.querySelector('summary');
  expect(summary.textContent.trim()).toBe('Read full post');
  const full = details.querySelector('[data-post-full]');
  expect(full).not.toBeNull();
  expect([...full.querySelectorAll('p')].flatMap((p) => words(p.textContent))).toEqual(words(FULL));
  expect(full.textContent).toContain(QUOTE);
  /* Paragraph breaks in the post stay paragraph breaks. */
  expect(full.querySelectorAll('p')).toHaveLength(2);
  expect(full.querySelectorAll('p')[1].textContent.startsWith('Second paragraph')).toBe(true);
});

test('the preview of a long post ends at a whole word with an ellipsis and keeps the words as stored', () => {
  render([longItem]);
  const preview = host.querySelector('[data-evidence-id="ev_long"] .t42-post-text');
  const shown = preview.textContent;
  expect(shown.endsWith('…')).toBe(true);
  expect(preview.hasAttribute('data-shortened')).toBe(true);
  const body = shown.slice(0, -1).trimEnd();
  expect(FULL.startsWith(body)).toBe(true);
  /* The cut falls between words, never inside one. */
  expect(/\s/.test(FULL[body.length])).toBe(true);
  expect(body.length).toBeLessThanOrEqual(280);
});

test('a short post shows its words with no disclosure, no ellipsis and no excerpt note', () => {
  render([{id: 'ev_short', platform: 'x', handle: '@short', url: 'https://x.com/short/status/1', text: 'Short and whole.', quote_text: 'Short and whole.'}]);
  const post = host.querySelector('[data-evidence-id="ev_short"]');
  expect(post.querySelector('details')).toBeNull();
  expect(post.querySelector('.t42-post-text').textContent).toBe('Short and whole.');
  expect(post.textContent).not.toContain('Excerpt');
});

test('a post with text only and no full text gets no false disclosure', () => {
  render([{id: 'ev_text', platform: 'x', handle: '@text', url: 'https://x.com/text/status/1', text: 'Only the text was stored.'}]);
  const post = host.querySelector('[data-evidence-id="ev_text"]');
  expect(post.querySelector('details')).toBeNull();
  expect(post.textContent).not.toContain('Excerpt');
});

test('a 280-character post with no full text says the full text is on the post, next to the link', () => {
  render([{id: 'ev_cut', platform: 'tiktok', handle: '@braai_za', url: 'https://www.tiktok.com/@braai_za/video/2', text: PREVIEW}]);
  const post = host.querySelector('[data-evidence-id="ev_cut"]');
  expect(post.querySelector('details')).toBeNull();
  const note = post.querySelector('[data-excerpt-note]');
  expect(note).not.toBeNull();
  expect(note.textContent).toBe('Excerpt; the full text is on the post');
  /* The words stay as stored; only a closing mark is added. */
  expect(post.querySelector('.t42-post-text').textContent.startsWith(PREVIEW)).toBe(true);
  const link = [...post.querySelectorAll('a')].find((a) => a.textContent === 'Open the post');
  expect(link).toBeTruthy();
});
