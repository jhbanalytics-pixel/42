/* The Today boards as one full-width horizontal band: one card per list in a
   single row, grouped by kind, scrolled by swipe, wheel, keys or the edge
   arrows, with Coming up in 14 days below it. Rendered into a real DOM so the
   row, the arrows and the focus can be read. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React, {act} from 'react';
import {readFileSync} from 'node:fs';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TodayBoards} = await import('../TodayBoards.jsx');

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
afterAll(() => {
  GlobalRegistrator.unregister();
});

const show = (props) => flushSync(() => root.render(<TodayBoards {...props} />));
const css = (name) => readFileSync(new URL(`../../styles/${name}`, import.meta.url), 'utf8');
const entry = (rank, title, item_id) => ({rank, title, item_id});
const board = (platform, list, entries = [entry(1, 'Song One by Artist One', platform + list)]) => ({platform, list, entries, left_out: 0, left_out_reason: null});

/* The eight lists the brief names, in an order the payload might send them. */
const payload = () => [
  board('tiktok', 'Hashtag board, 7 days'),
  board('youtube', 'Trending videos, today'),
  board('spotify', 'Daily top songs'),
  board('app_store', 'Top free apps (iPhone)'),
  board('shazam', 'National chart'),
  board('apple_music', 'Top 100: South Africa'),
  board('shazam', 'Johannesburg chart'),
  board('boomplay', 'Trending songs'),
];

const scroller = () => host.querySelector('[data-band-scroll]');
const arrow = (side) => host.querySelector(`[data-band-arrow="${side}"]`);
const cards = () => [...host.querySelectorAll('[data-board-card]')];
const heads = () => cards().map((card) => card.querySelector('.tb-card-title').textContent);

/* happy-dom has no layout, so a test says how wide the row is, how much shows
   and where it is scrolled, then scrolls. */
function lay(el, {scrollWidth, clientWidth, scrollLeft}){
  Object.defineProperty(el, 'scrollWidth', {configurable: true, get: () => scrollWidth});
  Object.defineProperty(el, 'clientWidth', {configurable: true, get: () => clientWidth});
  el.scrollLeft = scrollLeft;
  const calls = [];
  el.scrollBy = (arg) => { calls.push(arg); };
  return calls;
}
/* A scroll is a continuous event in React, so its update is flushed by act, not flushSync. */
const scrollTo = async (el, left) => {
  el.scrollLeft = left;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  try {
    await act(async () => { el.dispatchEvent(new Event('scroll', {bubbles: false})); });
  } finally {
    globalThis.IS_REACT_ACT_ENVIRONMENT = false;
  }
};

test('every list is one card in a single band, music charts then apps then social boards', () => {
  show({boards: payload()});
  expect(host.querySelectorAll('[data-band]')).toHaveLength(1);
  expect(cards()).toHaveLength(8);
  expect(heads()).toEqual([
    'Spotify Daily top songs', 'Shazam National chart', 'Apple Music Top 100: South Africa', 'Shazam Johannesburg chart', 'Boomplay Trending songs',
    'App Store Top free apps (iPhone)',
    'TikTok Hashtag board, 7 days', 'YouTube Trending videos, today',
  ]);
  const groups = [...host.querySelectorAll('[data-board-group]')];
  expect(groups.map((g) => g.getAttribute('data-board-group'))).toEqual(['music', 'apps', 'social']);
  expect(groups.map((g) => g.querySelectorAll('[data-board-card]').length)).toEqual([5, 1, 2]);
  /* One row: every group is inside the one track, none in a second band. */
  const track = host.querySelector('[data-band-track]');
  expect(groups.every((g) => g.parentElement === track)).toBe(true);
  expect(scroller().contains(track)).toBe(true);
});

test('each group has a small label that names its kind and counts its cards', () => {
  show({boards: payload()});
  const labels = [...host.querySelectorAll('.tb-group-title')].map((n) => n.textContent.replace(/\s+/g, ' ').trim());
  expect(labels).toEqual(['Music charts 5', 'Apps 1', 'Social boards 2']);
});

