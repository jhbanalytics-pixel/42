/* The host's own state and headline styles sit on the package scale.

   The instrument routes render inside the package shell, which owns the
   signature red rule, the eight-step type scale and the motion table. The
   host stylesheets that dress the state sections, the Discover and Compare
   headline, the Fieldwork lead and the route move predate all three, so each
   is read here as source and held to the tokens the installed package
   declares. Token names are taken from the package stylesheet rather than
   asserted from memory, so a renamed token fails here before it fails on a
   screen. */
import {expect, test} from 'bun:test';
import {readFileSync, readdirSync, statSync} from 'node:fs';
import {join, relative} from 'node:path';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

const frontendRoot = fileURLToPath(new URL('../../../', import.meta.url));
const sourceRoot = join(frontendRoot, 'src');
const packageCss = readFileSync(
  join(frontendRoot, 'node_modules', 'ogilvy-intelligence-design-system', 'dist', 'style.css'),
  'utf8',
);
const read = (path) => readFileSync(join(sourceRoot, path), 'utf8');

function packageTokens(prefix){
  const tokens = new Map();
  for (const match of packageCss.matchAll(new RegExp(`(--${prefix}-[a-z0-9-]+)\\s*:\\s*([^;}]+)`, 'g'))){
    tokens.set(match[1], match[2].trim());
  }
  return tokens;
}

const typeTokens = packageTokens('type');
const motionTokens = packageTokens('motion');

function milliseconds(value){
  const match = value.trim().match(/^(\d*\.?\d+)(ms|s)$/);
  if (!match) return null;
  return match[2] === 's' ? Number(match[1]) * 1000 : Number(match[1]);
}

const motionDurations = new Set(
  [...motionTokens.values()].map(milliseconds).filter((duration) => duration !== null),
);

function stylesheets(directory){
  const files = [];
  for (const entry of readdirSync(directory)){
    if (entry === '__tests__') continue;
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) files.push(...stylesheets(path));
    else if (entry.endsWith('.css')) files.push(path);
  }
  return files;
}

/* One rule for the selector at the top level, or inside the single media
   block whose params match when a media query is named. */
function rule(css, selector, media){
  const root = postcss.parse(css);
  let scope = root;
  if (media){
    const blocks = [];
    root.walkAtRules('media', (block) => { if (block.params === media) blocks.push(block); });
    expect(blocks, `one @media ${media} block`).toHaveLength(1);
    scope = blocks[0];
  }
  const matches = [];
  scope.walkRules((candidate) => {
    if (candidate.parent === scope && candidate.selectors.includes(selector)) matches.push(candidate);
  });
  expect(matches, `one rule for ${selector}${media ? ` under ${media}` : ''}`).toHaveLength(1);
  return matches[0];
}

function value(ruleNode, property){
  const declarations = ruleNode.nodes.filter((node) => node.type === 'decl' && node.prop === property);
  expect(declarations, `one ${property} on ${ruleNode.selector}`).toHaveLength(1);
  return declarations[0].value;
}

/* The size a font shorthand or a font-size declaration sets. The shorthand
   carries the size immediately before the optional line-height slash, after
   any weight, style or variant word. */
function fontSize(ruleNode){
  const explicit = ruleNode.nodes.find((node) => node.type === 'decl' && node.prop === 'font-size');
  if (explicit) return explicit.value.trim();
  const shorthand = value(ruleNode, 'font');
  const size = shorthand.match(/(?:^|\s)((?:clamp|var|min|max)\([^)]*(?:\([^)]*\)[^)]*)*\)|[\d.]+(?:px|rem|em))\s*(?:\/|\s)/);
  expect(size, `a size inside "${shorthand}"`).not.toBeNull();
  return size[1];
}

/* A scale step is one token. A clamp() or any viewport term is a continuum
   that only touches the scale at its ends, so both are rejected outright. */
