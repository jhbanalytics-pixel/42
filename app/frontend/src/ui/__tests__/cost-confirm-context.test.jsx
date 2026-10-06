/* Credit pass, 3 October 2026 (Albert: "the user needs context"). Before
   anything spends credits the confirm answers three plain questions, what
   it does, what it costs and what you get, and says what a credit is. A
   confirm given only a cost line keeps its one line. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());
const {renderToStaticMarkup} = await import('react-dom/server');
const {CostConfirm, CREDIT_NOTE} = await import('../SpikeConfirm.jsx');

const render = (props) => new window.DOMParser().parseFromString(renderToStaticMarkup(
  React.createElement(CostConfirm, {title: 'Ask why?', where: 'Somewhere', line: 'A quick scan, up to 60 credits.', confirmWord: 'Ask', onConfirm: () => {}, onClose: () => {}, ...props}),
), 'text/html');

test('a spend confirm says what it does, what it costs and what you get, then what a credit is', () => {
  const dom = render({does: '42 reads the posts again.', get: 'An answer on the Ask page.'});
  const rows = [...dom.querySelectorAll('[data-cost-confirm] > div')].map((row) => [row.querySelector('dt').textContent, row.querySelector('dd').textContent]);
  expect(rows).toEqual([
    ['What it does', '42 reads the posts again.'],
    ['What it costs', 'A quick scan, up to 60 credits.'],
    ['What you get', 'An answer on the Ask page.'],
  ]);
  expect(dom.querySelector('.w42-cost-note').textContent).toBe(CREDIT_NOTE);
  expect(CREDIT_NOTE).toMatch(/^Credits pay for/);
  expect(dom.body.textContent).not.toMatch(/[–—]|--/);
});

test('a confirm with only a cost line keeps that one line', () => {
  const dom = render({});
  expect(dom.querySelector('[data-cost-confirm]')).toBeNull();
  expect(dom.querySelector('.w42-line').textContent).toBe('A quick scan, up to 60 credits.');
});
