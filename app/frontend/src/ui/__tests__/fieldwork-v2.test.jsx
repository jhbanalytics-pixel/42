/* Restated 2 October 2026: these tests pinned the retired v2 workspace's
   operation counts (partial totals stayed unknown, never the visible row
   count). The roster keeps the same rule for its own numbers: a count the
   collection record does not hold stays unknown, never zero, and every count
   shown is the API's and grouped by thousands. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {FieldworkPage} from '../../fieldwork.jsx';

const days = JSON.parse(readFileSync(new URL('./fixtures/fieldwork42_days.json', import.meta.url), 'utf8'));
const clone = (value) => JSON.parse(JSON.stringify(value));
const text = (html) => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&amp;/g, '&').replace(/\s+/g, ' ');
const render = (payload) => text(renderToStaticMarkup(<FieldworkPage payload={payload} />));

/* Restated, design audit 2 October 2026: each source is now a table row, so
   a row ends at its closing tr, not at the closing li of the old list. */
function rowText(payload, series){
  const html = renderToStaticMarkup(<FieldworkPage payload={payload} />);
  const at = html.indexOf('data-series="' + series + '"');
  return text(html.slice(at, html.indexOf('</tr>', html.indexOf('fieldwork-source__plan', at))));
}

test('a source with no record shows no call, item or place count', () => {
  const seen = rowText(days.ready, 'board_apple_music');
  expect(seen).toContain('No record for this day');
  expect(seen).not.toMatch(/calls came back|items|placed in the market/);
  expect(seen).toContain('2 reads planned, 2 credits');
});

/* Restated, design audit 2 October 2026: the share sits in the "Placed in
   the market" column, whose heading names the place, so the cell says "Not
   measured" where the prose line said "Place not measured". */
test('an unmeasured place share says not measured, never 0%', () => {
  const seen = rowText(days.ready, 'board_tiktok_hashtag');
  expect(seen).toContain('Not measured');
  expect(seen).not.toContain('0% placed');
});

test('several parts give the measured range, not an invented average', () => {
  const payload = clone(days.ready);
  const reddit = payload.markets[0].sources.find((s) => s.series === 'list_reddit');
  reddit.health = {...reddit.health, located_share: null, located_range: [0.2, 0.6]};
  /* Restated, design audit 2 October 2026: the column heading "Placed in
     the market" carries the words, so the cell holds the range alone, where
     it read "20% to 60% placed in the market". */
  expect(rowText(payload, 'list_reddit')).toContain('20% to 60%');
});

test('the market tally counts only what the API counted', () => {
  expect(render(days.ready)).toContain('1 failed, 5 delivered, 14 with no record');
  // The tabs drop their source counts at phone width, so the tally carries it.
  expect(render(days.ready)).toContain(days.ready.markets[0].sources.length + ' sources: 1 failed');
  const payload = clone(days.ready);
  payload.markets[0].counts = {};
  expect(render(payload)).toContain('No sources listed');
});

test('credits spent and the cap group by thousands', () => {
  const payload = clone(days.ready);
  payload.credits.spent.value = 1234;
  payload.credits.cap = 12000;
  expect(render(payload)).toMatch(/1\D?234 of 12\D?000/);
  payload.credits.cap = 1000;
  expect(render(payload)).toContain('Over the daily cap');
});

test('unreadable spend is said, never shown as nothing charged', () => {
  const payload = clone(days.ready);
  payload.credits = {state: 'unavailable', cap: 1000, planned: 842, spent: null, markets: []};
  const seen = render(payload);
  expect(seen).toContain('Spend could not be read');
  expect(seen).not.toContain('None charged');
});

test('the tally keeps each count with its word so a phone line never ends on a bare number', () => {
  const html = renderToStaticMarkup(<FieldworkPage payload={days.ready} />);
  expect(html).toContain('1\u00a0failed, 5\u00a0delivered, 14\u00a0with no record');
});

/* Design audit 2 October 2026: the source list was a status table written as
   prose, with four stacked lines per source whose numbers did not line up
   from row to row (Tufte, The Visual Display of Quantitative Information, on
   tables). Each group is now a real table, labelled by its group heading,
   with one column per fact and the figures right aligned in tabular digits
   (Bringhurst, The Elements of Typographic Style, on tabular figures). */
function cellsOf(html, series){
  const at = html.indexOf('data-series="' + series + '"');
  const row = html.slice(at, html.indexOf('</tr>', at));
  return [...row.matchAll(/<td([^>]*)>([\s\S]*?)<\/td>/g)].map((m) => ({
    label: (/data-label="([^"]*)"/.exec(m[1]) || [])[1] || null,
    num: /fieldwork-num/.test(m[1]),
    text: text(m[2]).trim(),
  }));
}