test('the card keeps its logo, name, best rank caption, rows and Show all', () => {
  const many = Array.from({length: 8}, (_, i) => entry(i + 1, `Track ${i + 1} by Band ${i + 1}`, 't' + i));
  show({boards: [board('spotify', 'Daily top songs', many)]});
  const card = cards()[0];
  expect(card.querySelector('.tb-logo svg[data-logo="spotify"]')).not.toBeNull();
  expect(card.querySelector('.tb-caption').textContent).toBe('Best rank today');
  expect(card.querySelectorAll('.tb-row')).toHaveLength(5);
  expect(card.querySelector('button.tb-more').textContent).toBe('Show all 8');
});

test('the band is a labelled, focusable region and the cards are list items', () => {
  show({boards: payload()});
  const el = scroller();
  expect(el.getAttribute('role')).toBe('region');
  expect(el.getAttribute('aria-label')).toMatch(/platform lists/i);
  expect(el.getAttribute('tabindex')).toBe('0');
  expect(el.getAttribute('aria-roledescription')).toBeNull();
  for (const list of host.querySelectorAll('[data-board-group] > ul')) expect(list.querySelectorAll(':scope > li > [data-board-card]').length).toBeGreaterThan(0);
  expect(host.querySelector('[data-band]').getAttribute('data-band')).toBe('');
});

test('no arrow shows while every card fits', async () => {
  show({boards: [board('spotify', 'Daily top songs')]});
  lay(scroller(), {scrollWidth: 300, clientWidth: 600, scrollLeft: 0});
  await scrollTo(scroller(), 0);
  expect(arrow('prev')).toBeNull();
  expect(arrow('next')).toBeNull();
});

test('only the forward arrow shows at the start, both in the middle, only the back arrow at the end', async () => {
  show({boards: payload()});
  const el = scroller();
  lay(el, {scrollWidth: 2000, clientWidth: 600, scrollLeft: 0});
  await scrollTo(el, 0);
  expect(arrow('prev')).toBeNull();
  expect(arrow('next')).not.toBeNull();
  await scrollTo(el, 700);
  expect(arrow('prev')).not.toBeNull();
  expect(arrow('next')).not.toBeNull();
  await scrollTo(el, 1400);
  expect(arrow('prev')).not.toBeNull();
  expect(arrow('next')).toBeNull();
});

test('the arrows are labelled buttons that scroll the row a view at a time', async () => {
  show({boards: payload()});
  const el = scroller();
  const calls = lay(el, {scrollWidth: 2000, clientWidth: 600, scrollLeft: 700});
  await scrollTo(el, 700);
  const next = arrow('next');
  const prev = arrow('prev');
  expect(next.tagName).toBe('BUTTON');
  expect(next.getAttribute('type')).toBe('button');
  expect(next.getAttribute('aria-label')).toBe('Show later platform lists');
  expect(prev.getAttribute('aria-label')).toBe('Show earlier platform lists');
  flushSync(() => next.click());
  flushSync(() => prev.click());
  expect(calls).toHaveLength(2);
  expect(calls[0].left).toBeGreaterThan(0);
  expect(calls[1].left).toBeLessThan(0);
  expect(Math.abs(calls[0].left)).toBeLessThanOrEqual(600);
});

test('the arrows jump with no animation when the reader asks for reduced motion', async () => {
  const original = window.matchMedia;
  window.matchMedia = (query) => ({matches: /prefers-reduced-motion/.test(query), media: query, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}});
  try {
    show({boards: payload()});
    const el = scroller();
    const calls = lay(el, {scrollWidth: 2000, clientWidth: 600, scrollLeft: 0});
    await scrollTo(el, 0);
    flushSync(() => arrow('next').click());
    expect(calls[0].behavior).toBe('auto');
  } finally {
    window.matchMedia = original;
  }
});

