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
      if (!/\.t42-head\b/.test(selector)) continue;
      if (!rule.nodes.some((node) => node.type === 'decl' && /^background/.test(node.prop))) continue;
      found.push(selector);
      expect(selector).toContain('.t42-head:has(> .t42-lead)');
    }
  });
  expect(found.length).toBeGreaterThan(0);
});
