/* The 42 rail (EXPERIENCE.md, Navigation): Today, Ask, Discover, Compare,
   Investigations and Dossiers, History, Alerts and Coverage at the foot, and
   every other page under More, nothing deleted. It is the only navigation:
   the vendored shell's five jobs are hidden, so Build and Fieldwork move
   under More. Every link is a hash the app already resolves, the current
   page is marked, at phone width the rail folds into a bottom bar, and the
   theme offers Light (the default), Dark and Match system. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import postcss from 'postcss';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {Rail42, RAIL_TOP, RAIL_WORK, RAIL_MORE, RAIL_MORE_GROUPS} = await import('../../rail42.jsx');
const {resolveHostRoute, askAliasTarget, storedThemePreference, resolveTheme} = await import('../../App.jsx');

let host = null;
let root = null;
let themeCalls = [];

const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));
const pause = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
// Polls until check() holds or the deadline passes; the caller still asserts afterwards.
async function waitUntil(check, {interval = 10, timeout = 2000} = {}){
  const deadline = Date.now() + timeout;
  while (!check() && Date.now() < deadline) await pause(interval);
}
const nav = () => host.querySelector('nav[aria-label="Main"]');
const labels = (scope) => [...scope.querySelectorAll('a')].map((a) => a.textContent.trim());
const hrefs = (scope) => [...scope.querySelectorAll('a')].map((a) => a.getAttribute('href'));
const moreButton = () => [...host.querySelectorAll('button')].find((b) => b.textContent.trim().startsWith('More'));
const current = () => [...host.querySelectorAll('a[aria-current="page"]')].map((a) => a.textContent.trim());

function viewport(width){
  window.happyDOM.setViewport({width, height: 900});
}

function mount(props = {}){
  flushSync(() => root.render(
    <Rail42 route="pulse" askOpen={false} theme="daylight" onThemeChange={(value) => themeCalls.push(value)} {...props} />,
  ));
}

beforeEach(() => {
  themeCalls = [];
  viewport(1280);
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  try { localStorage.clear(); } catch (e){}
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

/* UX pass, 3 October 2026: the menu teaches one path. Start here holds the
   three pages a newcomer needs, Your work holds what a person keeps, and More
   holds two named groups: Dig deeper and How 42 works. Build left the menu
   (it repeats Ask, Investigations and Dossiers; #/console still opens it)
   and Map is named All pages. Page port, 3 October 2026: Board, Listen,
   Network and Browse read the desk API, so they stay off (legacy-redirects). */
const TOP = [['Today', '#/pulse'], ['Ask', '#/ask'], ['Discover', '#/explore']];
const WORK = [['Alerts', '#/alerts'], ['Investigations', '#/investigations'], ['Dossiers', '#/dossiers'], ['History', '#/history']];
const DEEPER = [['Compare', '#/compare'], ['Lexicon', '#/lexicon'], ['Communities', '#/communities'], ['Seed path', '#/seedpath'], ['Seeds', '#/seeds']];
const HOW = [
  ['Coverage', '#/coverage'], ['Fieldwork', '#/fieldwork'], ['Method', '#/method'], ['Schedules', '#/schedules'],
  ['Skins', '#/skins'], ['Hidden people', '#/people/hidden'], ['All pages', '#/map'],
];
const MORE = [...DEEPER, ...HOW];
const groupTitles = (scope) => [...scope.querySelectorAll('.rail42-group-title')].map((node) => node.textContent.trim());

