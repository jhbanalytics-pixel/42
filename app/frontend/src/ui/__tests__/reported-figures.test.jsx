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

/* Audit, 17 October 2026: figures the privacy pass withholds are said to be
   withheld, never that no source stated any, and the sweep's count says what
   it is (a floor at its cap, not what was read) or that the search failed. */
test('figures withheld for privacy are said to be withheld, never that none were found', () => {
  const all = draw(<ReportedFigures record={record({status: 'ok', tables: [], lead: '', withheld: 2})} />);
  const words = all.querySelector('.ask42-status').textContent;
  expect(words).not.toContain('No source this answer read states a percentage');
  expect(words).toBe('The figures 2 sources report are not shown, because they rest on posts 42 no longer shows.');
  const unchecked = draw(<ReportedFigures record={{...record({status: 'ok', tables: [], lead: '', withheld: 1}),
    privacy: {v: 1, state: 'unavailable', withheld: {posts: 0, claims: 0, summary: false}}}} />);
  expect(unchecked.querySelector('.ask42-status').textContent).toContain('could not check its hidden-people list');
  const some = draw(<ReportedFigures record={record({status: 'ok', withheld: 1, tables: [table('MarkData', [['ANC', 33.5]])]})} />);
  expect(some.querySelectorAll('.ask42-figures-table')).toHaveLength(1);
  expect(some.querySelector('[data-figures-withheld]').textContent).toBe(
    'Figures from 1 source more are not shown, because they rest on posts 42 no longer shows.');
  const odd = draw(<ReportedFigures record={record({status: 'ok', tables: []})} />);
  expect(odd.querySelector('.ask42-status').textContent).not.toContain('No source this answer read states a percentage');
});

test('a failed sweep gives no count, a capped one is a floor, and the read count is the records read', () => {
  const one = [table('MarkData', [['ANC', 33.5]])];
  const basis = (figures) => draw(<ReportedFigures record={record({status: 'ok', tables: one, basis: 'B.', ...figures})} />)
    .querySelector('.ask42-figures-basis').textContent;
  const failed = basis({sweep: {matched: 0, failed: 'search_failed'}, checked: {records_with_percentages: 12}});
  expect(failed).not.toContain('0 stored posts');
  expect(failed).not.toContain('search_failed');
  expect(failed).toContain('could not finish this time');
  expect(basis({sweep: {matched: 0, failed: null}})).toBe('B.');
  const capped = basis({sweep: {matched: 300, failed: null}, checked: {records_with_percentages: 60}});
  expect(capped).toContain('At least 300 stored posts in the window match the question');
  expect(capped).toContain('up to 60 records with a percentage');
  expect(capped).not.toContain('were read for these');
});
