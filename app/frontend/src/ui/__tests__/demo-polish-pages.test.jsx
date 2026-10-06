/* Demo polish, 2 October 2026. Board, Browse, Method, Seed path, Hidden
   people and the watch chooser, as a marketing leader reads them at the
   demo: one page title treatment shared with the 42 pages, section titles in
   sentence case with no letter spacing, the menu's own page names, plain
   words in place of pipeline language, no unbuilt rule offered as a choice,
   and native radios and checkboxes in the app's red in both themes. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import React from 'react';
import postcss from 'postcss';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {renderToStaticMarkup} = await import('react-dom/server');
const {BoardPage} = await import('../../board.jsx');
const {BrowsePage, MethodPage} = await import('../../views.jsx');
const {SeedPathPage} = await import('../../seedpath.jsx');
const {HiddenPeoplePage42} = await import('../../people42.jsx');
const {AlertsPage42} = await import('../../alerts42.jsx');
const {RuleChoice, FIRST_CHOICE} = await import('../WatchDialog.jsx');
const {clearCache} = await import('../../api.js');

const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');
const realFetch = globalThis.fetch;
let host = null;
let root = null;

const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const text = (scope = host) => scope.textContent.replace(/\s+/g, ' ');
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };

function serve(routes){
  globalThis.fetch = async (url) => {
    for (const [prefix, answer] of routes) if (String(url).startsWith(prefix)) return answer;
    return reply(404, {error: 'not_found', message: 'No route'});
  };
}

async function mount(element, routes){
  serve(routes);
  flushSync(() => root.render(element));
  await settle();
}

/* The markup of a static render, parsed so styles can be read per element. */
function staticDom(element){
  const box = document.createElement('div');
  box.innerHTML = renderToStaticMarkup(element);
  return box;
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('pulse_passcode', 'test-pass');
  clearCache();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  globalThis.fetch = realFetch;
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

/* The 42 pages set their title in the sans at the page title step, weight
   600. The legacy pages share that treatment through one app.css class. */
test('app.css carries one page title class on the same tokens the 42 pages use', () => {
  const root = postcss.parse(read('app.css'));
  let found = null;
  root.walkRules((rule) => { if (rule.selectors.includes('.page-title')) found = rule; });
  expect(found, 'a .page-title rule').not.toBeNull();
  const decls = Object.fromEntries(found.nodes.filter((n) => n.type === 'decl').map((n) => [n.prop, n.value]));
  expect(decls['font-size']).toBe('var(--type-page-title)');
  expect(decls['line-height']).toBe('var(--leading-page-title)');
  expect(decls['font-weight']).toBe('600');
  expect(decls['font-family']).toBe('var(--sans)');
  expect(decls['letter-spacing'] || '0').toBe('0');
});

const DESK = {topics: [{id: 'music_amapiano', region: 'ZA', topic: 'Amapiano dance challenges', momentum: 'rising', series: [3, 4, 5]}]};
const VOICES = {creators: [{handle: 'dbn.gogo', platform: 'tiktok', market: 'ZA', reach: 182000, posts: 14, series: [1, 2]}]};

async function mountBoard(){
  localStorage.setItem('pulse-watch', JSON.stringify([{id: 'music_amapiano', market: 'za'}]));
  localStorage.setItem('pulse-crm', JSON.stringify([{name: 'dbn.gogo'}]));
  await mount(<BoardPage />, [['/api/desk', reply(200, DESK)], ['/api/voices', reply(200, VOICES)]]);
}

/* Shell consistency, 2 October 2026: the menu's noun is the title itself, as
   on the 42 pages, so the eyebrow that carried it is gone and the old
   headline leads the sentence under the title. */
test('Board takes the 42 page title, the menu name as that title, and no card around the page', async () => {
  await mountBoard();
  const h1 = host.querySelector('h1');
  expect(h1.textContent).toBe('Board');
  expect(h1.className).toBe('page-title');
  expect(h1.getAttribute('style')).toBeNull();
  expect(host.querySelector('.eyebrow')).toBeNull();
  expect(h1.nextElementSibling.textContent).toContain('What you are tracking');
  const page = host.querySelector('.page');
  expect(page.classList.contains('board'), 'the page is not drawn as the .board card').toBe(false);
  expect(text()).not.toMatch(/header scope|watchlist/);
});

test('Board section titles are headings in sentence case with no letter spacing', async () => {
  await mountBoard();
  const titles = [...host.querySelectorAll('h2')];
  expect(titles.map((h) => h.textContent.trim())).toEqual(['Tracked topics', 'Tracked voices']);
  for (const h of titles){
    const style = h.getAttribute('style') || '';
    expect(style).not.toMatch(/text-transform|letter-spacing/);
  }
});

test('Board names markets in words and says a first look plainly', async () => {
  await mountBoard();
  const topic = host.querySelector('[data-topic-pin-market="za"]');
  expect(text(topic)).toContain('South Africa');
  expect(text(topic)).not.toMatch(/\bZA\b/);
  expect(text(topic)).toContain('First look, no change yet');
  const voice = [...host.querySelectorAll('.board-card')].find((card) => card.textContent.includes('@dbn.gogo'));
  expect(text(voice)).toContain('South Africa');
  expect(text(voice)).not.toMatch(/\bZA\b/);
  expect(text(voice)).toContain('182k engagement · 14 posts · last 30 days');
  expect(voice.querySelector('.board-reach').textContent).toBe('182k');
  expect(text()).not.toContain('No previous reading');
});

test('Browse takes the 42 page title in place of the display serif', () => {
  const dom = staticDom(<BrowsePage region="ZA" />);
  const h1 = dom.querySelector('h1');
  /* Shell consistency, 2 October 2026: the title is the menu's noun and the
     old headline leads the sentence under it. */
  expect(h1.textContent).toBe('Browse');
  expect(h1.nextElementSibling.textContent).toContain('Search what people actually said');
  expect(h1.className).toBe('page-title');
  expect(h1.getAttribute('style')).toBeNull();
});

test('Method section titles are sentence case headings, with no tracked label beside them', () => {
  const dom = staticDom(<MethodPage />);
  const titles = [...dom.querySelectorAll('h2')];
  expect(titles.map((h) => h.textContent.trim())).toEqual(['How it works', 'Rules every number follows']);
  for (const h of titles) expect(h.getAttribute('style') || '').not.toMatch(/text-transform|letter-spacing/);
  expect(dom.innerHTML).not.toMatch(/text-transform:\s*uppercase/);
  expect(text(dom)).not.toMatch(/point of the product/i);
});

test('Method reads in plain words and names pages as the menu does', () => {
  const dom = staticDom(<MethodPage />);
  const words = text(dom);
  expect(words).not.toMatch(/pipeline|staging|replay|evidence gates|Nano Banana|Lyria|commanded|Briefing|\bdesk\b(?! reads the signal)/i);
  expect(words).not.toMatch(/\((?:ZA|NG|KE)[, ]/);
  const next = [...dom.querySelectorAll('nav a')].map((a) => [a.textContent, a.getAttribute('href')]);
  expect(next[0]).toEqual(['Open Today', '#/pulse']);
});

/* The shared stage rail (loopRail.jsx) belongs to Seeds and Discover as well
   and is held by its own tests, so this reads the Seed path page without it. */
test('Seed path calls itself Seed path, drops the defensive copy and no longer offers Start typing', () => {
  const dom = staticDom(<SeedPathPage initialMarket="za" />);
  dom.querySelector('.loop-rail')?.remove();
  const words = text(dom);
  /* Restated, design audit 2 October 2026: every other page's h1 is its menu
     noun with no eyebrow, so Seed path now says its name in the h1 instead of
     an eyebrow over a question. The intent, that it calls itself Seed path,
     is unchanged. */
  expect(dom.querySelector('h1').textContent).toBe('Seed path');
  expect(dom.querySelector('.eyebrow')).toBeNull();
  expect(words).not.toMatch(/Explorer|Not the same as|engine graph|admitted dynamic signals|Focus the search box/);
  /* Design critique, 2 October 2026: Start typing only focused the field
     beside it, so it is gone and the empty state offers no button. */
  expect([...dom.querySelectorAll('button')].some((b) => b.textContent === 'Start typing')).toBe(false);
});

test('Seed path labels are the quiet label, never tracked capitals', () => {
  /* Seed path rebuild, 2 October 2026: the try-a-term label now names real
     Discover items and is styled by its class in styles/seedpath.css rather
     than inline, so the check reads the sheet. It only shows when Discover
     returns items, so this static render (no API) shows no label at all. */
  const dom = staticDom(<SeedPathPage initialMarket="za" />);
  expect([...dom.querySelectorAll('div')].some((el) => !el.children.length && /^Try a term/.test(el.textContent))).toBe(false);
  const source = read('seedpath.jsx');
  expect(source).toMatch(/className="sp-label">Things 42 is following in/);
  expect(source).not.toMatch(/textTransform:\s*'uppercase'|letterSpacing/);
  const sheet = read('styles/seedpath.css');
  const rule = sheet.slice(sheet.indexOf('.seedpath .sp-label,'), sheet.indexOf('}', sheet.indexOf('.seedpath .sp-label,')));
  expect(rule).toMatch(/font-family:\s*var\(--sans\)/);
  expect(rule).toMatch(/font-weight:\s*400/);
  expect(rule).not.toMatch(/letter-spacing|text-transform/);
});

test('Hidden people never shows a raw creator id and names the team, not a person', async () => {
  const rows = {suppressions: [
    {suppression_id: 'sup_app_000000000001', status_at: '2026-09-28T09:00:00+02:00', status: 'suppressed', creator_id: 'c_ng_macro', platform: null, handle: null, reason: 'Older hide', who: 'Thandi', matched: 1},
    {suppression_id: 'sup_app_000000000002', status_at: '2026-09-29T11:30:00+02:00', status: 'suppressed', creator_id: 'c_x', platform: 'tiktok', handle: '@someone_new', reason: 'Asked', who: 'Jo', matched: 1},
  ]};
  await mount(<HiddenPeoplePage42 />, [['/api/suppressions', reply(200, rows)]]);
  expect(text()).toContain('Ask the 42 team to bring someone back.');
  expect(text()).not.toMatch(/Albert|c_ng_macro|c_x/);
  const listed = [...host.querySelectorAll('[data-suppression]')];
  expect(listed[0].querySelector('.pp42-row-title').textContent).toBe('TikTok @someone_new');
  expect(listed[1].querySelector('.pp42-row-title').textContent).toBe('A creator');
});

test('the rule chooser offers only the rules that work, and keeps the others behind a flag', () => {
  const offered = staticDom(<RuleChoice choice={FIRST_CHOICE} onChange={() => {}} />);
  expect([...offered.querySelectorAll('input[type="radio"]')].map((input) => input.value)).toEqual(['rising', 'ratio', 'reach', 'breakout', 'tone_flip']);
  expect(text(offered)).toContain('When a creator breaks out');
  expect(text(offered)).toContain('When the tone flips');
  expect(text(offered)).not.toMatch(/Needs .* detection|views surge/);
  const creator = staticDom(<RuleChoice choice={FIRST_CHOICE} onChange={() => {}} creator />);
  expect([...creator.querySelectorAll('input[type="radio"]')].map((input) => input.value)).toEqual(['rising', 'ratio', 'reach', 'breakout', 'tone_flip', 'creator_surge']);
  const all = staticDom(<RuleChoice choice={FIRST_CHOICE} onChange={() => {}} showUnbuilt />);
  expect(all.querySelectorAll('input[type="radio"]')).toHaveLength(5);
  expect(text(all)).not.toMatch(/Needs .* detection/);
});

test('the watch list says it is shared with your team, not with a passcode', async () => {
  await mount(<AlertsPage42 />, [['/api/watches', reply(200, {watches: []})], ['/api/alerts', reply(200, {date: '2026-09-30', alerts: []})]]);
  const watches = host.querySelector('[data-section="watches"]');
  expect(text(watches)).toContain('This list is shared with your team.');
  expect(text()).not.toMatch(/passcode shares|Needs .* detection/);
});

test('native radios and checkboxes take the app red in both themes', () => {
  const root = postcss.parse(read('app.css'));
  const found = [];
  root.walkRules((rule) => {
    if (/input\[type=radio\]/.test(rule.selector) && /input\[type=checkbox\]/.test(rule.selector)){
      for (const n of rule.nodes) if (n.type === 'decl' && n.prop === 'accent-color') found.push([rule.selector, n.value]);
    }
  });
  expect(found).toHaveLength(1);
  expect(found[0][0]).toMatch(/\.oi-product/);
  expect(found[0][1]).toBe('var(--accent)');
});

test('the loop rail names its stages as the menu does, in plain words', async () => {
  /* Demo polish, 2 October 2026: the middle stage read "Explorer" while the
     menu calls the page Seed path, and the others read as engine notes. */
  const {LoopRail} = await import('../../loopRail.jsx');
  const {renderToStaticMarkup: render} = await import('react-dom/server');
  const html = render(<LoopRail active="explorer" />);
  expect(html).toContain('Seed path');
  expect(html).not.toContain('Explorer');
  expect(html).not.toContain('dynamic signals');
  expect(html).not.toContain('hypotheses');
});

/* Product review SPT-05: an empty or unreadable draft says why on Trace
   instead of doing nothing, and the field is marked invalid until edited. */
test('Seed path says why a draft cannot be traced', async () => {
  await mount(<SeedPathPage initialMarket="za" session="s" />, [['/api/discover', reply(200, {items: []})]]);
  const before = window.location.hash;
  const form = host.querySelector('form.sp-form');
  flushSync(() => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
  await settle();
  const input = host.querySelector('.sp-input');
  expect(host.querySelector('[data-seedpath-problem]').textContent).toBe('Type a word, hashtag or slang term to trace.');
  expect(input.getAttribute('aria-invalid')).toBe('true');
  expect(input.getAttribute('aria-describedby')).toBe('sp-problem');
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  flushSync(() => { setter.call(input, '#!'); input.dispatchEvent(new Event('input', {bubbles: true})); });
  await settle();
  expect(host.querySelector('[data-seedpath-problem]')).toBeNull();
  flushSync(() => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
  await settle();
  expect(host.querySelector('[data-seedpath-problem]').textContent).toBe('Type at least two characters, with a letter or number among them.');
  expect(window.location.hash).toBe(before);
});
