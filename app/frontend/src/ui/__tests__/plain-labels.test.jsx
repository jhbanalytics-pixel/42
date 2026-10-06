import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';
import {SourceLabView} from '../../sourceLab.jsx';
import {askCountText, catalogueLabel, lifecycleRuleWords, platformLabel, releasedRunLabel, snapshotTime} from '../../plainLabels.js';
import {sourceLab} from '../../../tests/browser/fixtures/42-capability-routes/coverage_source_lab.js';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const decode = (html) => html.replace(/&#x27;/g, "'").replace(/&amp;/g, '&');
/* What a reader sees without opening a disclosure. */
const surface = (html) => decode(html.replace(/<details[^>]*>[\s\S]*?<\/details>/g, ' ').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '));
const disclosed = (html) => decode([...html.matchAll(/<details[^>]*>([\s\S]*?)<\/details>/g)].map((match) => match[1]).join(' ').replace(/<[^>]+>/g, ' '));

/* Quiet register, 23 Sept 2026: a state is written in sentence case, so the
   kill state reads "Not applicable" and the route status "Catalogue only";
   the words are the same, only the first letter is raised. */
test('Source Lab names its lanes, states and catalogue in words and keeps each reference one disclosure away', () => {
  const html = renderToStaticMarkup(<SourceLabView state="ready" data={sourceLab()} />);
  const seen = surface(html);
  for (const words of ['Ogilvy funded lane', 'JHB core lane', 'Funded from the Ogilvy account (Albert)', 'Funded from the JHB Analytics account', 'Not applicable', 'SocialCrawl catalogue of August 2026', 'Catalogue availability is not activation.', 'Reddit', 'TikTok', 'checked 30 Aug 2026']){
    expect(seen).toContain(words);
  }
  for (const raw of ['ogilvy_funded', 'ogilvy_albert', 'jhb_core', 'jhb_analytics', 'not_applicable', 'socialcrawl_catalog_2026_08', 'Catalog ', '2026-08-30T06:00:00Z']){
    expect(seen).not.toContain(raw);
  }
  const references = disclosed(html);
  for (const raw of ['ogilvy_funded', 'ogilvy_albert', 'jhb_core', 'jhb_analytics', 'socialcrawl_catalog_2026_08', 'Catalogue only']){
    expect(references).toContain(raw);
  }
  expect(references).not.toContain('inventory_only');
});

test('an empty catalogue is spelt the South African way', () => {
  const data = sourceLab({measured: 0, catalogue: 0});
  const seen = surface(renderToStaticMarkup(<SourceLabView state="ready" data={data} />));
  expect(seen).toContain('No sources are documented in this catalogue yet.');
});

test('a catalogue, platform or run the build has no word for is never guessed at', () => {
  expect(catalogueLabel('socialcrawl_catalog_2026_09_07')).toBe('SocialCrawl catalogue of 07 Sept 2026');
  expect(catalogueLabel('another_catalogue')).toBe('Documented catalogue');
  expect(catalogueLabel('socialcrawl_catalog_2026_13')).toBe('Documented catalogue');
  expect(platformLabel('youtube')).toBe('YouTube');
  expect(platformLabel('apple_podcasts')).toBe('Apple podcasts');
  expect(releasedRunLabel('run_20260912_dynamic_apply_v2_r1')).toBe('Released run of 12 Sept 2026');
  expect(releasedRunLabel('run_20260231_x')).toBe('Released run');
  expect(releasedRunLabel('nightly')).toBe('Released run');
});

test('Discover names the released run by its date and keeps the run id in a reference', () => {
  const explore = read('../../explore.jsx');
  expect(explore).toContain('releasedRunLabel');
  expect(explore).toMatch(/<summary>Run reference<\/summary>/);
});

/* Quiet register, 23 Sept 2026. The run reference summary was muted text in
   a flex box, which hides the disclosure triangle, so at 1280 it read as a
   label with its value missing. It is a control: it takes the ink and the
   same turning triangle the Fieldwork and workspace Details draw. */
test('the Discover run reference reads as a disclosure, not a label with no value', () => {
  const rules = new Map();
  postcss.parse(read('../../ogilvy-intelligence.css')).walkRules((rule) => {
    if (rule.parent.type !== 'root') return;
    for (const selector of rule.selectors){
      const declarations = rules.get(selector.trim()) || {};
      rule.walkDecls((declaration) => { declarations[declaration.prop] = declaration.value.trim(); });
      rules.set(selector.trim(), declarations);
    }
  });
  const summary = rules.get('.explore-folio__run-reference summary');
  expect(summary.color).toBe('var(--ink)');
  expect(summary['list-style']).toBe('none');
  const marker = rules.get('.explore-folio__run-reference summary::before');
  expect(marker).toBeTruthy();
  expect(marker.content).toBe('""');
  expect(marker['border-inline-start']).toBe('6px solid currentColor');
  expect(rules.get('.explore-folio__run-reference[open] > summary::before').transform).toBe('rotate(90deg)');
  expect(rules.get('.explore-folio__run-reference summary::-webkit-details-marker').display).toBe('none');
});

test('the recent asks list counts one ask as one ask', () => {
  expect(askCountText(1)).toBe('1 ask');
  expect(askCountText(2)).toBe('2 asks');
  expect(askCountText(0)).toBe('0 asks');
  const workbench = read('../../ConsoleWorkbench.jsx');
  expect(workbench).toContain('askCountText(');
  expect(workbench).not.toMatch(/length\} asks/);
});

/* Quiet register, 23 Sept 2026: the hosts write every date a reader reads as
   day, short month and year, and keep machine ids in their own form. */
test('an ISO date and an ISO range read as dates a strategist would say', async () => {
  const {readableDate, readableDates} = await import('../../plainLabels.js');
  expect(readableDate('2026-09-12')).toBe('12 Sept 2026');
  expect(readableDate('2026-09-05')).toBe('5 Sept 2026');
  expect(readableDate('2026-02-31')).toBe('2026-02-31');
  expect(readableDate('not a date')).toBe('not a date');
  expect(readableDate(null)).toBeNull();
  expect(readableDates('2026-08-18 to 2026-08-24')).toBe('18 to 24 Aug 2026');
  expect(readableDates('2026-08-25 to 2026-09-07')).toBe('25 Aug to 7 Sept 2026');
  expect(readableDates('2025-12-29 to 2026-01-04')).toBe('29 Dec 2025 to 4 Jan 2026');
  expect(readableDates('Checked 2026-09-12.')).toBe('Checked 12 Sept 2026.');
  expect(readableDates('run_20260912_dynamic_apply_v2_r1')).toBe('run_20260912_dynamic_apply_v2_r1');
  expect(readableDates('run-2026-09-12 and 2026-09-12.json')).toBe('run-2026-09-12 and 2026-09-12.json');
});

test('a snapshot time reads like the other stamps: no leading zero, SAST', () => {
  /* Demo polish, 2 October 2026: Today's published line read "02 Oct 2026,
     06:15 SAST" while every other stamp in 42 reads "4 Sept 2026, 08:30 SAST". */
  expect(snapshotTime('2026-10-02T04:15:00Z')).toBe('2 Oct 2026, 06:15 SAST');
  expect(snapshotTime('2026-09-04T08:30:00+02:00')).toBe('4 Sept 2026, 08:30 SAST');
  expect(snapshotTime('2026-09-30T22:05:00Z')).toBe('1 Oct 2026, 00:05 SAST');
  expect(snapshotTime('not a time')).toBe('not a time');
});

/* Demo polish, 2 October 2026: the lifecycle rule the API sends for Rising
   reads in plain words; every other rule is already plain and passes as is. */
test('the Rising rule reads in plain words, and other rules pass unchanged', () => {
  expect(lifecycleRuleWords('Significant on 2 days with ratio 2 or more')).toBe('At least twice its usual posting on 2 days');
  expect(lifecycleRuleWords('Significant on 2 days with ratio 2 or more, or on 1 day plus another platform or market'))
    .toBe('At least twice its usual posting on 2 days, or on 1 day when another platform or market also rises clearly');
  expect(lifecycleRuleWords('Significant on 3 days with ratio 1.5 or more')).toBe('At least 1.5 times its usual posting on 3 days');
  expect(lifecycleRuleWords('Significant on 1 day with ratio 3 or more')).toBe('At least 3 times its usual posting on 1 day');
  expect(lifecycleRuleWords('Growth slowing while posting stays near its high')).toBe('Growth slowing while posting stays near its high');
  expect(lifecycleRuleWords('')).toBe('');
  expect(lifecycleRuleWords(null)).toBe('');
});
