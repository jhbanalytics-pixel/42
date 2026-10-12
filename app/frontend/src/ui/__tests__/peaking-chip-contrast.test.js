/* Design audit item 15, Release C: the dark Peaking and Spike chip set white
   bold 14 px text on --heat-2 (#E5484D), 3.91:1, under the 4.5:1 WCAG AA
   floor. The chip's text colour is resolved here from the stylesheets'
   own theme tokens, so a chip rule that points at a token that fails in
   either theme fails this test. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';

const sheet = (file) => postcss.parse(readFileSync(new URL(file, import.meta.url), 'utf8'));
const SHEETS = ['../../styles/nightdesk.css', '../../app.css'].map(sheet);
const CHIPS = [
  {file: '../../styles/nightdesk.css', selector: 'html .oi-product .t42 .t42-card-meta > .t42-state:is([data-state="peaking"], [data-state="spike"])'},
  {file: '../../styles/today-story.css', selector: 'html .oi-product .t42 .sc-momentum:is([data-state="peaking"], [data-state="spike"])'},
];

/* The custom properties a theme sets on .oi-product[data-dir=...] (or the
   root under the same data-dir), from every sheet that sets them. */
function themeTokens(theme){
  const tokens = {};
  for (const css of SHEETS){
    css.walkRules((rule) => {
      if (!rule.selector.split(',').some((part) => part.includes('[data-dir="' + theme + '"]') && !/\s/.test(part.trim().replace(/^:root/, '')))) return;
      rule.walkDecls(/^--/, (decl) => { tokens[decl.prop] = decl.value.trim(); });
    });
  }
  return tokens;
}

const resolve = (value, tokens) => {
  const found = /^var\((--[\w-]+)\)$/.exec(String(value).trim());
  return found ? resolve(tokens[found[1]], tokens) : String(value).trim();
};

const channel = (hex, at) => {
  const c = parseInt(hex.slice(at, at + 2), 16) / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
};
const luminance = (hex) => 0.2126 * channel(hex, 1) + 0.7152 * channel(hex, 3) + 0.0722 * channel(hex, 5);
const ratio = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

function chipRule({file, selector}){
  let found = null;
  sheet(file).walkRules((rule) => { if (rule.selector.replace(/\s+/g, ' ') === selector) found = rule; });
  const decls = {};
  found.walkDecls((decl) => { decls[decl.prop] = decl.value; });
  return decls;
}

for (const chip of CHIPS){
  for (const theme of ['midnight', 'daylight']){
    test(`the peaking chip in ${chip.file.split('/').pop()} reads at 4.5:1 or better in ${theme}`, () => {
      const tokens = themeTokens(theme);
      const decls = chipRule(chip);
      const background = resolve(decls.background, tokens);
      const color = resolve(decls.color, tokens);
      expect(background).toMatch(/^#[0-9a-f]{6}$/i);
      expect(color).toMatch(/^#[0-9a-f]{6}$/i);
      expect(ratio(color, background)).toBeGreaterThanOrEqual(4.5);
    });
  }
}