test('the rail lists Start here, then Your work, then More with its two named groups', () => {
  mount();
  expect(nav()).not.toBeNull();
  const top = nav().querySelector('[data-rail-group="top"]');
  const work = nav().querySelector('[data-rail-group="work"]');
  expect(labels(top)).toEqual(TOP.map(([label]) => label));
  expect(hrefs(top)).toEqual(TOP.map(([, href]) => href));
  expect(labels(work)).toEqual(WORK.map(([label]) => label));
  expect(hrefs(work)).toEqual(WORK.map(([, href]) => href));
  /* Each list is named by its group's label. */
  expect(document.getElementById(top.getAttribute('aria-labelledby')).textContent).toBe('Start here');
  expect(document.getElementById(work.getAttribute('aria-labelledby')).textContent).toBe('Your work');

  const more = moreButton();
  expect(more.getAttribute('aria-expanded')).toBe('false');
  const panel = document.getElementById(more.getAttribute('aria-controls'));
  expect(panel.hidden).toBe(true);
  click(more);
  expect(more.getAttribute('aria-expanded')).toBe('true');
  expect(panel.hidden).toBe(false);
  expect(labels(panel)).toEqual(MORE.map(([label]) => label));
  expect(hrefs(panel)).toEqual(MORE.map(([, href]) => href));
  expect(groupTitles(panel)).toEqual(['Dig deeper', 'How 42 works']);

  /* The DOM order is the visual order: Start here, Your work, then More. */
  const order = [...nav().querySelectorAll('a, button')].map((node) => node.textContent.trim());
  expect(order.indexOf('Discover')).toBeLessThan(order.indexOf('Alerts'));
  expect(order.indexOf('History')).toBeLessThan(order.indexOf('More'));
});

test('the exported lists are the rendered ones, so nothing is kept only in markup', () => {
  expect(RAIL_TOP.map((item) => [item.label, item.href])).toEqual(TOP);
  expect(RAIL_WORK.map((item) => [item.label, item.href])).toEqual(WORK);
  expect(RAIL_MORE.map((item) => [item.label, item.href])).toEqual(MORE);
  expect(RAIL_MORE_GROUPS.map((group) => group.items.map((item) => [item.label, item.href]))).toEqual([DEEPER, HOW]);
});

