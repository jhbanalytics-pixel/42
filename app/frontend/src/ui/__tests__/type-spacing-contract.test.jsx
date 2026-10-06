import {expect, test} from 'bun:test';
import postcss from 'postcss';
import {readFileSync} from 'node:fs';

const read = (path) => readFileSync(new URL(path, new URL('../../', import.meta.url)), 'utf8');
const activeCss = read('app.css');
const entry = read('main.jsx');
const root = postcss.parse(activeCss);

function declarations(css, selector){
  let found = null;
  postcss.parse(css).walkRules((rule) => {
    if (!found && rule.selectors?.includes(selector)) {
      found = Object.fromEntries(rule.nodes.filter((node) => node.type === 'decl').map(({prop, value}) => [prop, value]));
    }
  });
  return found;
}

function activeRoleTokens(){
  let tokens = null;
  root.walkRules((rule) => {
    if (!tokens && rule.selectors?.includes(':root')) {
      tokens = Object.fromEntries(rule.nodes.filter((node) => node.type === 'decl').map(({prop, value}) => [prop, value]));
    }
  });
  return tokens;
}

const routes = {
  today: read('styles/today42.css'),
  topic: read('styles/topic42.css'),
  people: read('styles/people42.css'),
  coverage: read('styles/coverage42.css'),
  ask: read('styles/ask42.css'),
  investigations: read('styles/investigations42.css'),
  discover: read('styles/discover42.css'),
  history: read('styles/history42.css'),
  rail: read('styles/rail42.css'),
  compare: read('styles/compare42.css'),
};

