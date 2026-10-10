/* Lines break between thoughts, never inside one. Albert, 2 October 2026:
   sentences ended on a lone word, facts such as "7 200 views" split across
   rows, Discover cut its summary off mid-sentence, and narrow columns on a
   wide page pushed two-word stragglers onto new rows. The rules below hold
   the fix in place:

   - Running text avoids a lone last word (text-wrap: pretty) and headings
     share their words evenly across lines (text-wrap: balance).
   - A line of facts joined by middots keeps each fact whole, so the line
     breaks between facts. The middot stays at the end of its fact, so a new
     row never starts with one.
   - No summary is clipped: the reader sees the whole sentence.
   - Short lists sit in one column at full width, not in narrow tracks. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());
const React = await import('react');
const {renderToStaticMarkup} = await import('react-dom/server');
const {Facts} = await import('../Facts.jsx');
const {EvidenceList} = await import('../TrendCard.jsx');
const {MethodPage} = await import('../../views.jsx');

const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');
const parse = (html) => new window.DOMParser().parseFromString(html, 'text/html');

function decls(sheet, match){
  const out = [];
  postcss.parse(read(sheet)).walkRules((rule) => {
    if (!match(rule.selector)) return;
    const media = rule.parent && rule.parent.type === 'atrule' ? rule.parent.params : '';
    rule.walkDecls((decl) => out.push({selector: rule.selector, media, prop: decl.prop, value: decl.value.trim()}));
  });
  return out;
}

test('running text avoids a lone last word and headings balance their lines', () => {
  const body = decls('app.css', (s) => s.split(',').some((p) => p.trim() === 'body'));
  expect(body.some((d) => /^text-wrap(-style)?$/.test(d.prop) && d.value === 'pretty')).toBe(true);
  const heads = decls('app.css', (s) => /:where\(h1, h2, h3, h4, h5, h6/.test(s));
  expect(heads.some((d) => /^text-wrap(-style)?$/.test(d.prop) && d.value === 'balance')).toBe(true);
});

test('a facts line keeps each fact whole and never starts a row with a middot', () => {
  const html = renderToStaticMarkup(React.createElement('p', null,
    React.createElement(Facts, {parts: ['TikTok', null, '@fixture_za_6', '26 September 2026', '7 200 views']})));
  const p = parse(html).querySelector('p');
  expect(p.textContent).toBe('TikTok · @fixture_za_6 · 26 September 2026 · 7 200 views');
  const units = [...p.querySelectorAll('.fact-unit')].map((s) => s.textContent);
  expect(units).toEqual(['TikTok ·', '@fixture_za_6 ·', '26 September 2026 ·', '7 200 views']);
  const unit = decls('app.css', (s) => s.trim() === '.fact-unit');
  expect(unit).toContainEqual(expect.objectContaining({prop: 'display', value: 'inline-block'}));
});

test('a post line under a trend keeps its facts whole', () => {
  const html = renderToStaticMarkup(React.createElement(EvidenceList, {items: [
    {id: 'e1', platform: 'tiktok', handle: '@fixture_za_6', posted_at: '2026-09-26T08:00:00Z', engagement: {views: 7200}, text: 'Post 6'},
  ]}));
  const meta = parse(html).querySelector('.t42-post-meta');
  expect(meta.querySelectorAll('.fact-unit').length).toBe(4);
  expect(meta.lastElementChild.textContent).toMatch(/views$/);
});

test('Discover shows the whole summary and keeps each fact in its row whole', () => {
  const clamps = decls('styles/discover42.css', () => true).filter((d) => /line-clamp/.test(d.prop));
  expect(clamps).toEqual([]);
  const items = decls('styles/discover42.css', (s) => /\.d42-feed \.t42-card \.t42-card-metrics > \*$/.test(s.trim()));
  expect(items).toContainEqual(expect.objectContaining({prop: 'display', value: 'inline-block'}));
});

test('the sections under Today stand in one full-width column', () => {
  const below = decls('styles/today42.css', (s) => s.trim() === '.t42-below');
  const cols = below.find((d) => d.prop === 'grid-template-columns');
  expect(cols.value).toBe('minmax(0, 1fr)');
});

test('Method rules are a plain ordered list with plain section headings', () => {
  const dom = parse(renderToStaticMarkup(React.createElement(MethodPage)));
  const rules = [...dom.querySelectorAll('ol.method-rules > li')];
  expect(rules.length).toBe(5);
  for (const li of rules) expect(li.getAttribute('style') || '').not.toMatch(/border/);
  const html = dom.body.innerHTML;
  expect(html).not.toMatch(/width:\s*16px;\s*height:\s*2px/);
  expect([...dom.querySelectorAll('h2')].map((h) => h.textContent)).toEqual(['How it works', 'Rules every number follows']);
});

test('on a phone a row of next links and empty-state actions stack, so no link is left alone', () => {
  const next = decls('app.css', (s) => s.trim() === '.legacy-next').filter((d) => /max-width:\s*479px/.test(d.media));
  expect(next).toContainEqual(expect.objectContaining({prop: 'flex-direction', value: 'column'}));
  const actions = decls('styles/empty-states.css', (s) => s.trim() === '.es-actions').filter((d) => /max-width:\s*479px/.test(d.media));
  expect(actions).toContainEqual(expect.objectContaining({prop: 'flex-direction', value: 'column'}));
});

test('on a phone the Discover filters sit in a bottom sheet, with sort across its full width', () => {
  const sheet = decls('styles/discover42.css', (s) => s.trim() === '.d42-groups').filter((d) => /max-width:\s*767px/.test(d.media));
  expect(sheet).toContainEqual(expect.objectContaining({prop: 'position', value: 'fixed'}));
  const sort = decls('styles/discover42.css', (s) => s.trim() === '.d42-select').filter((d) => /max-width:\s*767px/.test(d.media));
  expect(sort).toContainEqual(expect.objectContaining({prop: 'inline-size', value: '100%'}));
});