test('every rail link is a hash the app already resolves to the page the link marks', () => {
  for (const item of [...RAIL_TOP, ...RAIL_WORK, ...RAIL_MORE]){
    const view = item.href.replace(/^#\//, '').split('?')[0].split('/')[0];
    expect(resolveHostRoute(view, undefined)).toBe(item.routes[0]);
  }
  /* Today keeps #/pulse, and the old #/today still lands on it. */
  expect(resolveHostRoute('today')).toBe('pulse');
  /* #/console?work=ask stays an alias of #/ask. */
  expect(askAliasTarget('#/console?work=ask')).toBe('/ask');
});

test('the current page is marked on every route, and only one link is marked', () => {
  const cases = [
    /* Build is off the menu since the UX pass, so its page marks no link. */
    ['pulse', false, 'Today'], ['ask', false, 'Ask'], ['console', true, 'Ask'], ['console', false, null],
    ['explore', false, 'Discover'], ['compare', false, 'Compare'],
    ['investigations', false, 'Investigations'], ['investigation', false, 'Investigations'],
    ['dossiers', false, 'Dossiers'], ['dossier', false, 'Dossiers'],
    ['history', false, 'History'], ['history-item', false, 'History'], ['alerts', false, 'Alerts'], ['coverage', false, 'Coverage'],
    ['seeds', false, 'Seeds'], ['seedpath', false, 'Seed path'], ['map', false, 'All pages'], ['lexicon', false, 'Lexicon'],
    /* Off the menu since 3 October 2026: their links open another page. */
    ['board', false, null], ['listen', false, null], ['network', false, null], ['browse', false, null],
    ['method', false, 'Method'], ['schedules', false, 'Schedules'], ['skins', false, 'Skins'], ['skin', false, 'Skins'],
    ['people-hidden', false, 'Hidden people'], ['communities', false, 'Communities'], ['community', false, 'Communities'],
    ['topic42', false, null], ['fieldwork', false, 'Fieldwork'],
  ];
  for (const [route, askOpen, expected] of cases){
    mount({route, askOpen});
    expect([route, current()]).toEqual([route, expected ? [expected] : []]);
  }
});

test('on a page under More the list opens on its own, so the marked link is on screen', () => {
  mount({route: 'lexicon'});
  const more = moreButton();
  expect(more.getAttribute('aria-expanded')).toBe('true');
  expect(document.getElementById(more.getAttribute('aria-controls')).hidden).toBe(false);
  expect(current()).toEqual(['Lexicon']);
});

test('More is a keyboard disclosure: Escape closes it and gives focus back to the button', async () => {
  mount();
  const more = moreButton();
  expect(more.tagName).toBe('BUTTON');
  expect(more.getAttribute('type')).toBe('button');
  expect(more.getAttribute('aria-expanded')).toBe('false');
  click(more);
  expect(more.getAttribute('aria-expanded')).toBe('true');
  const panel = document.getElementById(more.getAttribute('aria-controls'));
  panel.querySelector('a').focus();
  flushSync(() => panel.querySelector('a').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
  expect(more.getAttribute('aria-expanded')).toBe('false');
  expect(panel.hidden).toBe(false);
  expect(panel.getAttribute('aria-hidden')).toBe('true');
  expect(panel.hasAttribute('inert')).toBe(true);
  expect(document.activeElement).toBe(more);
  await waitUntil(() => panel.hidden);
  expect(panel.hidden).toBe(true);
  for (const link of nav().querySelectorAll('a')){
    expect(link.getAttribute('tabindex')).toBeNull();
    expect(link.getAttribute('href')).toMatch(/^#\//);
  }
});

test('ten rapid More toggles cancel stale cleanup after the panel reopens', async () => {
  mount({route: 'lexicon'});
  const more = moreButton();
  const panel = document.getElementById(more.getAttribute('aria-controls'));
  for (let i = 0; i < 10; i++) click(more);
  expect(more.getAttribute('aria-expanded')).toBe('true');
  expect(panel.hidden).toBe(false);
  expect(panel.hasAttribute('inert')).toBe(false);
  await pause(140);
  expect(more.getAttribute('aria-expanded')).toBe('true');
  expect(panel.hidden).toBe(false);
  expect(panel.hasAttribute('inert')).toBe(false);
});

test('outside pointer closes the wide More panel without moving focus to More', () => {
  mount({route: 'lexicon'});
  const more = moreButton();
  const panel = document.getElementById(more.getAttribute('aria-controls'));
  const outside = document.createElement('button');
  document.body.appendChild(outside);
  try {
    outside.focus();
    flushSync(() => outside.dispatchEvent(new Event('pointerdown', {bubbles: true})));
    expect(more.getAttribute('aria-expanded')).toBe('false');
    expect(panel.getAttribute('aria-hidden')).toBe('true');
    expect(panel.hasAttribute('inert')).toBe(true);
    expect(document.activeElement).toBe(outside);
  } finally {
    outside.remove();
  }
});

test('outside pointer closes the compact More sheet without moving focus to More', () => {
  viewport(390);
  mount();
  const more = moreButton();
  click(more);
  const panel = document.getElementById(more.getAttribute('aria-controls'));
  const outside = document.createElement('button');
  document.body.appendChild(outside);
  try {
    outside.focus();
    flushSync(() => outside.dispatchEvent(new Event('pointerdown', {bubbles: true})));
    expect(more.getAttribute('aria-expanded')).toBe('false');
    expect(panel.getAttribute('aria-hidden')).toBe('true');
    expect(panel.hasAttribute('inert')).toBe(true);
    expect(document.activeElement).toBe(outside);
  } finally {
    outside.remove();
  }
});

test('motion set to off while More is open closes the panel immediately', () => {
  const previousMotion = document.documentElement.getAttribute('data-motion');
  document.documentElement.setAttribute('data-motion', 'on');
  try {
    mount();
    const more = moreButton();
    click(more);
    const panel = document.getElementById(more.getAttribute('aria-controls'));
    document.documentElement.setAttribute('data-motion', 'off');
    click(more);
    expect(more.getAttribute('aria-expanded')).toBe('false');
    expect(panel.hidden).toBe(true);
    expect(panel.hasAttribute('inert')).toBe(true);
  } finally {
    if (previousMotion === null) document.documentElement.removeAttribute('data-motion');
    else document.documentElement.setAttribute('data-motion', previousMotion);
  }
});

test('a live reduced motion change settles a closing More panel at once', async () => {
  const css = postcss.parse(await Bun.file(new URL('../../styles/rail42.css', import.meta.url)).text());
  const quote = String.fromCharCode(34);
  const unquote = (selector) => selector.split(quote).join('');
  const closing = '.rail42-sheet[data-motion-state=closing]';
  let off = null;
  css.walkRules((rule) => {
    if (unquote(rule.selector) === '[data-motion=off] ' + closing) off = rule;
  });
  expect(off).not.toBeNull();
  if (!off) return;
  const declarations = (rule) => Object.fromEntries(rule.nodes.filter((node) => node.type === 'decl').map((node) => [node.prop, {value: node.value.trim(), important: Boolean(node.important)}]));
  expect(declarations(off)).toMatchObject({
    animation: {value: 'none', important: true},
    opacity: {value: '0', important: true},
    transform: {value: 'none', important: true},
  });
  let reduced = null;
  css.walkAtRules('media', (media) => {
    if (!media.params.includes('prefers-reduced-motion: reduce')) return;
    media.walkRules((rule) => { if (unquote(rule.selector) === closing) reduced = rule; });
  });
  expect(reduced).not.toBeNull();
  if (!reduced) return;
  expect(declarations(reduced)).toMatchObject({
    animation: {value: 'none', important: true},
    opacity: {value: '0', important: true},
    transform: {value: 'none', important: true},
  });
});
test('the desktop More button has a decorative chevron', () => {
  mount({only: 'wide'});
  const chevron = moreButton().querySelector('.rail42-more-chevron');
  expect(chevron).not.toBeNull();
  expect(chevron.getAttribute('aria-hidden')).toBe('true');
});

test('at phone width the rail is a bottom bar of four pages and More, and More holds everything else', () => {
  viewport(390);
  mount();
  const bar = nav();
  expect(bar.getAttribute('data-rail-layout')).toBe('bar');
  const inBar = [...bar.querySelectorAll('[data-rail-group="bar"] a')].map((a) => a.textContent.trim());
  expect(inBar).toEqual(['Today', 'Ask', 'Discover', 'Alerts']);
  const more = moreButton();
  click(more);
  const sheet = document.getElementById(more.getAttribute('aria-controls'));
  expect(labels(sheet)).toEqual(['Investigations', 'Dossiers', 'History', ...MORE.map(([label]) => label)]);
  expect(groupTitles(sheet)).toEqual(['Your work', 'Dig deeper', 'How 42 works']);
  /* Nothing is lost at phone width: every page is still one link away. */
  expect(new Set(labels(bar))).toEqual(new Set([...TOP, ...WORK, ...MORE].map(([label]) => label)));
  expect(labels(bar)).toHaveLength(TOP.length + WORK.length + MORE.length);
  expect(sheet.querySelector('select')).not.toBeNull();
});

test('at phone width a page held in the sheet marks the More button without opening the sheet', () => {
  viewport(390);
  mount({route: 'history'});
  const more = moreButton();
  expect(more.getAttribute('aria-expanded')).toBe('false');
  expect(more.hasAttribute('data-holds-current')).toBe(true);
  expect(more.textContent).toContain('History');
});

test('the theme offers Light, Dark and Match system, and reports the choice', () => {
  mount({theme: 'daylight'});
  const select = nav().querySelector('select');
  expect(select).not.toBeNull();
  const label = nav().querySelector('label[for="' + select.id + '"]');
  expect(select.id).toBeTruthy();
  expect(label.textContent.trim()).toBe('Theme');
  expect([...select.options].map((option) => [option.textContent.trim(), option.value])).toEqual([
    ['Light', 'daylight'], ['Dark', 'midnight'], ['Match system', 'system'],
  ]);
  expect(select.value).toBe('daylight');
  select.value = 'system';
  flushSync(() => select.dispatchEvent(new Event('change', {bubbles: true})));
  expect(themeCalls).toEqual(['system']);
});

test('Light is the default theme, a stored choice holds, and Match system follows the system', () => {
  localStorage.removeItem('oi-theme');
  expect(storedThemePreference()).toBe('daylight');
  localStorage.setItem('oi-theme', 'midnight');
  expect(storedThemePreference()).toBe('midnight');
  localStorage.setItem('oi-theme', 'system');
  expect(storedThemePreference()).toBe('system');
  localStorage.setItem('oi-theme', 'sepia');
  expect(storedThemePreference()).toBe('daylight');
  expect(resolveTheme('system', true)).toBe('midnight');
  expect(resolveTheme('system', false)).toBe('daylight');
  expect(resolveTheme('midnight', false)).toBe('midnight');
  expect(resolveTheme('daylight', true)).toBe('daylight');
});

/* The wide rail is the left column, so it comes before the shell and the
   page in the DOM and the Tab order matches what the reader sees. The phone
   bar is the foot of the screen, so it stays inside the shell after the
   workspace, where a dialog the page opens still paints over the bar. */
test('App mounts the wide rail before the shell and the phone bar after the workspace', async () => {
  const source = await Bun.file(new URL('../../App.jsx', import.meta.url)).text();
  const shellStart = source.indexOf('<InstrumentShell');
  const layerEnd = source.indexOf('</RouteLayer>');
  const shellEnd = source.indexOf('</InstrumentShell>');
  const compact = source.indexOf('<Rail42 only="compact"');
  const wide = source.indexOf('<Rail42 only="wide"');
  expect(shellStart).toBeGreaterThan(0);
  expect(layerEnd).toBeGreaterThan(0);
  expect(wide).toBeGreaterThan(0);
  expect(wide).toBeLessThan(shellStart);
  expect(compact).toBeGreaterThan(layerEnd);
  expect(compact).toBeLessThan(shellEnd);
  expect(source.match(/<Rail42 /g)).toHaveLength(2);
});

/* With the rail ahead of the shell, the shell's own skip link would come
   after every rail link, so the wide rail brings its own, first in the
   document. It moves focus to the workspace itself: a bare #instrument-workspace
   hash is a route change to the host router. */
test('the wide rail opens with a skip link that moves focus to the workspace and keeps the route', () => {
  const main = document.createElement('main');
  main.id = 'instrument-workspace';
  main.setAttribute('tabindex', '-1');
  document.body.appendChild(main);
  try {
    window.location.hash = '#/console';
    mount({only: 'wide', route: 'console'});
    const first = host.firstElementChild;
    expect(first.tagName).toBe('A');
    expect(first.textContent.trim()).toBe('Skip to workspace');
    expect(first.getAttribute('href')).toBe('#instrument-workspace');
    expect(first.nextElementSibling).toBe(nav());
    /* A reader's click is cancelable, so the handler can keep the hash. */
    flushSync(() => first.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true})));
    expect(document.activeElement).toBe(main);
    expect(window.location.hash).toBe('#/console');
  } finally {
    main.remove();
  }
});

