/* Today, Discover, Compare, Alerts and Ask read in one clear order. Design
   critique, 2 October 2026: short sentences ended on a two-word straggler
   ("the weekend.", "trends update."), Today's header stacked five registers
   with a ruled bold notice, the heading split its date, the feedback taps
   had no label, Alerts' section heads sat cramped on their bodies, and
   Compare's heading sat under the subtitle with no room. These rules hold
   the fixes in place. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');

function decls(sheet, match){
  const out = [];
  postcss.parse(read(sheet)).walkRules((rule) => {
    if (!rule.selector.split(',').some((part) => match(part.trim()))) return;
    const media = rule.parent && rule.parent.type === 'atrule' ? rule.parent.params : '';
    rule.walkDecls((decl) => out.push({selector: rule.selector, media, prop: decl.prop, value: decl.value.trim()}));
  });
  return out;
}
const has = (list, prop, value, media = '') => list.some((d) => d.prop === prop && d.value === value && d.media === media);
/* Restated 4 October 2026. Balanced wrapping split running sentences after
   half a line, so on a wide screen claims and explanations read as cut off
   (Albert). Running text now uses pretty, which still keeps a lone last word
   off the final line; headings keep balance. */
const pretty = (sheet, selector) => decls(sheet, (s) => s === selector)
  .some((d) => /^text-wrap(-style)?$/.test(d.prop) && d.value === 'pretty' && d.media === '');

test('short sentences on cards and under headings fill their line and leave no lone last word', () => {
  expect(pretty('styles/today42.css', '.t42-explanation')).toBe(true);
  expect(pretty('styles/today42.css', '.t42-post-text')).toBe(true);
  expect(pretty('styles/today42.css', '.t42-head > .t42-status')).toBe(true);
  expect(pretty('styles/today42.css', '[data-dropped-row] > .t42-row-reason')).toBe(true);
  expect(pretty('styles/today42.css', '[data-held-row] > .t42-line-text')).toBe(true);
  expect(pretty('styles/alerts42.css', '.a42-empty')).toBe(true);
});

test('Today keeps the heading date whole and its header in one quiet register under the headline', () => {
  expect(has(decls('styles/today42.css', (s) => s === '.t42-nowrap'), 'white-space', 'nowrap')).toBe(true);
  const notice = decls('styles/today42.css', (s) => s === '.t42-notice');
  expect(notice.some((d) => d.prop.startsWith('border'))).toBe(false);
  expect(has(notice, 'font-size', 'var(--type-meta)')).toBe(true);
  const facts = decls('styles/today42.css', (s) => s === '.t42-facts');
  expect(has(facts, 'flex-direction', 'column', '(max-width: 479px)')).toBe(true);
});

test('the feedback label is quiet text, not a control, and shares the taps\' line box', () => {
  const label = decls('styles/alerts42.css', (s) => s === '.f42-label');
  expect(has(label, 'color', 'var(--muted)')).toBe(true);
  expect(has(label, 'min-height', 'var(--target-min)')).toBe(true);
  const market = decls('styles/ask42.css', (s) => s === '.ask42-form-row > .ask42-label');
  expect(has(market, 'min-height', 'var(--target-min)')).toBe(true);
});

test('Alerts spaces its sections like Today and caps its subtitle at a readable measure', () => {
  const title = decls('styles/alerts42.css', (s) => s === '.a42-title');
  expect(has(title, 'font-size', 'var(--t42-lead)')).toBe(true);
  expect(has(decls('styles/alerts42.css', (s) => s === '.t42.a42'), '--t42-lead', 'var(--type-section-title)')).toBe(true);
  expect(has(title, 'margin', '0 0 var(--s-3)')).toBe(true);
  const subtitle = decls('styles/alerts42.css', (s) => s === '.a42 .t42-head > .t42-status');
  // Restated 4 October 2026: the shared prose measure replaced 66ch.
  expect(has(subtitle, 'max-width', 'var(--measure)')).toBe(true);
  expect(has(decls('styles/alerts42.css', (s) => s === '.a42-note'), 'color', 'var(--muted)')).toBe(true);
});

test('Compare gives its first section heading room under the page header', () => {
  const head = decls('styles/compare42.css', (s) => s === '.t42.c42 .t42-head');
  expect(has(head, 'margin-bottom', 'var(--s-6)')).toBe(true);
  const empty = decls('styles/compare42.css', (s) => s === '.c42-need:empty');
  expect(has(empty, 'min-height', '0')).toBe(true);
});

test('Ask claims and short list items fill their line and leave no lone last word', () => {
  expect(pretty('styles/ask42.css', '.ask42-claim-text')).toBe(true);
  expect(pretty('styles/ask42.css', '.ask42-answer .ask42-list > li')).toBe(true);
  expect(pretty('styles/ask42.css', '.ask42-log-steps > li')).toBe(true);
});
