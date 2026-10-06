/* The Map lists every page in the menu exactly once, grouped by what a
   person is trying to do, each with one plain line on what you get there.
   It is built from one list checked here against the menu, so a page added
   to the menu and not to the Map fails this test. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());
const {renderToStaticMarkup} = await import('react-dom/server');
const {MapPage, MAP_GROUPS} = await import('../../map.jsx');
const {RAIL_ALL} = await import('../../rail42.jsx');

const menu = RAIL_ALL;
const listed = MAP_GROUPS.flatMap((group) => group.pages.map((page) => page.href));
const html = renderToStaticMarkup(React.createElement(MapPage));
const dom = new window.DOMParser().parseFromString(html, 'text/html');

test('every menu page is on the Map', () => {
  const missing = menu.filter((item) => !listed.includes(item.href)).map((item) => item.label);
  expect(missing).toEqual([]);
});

test('the Map lists each page once and nothing the menu does not hold', () => {
  expect(new Set(listed).size).toBe(listed.length);
  expect(listed.filter((href) => !menu.some((item) => item.href === href))).toEqual([]);
  expect([...dom.querySelectorAll('a.map-node')].map((a) => a.getAttribute('href')).sort()).toEqual(menu.map((item) => item.href).sort());
});

/* UX pass, 3 October 2026: All pages uses the menu's own four groups, so a
   person learns one set of names, and each page carries the question it
   answers (pageQuestions.js). */
test('the pages sit in the menu\'s four groups, each with a heading and one line', () => {
  const groups = [...dom.querySelectorAll('section.map42-group')];
  expect(groups.map((g) => g.querySelector('h2').textContent)).toEqual([
    'Start here',
    'Your work',
    'Dig deeper',
    'How 42 works',
  ]);
  for (const g of groups){
    expect(g.querySelector('.map42-group-blurb').textContent.length).toBeGreaterThan(20);
    expect(g.querySelectorAll('a.map-node').length).toBeGreaterThan(0);
  }
});

test('each page carries one plain question, with no dashes and no pipeline words', () => {
  for (const node of dom.querySelectorAll('a.map-node')){
    const desc = node.querySelector('.map42-desc').textContent;
    expect(desc).toMatch(/^[A-Z].{20,100}\?$/);
    /* Second critique pass: the joined last pair left half-empty lines, so
       descriptions are plain text again, wrapped by text-wrap: pretty. */
    expect(desc, 'no non-breaking space is forced into the line').not.toMatch(/\u00a0/);
    expect(desc).not.toMatch(/[–—]|--|\b(?:lane|route|series|run id|contract|estate)\b/i);
  }
});

test('the Map marks itself as the page you are on and uses no inline styles', () => {
  const here = [...dom.querySelectorAll('a[aria-current="page"]')];
  expect(here.map((a) => a.getAttribute('href'))).toEqual(['#/map']);
  expect(dom.querySelectorAll('[style]')).toHaveLength(0);
  expect(dom.body.textContent).not.toMatch(/Entry desks|Lenses|Reading the evidence/);
});
