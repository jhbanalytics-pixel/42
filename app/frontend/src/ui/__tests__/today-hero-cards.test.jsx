/* The Today hero and the trend story cards. The hero is the lead story as a
   two-column grid (headline 7 of 12, evidence panel 5 of 12); each trend is
   one full-width story card on the same grid, with a live panel on the right
   and a swipeable strip of the real example posts at the foot. Rendered into a
   real DOM from the Today fixture so the zones, the guards and the fields the
   payload does not send can be read. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React, {act} from 'react';
import {readFileSync} from 'node:fs';
import todayFixture from './fixtures/today42.json';
import trendFixture from './fixtures/trend42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TodayPage42} = await import('../../today42.jsx');
const {StoryCard, countedCreators} = await import('../StoryCard.jsx');

const realFetch = globalThis.fetch;
const realNow = Date.now;
const FIXTURE_DAY_NOW = Date.parse('2026-09-30T08:00:00Z');
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const css = (name) => readFileSync(new URL(`../../styles/${name}`, import.meta.url), 'utf8');

beforeEach(() => {
  localStorage.setItem('pulse_passcode', 'test-pass');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  Date.now = realNow;
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  globalThis.fetch = realFetch;
});
afterAll(() => {
  GlobalRegistrator.unregister();
});

async function mount(props = {}, today = todayFixture){
  Date.now = () => FIXTURE_DAY_NOW;
  globalThis.fetch = async (url) => {
    if (String(url).startsWith('/api/today')) return reply(200, today);
    if (String(url).startsWith('/api/trends/')) return reply(200, trendFixture);
    return reply(404, {error: 'not_found', message: 'No route'});
  };
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" {...props} />));
  await settle();
}

const za = (today) => today.markets.find((market) => market.market === 'ZA');
const leadCard = (today) => [...za(today).cards, ...(za(today).more || [])].find((card) => card.item_id === today.headline.item_id);
const panel = () => host.querySelector('[data-today-lead-panel]');
/* A second trend that passes the page's checks: the lead card under a new id and name. */
function another(today, title, edit = () => {}){
  const copy = clone(leadCard(today));
  copy.item_id = 'f'.repeat(64);
  copy.title = title;
  edit(copy);
  za(today).cards.push(copy);
  return copy;
}
const storyCards = () => [...host.querySelectorAll('[data-card][data-story]')];
const words = (el) => el.textContent.replace(/ /g, ' ').replace(/\s+/g, ' ').trim();

/* Part 1: the hero */

test('the hero panel holds the creators figure, the posts count beside it, and a full width chart with its dates', async () => {
  await mount();
  const side = panel().querySelector('.t42-lead-side');
  expect(side.getAttribute('aria-hidden')).toBe('true');
  expect(words(side.querySelector('.t42-lead-figure'))).toBe('31 creators in 3 days');
  expect(side.querySelector('.t42-lead-figure').getAttribute('data-query-id')).toBe('q_creators3_za_a');
  expect(words(side.querySelector('[data-lead-posts]'))).toBe('58 posts in 3 days');
  const svg = side.querySelector('svg.t42-spark');
  expect(svg.getAttribute('data-wide')).toBe('');
  expect(Number(svg.getAttribute('height'))).toBeGreaterThanOrEqual(120);
  const days = [...svg.querySelectorAll('.t42-tick-day')].map((tick) => tick.textContent);
  expect(days).toEqual(['24 Sep', '30 Sep']);
  expect(svg.querySelectorAll('.t42-dot-now')).toHaveLength(1);
  expect(side.querySelector('.t42-trend-caption')).not.toBeNull();
});

test('the hero names the platforms of its evidence with their marks, then two real example posts', async () => {
  await mount();
  const marks = [...panel().querySelectorAll('[data-platforms] [data-lead-platform]')];
  expect(marks.map((mark) => mark.getAttribute('data-lead-platform'))).toEqual(['tiktok', 'instagram', 'youtube']);
  expect(marks.map((mark) => words(mark.querySelector('.sc-platform-name')))).toEqual(['TikTok', 'Instagram', 'YouTube']);
  expect(marks[0].querySelector('svg.pl-logo').getAttribute('data-logo')).toBe('tiktok');
  expect(marks[1].querySelector('.pl-monogram')).not.toBeNull();
  const examples = [...panel().querySelectorAll('[data-lead-example]')];
  expect(examples).toHaveLength(2);
  const card = leadCard(todayFixture);
  expect(examples.map((el) => el.getAttribute('data-lead-example'))).toEqual(['tt_za_006', 'ig_za_007']);
  expect(words(examples[0])).toContain('@fixture_za_6');
  expect(words(examples[0])).toContain('TikTok');
  expect(words(examples[0])).toContain('26 September 2026');
  expect(words(examples[0])).toContain(card.evidence[0].text);
  const link = examples[0].querySelector('a[href]');
  expect(link.getAttribute('href')).toBe(card.evidence[0].url);
  expect(link.getAttribute('rel')).toContain('noopener');
});

