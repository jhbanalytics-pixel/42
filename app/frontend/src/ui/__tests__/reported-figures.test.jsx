/* Reported figures, 16 October 2026 (Albert: "the output should be a chart
   and %"). A quantitative answer's checked figures draw as one bar chart per
   source with its pollster, date and sample as the source states them, the
   exact quote behind each bar, and a lead line; nothing from two sources
   shares a chart, and a payload with nothing to draw says why in words. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());
const {renderToStaticMarkup} = await import('react-dom/server');
const {ReportedFigures, figuresOf} = await import('../../ReportedFigures.jsx');
const draw = (el) => new window.DOMParser().parseFromString(renderToStaticMarkup(el), 'text/html');

const table = (source, rows, extra = {}) => ({
  title: 'voting intention', source, fieldwork: '', sample: '', population: 'Gauteng', publisher: 'News24',
  platform: 'x', url: 'https://x.com/News24/status/1', posted_at: '2026-10-13T08:00:00', evidence_id: 'p1',
  rows: rows.map(([subject, value]) => ({subject, value, unit: '%', quote: `the ${subject} on ${value}%`})),
  note: '', also_reported_by: [], ...extra,
});

const record = (figures) => ({question: 'q', answer: {}, run: {figures}});

test('each source is its own chart with its fields, a % on every bar and its quotes', () => {
  const dom = draw(<ReportedFigures record={record({
    status: 'ok', lead: 'eNCA/MarkData, published 13 October 2026: ANC 33.5%, DA 31%.',
    basis: 'Figures are copied from the cited posts.', sweep: {matched: 42},
    tables: [table('eNCA/MarkData', [['ANC', 33.5], ['DA', 31]], {sample: '1,500 registered voters',
      also_reported_by: [{publisher: 'TimesLIVE'}]}), table('', [['ANC', 36]])],
  })} />);
  expect(dom.querySelector('.ask42-figures-lead').textContent).toBe('eNCA/MarkData, published 13 October 2026: ANC 33.5%, DA 31%.');
  const charts = [...dom.querySelectorAll('.ask42-figures-table')];
  expect(charts).toHaveLength(2);
  expect([...charts[0].querySelectorAll('.ch42-bar-value')].map((v) => v.textContent)).toEqual(['33.5%', '31%']);
  expect(charts[0].querySelector('.ch42-caption').textContent).toBe(
    'eNCA/MarkData · published 13 October 2026 by News24 · sample 1,500 registered voters · covers Gauteng');
  expect(charts[1].querySelector('.ch42-caption').textContent).toContain('Pollster or source not stated');
  expect(charts[1].querySelector('.ch42-caption').textContent).toContain('sample not stated');
  expect(charts[0].querySelector('a').getAttribute('href')).toBe('https://x.com/News24/status/1');
  expect(charts[0].textContent).toContain('Also reported by TimesLIVE');
  expect([...charts[0].querySelectorAll('details li')].map((li) => li.textContent)).toEqual(['ANC “the ANC on 33.5%”', 'DA “the DA on 31%”']);
  expect(dom.querySelector('.ask42-figures-basis').textContent).toContain('42 stored posts');
});

test('a link that is not a plain web address is not drawn as a link', () => {
  const dom = draw(<ReportedFigures record={record({status: 'ok', tables: [table('X', [['ANC', 1]], {url: 'javascript:alert(1)'})]})} />);
  expect(dom.querySelector('a')).toBeNull();
  expect(dom.querySelector('.ask42-figures-source').textContent).toContain('Source link unavailable');
});

test('no figures are said in words, and no payload draws nothing', () => {
  const none = draw(<ReportedFigures record={record({status: 'none_found', tables: []})} />);
  expect(none.querySelector('.ask42-status').textContent).toContain('No source this answer read states a percentage');
  const failed = draw(<ReportedFigures record={record({status: 'failed', tables: []})} />);
  expect(failed.querySelector('.ask42-status').textContent).toContain('could not be listed');
  expect(renderToStaticMarkup(<ReportedFigures record={{run: {}}} />)).toBe('');
  expect(figuresOf({run: {figures: {tables: [{rows: [{subject: 'A', value: 'x'}]}]}}}).tables).toEqual([]);
});
