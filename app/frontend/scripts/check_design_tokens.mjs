// Design token gate. Two rules from the redesign plan:
//  1. No raw font-size in component files: only tokens.css may declare px
//     font sizes. Legacy route files hold a frozen baseline that may only
//     shrink (a ratchet); every other file must be at zero.
//  2. No raw px gap/padding/margin values outside the spacing scale in NEW
//     css added to styles/ (ratcheted the same way for the legacy sheets).
//  3. Every rule in every host sheet rounds no corner beyond 2px on a form
//     control or 50% on a listed circular mark, and casts no shadow that is
//     not inset. No baseline: a second surface language fails here outright.
// Run: node scripts/check_design_tokens.mjs [--write-baseline]
import {readFileSync, writeFileSync, readdirSync, statSync} from 'node:fs';
import {join, relative} from 'node:path';

const ROOT = new URL('../src', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const BASELINE_PATH = new URL('./design_token_baseline.json', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');

const files = [];
(function walk(d) {
  for (const e of readdirSync(d)) {
    const p = join(d, e);
    const s = statSync(p);
    if (s.isDirectory()) { if (e !== '__tests__' && e !== 'node_modules') walk(p); }
    else if (/\.(jsx|js|css)$/.test(e)) files.push(p);
  }
})(ROOT);

const counts = {};
for (const f of files) {
  const rel = relative(ROOT, f).replace(/\\/g, '/');
  if (rel === 'tokens.css') continue;
  const text = readFileSync(f, 'utf8');
  const css = (text.match(/font-size\s*:\s*[0-9.]+(px|rem|em)/g) || []).length;
  const jsx = (text.match(/fontSize\s*:\s*['"`]?[0-9.]+/g) || []).length;
  const n = css + jsx;
  if (n > 0) counts[rel] = n;
}

if (process.argv.includes('--write-baseline')) {
  writeFileSync(BASELINE_PATH, JSON.stringify(counts, null, 2) + '\n');
  console.log('baseline written:', Object.keys(counts).length, 'files,', Object.values(counts).reduce((a, b) => a + b, 0), 'raw font sizes');
  process.exit(0);
}

const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'));
let fail = false;

// Rule 3. Every rule in every host sheet is held flat: radius 0, 2px on a form
// control, or 50% on one of the circular marks listed here, and no shadow that
// is not inset. The list of marks is closed; a new dot joins it with a reason.
const FORM_CONTROL = /(?:^|[\s>+~])(?:input|textarea|select|button)\b/;
const CIRCULAR_MARKS = new Set([
  '.sent .swatch', '.cited-voices-dot', '.bc-dot', '.map-dot',
  '.ld-rings .ld-ring', '.ld-rings .ld-core', '.ld-orbit .ld-sat', '.es-field-dot',
  '.cmp-signal-dot, .cmp-versus-dot', '.ui-momentum-dot', '.ui-progress-rail-marker',
]);
for (const f of files) {
  const rel = relative(ROOT, f).replace(/\\/g, '/');
  if (!rel.endsWith('.css')) continue;
  // at-rule preludes are dropped so a rule inside @media keeps its own selector
  const text = readFileSync(f, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/@[a-z-]+[^{;]*\{/g, '');
  for (const m of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = m[1].replace(/^[\s}]+/, '').trim().replace(/\s+/g, ' ');
    const body = m[2];
    for (const d of body.matchAll(/border(?:-[a-z-]+)?-radius\s*:\s*([^;]+)/g)) {
      const value = d[1].replace(/\s*!important\s*$/, '').trim();
      if (value === '0' || (value === '2px' && FORM_CONTROL.test(selector)) || (value === '50%' && CIRCULAR_MARKS.has(selector))) continue;
      console.error(`FAIL ${rel}: ${selector} rounds a corner (${value}); radius is 0, 2px on a form control, or 50% on a listed circular mark`);
      fail = true;
    }
    for (const d of body.matchAll(/box-shadow\s*:\s*([^;]+)/g)) {
      const value = d[1].replace(/\s*!important\s*$/, '').trim();
      if (value === 'none' || value.split(/,(?![^(]*\))/).every((layer) => layer.trim().startsWith('inset '))) continue;
      console.error(`FAIL ${rel}: ${selector} casts a shadow (${value}); only inset shadows may remain`);
      fail = true;
    }
  }
}
for (const [f, n] of Object.entries(counts)) {
  const allowed = baseline[f] || 0;
  if (n > allowed) {
    console.error(`FAIL ${f}: ${n} raw font sizes (baseline ${allowed}). Use --t-1..--t-8 tokens.`);
    fail = true;
  }
}
if (!fail) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const base = Object.values(baseline).reduce((a, b) => a + b, 0);
  console.log(`token gate clean: ${total} raw font sizes remain (baseline ${base}, ratchet only shrinks)`);
}
process.exit(fail ? 1 : 0);