test('the hero offers Ask about this first and Watch second, and Watch only when the page can create one', async () => {
  await mount();
  const ask = panel().querySelector('a.t42-action-primary');
  expect(ask.textContent).toBe('Ask about this');
  expect(panel().querySelector('button.t42-action')).toBeNull();
  resetRoot();
  await mount({onCreateWatch: async () => ({watch_id: 'w1'})});
  const actions = [...panel().querySelectorAll('.t42-lead-actions > *')];
  expect(actions.map((el) => el.textContent)).toEqual(['Ask about this', 'Watch']);
  expect(actions[1].className).not.toContain('primary');
});

function resetRoot(){
  flushSync(() => root.unmount());
  root = createRoot(host);
}

test('a hero with no figures and no chart shrinks to what it has, with no empty box', async () => {
  const today = clone(todayFixture);
  Object.assign(leadCard(today), {numbers: [], reach: null, sparkline: null});
  await mount({}, today);
  expect(panel().querySelector('.t42-lead-side')).toBeNull();
  expect(panel().querySelectorAll('[data-lead-example]')).toHaveLength(2);
  for (const child of panel().children) expect(child.textContent.trim().length).toBeGreaterThan(0);
});

test('the meta line and the guide sit inside the header under the headline', async () => {
  await mount();
  const head = host.querySelector('header.t42-head');
  const kids = [...head.children].map((el) => el.className.split(' ')[0]);
  expect(kids.indexOf('t42-lead')).toBeGreaterThan(0);
  expect(kids.indexOf('t42-status-area')).toBeGreaterThan(kids.indexOf('t42-lead'));
  expect(head.querySelector('[data-today-guide]')).not.toBeNull();
});

test('the hero grid is 12 columns: headline 7, panel 5, one column below desktop; the headline line is capped', () => {
  const sheet = css('today-story.css');
  expect(sheet).toMatch(/repeat\(12, minmax\(0, 1fr\)\)/);
  expect(sheet).toMatch(/@media \(min-width: 1000px\)/);
  expect(sheet).toMatch(/grid-column: 1 \/ span 7/);
  expect(sheet).toMatch(/grid-column: 8 \/ span 5/);
  expect(sheet).toMatch(/\.t42-headline[^}]*max-width: \d+(\.\d+)?ch/s);
});

/* Part 2: the story cards */

test('each trend is one story card with the story left, the live panel right and the evidence strip at the foot', async () => {
  const today = clone(todayFixture);
  another(today, '#second_trend');
  await mount({}, today);
  const [first, second] = storyCards();
  const card = za(todayFixture).cards[0];
  expect(storyCards()).toHaveLength(2);
  expect(second.querySelector('.sc-rank').textContent).toBe('02');
  expect(second.querySelector('h3').textContent).toBe('#second_trend');
  expect(first.querySelector('.sc-rank').textContent).toBe('01');
  expect(first.querySelector('h3 a.tc-title-link').getAttribute('href')).toContain(card.item_id);
  const badges = [...first.querySelectorAll('.sc-story .sc-badges > *')].map((el) => el.textContent);
  expect(badges).toContain('Moved up');
  expect(first.querySelector('.sc-story .t42-specificity-why-now').textContent).toContain(card.explanation);
  const quote = first.querySelector('.sc-story blockquote.sc-pull');
  expect(quote.textContent).toContain(card.specificity.quote.text);
  expect(first.querySelector('.sc-live .tc-big-lead .tc-big-value').textContent).toBe('31');
  expect(words(first.querySelector('.sc-live'))).toContain('58 posts in 3 days');
  expect(first.querySelector('.sc-live svg.t42-spark[data-wide]')).not.toBeNull();
  expect([...first.querySelectorAll('.sc-live [data-card-platform]')].map((el) => el.getAttribute('data-card-platform'))).toEqual(['tiktok', 'instagram', 'youtube']);
  const zones = [...first.querySelector('.sc-body').children].map((el) => el.className.split(' ')[0]);
  expect(zones.indexOf('sc-story')).toBeLessThan(zones.indexOf('sc-live'));
  expect(zones.indexOf('sc-live')).toBeLessThan(zones.indexOf('sc-evidence'));
});

