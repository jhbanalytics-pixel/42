/* Control room, 17 October 2026. The skin loads last among the deferred
   sheets, and Today's lead wash stays on the lead header: other routes reuse
   .t42-head, and an unscoped wash drew a red banner behind their titles. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';

const css = readFileSync(new URL('../../styles/controlroom.css', import.meta.url), 'utf8');
const main = readFileSync(new URL('../../main.jsx', import.meta.url), 'utf8');

test('controlroom.css is the last deferred sheet', () => {
  const sheets = [...main.matchAll(/import\('([^']+\.css)'\)/g)].map((match) => match[1]);
  expect(sheets.at(-1)).toBe('./styles/controlroom.css');
  expect(sheets.indexOf('./styles/nightdesk.css')).toBe(sheets.length - 2);
});

test('every rule that paints a background on .t42-head is scoped to the lead header', () => {
  const found = [];
  postcss.parse(css).walkRules((rule) => {
    for (const selector of rule.selectors){
      if (!/\.t42-head(?::has\(> \.t42-lead\))?$/.test(selector.trim())) continue;
      if (!rule.nodes.some((node) => node.type === 'decl' && /^background/.test(node.prop))) continue;
      found.push(selector);
      expect(selector).toContain('.t42-head:has(> .t42-lead)');
    }
  });
  expect(found.length).toBeGreaterThan(0);
});

/* Slice 2: the LED field draws the lead's posts a day, each day as its share
   of the busiest day; a day that was not measured stays dark rather than
   reading as zero, and a series with nothing measured draws no field. */
const {ledLevels} = await import('../LedField.jsx');

test('the LED field scales each measured day to the busiest day and keeps gaps dark', () => {
  expect(ledLevels([{value: 2}, {value: null}, {value: 8}, {value: 4}])).toEqual([0.25, null, 1, 0.5]);
  expect(ledLevels([{value: 0}, {value: 0}])).toEqual([null, null]);
  expect(ledLevels(undefined)).toEqual([]);
});

test('the field fades in once and only for a reader who has not asked for less motion', () => {
  const found = [];
  postcss.parse(css).walkDecls('animation', (declaration) => {
    if (!/crLedIgnite/.test(declaration.value)) return;
    found.push(declaration.parent.parent.type === 'atrule' && /prefers-reduced-motion:\s*no-preference/.test(declaration.parent.parent.params));
  });
  expect(found).toEqual([true]);
});
