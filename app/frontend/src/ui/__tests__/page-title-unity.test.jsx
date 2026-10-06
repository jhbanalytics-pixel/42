/* One page title across 42. The core pages (Today, Ask, Discover, Compare,
   History, Alerts) and Board and Browse set their title in the sans at the
   page title step, weight 600. Seeds, Seed path, Map, Listen, Lexicon, Method,
   Network and Fieldwork still set theirs in the display serif at weight 400,
   so moving between pages changed the voice of the heading. Every title rule
   for those pages must now use the sans at weight 600 and never the serif. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());
const React = await import('react');
const {renderToStaticMarkup} = await import('react-dom/server');
const {MethodPage} = await import('../../views.jsx');
const renderMethod = () => renderToStaticMarkup(React.createElement(MethodPage));

const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');

const TITLES = [
  ['ui/ui.css', 'ui-page-hero-title'],
  ['styles/lexlisten.css', 'ui-page-hero-title'],
  ['styles/graphs.css', 'ui-page-hero-title'],
  ['styles/graphs.css', 'net-page-title'],
  ['styles/fieldwork.css', 'fieldwork-lead__title'],
];

function titleDeclarations(sheet, name){
  const out = [];
  postcss.parse(read(sheet)).walkRules((rule) => {
    if (!rule.selector.split(',').some((part) => part.trim().split(/\s+/).pop().includes('.' + name))) return;
    rule.walkDecls((decl) => out.push({prop: decl.prop, value: decl.value.trim(), selector: rule.selector}));
  });
  return out;
}

test('every page title rule uses the sans at weight 600, never the display serif', () => {
  const findings = [];
  for (const [sheet, name] of TITLES){
    const decls = titleDeclarations(sheet, name);
    for (const {prop, value, selector} of decls){
      if ((prop === 'font' || prop === 'font-family') && /serif\b|Georgia/.test(value) && !/var\(--sans\)/.test(value)) findings.push(`${sheet} ${selector}: ${prop}: ${value}`);
      if (prop === 'font-weight' && value !== '600') findings.push(`${sheet} ${selector}: font-weight: ${value}`);
      if (prop === 'font' && /^\s*400\b/.test(value)) findings.push(`${sheet} ${selector}: font: ${value}`);
    }
  }
  expect(findings).toEqual([]);
});

test('the shared hero title takes the 42 page title step in the sans', () => {
  const decls = titleDeclarations('ui/ui.css', 'ui-page-hero-title');
  const value = (prop) => decls.filter((d) => d.prop === prop).map((d) => d.value);
  expect(value('font-family')).toContain('var(--sans)');
  expect(value('font-weight')).toContain('600');
  expect(value('font-size')).toContain('var(--type-page-title)');
});

const SECTION_TITLES = [
  ['styles/empty-states.css', 'es-title'],
  ['app.css', 'es-title'],
  ['app.css', 'id-head'],
  ['styles/lexlisten.css', 'id-head'],
  ['styles/graphs.css', 'id-head'],
  ['app.css', 'map-node-label'],
  ['styles/loop.css', 'loop-stage-name'],
];

test('empty-state, unavailable and stage headings speak in the same sans as the page titles', () => {
  const findings = [];
  for (const [sheet, name] of SECTION_TITLES){
    for (const {prop, value, selector} of titleDeclarations(sheet, name)){
      if ((prop === 'font' || prop === 'font-family') && /var\(--serif\)|Georgia/.test(value)) findings.push(`${sheet} ${selector}: ${prop}: ${value}`);
      if (prop === 'font-weight' && value === '400') findings.push(`${sheet} ${selector}: font-weight: 400`);
      if (prop === 'font' && /^\s*400\b/.test(value)) findings.push(`${sheet} ${selector}: font: ${value}`);
    }
  }
  expect(findings).toEqual([]);
});

test('Method steps and rules carry no display serif, no mono numerals and no accent-coloured numbers', () => {
  const html = renderMethod();
  expect(html).not.toMatch(/font-family:\s*var\(--serif\)/);
  expect(html).not.toMatch(/font-family:\s*var\(--mono\)/);
  expect(html).not.toMatch(/>0[1-4]</);
  expect(html).not.toMatch(/accent-text[^>]*>(?:Step )?\d+</);
});

/* The four steps were a row of bordered feature cards. They are an ordered
   process, so they read as one ordered list of short paragraphs with run-in
   heads, at a reading measure. */
test('Method says how it works as an ordered list with run-in heads, not a row of cards', () => {
  const html = renderMethod();
  const dom = new window.DOMParser().parseFromString(html, 'text/html');
  const steps = [...dom.querySelectorAll('ol.method-steps > li')];
  expect(steps.map((li) => li.querySelector('strong').textContent)).toEqual(['Collect.', 'Organise.', 'Read.', 'Brief.']);
  for (const li of steps) expect(li.getAttribute('style') || '').not.toMatch(/border/);
});