test('the momentum chip shows only when the payload sends a state word', async () => {
  await mount();
  expect(storyCards()[0].querySelector('.sc-live [data-momentum]').textContent).toBe('Emerging');
  const today = clone(todayFixture);
  const card = za(today).cards[0];
  Object.assign(card, {state_word: null, state: null, lifecycle: null});
  resetRoot();
  await mount({}, today);
  expect(storyCards()[0].querySelector('[data-momentum]')).toBeNull();
});

test('the evidence strip is a snapping row of tiles with mark, handle, date, views, one text and a link out', async () => {
  await mount();
  const strip = storyCards()[0].querySelector('.sc-evidence');
  expect(strip.querySelector('[data-band]')).not.toBeNull();
  expect(strip.querySelector('[data-band-scroll]').getAttribute('role')).toBe('region');
  const tiles = [...strip.querySelectorAll('[data-evidence-id]')];
  expect(tiles.map((tile) => tile.getAttribute('data-evidence-id'))).toEqual(['tt_za_006', 'ig_za_007']);
  const tile = tiles[0];
  expect(tile.querySelector('.pl-logo[data-logo="tiktok"]')).not.toBeNull();
  expect(words(tile.querySelector('.sc-tile-who'))).toContain('@fixture_za_6');
  expect(words(tile.querySelector('.sc-tile-when'))).toContain('26 September 2026');
  expect(words(tile.querySelector('.sc-tile-when'))).toContain('7 200 views');
  expect(tile.querySelector('[data-quoted-above]').textContent).toBe('Quoted above');
  expect(tiles[1].querySelector('.sc-tile-text').textContent).toBe('Post 7: the #fixture_za_step routine at the taxi rank');
  expect(words(tile.querySelector('.sc-tile-note'))).toBe('posted before the counted days');
  const link = tile.querySelector('a.sc-tile-open');
  expect(link.textContent).toBe('Open the post');
  expect(link.getAttribute('target')).toBe('_blank');
  expect(words(tile.querySelector('.t42-post-meta'))).toBe('TikTok · @fixture_za_6 · 26 September 2026 · posted before the counted days · 7 200 views');
  expect(strip.querySelector('[data-examples-window]').textContent).toBe('From the last 7 days of posts. The counts above cover 3 days.');
});

test('the evidence strip shows arrows only while more tiles are off screen', async () => {
  await mount();
  const strip = storyCards()[0].querySelector('.sc-evidence');
  expect(strip.querySelector('[data-band-arrow]')).toBeNull();
  const scroller = strip.querySelector('[data-band-scroll]');
  Object.defineProperty(scroller, 'scrollWidth', {configurable: true, get: () => 900});
  Object.defineProperty(scroller, 'clientWidth', {configurable: true, get: () => 300});
  scroller.scrollLeft = 0;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  try { await act(async () => { scroller.dispatchEvent(new Event('scroll', {bubbles: false})); }); } finally { globalThis.IS_REACT_ACT_ENVIRONMENT = false; }
  expect(strip.querySelector('[data-band-arrow="next"]')).not.toBeNull();
  expect(strip.querySelector('[data-band-arrow="prev"]')).toBeNull();
});

test('a card with no chart data keeps its figures and leaves no empty chart box', async () => {
  const today = clone(todayFixture);
  za(today).cards[0].sparkline = null;
  await mount({}, today);
  const live = storyCards()[0].querySelector('.sc-live');
  expect(live.querySelector('svg.t42-spark')).toBeNull();
  expect(live.querySelector('.sc-chart')).toBeNull();
  expect(words(live)).toContain('31');
  for (const child of live.children) expect(child.textContent.trim().length + child.querySelectorAll('svg, img').length).toBeGreaterThan(0);
});

test('a market with one card renders one story card', async () => {
  await mount();
  expect(storyCards()).toHaveLength(1);
});

test('a published card whose counted creators is 0 is never rendered', async () => {
  const today = clone(todayFixture);
  const zero = another(today, '#zero_creators', (card) => { card.numbers[0].value = 0; });
  await mount({}, today);
  const ids = storyCards().map((card) => card.querySelector('.tc-title-link').getAttribute('href'));
  expect(ids.some((href) => href.includes(zero.item_id))).toBe(false);
  expect(storyCards()).toHaveLength(1);
  expect(host.textContent).not.toContain(zero.title);
});

test('countedCreators reads the creators figure and says nothing when there is none', () => {
  expect(countedCreators({reach: {value: 0, unit: 'creators in 3 days'}, numbers: []})).toBe(0);
  expect(countedCreators({numbers: [{value: 4, unit: 'posts in 3 days'}, {value: 9, unit: 'creators in 3 days'}]})).toBe(9);
  expect(countedCreators({numbers: [{value: 4, unit: 'posts in 3 days'}]})).toBeNull();
  expect(countedCreators({})).toBeNull();
});