test('the phone bar brings no skip link, since the shell keeps its own there', () => {
  viewport(390);
  mount({only: 'compact'});
  expect(nav()).not.toBeNull();
  expect(host.querySelectorAll('a[href="#instrument-workspace"]')).toHaveLength(0);
});

/* The shell still renders its five jobs; the rail's stylesheet hides them
   and the Menu button that opens them at phone width, collapses the column
   they held, and hides the shell's skip link where the rail brings its own.
   The rules come with the rail's chunk, so a chunk that fails to arrive
   leaves the shell's rail showing. The browser proof is console-focus. */
test('the rail stylesheet hides the shell job rail and its menu, and the shell skip link only at the rail width', async () => {
  const css = await Bun.file(new URL('../../styles/rail42.css', import.meta.url)).text();
  const plain = css.replace(/\/\*[^]*?\*\//g, '');
  const wide = plain.slice(plain.indexOf('@media (min-width: 1024px)'));
  const hides = (selector, text) => new RegExp(selector.replace(/[.>()]/g, (c) => '\\' + c).replace(/ /g, '\\s*') + '[^{]*\\{[^}]*display:\\s*none').test(text);
  expect(hides('.app.has-rail42:has(nav.rail42) .instrument-job-rail', plain)).toBe(true);
  // Never hidden on the class alone: a rail that failed to mount leaves the shell's navigation in place.
  expect(hides('.app.has-rail42 .instrument-job-rail', plain)).toBe(false);
  expect(hides('.app.has-rail42:has(nav.rail42) .instrument-menu', plain)).toBe(true);
  expect(hides('.app.has-rail42:has(nav.rail42) > .oi-product > .instrument-skip-link', wide)).toBe(true);
  expect(hides('.app.has-rail42:has(nav.rail42) > .oi-product > .instrument-skip-link', plain.slice(0, plain.indexOf('@media (min-width: 1024px)')))).toBe(false);
});

test('each mount shows only at its own width', () => {
  viewport(390);
  mount({only: 'wide'});
  expect(nav()).toBeNull();
  mount({only: 'compact'});
  expect(nav().getAttribute('data-rail-layout')).toBe('bar');
  flushSync(() => root.render(null));
  viewport(1280);
  mount({only: 'compact'});
  expect(nav()).toBeNull();
  mount({only: 'wide'});
  expect(nav().getAttribute('data-rail-layout')).toBe('rail');
});

test('the rail uses body and metadata roles, keeps red to focus, and never scrolls sideways', async () => {
  const css = await Bun.file(new URL('../../styles/rail42.css', import.meta.url)).text();
  const rail = css.split('.rail42 {')[1]?.split('}')[0] || '';
  const links = css.split('.rail42-link,')[1]?.split('}')[0] || '';
  const theme = css.split('.rail42-theme {')[1]?.split('}')[0] || '';
  const select = css.split('.rail42-theme select {')[1]?.split('}')[0] || '';
  const mobile = css.split('.rail42--bar > .rail42-list .rail42-link,')[1]?.split('}')[0] || '';
  expect(rail).toContain('font-size: var(--type-body)');
  expect(rail).toContain('line-height: var(--leading-body)');
  expect(links).toContain('font-size: var(--type-body)');
  expect(links).toContain('line-height: var(--leading-body)');
  expect(links).toContain('padding: var(--s-2) var(--s-4)');
  expect(theme).toContain('font-size: var(--type-meta)');
  expect(select).toContain('font-size: var(--type-body)');
  expect(mobile).toContain('font-size: var(--type-meta)');
  const rules = css.replace(/\/\*[^]*?\*\//g, '').split('}');
  const red = rules.filter((rule) => /--accent|--red|--color-daylight-red|#e4002b|rgb\(\s*228/i.test(rule));
  expect(red.length).toBeGreaterThan(0);
  expect(red.every((rule) => /:focus-visible/.test(rule))).toBe(true);
  expect(css).not.toMatch(/overflow-x:\s*(auto|scroll)/);
  const source = await Bun.file(new URL('../../rail42.jsx', import.meta.url)).text();
  expect(source + css).not.toMatch(/gen ?z|millennial|youth|google trends|prompt pulse|nano banana|—|–|[^-!]--[^\w-]/i);
});
