/* Monday 5 October 2026: a panel series is posts scaled for the share of the
   panel read that day (views.sql v_series_daily, posts * k), so a day can be
   2.3333333333333335. The chart printed that float in its caption and on its
   top tick. Chart figures are rounded to one decimal; whole numbers stay
   whole. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';

GlobalRegistrator.register();
const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TrendCard} = await import('../TrendCard.jsx');
const {seriesFigure} = await import('../../api.js');

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
afterAll(async () => { await tick(); await GlobalRegistrator.unregister(); });

test('a series figure keeps at most one decimal and whole numbers whole', () => {
  expect(seriesFigure(7 / 3)).toBe('2.3');
  expect(seriesFigure(2.96)).toBe('3');
  expect(seriesFigure(4)).toBe('4');
  expect(seriesFigure(12500.25)).toBe('12 500.3');
  expect(seriesFigure(0.04)).toBe('0');
});

test('the Today chart never prints a raw float in its caption, tick or pointer read-out', async () => {
  const card = JSON.parse(JSON.stringify(todayFixture.markets[0].cards[0]));
  const points = card.sparkline.points;
  points.forEach((p, i) => { p.value = i < points.length - 1 ? 0 : 7 / 3; p.expected_low = null; p.expected_high = null; });
  card.sparkline.unit = 'posts a day';
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  flushSync(() => root.render(<TrendCard card={card} market="NG" date="2026-10-05" posts={false} />));
  const figure = host.querySelector('figure.t42-trend');
  expect(figure.textContent).not.toContain('2.333');
  expect(host.querySelector('.t42-trend-caption').textContent).toContain('latest 2.3');
  const ticks = [...host.querySelectorAll('svg.t42-spark .t42-tick')].map((t) => t.textContent);
  expect(ticks).toContain('2.3');
  const svg = host.querySelector('svg.t42-spark');
  const dots = [...svg.querySelectorAll('.t42-dot')];
  svg.dispatchEvent(new window.PointerEvent('pointermove', {bubbles: true, clientX: Number(dots.at(-1).getAttribute('cx')), clientY: 10}));
  await tick();
  expect(host.querySelector('.t42-probe').textContent).toContain('2.3');
  expect(host.querySelector('.t42-probe').textContent).not.toContain('2.333');
  root.unmount();
  host.remove();
});
