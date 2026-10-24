/* Parts of a whole (Charts42 PartsBar: Today's "Why topics were held back",
   for one) step through tones 0 to 3 in order, and the key matches each
   swatch to its segment by colour. Control room (17 October 2026) moved the
   chart tones onto the all red heat ramp, where heat-3 and heat-2 sit 1.16:1
   apart in Dark and 1.24:1 in Light, so the second and third parts read as
   one. Each tone is resolved here from the stylesheets' own tokens, through
   the rules a parts chart takes under the product, in both themes. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';

const sheet = (file) => postcss.parse(readFileSync(new URL(file, import.meta.url), 'utf8'));
const NIGHTDESK = sheet('../../styles/nightdesk.css');
const CHARTS = sheet('../../styles/charts42.css');
const THEME_SHEETS = [NIGHTDESK, sheet('../../app.css')];

function themeTokens(theme){
  const tokens = {};
  for (const css of THEME_SHEETS){
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

/* The chart custom properties a parts chart sees under the product: the
   charts42 defaults, then each nightdesk rule that applies to a
   figure.ch42.ch42-parts inside html .oi-product, in source order. */
function partsProperties(){
  const props = {};
  const take = (rule) => rule.walkDecls(/^--ch-/, (decl) => { props[decl.prop] = decl.value.trim(); });
  CHARTS.walkRules((rule) => { if (rule.selector.trim() === '.ch42') take(rule); });
  const applies = (selector) => selector.split(',').map((s) => s.trim().replace(/\s+/g, ' '))
    .some((s) => s === 'html .oi-product .ch42' || s === 'html .oi-product .ch42.ch42-parts');
  NIGHTDESK.walkRules((rule) => { if (applies(rule.selector)) take(rule); });
  return props;
}

/* The --ch token each tone class paints with (charts42.css). */
function toneVariables(){
  const out = [];
  CHARTS.walkRules((rule) => {
    const found = /^\.ch42-tone-(\d)$/.exec(rule.selector.trim());
    if (!found) return;
    rule.walkDecls('background', (decl) => { out[Number(found[1])] = /var\((--ch-\d)\)/.exec(decl.value)[1]; });
  });
  return out;
}

for (const theme of ['midnight', 'daylight']){
  test(`neighbouring parts of a whole read apart in ${theme}`, () => {
    const tokens = themeTokens(theme);
    const props = partsProperties();
    const tones = toneVariables().slice(0, 4).map((variable) => resolve(props[variable], tokens));
    for (const tone of tones) expect(tone).toMatch(/^#[0-9a-f]{6}$/i);
    expect(new Set(tones.map((t) => t.toLowerCase())).size).toBe(4);
    for (let at = 1; at < tones.length; at += 1){
      expect(ratio(tones[at - 1], tones[at])).toBeGreaterThanOrEqual(1.5);
    }
  });
}

test('ranked bars keep the heat ramp: the parts tones are scoped to parts charts', () => {
  const generic = {};
  NIGHTDESK.walkRules((rule) => {
    if (rule.selector.trim().replace(/\s+/g, ' ') === 'html .oi-product .ch42') rule.walkDecls(/^--ch-/, (d) => { generic[d.prop] = d.value.trim(); });
  });
  expect(generic['--ch-2']).toBe('var(--heat-3)');
});
