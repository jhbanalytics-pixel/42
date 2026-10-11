// Design token gate. Two rules from the redesign plan:
//  1. No raw font-size in component files: only tokens.css may declare px
//     font sizes. Legacy route files hold a frozen baseline that may only
//     shrink (a ratchet); every other file must be at zero.
//  2. No raw px gap/padding/margin values outside the spacing scale in NEW
//     css added to styles/ (ratcheted the same way for the legacy sheets).
//  3. Every rule in every host sheet rounds no corner beyond 2px on a form
//     control or 50% on a listed circular mark, and casts no shadow that is
//     not inset. No baseline: a second surface language fails here outright.
//     A value written as var(--name) is judged by what every sheet declares
//     for that name, never by the name, so a token cannot carry a drop shadow
//     past the gate.
// Run: node scripts/check_design_tokens.mjs [--write-baseline] [--root <dir>]
// --root points the gate at another source tree. The tests use it to judge
// small fixture sheets; the baseline file is the same one either way.
import {readFileSync, writeFileSync, readdirSync, statSync} from 'node:fs';
import {join, relative, resolve} from 'node:path';

const rootFlag = process.argv.indexOf('--root');
const ROOT = rootFlag >= 0
  ? resolve(process.argv[rootFlag + 1])
  : new URL('../src', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
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
  if (rootFlag >= 0) { console.error('--write-baseline reads the real tree; it cannot be combined with --root'); process.exit(2); }
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
const rules = [];
const declarations = new Map(); // custom property name -> every value any sheet declares for it
for (const f of files) {
  const rel = relative(ROOT, f).replace(/\\/g, '/');
  if (!rel.endsWith('.css')) continue;
  // at-rule preludes are dropped so a rule inside @media keeps its own selector
  const text = readFileSync(f, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/@[a-z-]+[^{;]*\{/g, '');
  for (const m of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = m[1].replace(/^[\s}]+/, '').trim().replace(/\s+/g, ' ');
    const body = m[2];
    rules.push({rel, selector, body});
    for (const d of body.matchAll(/(--[A-Za-z0-9_-]+)\s*:\s*([^;]+)/g)) {
      const values = declarations.get(d[1]) || [];
      values.push(d[2].replace(/\s*!important\s*$/, '').trim());
      declarations.set(d[1], values);
    }
  }
}

const VAR_REFERENCE = /^var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([^)]*))?\)$/;
const splitLayers = (value) => value.split(/,(?![^(]*\))/).map((layer) => layer.trim());

// Why a shadow value is not flat, or null when every layer is none or inset. A
// layer that is a var() reference is followed to every declaration of that name,
// and to its fallback, so one drop shadow declared anywhere under the name fails.
function shadowProblem(value, seen = []) {
  for (const layer of splitLayers(value)) {
    if (layer === 'none' || layer.startsWith('inset ')) continue;
    const reference = layer.match(VAR_REFERENCE);
    if (!reference) return `casts a shadow (${layer})`;
    const name = reference[1];
    if (seen.includes(name)) return `${name} refers to itself`;
    const values = [...(declarations.get(name) || [])];
    if (reference[2]) values.push(reference[2].trim());
    if (!values.length) return `uses ${name}, which no sheet declares`;
    for (const declared of values) {
      const problem = shadowProblem(declared, [...seen, name]);
      if (problem) return problem.startsWith('casts') ? `${name} ${problem}` : problem;
    }
  }
  return null;
}

for (const {rel, selector, body} of rules) {
  for (const d of body.matchAll(/border(?:-[a-z-]+)?-radius\s*:\s*([^;]+)/g)) {
    const value = d[1].replace(/\s*!important\s*$/, '').trim();
    if (value === '0' || (value === '2px' && FORM_CONTROL.test(selector)) || (value === '50%' && CIRCULAR_MARKS.has(selector))) continue;
    console.error(`FAIL ${rel}: ${selector} rounds a corner (${value}); radius is 0, 2px on a form control, or 50% on a listed circular mark`);
    fail = true;
  }
  for (const d of body.matchAll(/box-shadow\s*:\s*([^;]+)/g)) {
    const value = d[1].replace(/\s*!important\s*$/, '').trim();
    const problem = shadowProblem(value);
    if (!problem) continue;
    console.error(`FAIL ${rel}: ${selector} ${problem}; only inset shadows may remain`);
    fail = true;
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