test('active route headings and copy use the approved desktop type roles', () => {
  expect(entry).toContain("import('./app.css')");
  const tokens = activeRoleTokens();
  const sizes = [
    ['--type-page-title', 28, 32],
    /* Restated, design audit 2 October 2026: section title, signal title and
       body move from 19, 17 and 15 px onto the package scale at 24, 18 and
       16 px, so the three levels are far enough apart to tell apart
       (Butterick, Practical Typography; Bringhurst, modular scale). Old
       ranges were 18 to 20, 16 to 18 and 14 to 15. */
    ['--type-section-title', 24, 24],
    ['--type-signal-title', 18, 18],
    ['--type-body', 16, 16],
    ['--type-meta', 12, 13],
  ];
  for (const [name, min, max] of sizes) {
    const size = Number.parseFloat(tokens[name]);
    expect(size).toBeGreaterThanOrEqual(min);
    expect(size).toBeLessThanOrEqual(max);
  }
  expect(tokens['--leading-page-title']).toBe('1.2');
  expect(tokens['--leading-section-title']).toBe('1.3');
  expect(tokens['--leading-signal-title']).toBe('1.35');
  expect(tokens['--leading-body']).toBe('1.5');
  expect(tokens['--leading-meta']).toBe('1.4');

  for (const [sheet, selector] of [
    [routes.today, '.t42-heading'],
    [routes.topic, '.tp42-heading'],
    [routes.people, '.pp42-heading'],
    [routes.coverage, '.cv42-heading'],
    [routes.ask, '.ask42-title'],
    [routes.investigations, '.inv42-question'],
    [routes.history, '.hi42-heading'],
  ]) {
    const rule = declarations(sheet, selector);
    expect(rule['font-size']).toBe('var(--type-page-title)');
    expect(rule['line-height']).toBe('var(--leading-page-title)');
  }
  expect(declarations(routes.today, '.t42')['font-size']).toBe('var(--type-body)');
  expect(declarations(routes.today, '.t42')['line-height']).toBe('var(--leading-body)');
  expect(declarations(routes.topic, '.tp42')['font-size']).toBe('var(--type-body)');
  expect(declarations(routes.people, '.pp42')['font-size']).toBe('var(--type-body)');
  expect(declarations(routes.coverage, '.cv42')['font-size']).toBe('var(--type-body)');
  expect(declarations(routes.ask, '.ask42')['font-size']).toBe('var(--type-body)');
  expect(declarations(routes.history, '.hi42')['font-size']).toBe('var(--type-body)');
  expect(declarations(routes.history, '.hi42')['line-height']).toBe('var(--leading-body)');
  expect(declarations(routes.history, '.hi42')['font-size']).toBe('var(--type-body)');
  expect(declarations(routes.history, '.hi42')['line-height']).toBe('var(--leading-body)');
  expect(declarations(routes.today, '.t42-section-title')['font-size']).toBe('var(--type-section-title)');
  expect(declarations(routes.discover, '.d42-title')['font-size']).toBe('var(--type-section-title)');
  expect(declarations(routes.compare, '.c42-title')['font-size']).toBe('var(--type-section-title)');
  expect(declarations(routes.compare, '.c42-multiple-title')['font-size']).toBe('var(--type-signal-title)');
  expect(declarations(routes.history, '.hi42-part-title')['font-size']).toBe('var(--type-section-title)');
  expect(declarations(routes.history, '.hi42-tab')['font-size']).toBe('var(--type-body)');
  expect(declarations(routes.history, '.hi42-lead')['font-size']).toBe('var(--type-signal-title)');
  expect(declarations(routes.topic, '.tp42-part-title')['font-size']).toBe('var(--type-section-title)');
  expect(declarations(routes.people, '.pp42-part-title')['font-size']).toBe('var(--type-section-title)');
  expect(declarations(routes.coverage, '.cv42-part-title')['font-size']).toBe('var(--type-section-title)');
  expect(declarations(routes.ask, '.ask42-section-title')['font-size']).toBe('var(--type-section-title)');
  expect(declarations(routes.investigations, '.inv42-section-title')['font-size']).toBe('var(--type-section-title)');
  expect(declarations(routes.today, '.t42-card-title')['font-size']).toBe('var(--type-signal-title)');
  expect(declarations(routes.today, '.t42-card-meta')['font-size']).toBe('var(--type-meta)');
  // Restated for the polish pass: the History lede reads at body size, like the ledes on Alerts and Schedules.
  expect(declarations(routes.history, '.hi42-sub')['font-size']).toBe('var(--type-body)');
  expect(declarations(routes.history, '.hi42-fig')['font-variant-numeric']).toBe('tabular-nums');
  expect(declarations(routes.rail, '.rail42')['font-size']).toBe('var(--type-body)');
  expect(declarations(routes.rail, '.rail42-theme')['font-size']).toBe('var(--type-meta)');
  expect(declarations(activeCss, 'html .oi-product')['font-variant-numeric']).toBe('tabular-nums');
});

test('active shell spacing and shapes stay within the approved scale', () => {
  const tokens = activeRoleTokens();
  expect(tokens['--shape-control']).toBe('6px');
  expect(tokens['--shape-default']).toBe('8px');
  expect(tokens['--shape-card']).toBe('10px');
  const midnight = declarations(activeCss, 'html:root[data-dir="midnight"]');
  expect(midnight['--shape-control']).toBe('6px');
  expect(midnight['--shape-default']).toBe('8px');
  expect(midnight['--shape-card']).toBe('10px');
  expect(declarations(activeCss, '.wrap')['padding']).toBe('0 var(--s-6)');
  expect(declarations(routes.ask, '.ask42')['padding']).toBe('var(--s-6) var(--s-5)');
  expect(declarations(routes.history, '.hi42')['padding']).toBe('var(--s-6) var(--s-5)');
  expect(declarations(routes.rail, '.rail42-link')['padding']).toBe('var(--s-2) var(--s-4)');
  expect(declarations(activeCss, 'html .oi-product :is(button, select, textarea, input:not([type=checkbox]):not([type=radio]):not([type=range]))')['border-radius']).toBe('var(--shape-control)');
  expect(declarations(activeCss, 'html .oi-product :is(.card, .panel, .board, .lead, .mkt, .t42-card, .pp42-card, .ask42-source, .ask42-popover, .ask42-notices, .workbench-action-card, .research-evidence-card, .research-batch-progress, .research-batch-item)')['border-radius']).toBe('var(--shape-card)');
});
