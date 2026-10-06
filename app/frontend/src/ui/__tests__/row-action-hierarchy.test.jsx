/* Design audit, 2 October 2026: every card row on Today and Discover showed
   three to six underlined actions at one weight, so Ask about this did not
   read as the first action. A primary action carries more visual weight than
   the secondary ones (NN/g, visual hierarchy of actions). These tests hold
   the card foot to that: Ask in ink, 600 and underlined; the rest 400 and
   muted with no underline until hover or keyboard focus; and the Today
   feedback taps as one quiet segmented group that keeps a target of at least
   24 px (WCAG 2.2, 2.5.8). */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');

/* The declarations a top-level rule gives one exact selector, later rules
   winning, as the cascade does for selectors of equal weight. */
function resolved(sheet, selector){
  const out = {};
  postcss.parse(read(sheet)).walkRules((rule) => {
    if (rule.parent && rule.parent.type === 'atrule') return;
    if (!rule.selectors.map((s) => s.trim()).includes(selector)) return;
    rule.walkDecls((decl) => { out[decl.prop] = decl.value.trim(); });
  });
  return out;
}
const underlined = (d) => /(^|\s)underline(\s|$)/.test(d['text-decoration'] || d['text-decoration-line'] || '');
const bare = (d) => (d['text-decoration'] || d['text-decoration-line']) === 'none';

const TODAY = 'styles/today42.css';
const DISCOVER = 'styles/discover42.css';

test('Ask about this is the one heavy action: ink, 600 and underlined', () => {
  const primary = {...resolved(TODAY, '.t42-action'), ...resolved(TODAY, '.t42-action-primary')};
  expect(primary.color).toBe('var(--ink)');
  expect(primary['font-weight']).toBe('600');
  expect(underlined(primary)).toBe(true);
});

test('the other actions are 400 and muted with no underline at rest', () => {
  const secondary = resolved(TODAY, '.t42-action');
  expect(secondary['font-weight']).toBe('400');
  expect(secondary.color).toBe('var(--muted)');
  expect(bare(secondary)).toBe(true);
});

test('a secondary action turns ink and underlined on hover and on keyboard focus', () => {
  for (const state of ['.t42-action:hover', '.t42-action:focus-visible']){
    const d = resolved(TODAY, state);
    expect(d.color).toBe('var(--ink)');
    expect(underlined(d)).toBe(true);
  }
});

test('Discover sets no rule of its own that flattens the card foot back to one weight', () => {
  const findings = [];
  postcss.parse(read(DISCOVER)).walkRules((rule) => {
    if (!/\.t42-action\b/.test(rule.selector)) return;
    rule.walkDecls(/^(font-weight|color|text-decoration(-line)?)$/, (decl) => findings.push(rule.selector + ' ' + decl.prop));
  });
  expect(findings).toEqual([]);
});

/* Restated 4 October 2026: the boxed segmented taps read as generated chrome, so the taps are now plain words split by a thin rule. */
test('the Today feedback taps are plain words split by a thin rule', () => {
  expect(resolved(TODAY, '.t42-card-foot .f42-feedback').gap).toBe('0');
  const tap = resolved(TODAY, '.t42-card-foot .f42-tap');
  expect(tap.border).toBe('0');
  expect(tap['border-radius']).toBe('0');
  expect(tap['font-size']).toBe('var(--type-2)');
  expect(tap['min-height']).toBe('32px');
  expect(Number.parseFloat(tap['min-height'])).toBeGreaterThanOrEqual(24);
  expect(bare(tap)).toBe(true);
  /* One rule between neighbouring words, drawn by the later word. */
  expect(resolved(TODAY, '.t42-card-foot .f42-tap + .f42-tap')['border-inline-start']).toBe('1px solid var(--line)');
  const chosen = resolved(TODAY, '.t42-card-foot .f42-tap[aria-pressed="true"]');
  expect(chosen.color).toBe('var(--ink)');
  expect(underlined(chosen)).toBe(true);
});
