/* The wide chart's latest figure sits beside its now dot, never under it.
   The dot is drawn last, so it stays whole (Lead polish, 17 October 2026);
   the label's end must therefore clear the dot's ring, or the dot covers the
   top right of the last digit whenever the latest day is near the peak. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();
const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {Sparkline} = await import('../TrendCard.jsx');

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
afterAll(async () => { await tick(); await GlobalRegistrator.unregister(); });

/* today-story.css: the wide now dot has a 1.5px ink ring. */
const RING = 1.5;
const GAP = 2;

function draw(values){
  const points = values.map((value, i) => ({date: '2026-10-' + String(i + 1).padStart(2, '0'), value}));
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  flushSync(() => root.render(<Sparkline sparkline={{unit: 'posts a day', points}} wide />));
  const svg = host.querySelector('svg.t42-spark');
  const label = svg.querySelector('.t42-now-label');
  const dot = svg.querySelector('.t42-dot-now');
  const out = {
    label: {x: Number(label.getAttribute('x')), anchor: label.getAttribute('text-anchor'), text: label.textContent},
    dot: {cx: Number(dot.getAttribute('cx')), r: Number(dot.getAttribute('r'))},
    labelBeforeDot: Boolean(label.compareDocumentPosition(dot) & 4),
  };
  root.unmount();
  host.remove();
  return out;
}

for (const [name, values] of [
  ['the latest day is the peak', [3, 5, 8, 12, 19]],
  ['the latest day is low', [19, 12, 8, 5, 3]],
]){
  test(`the now label ends clear of the now dot when ${name}`, () => {
    const {label, dot, labelBeforeDot} = draw(values);
    expect(label.anchor).toBe('end');
    expect(label.text).toBe(String(values.at(-1)));
    expect(labelBeforeDot).toBe(true);
    expect(label.x).toBeLessThanOrEqual(dot.cx - dot.r - RING / 2 - GAP);
  });
}