test('the left and right keys move the focused band one card step, and other keys are left alone', async () => {
  show({boards: payload()});
  const el = scroller();
  const calls = lay(el, {scrollWidth: 2000, clientWidth: 600, scrollLeft: 700});
  await scrollTo(el, 700);
  const press = (key) => {
    const event = new KeyboardEvent('keydown', {key, bubbles: true, cancelable: true});
    el.dispatchEvent(event);
    return event;
  };
  expect(press('ArrowRight').defaultPrevented).toBe(true);
  expect(press('ArrowLeft').defaultPrevented).toBe(true);
  expect(calls[0].left).toBeGreaterThan(0);
  expect(calls[1].left).toBeLessThan(0);
  expect(press('ArrowDown').defaultPrevented).toBe(false);
  expect(press('a').defaultPrevented).toBe(false);
  expect(calls).toHaveLength(2);
});

test('the arrow keys do not scroll the band when the row already fits', () => {
  show({boards: [board('spotify', 'Daily top songs')]});
  const el = scroller();
  const calls = lay(el, {scrollWidth: 300, clientWidth: 600, scrollLeft: 0});
  const event = new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true, cancelable: true});
  el.dispatchEvent(event);
  expect(calls).toHaveLength(0);
  expect(event.defaultPrevented).toBe(false);
});

test('an arrow that has just hidden itself hands its focus to the band', async () => {
  show({boards: payload()});
  const el = scroller();
  lay(el, {scrollWidth: 2000, clientWidth: 600, scrollLeft: 1000});
  await scrollTo(el, 1000);
  arrow('next').focus();
  expect(document.activeElement).toBe(arrow('next'));
  await scrollTo(el, 1400);
  expect(arrow('next')).toBeNull();
  expect(document.activeElement === el || document.activeElement === arrow('prev')).toBe(true);
});

test('the band carries its own scroll state for the edge fades', async () => {
  show({boards: payload()});
  const el = scroller();
  lay(el, {scrollWidth: 2000, clientWidth: 600, scrollLeft: 0});
  await scrollTo(el, 0);
  const wrap = host.querySelector('[data-band]');
  expect(wrap.hasAttribute('data-more-before')).toBe(false);
  expect(wrap.hasAttribute('data-more-after')).toBe(true);
  await scrollTo(el, 700);
  expect(wrap.hasAttribute('data-more-before')).toBe(true);
  expect(wrap.hasAttribute('data-more-after')).toBe(true);
  await scrollTo(el, 1400);
  expect(wrap.hasAttribute('data-more-before')).toBe(true);
  expect(wrap.hasAttribute('data-more-after')).toBe(false);
});

test('the All view keeps one band for each market with its own cards', () => {
  show({groups: [
    {market: 'ZA', label: 'South Africa', boards: payload()},
    {market: 'NG', label: 'Nigeria', boards: [board('apple_music', 'Top 100: Nigeria')]},
    {market: 'KE', label: 'Kenya', boards: []},
  ]});
  const bands = [...host.querySelectorAll('[data-band]')];
  expect(bands).toHaveLength(2);
  expect(bands[0].querySelectorAll('[data-board-card]')).toHaveLength(8);
  expect(bands[1].querySelectorAll('[data-board-card]')).toHaveLength(1);
  const labels = [...host.querySelectorAll('[data-band-scroll]')].map((n) => n.getAttribute('aria-label'));
  expect(new Set(labels).size).toBe(2);
  expect(host.querySelector('[data-board-market="KE"]').textContent).toContain('No platform lists were read today.');
  const ids = [...host.querySelectorAll('[id]')].map((n) => n.id);
  expect(new Set(ids).size).toBe(ids.length);
});

test('an empty payload has no band and no arrows', () => {
  show({boards: []});
  expect(host.querySelector('[data-band]')).toBeNull();
  expect(host.querySelector('[data-band-arrow]')).toBeNull();
  expect(host.textContent).toContain('No platform lists were read today.');
});

/* The stylesheet: the layout facts that happy-dom cannot compute. */

const rule = (source, selector) => {
  const text = source.replace(/\/\*[\s\S]*?\*\//g, '');
  const found = [...text.matchAll(/([^{}]+)\{([^}]*)\}/g)].filter((m) => m[1].split(',').map((s) => s.trim()).includes(selector));
  return found.map((m) => m[2]).join(';');
};

