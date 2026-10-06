/* Charts, 3 October 2026: the shared chart forms draw only measured values,
   say a missing one in words, label marks directly and carry a hidden table
   with the same numbers. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());
const {renderToStaticMarkup} = await import('react-dom/server');
const {BarList, PartsBar, StepMeter, BulletBar, Dumbbell, SmallMultiples, RangeTimeline} = await import('../Charts42.jsx');
const draw = (el) => new window.DOMParser().parseFromString(renderToStaticMarkup(el), 'text/html');

test('a bar list scales to its largest measured row and says an unmeasured row in words', () => {
  const dom = draw(<BarList title="Posts by platform" caption="7 days" unit="posts" rows={[
    {key: 'tiktok', label: 'TikTok', value: 300, queryId: 'q1', focus: true},
    {key: 'x', label: 'X', value: 150},
    {key: 'yt', label: 'YouTube', value: null},
  ]} />);
  const fills = [...dom.querySelectorAll('.ch42-bar-fill')];
  expect(fills.map((f) => f.getAttribute('style'))).toEqual(['--share:1;--row:0', '--share:0.5;--row:1']);
  expect(fills[0].getAttribute('data-query-id')).toBe('q1');
  expect(dom.querySelector('.ch42-bar-row.is-focus .ch42-bar-label').textContent).toBe('TikTok');
  expect(dom.querySelector('.ch42-bar-none').textContent).toBe('Not measured');
  expect([...dom.querySelectorAll('table td')].map((td) => td.textContent)).toEqual(['300 posts', '150 posts', 'not measured']);
});

test('a bar list with nothing measured draws nothing', () => {
  expect(renderToStaticMarkup(<BarList rows={[{label: 'X', value: null}]} />)).toBe('');
});

test('a parts bar drops empty parts and writes each part\'s count and share', () => {
  const dom = draw(<PartsBar title="Held back" parts={[
    {key: 'a', label: 'Too few creators', value: 3},
    {key: 'b', label: 'Data issue', value: 1},
    {key: 'c', label: 'Other', value: 0},
  ]} />);
  expect(dom.querySelectorAll('.ch42-part')).toHaveLength(2);
  expect([...dom.querySelectorAll('.ch42-parts-key li')].map((li) => li.textContent)).toEqual(['Too few creators3 · 75%', 'Data issue1 · 25%']);
  const thirds = draw(<PartsBar parts={[{label: 'A', value: 1}, {label: 'B', value: 1}, {label: 'C', value: 1}]} />);
  expect([...thirds.querySelectorAll('.ch42-key-share')].map((s) => Number(s.textContent.replace(/\D/g, ''))).reduce((a, v) => a + v, 0)).toBe(100);
});

test('a step meter marks the steps done, the current step and those to come', () => {
  const dom = draw(<StepMeter title="Warming up" value={2} of={14} />);
  expect(dom.querySelectorAll('.ch42-step')).toHaveLength(14);
  expect(dom.querySelectorAll('.ch42-step.is-done')).toHaveLength(1);
  expect(dom.querySelectorAll('.ch42-step.is-now')).toHaveLength(1);
  expect(renderToStaticMarkup(<StepMeter value={null} of={14} />)).toBe('');
});

test('a bullet bar caps its share at the cap and needs both numbers', () => {
  const dom = draw(<BulletBar value={60} cap={50} unit="credits" />);
  expect(dom.querySelector('.ch42-bar-fill').getAttribute('style')).toBe('--share:1');
  expect(dom.querySelector('.ch42-bullet-figure').textContent).toBe('60 of 50 credits');
  expect(renderToStaticMarkup(<BulletBar value={null} cap={50} />)).toBe('');
});

test('a dumbbell joins two moments on one scale and marks a rise', () => {
  const dom = draw(<Dumbbell fromLabel="Last week" toLabel="This week" rows={[
    {key: 'a', label: 'amapiano', from: 10, to: 40},
    {key: 'b', label: 'braai', from: 20, to: 5},
    {key: 'c', label: 'new', from: null, to: 3},
  ]} />);
  expect(dom.querySelectorAll('.ch42-db-to.is-up')).toHaveLength(1);
  expect(dom.querySelectorAll('.ch42-db-track')).toHaveLength(2);
  expect([...dom.querySelectorAll('.ch42-bar-value')].map((v) => v.textContent)).toEqual(['10 to 40', '20 to 5', '']);
});

test('small multiples share one scale, break at a null day and mark the latest point', () => {
  const dom = draw(<SmallMultiples unit="posts" series={[
    {key: 'tt', label: 'TikTok', points: [{date: '2026-09-28', value: 4}, {date: '2026-09-29', value: null}, {date: '2026-09-30', value: 8}, {date: '2026-10-01', value: 6}]},
    {key: 'none', label: 'Reddit', points: [{date: '2026-09-28', value: null}]},
  ]} />);
  expect(dom.querySelectorAll('.ch42-multiple')).toHaveLength(1);
  expect(dom.querySelector('.ch42-line').getAttribute('d').match(/M/g)).toHaveLength(1);
  expect(dom.querySelector('.ch42-multiple-figure').textContent).toBe('18 posts');
  expect(dom.querySelectorAll('.ch42-now')).toHaveLength(1);
  expect(dom.querySelector('.ch42-multiple-days').textContent).toBe('28 Sep1 Oct');
  expect([...dom.querySelectorAll('table th[scope="row"]')].map((th) => th.parentElement.lastElementChild.textContent)).toEqual(['8 posts', 'not measured']);
});

test('a timeline draws rise and fade on one axis and leaves a running wave open', () => {
  const dom = draw(<RangeTimeline unit="posts" rows={[
    {key: 'a', label: '2024', start: '2024-03-01', peak: '2024-03-05', end: '2024-03-20', peakValue: 900},
    {key: 'b', label: 'Now', start: '2024-03-10', peak: '2024-03-12', end: null, peakValue: 300},
  ]} />);
  expect(dom.querySelectorAll('.ch42-tl-rise')).toHaveLength(2);
  expect(dom.querySelectorAll('.ch42-tl-fade')).toHaveLength(1);
  expect([...dom.querySelectorAll('table td')].map((td) => td.textContent)).toContain('still running');
});

test('unit rows draw one square per unit in state order, outline open states and name every count', async () => {
  const {UnitRows} = await import('../Charts42.jsx');
  const dom = draw(<UnitRows unit="sources" states={[
    {key: 'delivered', label: 'delivered', tone: 'ink'},
    {key: 'failed', label: 'failed', tone: 'alert'},
    {key: 'off', label: 'off', tone: 'open'},
  ]} rows={[
    {key: 'ZA', label: 'South Africa', counts: {delivered: 3, failed: 1, off: 0}},
    {key: 'XX', label: 'Empty', counts: {}},
  ]} />);
  expect(dom.querySelectorAll('.ch42-unit-row')).toHaveLength(1);
  expect([...dom.querySelectorAll('.ch42-unit-cells .ch42-unit')].map((c) => c.className.replace('ch42-unit ', ''))).toEqual(['ch42-unit-ink', 'ch42-unit-ink', 'ch42-unit-ink', 'ch42-unit-alert']);
  expect(dom.querySelector('.ch42-unit-key').textContent).toBe(' 3 delivered 1 failed');
  expect(dom.querySelector('.ch42-bar-label').textContent).toBe('South Africa · 4 sources');
});