test('the story card component itself renders nothing for a zero creator card', () => {
  const card = clone(za(todayFixture).cards[0]);
  card.numbers[0].value = 0;
  flushSync(() => root.render(<ol><StoryCard card={card} index={0} market="ZA" date="2026-09-30" todaySpecificity={null} /></ol>));
  expect(host.querySelector('[data-card]')).toBeNull();
});

test('Was this right? is one quiet segmented control of Real, Not real and Useful', async () => {
  await mount({onFeedback: async () => ({})});
  const group = storyCards()[0].querySelector('.f42-feedback');
  expect(group.getAttribute('role')).toBe('group');
  expect([...group.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['Real', 'Not real', 'Useful']);
  expect(group.closest('.sc-foot')).not.toBeNull();
});

test('Ask about this is primary; Watch and Posts are secondary actions of the card', async () => {
  await mount({onCreateWatch: async () => ({watch_id: 'w1'})});
  const actions = [...storyCards()[0].querySelectorAll('.sc-foot .t42-actions > *')].map((el) => [el.textContent, el.className.includes('primary')]);
  expect(actions).toEqual([['Ask about this', true], ['Watch', false], ['Posts', false]]);
});

test('on a phone the zones stack and the evidence tiles are narrower than the screen', () => {
  const sheet = css('today-story.css');
  expect(sheet).toMatch(/@media \(min-width: 1000px\)[\s\S]*grid-template-columns: repeat\(12, minmax\(0, 1fr\)\)/);
  expect(sheet).toMatch(/\.sc-tile\s*\{[^}]*flex: 1 0 min\(/s);
});

/* Motion, space and touch */

test('the story sheet has no layout-shifting reveal, no loop, and respects reduced motion', () => {
  const sheet = css('today-story.css');
  expect(sheet).not.toMatch(/infinite/);
  expect([...sheet.matchAll(/box-shadow:\s*([^;]+);/g)].filter((m) => !/^(none|inset)/.test(m[1].trim()))).toEqual([]);
  expect([...sheet.matchAll(/border-radius:\s*([^;]+);/g)].filter((m) => m[1].trim() !== '0')).toEqual([]);
  const animated = [...sheet.matchAll(/animation:[^;]+;/g)].map((m) => m[0]);
  for (const line of animated) expect(line).toMatch(/var\(--motion-handoff\)/);
  const open = sheet.split('@media (prefers-reduced-motion: no-preference)');
  expect(open.length).toBeGreaterThan(1);
  expect(open[0]).not.toMatch(/animation:/);
});

test('counts and dates use tabular numerals, tiles skip off-screen rendering, and the strip has touch momentum', () => {
  const sheet = css('today-story.css');
  expect(sheet).toMatch(/\.sc-rank[^{]*\{[^}]*font-variant-numeric: tabular-nums/s);
  expect(sheet).toMatch(/\.sc-tile\s*\{[^}]*content-visibility: auto/s);
  expect(sheet).toMatch(/contain-intrinsic-size/);
  expect(sheet).toMatch(/-webkit-overflow-scrolling: touch/);
  expect(sheet).toMatch(/scroll-snap-stop: always/);
});

test('while Today loads, the hero and a story card are held by blocks of their final size', async () => {
  globalThis.fetch = () => new Promise(() => {});
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
  await settle();
  const skeletons = [...host.querySelectorAll('.t42-loading-structure')];
  expect(skeletons).toHaveLength(2);
  const skeleton = {querySelector: (selector) => skeletons.map((el) => el.querySelector(selector)).find(Boolean) || null, getAttribute: (name) => skeletons.every((el) => el.getAttribute(name) === 'true') ? 'true' : null};
  expect(skeleton.getAttribute('aria-hidden')).toBe('true');
  expect(skeleton.querySelector('.sc-skeleton-hero')).not.toBeNull();
  expect(skeleton.querySelector('.sc-skeleton-card .sc-skeleton-chart')).not.toBeNull();
  expect(skeleton.querySelector('svg')).toBeNull();
});

/* The Boomplay mark */

test('Boomplay keeps its monogram: simple-icons 13.21.0 is not in node_modules to read a mark from', async () => {
  const {PlatformLogo, logoKey} = await import('../PlatformLogo.jsx');
  expect(logoKey('boomplay')).toBeNull();
  flushSync(() => root.render(<PlatformLogo platform="boomplay" />));
  expect(host.querySelector('.pl-monogram').textContent).toBe('B');
});
