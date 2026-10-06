import {expect, test} from 'bun:test';
import {creatorCount, creatorSourceUrl} from '../../creator.jsx';
import {normalizeView} from '../../redesignContract.js';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {BigChart, MultiChart} from '../../charts.jsx';

test('existing creator links remain on their destination', () => {
  expect(normalizeView('creator')).toBe('creator');
});

test.each([null, undefined, NaN, Infinity, -1, '42'])('unknown creator measurements stay unmeasured: %s', (value) => {
  expect(creatorCount(value)).toBe('Unmeasured');
});

test('zero is a measured creator count', () => {
  expect(creatorCount(0)).toBe('0');
});

test.each(['javascript:alert(1)', 'data:text/html,test', '/relative', 'https://user:secret@example.test/post', null])('unsafe or missing source URL is withheld: %s', (value) => {
  expect(creatorSourceUrl(value)).toBeNull();
});

test('source URLs retain their supplied post identity', () => {
  expect(creatorSourceUrl('https://example.test/post?id=42')).toBe('https://example.test/post?id=42');
});

test('chart endpoint labels use supplied dates without claiming today', () => {
  const single = renderToStaticMarkup(<BigChart values={[1, 2]} dates={['2000-01-01', '2000-01-02']} />);
  const multiple = renderToStaticMarkup(<MultiChart series={[{label: 'A', color: '#000', values: [1, 2]}]} dates={['2000-01-01', '2000-01-02']} />);
  for (const markup of [single, multiple]){
    expect(markup).toContain('2 Jan');
    expect(markup).not.toMatch(/today/i);
  }
});

test('identical chart values still have unique accessible IDs', () => {
  const markup = renderToStaticMarkup(<><BigChart values={[1, 2]} /><BigChart values={[1, 2]} /></>);
  const ids = [...markup.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  expect(new Set(ids).size).toBe(ids.length);
});