test('the stylesheet lays the cards in one snapping row of equal fixed width, with no page scroll', () => {
  const text = css('today-boards.css');
  const scroll = rule(text, '.tb-scroll');
  expect(scroll).toMatch(/overflow-x:\s*auto/);
  expect(scroll).toMatch(/scroll-snap-type:\s*x\s+(mandatory|proximity)/);
  expect(scroll).toMatch(/overscroll-behavior-x:\s*contain/);
  expect(scroll).toMatch(/max-inline-size:\s*100%|max-width:\s*100%/);
  expect(rule(text, '.tb-track')).toMatch(/display:\s*flex/);
  expect(rule(text, '.tb-track')).toMatch(/align-items:\s*stretch/);
  expect(rule(text, '.tb-grid')).toMatch(/display:\s*flex/);
  expect(rule(text, '.tb-grid')).toMatch(/align-items:\s*stretch/);
  expect(rule(text, '.tb-grid')).not.toMatch(/flex-wrap:\s*wrap/);
  expect(rule(text, '.tb-grid > li')).toMatch(/flex:\s*0\s+0\s+var\(--tb-card-w\)/);
  expect(rule(text, '.tb-grid > li')).toMatch(/scroll-snap-align:\s*start/);
  expect(rule(text, '.tb-card')).toMatch(/block-size:\s*100%|height:\s*100%/);
  const width = rule(text, '.tb') + rule(text, '.tb-band');
  const w = width.match(/--tb-card-w:\s*clamp\(\s*([\d.]+)rem\s*,\s*([\d.]+)vw\s*,\s*([\d.]+)rem\s*\)/);
  expect(w).not.toBeNull();
  expect(Number(w[1])).toBeGreaterThanOrEqual(17.5);
  expect(Number(w[3])).toBeLessThanOrEqual(20);
  expect(Number(w[2])).toBeLessThanOrEqual(85);
});

test('the stylesheet fades the edges, draws a focus ring on the band and the arrows, and keeps targets large', () => {
  const text = css('today-boards.css');
  expect(text).toMatch(/\.tb-band\[data-more-after\][\s\S]*?mask-image/);
  expect(text).toMatch(/\.tb-scroll:focus-visible\s*\{[^}]*outline:/);
  expect(text).toMatch(/\.tb-arrow:focus-visible\s*\{[^}]*outline:/);
  expect(rule(text, '.tb-arrow')).toMatch(/inline-size:\s*var\(--target-min\)/);
  expect(rule(text, '.tb-arrow')).toMatch(/block-size:\s*var\(--target-min\)/);
  /* The row is a positioned layer too, so the strip that holds the arrows needs a z-index to paint over it. */
  expect(rule(text, '.tb-arrows')).toMatch(/z-index:\s*1/);
});

test('smooth scrolling and the fades only animate when motion is allowed', () => {
  const text = css('today-boards.css');
  const motion = [...text.matchAll(/@media\s*\(prefers-reduced-motion:\s*no-preference\)\s*\{([\s\S]*?\n\})/g)].find((m) => /scroll-behavior/.test(m[1]));
  expect(motion).not.toBeUndefined();
  expect(motion[1]).toMatch(/scroll-behavior:\s*smooth/);
  const outside = text.replace(motion[0], "");
  expect(outside).not.toMatch(/scroll-behavior:\s*smooth/);
});

test('the stylesheet has no dash characters in prose and no fixed pixel widths', () => {
  const text = css('today-boards.css');
  expect(text).not.toMatch(new RegExp('[' + String.fromCharCode(0x2013, 0x2014) + ']'));
  expect(text).not.toMatch(/(?<![-\w])(?:width|min-width):\s*\d{3,}px/);
  expect(text).not.toMatch(/font-size:\s*\d+px/);
});

/* Coming up in 14 days sits below the band, full width. */

test('the stylesheet no longer sets Coming up beside the boards', () => {
  const text = css('today42.css');
  expect(text).not.toMatch(/data-section="moments"\]\):has\(> \[data-section="boards"\]\)/);
  expect(text).not.toMatch(/repeat\(2,\s*minmax\(0,\s*1fr\)\)[^}]*column-gap:\s*var\(--s-6\)/);
});