function assertOnScale(ruleNode){
  const size = fontSize(ruleNode);
  expect(size, `${ruleNode.selector} carries a pixel literal: ${size}`).not.toMatch(/\d(?:px|rem|em)/);
  expect(size, `${ruleNode.selector} sits on a continuum rather than a step: ${size}`).not.toMatch(/\b(?:clamp|min|max)\(|\d(?:vw|vh|vmin|vmax|dvw|dvh|svw|svh|lvw|lvh)\b/);
  const references = [...size.matchAll(/var\((--type-[a-z0-9-]+)\)/g)].map((match) => match[1]);
  expect(references.length, `${ruleNode.selector} names no scale step: ${size}`).toBeGreaterThan(0);
  for (const reference of references){
    expect(typeTokens.has(reference), `${reference} is not a package type token`).toBe(true);
  }
}

const STRUCTURAL = /^(?:border(?:-(?:top|right|bottom|left))?(?:-width)?|outline(?:-width)?)$/;
const SIX = /(?:^|[^\d.])6px\b/;

/* The instrument routes render through these host sheets. Nothing in them
   may draw a six pixel structure: the addendum permits 1, 2, 4 and 5. */
const HOST_SHEETS = ['ogilvy-intelligence.css', 'app.css', 'styles/fieldwork.css'];

/* The gate page, the boot fallback and the unimported product sheet drew the
   last four six pixel structures; round 5, task 27 moved each to the 1px
   rule, so every consumer sheet is held at zero. The ratchet only shrinks,
   and it has nowhere left to go. */
const OUTSIDE_THIS_FIX = {};

function sixPixelStructures(css){
  const hits = [];
  postcss.parse(css).walkDecls((declaration) => {
    if (STRUCTURAL.test(declaration.prop) && SIX.test(declaration.value)) hits.push(declaration);
  });
  return hits;
}

test('no host stylesheet on the instrument routes draws a six pixel structure', () => {
  const found = {};
  for (const sheet of HOST_SHEETS){
    const hits = sixPixelStructures(read(sheet));
    if (hits.length) found[sheet] = hits.map((hit) => `${hit.source.start.line}: ${hit.prop}: ${hit.value}`);
  }
  expect(found).toEqual({});
});

test('six pixel structures across every consumer stylesheet only ever shrink', () => {
  const counts = {};
  for (const path of stylesheets(sourceRoot)){
    const hits = sixPixelStructures(readFileSync(path, 'utf8'));
    if (hits.length) counts[relative(sourceRoot, path).replace(/\\/g, '/')] = hits.length;
  }
  for (const [sheet, count] of Object.entries(counts)){
    expect(count, `${sheet} draws ${count} six pixel structures`).toBeLessThanOrEqual(OUTSIDE_THIS_FIX[sheet] || 0);
  }
});

/* Round 9, item 4. The folio title "Discover" at type-8 broke mid-word in
   the 196px folio column at 1730, and widening the column would take space
   from the candidates, so the folio steps to type-7. The tracking follows the
   step: the addendum's type role table pairs type-7, the section proposition,
   with -0.02em, and -0.03em belongs to type-8. */
test('the Discover folio title sits on type-7 with the addendum tracking', () => {
  const css = read('ogilvy-intelligence.css');
  expect(typeTokens.get('--type-7')).toBeTruthy();
  for (const selector of ['.explore-folio h1']){
    const headline = rule(css, selector);
    expect(fontSize(headline)).toBe('var(--type-7)');
    expect(value(headline, 'letter-spacing')).toBe('-0.02em');
  }
});

test('the Fieldwork lead and its state note sit on scale steps', () => {
  const css = read('styles/fieldwork.css');
  for (const selector of ['.fieldwork-lead__kicker', '.fieldwork-lead__title', '.fieldwork-state__note']){
    assertOnScale(rule(css, selector));
  }
  expect(fontSize(rule(css, '.fieldwork-lead__title'))).toBe('var(--type-7)');
  const narrow = rule(css, '.fieldwork-lead__title', '(max-width: 680px)');
  assertOnScale(narrow);
  expect(fontSize(narrow)).toBe('var(--type-6)');
});

/* Round 3, Task 18, item 8. The addendum sets every Newsreader role at 400
   and the provenance role in mono: Recursive Mono at type-1, weight 500,
   0.18em, uppercase, muted. The Fieldwork lead measured Recursive Sans 12px
   600 at 0.08em over a Newsreader title at 600, so both rules are held to
   the role here. The font shorthand carries the weight first and the family
   last, and the family has to name the package mono token. */
/* Quiet register, 23 Sept 2026: the kicker is a label a reader reads, so it
   leaves the mono capitals for the sans at 14px and weight 400 in sentence
   case, with no tracking and the MONO axis off. */
/* One title voice, 2 October 2026: every page title is the sans at weight
   600 (Today, Ask, Discover, History), so the Fieldwork title leaves the
   Newsreader 400 role for that, still at the type-7 step. */
test('the Fieldwork lead kicker takes the quiet label and the title sits in the sans at weight 600', () => {
  const css = read('styles/fieldwork.css');
  const kicker = rule(css, '.fieldwork-lead__kicker');
  const kickerFont = value(kicker, 'font');
  expect(kickerFont).toMatch(/^400 var\(--type-3\)\/[\d.]+ var\(--(?:sans|font-sans|oi-sans)\)/);
  const declares = (property) => kicker.nodes.some((node) => node.type === 'decl' && node.prop === property);
  expect(declares('letter-spacing'), 'the kicker sets no tracking').toBe(false);
  expect(declares('text-transform'), 'the kicker sets no case').toBe(false);
  expect(value(kicker, 'color')).toBe('var(--muted)');
  expect(value(kicker, 'font-variation-settings')).toMatch(/"MONO" 0/);
  const title = rule(css, '.fieldwork-lead__title');
  expect(value(title, 'font')).toMatch(/^600 var\(--type-7\)\/.* var\(--sans\)$/);
});

test('the route move runs at a duration from the motion table', () => {
  const css = read('app.css');
  expect(motionTokens.has('--motion-layer')).toBe(true);
  expect(milliseconds(motionTokens.get('--motion-layer'))).toBe(240);
  const moves = [];
  postcss.parse(css).walkDecls('animation', (declaration) => {
    if (/\brouteIn\b/.test(declaration.value)) moves.push(declaration);
  });
  /* One layer open on every lane; the scale and slide variants are retired. */
  expect(moves).toHaveLength(1);
  for (const move of moves){
    const parts = move.value.split(/\s+/);
    const token = parts.find((part) => /^var\(--motion-[a-z-]+\)$/.test(part));
    const literal = parts.map(milliseconds).find((duration) => duration !== null);
    const duration = token ? milliseconds(motionTokens.get(token.slice(4, -1)) || '') : literal;
    expect(duration, `${move.parent.selector} moves for "${move.value}"`).not.toBeNull();
    expect(motionDurations.has(duration), `${duration}ms is not in the motion table`).toBe(true);
    expect(token, `${move.parent.selector} names its duration as a literal rather than a token`).toBeDefined();
  }
});

/* Round 4, task 26, P13. The brief tab's scan heading was the one heading on
   the console left at the browser's own h2 weight, 700, where every other
   Newsreader role sits at 400. */
test('the behaviour scan heading uses the section role at weight 400', () => {
  const heading = rule(read('app.css'), '.bscan-head h2');
  expect(fontSize(heading)).toBe('var(--type-section-title)');
  expect(value(heading, 'line-height')).toBe('var(--leading-section-title)');
  expect(value(heading, 'font-weight')).toBe('400');
});

/* Round 4, task 26, P21. The package ring sits at --focus-offset, 2px, on
   every shell control, and the consumer wrote its own offsets: 2px written
   out, 3px on the frame submit, the recent investigation links and the
   compare links, 4px on the scan heading, 6px on the gate field. One offset
   everywhere means one token, so every consumer sheet names it; a ring drawn
   inside its box names the same token, negated. */
const FOCUS_OFFSET = /^(?:var\(--focus-offset\)|calc\(var\(--focus-offset\) \* -[12]\))$/;

test('every focus offset in every consumer sheet is on the package token', () => {
  const findings = [];
  for (const path of stylesheets(sourceRoot)){
    const sheet = relative(sourceRoot, path).replace(/\\/g, '/');
    postcss.parse(readFileSync(path, 'utf8')).walkDecls('outline-offset', (declaration) => {
      if (!FOCUS_OFFSET.test(declaration.value.trim())) findings.push(`${sheet}:${declaration.source.start.line} ${declaration.parent.selector.replace(/\s+/g, ' ')}: ${declaration.value.trim()}`);
    });
  }
  expect(findings).toEqual([]);
});

/* Ask redesign, 23 Sept 2026: the rail title repeated the page head, which
   now names the page once, so the rail carries no title and no sheet dresses
   one. The provenance role it held is still pinned on the remaining kickers. */
test('the Build rail carries no title of its own', () => {
  const css = read('app.css');
  expect(css).not.toMatch(/\.workbench-rail-title\b/);
  const consoleCss = read('styles/console.css');
  expect(consoleCss).not.toMatch(/\.workbench-rail-title\b/);
  expect(read('ConsoleWorkbench.jsx')).not.toMatch(/workbench-rail-title/);
});
