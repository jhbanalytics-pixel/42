import {expect, test} from 'bun:test';
import {spawnSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

/* Release C review m3. The design gate (scripts/check_design_tokens.mjs) is red on other sheets and unrun by CI, so
   its verdict on Discover's own sheet is pinned here: no line names discover42.css. And every Discover control keeps
   the app's touch target, the target-min token (48 px), at every width. */

const sheet = fileURLToPath(new URL('../../styles/discover42.css', import.meta.url));
const gate = fileURLToPath(new URL('../../../scripts/check_design_tokens.mjs', import.meta.url));

test('the design token gate has no line about the Discover sheet', () => {
  const run = spawnSync('node', [gate], {encoding: 'utf8'});
  expect(run.error).toBeUndefined();
  const lines = `${run.stdout}\n${run.stderr}`.split('\n').filter((line) => line.includes('discover42.css'));
  expect(lines).toEqual([]);
});

const CONTROLS = ['.d42-segment-tab', '.d42-chip', '.d42-select', '.d42-filters-open', '.d42-sheet-done'];

test('every Discover control is at least the app target size, in every media query too', () => {
  const heights = new Map(CONTROLS.map((name) => [name, []]));
  postcss.parse(readFileSync(sheet, 'utf8')).walkRules((rule) => {
    const names = rule.selectors.map((selector) => selector.trim());
    for (const [name, found] of heights){
      if (!names.includes(name)) continue;
      rule.walkDecls(/^min-(block-size|height)$/, (decl) => found.push(decl.value.trim()));
    }
  });
  for (const [name, found] of heights){
    expect(found.length, name + ' declares a minimum height').toBeGreaterThan(0);
    expect(found.every((value) => value === 'var(--target-min)'), name + ' ' + found.join(', ')).toBe(true);
  }
});
