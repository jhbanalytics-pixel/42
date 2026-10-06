/* The Map names every page the way the menu does and shows no route codes.
   It still said Briefing, Explorer, Console and My board, printed hash routes
   such as #/pulse under each name, and numbered its lanes with red numerals. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());
const {renderToStaticMarkup} = await import('react-dom/server');
const {MapPage} = await import('../../map.jsx');
const {RAIL_ALL} = await import('../../rail42.jsx');

const html = renderToStaticMarkup(React.createElement(MapPage));
const dom = new window.DOMParser().parseFromString(html, 'text/html');
const menu = new Map(RAIL_ALL.map((item) => [item.href, item.label]));

test('every Map node is named as the menu names its page', () => {
  const nodes = [...dom.querySelectorAll('a.map-node')];
  expect(nodes.length).toBeGreaterThan(0);
  for (const node of nodes){
    const href = node.getAttribute('href');
    expect(menu.has(href), href + ' is a menu page').toBe(true);
    expect(node.querySelector('.map-node-label').textContent).toBe(menu.get(href));
  }
});

test('the Map prints no route codes, retired names or lane numerals', () => {
  const words = dom.body.textContent;
  expect(words).not.toMatch(/#\//);
  expect(words).not.toMatch(/\b(?:Briefing|Explorer|Console|My board)\b/);
  expect(words).not.toMatch(/\b0[1-3]\b/);
});

test('the Discover, Seed path, Seeds rail says its order in words and marks the page the reader is on', async () => {
  const {LoopRail} = await import('../../loopRail.jsx');
  const rail = new window.DOMParser().parseFromString(renderToStaticMarkup(React.createElement(LoopRail, {active: 'explorer'})), 'text/html');
  expect([...rail.querySelectorAll('.loop-stage-eyebrow')].map((node) => node.textContent)).toEqual(['Step 1 of 3', 'Step 2 of 3', 'Step 3 of 3']);
  expect(rail.body.textContent).not.toMatch(/Stage 0\d/);
  expect([...rail.querySelectorAll('[aria-current="step"]')].map((node) => node.querySelector('.loop-stage-name').textContent)).toEqual(['Seed path']);
});