test('each source group is a table labelled by its group heading, with one column per fact', () => {
  const html = renderToStaticMarkup(<FieldworkPage payload={days.ready} />);
  /* Charts, 3 October 2026: restated. The source-state chart above the roster
     carries its own screen-reader table (class sr-only), which is not a
     source group, so it is left out of this count. */
  const tables = [...html.matchAll(/<table([^>]*)>([\s\S]*?)<\/table>/g)].filter(([, attrs]) => !/class="sr-only"/.test(attrs));
  const groups = days.ready.groups.filter((g) => days.ready.markets[0].sources.some((s) => s.group === g.key));
  expect(tables).toHaveLength(groups.length);
  for (const [, attrs, body] of tables){
    const labelledby = /aria-labelledby="([^"]+)"/.exec(attrs)[1];
    const heading = new RegExp('<h3[^>]*id="' + labelledby.replace(/[^\w-]/g, (c) => '\\' + c) + '"[^>]*>([^<]*)</h3>').exec(html);
    expect(heading).toBeTruthy();
    expect(groups.map((g) => g.label)).toContain(heading[1]);
    const heads = [...body.slice(0, body.indexOf('</thead>')).matchAll(/<th scope="col"[^>]*>([\s\S]*?)<\/th>/g)].map((m) => text(m[1]).trim());
    expect(heads).toEqual(['Source', 'Status', 'Calls came back', 'Items', 'Location known', 'Planned']);
    expect(body).toMatch(/<tr class="fieldwork-source"[^>]*><th scope="row"/);
  }
});

test('a measured row puts each figure in its own labelled, numeric cell', () => {
  const html = renderToStaticMarkup(<FieldworkPage payload={days.ready} />);
  expect(cellsOf(html, 'feed_tiktok')).toEqual([
    {label: 'Status', num: false, text: 'Delivered'},
    {label: 'Calls came back', num: true, text: '12 of 12'},
    {label: 'Items', num: true, text: '540'},
    {label: 'Location known', num: true, text: '45%'},
    {label: 'Planned', num: true, text: '3 reads planned, 15 credits'},
  ]);
  const failed = cellsOf(html, 'board_tiktok_hashtag');
  expect(failed[0].text).toBe('Failed: could not be read');
  expect(failed[1].text).toBe('2 of 12');
  expect(failed[3].text).toBe('Not measured');
});

test('a row with no record says so in each figure cell, never a zero', () => {
  const cells = cellsOf(renderToStaticMarkup(<FieldworkPage payload={days.ready} />), 'board_apple_music');
  expect(cells.map((c) => c.text)).toEqual(['No record for this day', 'Not recorded', 'Not recorded', 'Not recorded', '2 reads planned, 2 credits']);
});

test('the sheet right aligns the figures in tabular digits, rules rows at 1px and stacks under 720px', async () => {
  const postcss = (await import('postcss')).default;
  const root = postcss.parse(readFileSync(new URL('../../styles/fieldwork.css', import.meta.url), 'utf8'));
  const found = (selector, media = null) => {
    const out = {};
    root.walkRules((rule) => {
      const at = rule.parent && rule.parent.type === 'atrule' ? rule.parent.params : null;
      if (at !== media) return;
      if (!rule.selectors.map((s) => s.replace(/\s+/g, ' ').trim()).includes(selector)) return;
      rule.walkDecls((d) => { out[d.prop] = d.value.trim(); });
    });
    return out;
  };
  expect(found('.fieldwork-num')['text-align']).toBe('right');
  expect(found('.fieldwork-num')['font-variant-numeric']).toBe('tabular-nums');
  expect(found('.fieldwork-sources')['border-collapse']).toBe('collapse');
  expect(found('.fieldwork-sources')['border-radius']).toBe('0');
  expect(found('.fieldwork-sources td')['border-bottom']).toBe('1px solid var(--line)');
  expect(found('.fieldwork-sources td[data-label]::before', '(max-width: 719px)').content).toBe('attr(data-label)');
  expect(found('.fieldwork-sources thead', '(max-width: 719px)')['clip-path']).toBe('inset(50%)');
  /* One size and weight for every status: no state sets its own weight. */
  root.walkRules((rule) => {
    if (!/data-status/.test(rule.selector) || !/fieldwork-source__status/.test(rule.selector)) return;
    rule.walkDecls((d) => { expect(d.prop).not.toMatch(/^font/); });
  });
});
