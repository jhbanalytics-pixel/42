/* Source records on a cited answer say what the source recorded, and name the
   gap plainly when there is none. A missing publication time is the source's:
   the vendor sent no date for 23 of the 24 receipts on the stored reply for
   60a5fd02. Every other gap is in 42's own released data (the retained capture
   has no source family column, and the released relation carries no collection
   time), so the view says the field is not available in the released data and
   never blames the source for it. A completed, settled answer also offers its
   export. */
import {expect, test} from 'bun:test';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {GeneralIntelligence} from '../GeneralIntelligence.jsx';
import {intelligenceFixture} from './fixtures/general-intelligence.js';
import {storedReplyFixture} from './fixtures/stored-reply-60a5fd02.js';

const render = (intelligence, props = {}) => renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence, ...props}));
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/\s+/g, ' ');

/* Ask redesign, 23 Sept 2026: record dates read without a leading zero
   ("7 Sept 2026"), and the downloads are named "Download as HTML" and
   "Download as PDF" in the answer's one actions row. Each absence check
   names the new label so it still means something. */
test('a receipt without a publication time says the source did not record one', () => {
  const html = text(render(storedReplyFixture()));
  expect(html).toContain('Published: not recorded by the source · Collected: 7 Sept 2026');
  expect(html).not.toContain('Published: Unavailable');
});

const RELEASED = 'Not available in the released data';

test('a receipt without a source family shows that in view as a gap in the released data', () => {
  const source = storedReplyFixture();
  const html = render(source);
  const visible = text(html.replace(/<details><summary>Record reference<\/summary>.*?<\/details>/g, ''));
  expect(visible.match(/Source family: Not available in the released data/g)?.length).toBe(6);
  expect(text(html)).not.toContain('Source family unavailable');
  expect(text(html)).not.toContain('Source family: not recorded by the source');
});

test('a recorded publication time and source family are shown as recorded', () => {
  const html = text(render(intelligenceFixture()));
  expect(html).toContain('Published: 3 Sept 2026 · Collected: 4 Sept 2026');
  /* Quiet register, 23 Sept 2026: the recorded family reads in sentence
     case, set in the JSX, so "social" shows as "Social". */
  expect(html).toContain('Source family: Social');
  expect(html).not.toContain('Source family: social');
  expect(html).not.toContain('not recorded by the source');
  expect(html).not.toContain(RELEASED);
});

test('a receipt without a collection time says the released data has none and never says collected', () => {
  const source = intelligenceFixture();
  source.receipts[0].collected_at = null;
  const html = text(render(source));
  /* Ask redesign, 23 Sept 2026: receipt dates read the way a reader writes
     them, with no zero padding on the day, so 03 Sept becomes 3 Sept. */
  expect(html).toContain('Published: 3 Sept 2026 · Collection time: Not available in the released data');
  expect(html).not.toContain('Collected');
  expect(html).not.toContain('not recorded by the source');
});

test('market, platform, excerpt and link gaps are named as gaps in the released data', () => {
  const source = intelligenceFixture();
  Object.assign(source.receipts[0], {market: null, platform: null, excerpt: null, url: null});
  const html = text(render(source));
  expect(html).toContain('Content record · Market: Not available in the released data');
  expect(html).toContain('Platform: Not available in the released data · maker');
  expect(html).toContain('Excerpt: Not available in the released data');
  expect(html).toContain('Source link: Not available in the released data');
  expect(html).not.toContain('unavailable');
  expect(html).not.toContain('not recorded by the source');
});

test('a source link that is not a web address is withheld, not called missing', () => {
  const source = intelligenceFixture();
  source.receipts[0].url = 'javascript:alert(1)';
  const html = text(render(source));
  expect(html).toContain('Source link: withheld because it is not a web address');
  expect(html).not.toContain(RELEASED);
  expect(html).not.toContain('javascript:');
});

test('a completed settled answer offers HTML and PDF downloads', () => {
  const html = render(storedReplyFixture());
  expect(html).toContain('aria-label="Download this answer"');
  expect(html).toContain('>Download as HTML</button>');
  expect(html).toContain('>Download as PDF</button>');
});

test('a held answer offers no download', () => {
  const source = intelligenceFixture();
  Object.assign(source.usage, {status: 'unresolved', model_calls: null, input_tokens: null, output_tokens: null, reason: 'response_usage_unavailable'});
  const html = render(source);
  expect(html).not.toContain('Reply withheld');
  expect(html).not.toContain('Download as HTML');
});

test.each(['unavailable', 'refused'])('a %s reply offers no download', status => {
  const source = intelligenceFixture();
  Object.assign(source, {status, snapshot_id: null, window: null, sections: [], claims: [], receipts: [], readings: []});
  const html = render(source);
  expect(html).not.toContain('Reply withheld');
  expect(html).not.toContain('Download as PDF');
});

test('an answer inside a terminal error offers no download', () => {
  const html = render(intelligenceFixture(), {terminalError: true});
  expect(html).not.toContain('Download as HTML');
});

/* Quiet register, 23 Sept 2026: the actions row had three equal outlined
   buttons. The cited brief is now the one bounded control and leads the row,
   and the downloads follow as underlined text links that keep their 48 pixel
   target, so the row has one lead and nothing competes with Send. */
test('the actions row leads with the cited brief and sets the downloads as text links', async () => {
  const html = render(storedReplyFixture(), {onBuildBrief: () => {}});
  const brief = html.indexOf('>Turn into a cited brief</button>');
  expect(brief).toBeGreaterThan(-1);
  expect(brief).toBeLessThan(html.indexOf('>Download as HTML</button>'));
  const {readFileSync} = await import('node:fs');
  const css = readFileSync(new URL('../general-intelligence.css', import.meta.url), 'utf8');
  const rule = css.match(/\.general-export \.general-more \{([^}]*)\}/);
  expect(rule).toBeTruthy();
  expect(rule[1]).toMatch(/border: 0/);
  expect(rule[1]).toMatch(/text-decoration: underline/);
  expect(css).toMatch(/\.general-more \{[^}]*min-height: 48px/);
  expect(css).toMatch(/\.general-source a \{[^}]*text-decoration: underline/);
});

/* Quiet register, 23 Sept 2026: the answer page carries the dates Ask covers
   in its head and the answer's own window in its meta line. The meta line
   names its window as the dates the evidence comes from, so the two are not
   read as the same fact stated twice. The fixture's status is always
   partial, so the line is pinned to that word. */
test('the meta line names the answer window as the dates its evidence comes from', () => {
  const html = text(render(intelligenceFixture()));
  expect(html).toMatch(/South Africa · Evidence from \d+ \w+ (?:\d{4} )?to \d+ \w+ \d{4} · Partial/);
});