/* Found in the phone render: each card carries screen reader text that is
   absolutely positioned. Unless the scrolling row is its containing block,
   that text sits outside the row's clip and widens the whole page. */
test('the scrolling row is the containing block of the cards, so hidden text cannot widen the page', () => {
  const text = css('today-boards.css');
  expect(rule(text, '.tb-scroll')).toMatch(/position:\s*relative/);
  expect(rule(text, '.tb-band')).toMatch(/position:\s*relative/);
});

test('the Show all button has square corners like the rest of the card', () => {
  expect(rule(css('today-boards.css'), 'html .oi-product .tb .tb-more')).toMatch(/border-radius:\s*0/);
});

test('Coming up in 14 days wraps its date chips in a row, each as wide as its words', () => {
  const text = css('today42.css');
  expect(rule(text, '.t42-below [data-section="moments"] .t42-row')).toMatch(/display:\s*flex/);
  expect(rule(text, '.t42-below [data-section="moments"] .t42-row')).toMatch(/flex-wrap:\s*wrap/);
  expect(rule(text, '.t42-below [data-section="moments"] .t42-chip')).toMatch(/flex:\s*0\s+1\s+auto/);
});

/* Found in the phone render: arrows over the cards covered a card's logo and
   title. They now sit together in the label row above the cards, at the end. */
test('the arrows sit together in one corner strip before the row, and the band says when it can scroll', async () => {
  show({boards: payload()});
  const el = scroller();
  const wrap = host.querySelector('[data-band]');
  lay(el, {scrollWidth: 300, clientWidth: 600, scrollLeft: 0});
  await scrollTo(el, 0);
  expect(wrap.hasAttribute('data-scrollable')).toBe(false);
  expect(wrap.querySelector('.tb-arrows')).toBeNull();
  lay(el, {scrollWidth: 2000, clientWidth: 600, scrollLeft: 700});
  await scrollTo(el, 700);
  expect(wrap.hasAttribute('data-scrollable')).toBe(true);
  const strip = wrap.querySelector('.tb-arrows');
  expect(strip.parentElement).toBe(wrap);
  expect(wrap.firstElementChild).toBe(strip);
  expect([...strip.querySelectorAll('[data-band-arrow]')].map((a) => a.getAttribute('data-band-arrow'))).toEqual(['prev', 'next']);
  await scrollTo(el, 1400);
  expect([...wrap.querySelectorAll('.tb-arrows [data-band-arrow]')].map((a) => a.getAttribute('data-band-arrow'))).toEqual(['prev']);
});

test('the stylesheet puts the arrow strip in the corner of the label row and gives that row the arrows height', () => {
  const text = css('today-boards.css');
  const strip = rule(text, '.tb-arrows');
  expect(strip).toMatch(/position:\s*absolute/);
  expect(strip).toMatch(/inset-block-start:\s*0/);
  expect(strip).toMatch(/inset-inline-end:\s*0/);
  expect(strip).toMatch(/z-index:\s*1/);
  expect(rule(text, '.tb-arrow')).not.toMatch(/position:\s*absolute/);
  expect(rule(text, '.tb-band[data-scrollable] .tb-group-title')).toMatch(/min-block-size:\s*var\(--target-min\)/);
});

/* The card design: brand header, hero row, tie groups, Show all with a chevron. */
const {brandOf} = await import('../PlatformLogo.jsx');
const rankOf = (row) => row.querySelector('.tb-rank').textContent;
const rows = (card) => [...card.querySelectorAll('.tb-row')];

test('a platform with an official mark has its brand colour, a readable ink on it, and one without has neither', () => {
  expect(brandOf('spotify').hex).toBe('#1ED760');
  expect(brandOf('apple_music').hex).toBe('#FA243C');
  expect(brandOf('shazam').hex).toBe('#0088FF');
  expect(brandOf('app_store_iphone').hex).toBe('#0D96F6');
  expect(brandOf('youtube').hex).toBe('#FF0000');
  expect(brandOf('tiktok').hex).toBe('#000000');
  expect(brandOf('boomplay')).toBeNull();
  expect(brandOf('nairaland')).toBeNull();
  expect(brandOf('spotify').on).toBe('#111111');
  expect(brandOf('youtube').on).toBe('#FFFFFF');
  expect(brandOf('tiktok').on).toBe('#FFFFFF');
});

