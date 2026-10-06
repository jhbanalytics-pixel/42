/* Night Desk, 4 October 2026: the trend line answers a pointer. Moving over
   it marks the nearest measured day and reads out that day's figure; leaving
   clears it. A day that was not collected is never read out as a number. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';

GlobalRegistrator.register();
const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TrendCard} = await import('../TrendCard.jsx');

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
afterAll(async () => { await tick(); await GlobalRegistrator.unregister(); });

function mount(){
  const card = JSON.parse(JSON.stringify(todayFixture.markets[0].cards[0]));
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  flushSync(() => root.render(<TrendCard card={card} market="ZA" date="2026-09-30" posts={false} />));
  return {host, root, card};
}

async function move(svg, viewX){
  svg.dispatchEvent(new window.PointerEvent('pointermove', {bubbles: true, clientX: viewX, clientY: 10}));
  await tick();
}

test('pointing at the line reads out the nearest measured day', async () => {
  const {host, root, card} = mount();
  const svg = host.querySelector('svg.t42-spark');
  expect(host.querySelector('.t42-probe')).toBeNull();
  const dots = [...svg.querySelectorAll('.t42-dot')];
  const last = dots[dots.length - 1];
  await move(svg, Number(last.getAttribute('cx')));
  const probe = host.querySelector('.t42-probe');
  expect(probe).not.toBeNull();
  const points = card.sparkline.points.filter((p) => typeof p.value === 'number');
  expect(probe.textContent).toBe('30 Sep: ' + points[points.length - 1].value);
  svg.dispatchEvent(new window.PointerEvent('pointerout', {bubbles: true, relatedTarget: document.body}));
  await tick();
  expect(host.querySelector('.t42-probe')).toBeNull();
  root.unmount();
});

test('a day not collected is skipped for the nearest measured one', async () => {
  const {host, root, card} = mount();
  const svg = host.querySelector('svg.t42-spark');
  const gapIndex = card.sparkline.points.findIndex((p) => p.value === null);
  expect(gapIndex).toBeGreaterThan(0);
  const dots = [...svg.querySelectorAll('.t42-dot')];
  const before = Number(dots[gapIndex - 1].getAttribute('cx'));
  const after = Number(dots[gapIndex].getAttribute('cx'));
  await move(svg, (before + after) / 2 - 1);
  expect(host.querySelector('.t42-probe').textContent).not.toMatch(/null|NaN|undefined/);
  root.unmount();
});

// Design review, 4 October 2026: the state chip carries its state as data, so
// its colour can mean the state rather than painting every chip alike.
test('the state chip names its state for the colour key', () => {
  const {host, root, card} = mount();
  const chip = host.querySelector('.t42-state');
  expect(chip).not.toBeNull();
  expect(chip.getAttribute('data-state')).toBe(card.state);
  root.unmount();
});