test('a card with a brand carries it as one custom property for its top rule and its logo chip', () => {
  show({boards: payload()});
  const spotify = [...cards()].find((c) => c.getAttribute('data-platform') === 'spotify');
  expect(spotify.getAttribute('style')).toMatch(/--brand:\s*#1ED760/i);
  expect(spotify.getAttribute('style')).toMatch(/--brand-on:\s*#111111/i);
  const boomplay = [...cards()].find((c) => c.getAttribute('data-platform') === 'boomplay');
  expect(boomplay.getAttribute('style') || '').not.toMatch(/--brand/);
});

test('only the first row, when it is rank 1, is the hero row', () => {
  show({boards: [board('spotify', 'Daily top songs', [entry(1, 'Lead by Artist', 'a'), entry(2, 'Next by Artist', 'b'), entry(3, 'Third by Artist', 'c')])]});
  const list = rows(cards()[0]);
  expect(list.map((row) => row.hasAttribute('data-hero'))).toEqual([true, false, false]);
  show({boards: [board('spotify', 'Daily top songs', [entry(2, 'Not first by Artist', 'a'), entry(3, 'Next by Artist', 'b')])]});
  expect(rows(cards()[0]).some((row) => row.hasAttribute('data-hero'))).toBe(false);
});

test('a tie group shows its rank once, then says tied, and never an equals sign', () => {
  show({boards: [board('apple_music', 'Top', [entry(1, 'One A by X', 'a'), entry(1, 'One B by X', 'b'), entry(2, 'Two by X', 'c'), entry(2, 'Two B by X', 'd'), entry(2, 'Two C by X', 'e')])]});
  const list = rows(cards()[0]);
  expect(list.map(rankOf)).toEqual(['1', 'tied', '2', 'tied', 'tied']);
  expect(cards()[0].textContent).not.toContain('=');
  expect(list[1].querySelector('.sr-only').textContent).toMatch(/Tied rank 1/);
  expect(list.map((row) => row.hasAttribute('data-hero'))).toEqual([true, false, false, false, false]);
});

test('Show all is a text button with a chevron that turns when the list is open', () => {
  const many = Array.from({length: 8}, (_, i) => entry(i + 1, `Track ${i + 1} by Band ${i + 1}`, 't' + i));
  show({boards: [board('spotify', 'Daily top songs', many)]});
  const button = cards()[0].querySelector('button.tb-more');
  expect(button.querySelector('svg.tb-more-mark')).not.toBeNull();
  expect(button.textContent).toBe('Show all 8');
  flushSync(() => button.click());
  expect(button.textContent).toBe('Show fewer');
  expect(button.getAttribute('aria-expanded')).toBe('true');
});

test('the card is flat and square like 42 trend cards, with a brand top rule, a hover lift and no shadow', () => {
  const text = css('today-boards.css');
  const card = rule(text, '.tb-card');
  expect(card).toMatch(/border-top:\s*2px solid var\(--brand,/);
  expect(card).toMatch(/background:\s*var\(--surface\)/);
  expect(card).not.toMatch(/border-radius|box-shadow/);
  expect(text).not.toMatch(/box-shadow:\s*(?!none|inset)/);
  expect(rule(text, '.tb-card:hover')).toMatch(/border-color:\s*var\(--line-2\)/);
  expect(text).toMatch(/\.tb-card:hover[^{]*\{[^}]*transform:\s*translateY\(-2px\)/);
  expect(rule(text, '.tb-row[data-hero] .tb-rank')).toMatch(/font-family:\s*var\(--nd-face\)/);
  expect(rule(text, '.tb-row[data-hero] .tb-rank')).toMatch(/color:\s*var\(--heat-3\)/);
  expect(rule(text, '.tb-logo')).toMatch(/background:\s*var\(--brand,/);
});
