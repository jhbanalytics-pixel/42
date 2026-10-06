/* The console workbench is drawn in the instrument's language.

   The workbench predates the ink direction. It kept a card language of its
   own: rounded corners, drop shadows, a glow on the approved action and a
   private set of type sizes. The addendum rules radius at zero, with two
   pixels permitted on a form control, rules every shadow off the surface, and
   holds every size to the eight-step scale the package declares. These tests
   read every consumer stylesheet as source and hold each rule that dresses a
   console selector to those three rules, wherever it lives, so a second
   surface language fails here rather than in a review. The console sheet is
   held whole; the shared sheets are held for their console-prefixed rules. */
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

const semanticTypeValues = new Map([
  ['--type-page-title', '32px'],
  /* Restated, design audit 2 October 2026: the three roles were 19, 17 and
     15 px, off the package scale and too close to tell apart. They now sit on
     the package's own steps, 24, 18 and 16 px (Butterick, Practical
     Typography; Bringhurst, modular scale). */
  ['--type-section-title', '24px'],
  ['--type-signal-title', '18px'],
  ['--type-body', '16px'],
  ['--type-meta', '12px'],
]);
const semanticShapeValues = new Map([
  ['--shape-control', '6px'],
  ['--shape-default', '8px'],
  ['--shape-card', '10px'],
]);
const appRoleValues = new Map();
postcss.parse(readFileSync(join(sourceRoot, 'app.css'), 'utf8')).walkDecls((declaration) => {
  if (semanticTypeValues.has(declaration.prop) || semanticShapeValues.has(declaration.prop)) {
    appRoleValues.set(declaration.prop, declaration.value.trim());
  }
});
const typeTokens = new Set(
  [...packageCss.matchAll(/(--type-[a-z0-9-]+)\s*:/g)].map((match) => match[1]),
);
for (const token of semanticTypeValues.keys()) typeTokens.add(token);

/* The selectors the workbench renders: its own chrome, the behaviour scan,
   the brief flow and the shared flow strip, hero and market chip. */
/* Anchored at the start of a compound, not of the selector, so a rule that
   reaches a console class behind an attribute or an element is walked too. */
const CONSOLE_SELECTOR = /(?:^|[\s>+~])\.(?:workbench|bscan|research-|ui-flowbar|ui-page-hero|ui-market|investigation)/;
const CONSOLE_SHEET = 'styles/console.css';

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

/* Every rule on a console selector across the consumer sheets, media blocks
   included, plus every rule in the console sheet. */
function consoleRules(){
  const rules = [];
  for (const path of stylesheets(sourceRoot)){
    const sheet = relative(sourceRoot, path).replace(/\\/g, '/');
    postcss.parse(readFileSync(path, 'utf8')).walkRules((rule) => {
      if (sheet === CONSOLE_SHEET || rule.selectors.some((selector) => CONSOLE_SELECTOR.test(selector))) rules.push({sheet, rule});
    });
  }
  return rules;
}

const RULES = consoleRules();

/* Every rule in every consumer sheet. Host radii use an approved shape role,
   stay at zero, use 2px on a form control, or use 50% on a listed circular mark.
   No shadow leaves the surface. */
function everyRule(){
  const rules = [];
  for (const path of stylesheets(sourceRoot)){
    const sheet = relative(sourceRoot, path).replace(/\\/g, '/');
    postcss.parse(readFileSync(path, 'utf8')).walkRules((rule) => rules.push({sheet, rule}));
  }
  return rules;
}

const EVERY_RULE = everyRule();

function where(sheet, declaration){
  return `${sheet}:${declaration.source.start.line} ${declaration.parent.selector.replace(/\s+/g, ' ')}`;
}

const FORM_CONTROL = /(?:^|[\s>+~])(?:input|textarea|select|button)\b/;

/* The circular marks the host draws: a status dot, a plotted point, a loading
   figure. Each is a square box whose only content is its own fill, which is
   the one place a 50% radius draws a circle rather than rounds a corner. The
   list is closed; a new dot is added here with its reason or it stays square. */

const CIRCULAR_MARKS = new Set([
  '.sent .swatch',                /* the sentiment swatch beside a sentiment word */
  '.cited-voices-dot',            /* the live dot on the cited voices eyebrow */
  '.bc-dot',                      /* the plotted point on the behaviour chart */
  '.map-dot',                     /* the node point on the map */
  '.ld-rings .ld-ring',           /* the loading figure's rings */
  '.ld-rings .ld-core',           /* the loading figure's core */
  '.ld-orbit .ld-sat',            /* the loading figure's satellites */
  '.es-field-dot',                /* the field marker on an empty state */
  '.cmp-signal-dot, .cmp-versus-dot', /* the signal points on the compare strips */
  '.ui-momentum-dot',             /* the momentum dot in the momentum pill */
  '.ui-progress-rail-marker',     /* the marker on the progress rail */
]);

test('host radii use approved shape roles while preserving controls and circular marks', () => {
  const findings = [];
  for (const {sheet, rule} of EVERY_RULE){
    rule.walkDecls(/^border(?:-[a-z-]+)?-radius$/, (declaration) => {
      const value = declaration.value.trim();
      if (value === '0') return;
      if ([...semanticShapeValues.keys()].some((token) => value === 'var(' + token + ')')) return;
      if (value === '2px' && FORM_CONTROL.test(rule.selector)) return;
      if (value === '50%' && CIRCULAR_MARKS.has(rule.selector.replace(/\s+/g, ' '))) return;
      findings.push(`${where(sheet, declaration)}: ${declaration.prop}: ${value}`);
    });
  }
  for (const [token, size] of semanticShapeValues) expect(appRoleValues.get(token), token).toBe(size);
  expect(findings).toEqual([]);
});

test('the app type roles resolve to the approved desktop sizes', () => {
  for (const [token, size] of semanticTypeValues) expect(appRoleValues.get(token), token).toBe(size);
});

test('no rule in any host sheet casts a shadow or a glow', () => {
  const findings = [];
  for (const {sheet, rule} of EVERY_RULE){
    rule.walkDecls('box-shadow', (declaration) => {
      const value = declaration.value.trim();
      if (value === 'none') return;
      /* Task 65. The press mark of section 7 is two inset rules declared once
         as a token, so it reads as a var rather than as a list of layers here.
         It is named rather than pattern matched, because a var is opaque and
         the point of this check is that nothing casts a shadow outward. */
      if (/^var\(--press-grip(?:-inverse)?\)$/.test(value)) return;
      const layers = value.split(/,(?![^(]*\))/).map((layer) => layer.trim());
      if (layers.every((layer) => layer.startsWith('inset ') || /^var\(--press-grip(?:-inverse)?\)$/.test(layer))) return;
      findings.push(`${where(sheet, declaration)}: box-shadow: ${value}`);
    });
    /* A radial halo painted behind a card is a glow by another property. The
       one radial gradient that is not a halo is the body's dot grid, a one
       pixel point of the rule colour repeated on a 30px step. */
    rule.walkDecls(/^background(?:-image)?$/, (declaration) => {
      if (/radial-gradient\((?!circle at 1px 1px, var\(--line\) 1px, transparent 1\.5px\))/.test(declaration.value)) findings.push(`${where(sheet, declaration)}: ${declaration.prop}: ${declaration.value.trim()}`);
    });
  }
  expect(findings).toEqual([]);
});

/* The size a font-size declaration or a font shorthand sets. */
function sizes(rule){
  const found = [];
  rule.walkDecls((declaration) => {
    if (declaration.prop === 'font-size'){
      found.push({declaration, size: declaration.value.trim()});
    } else if (declaration.prop === 'font'){
      const match = declaration.value.match(/(?:^|\s)((?:clamp|var|min|max)\([^)]*(?:\([^)]*\)[^)]*)*\)|[\d.]+(?:px|rem|em|%))\s*(?:\/|\s)/);
      found.push({declaration, size: match ? match[1] : declaration.value.trim()});
    }
  });
  return found;
}

function offScale(size){
  if (/\d(?:px|rem|em|%)/.test(size)) return 'carries a literal';
  if (/\b(?:clamp|min|max)\(|\d(?:vw|vh|vmin|vmax|dvw|dvh|svw|svh|lvw|lvh)\b/.test(size)) return 'sits on a continuum';
  if (size === 'inherit') return null;
  const references = [...size.matchAll(/var\((--[a-z0-9-]+)\)/g)].map((match) => match[1]);
  if (references.length === 0) return 'names no scale step';
  const stray = references.find((reference) => !typeTokens.has(reference));
  return stray ? `${stray} is not a package type token` : null;
}

test('every font size on a console rule sits on a scale step', () => {
  const findings = [];
  for (const {sheet, rule} of RULES){
    for (const {declaration, size} of sizes(rule)){
      const reason = offScale(size);
      if (reason) findings.push(`${where(sheet, declaration)}: ${size} ${reason}`);
    }
  }
  expect(findings).toEqual([]);
});

/* The chrome the workbench inherits from the shared sheets: the topbar, the
   rail, its tabs and actions, the landing cards and the flow strip. Each of
   these had a private size, and the console sheet restates every one on a
   scale step under the .workbench scope, so the restatement wins whatever
   the shared sheet says. A renamed class fails here before it ships. */
/* Ask redesign, 23 Sept 2026: the rail head (its title and data lines)
   repeated the page head and is gone, so neither is listed; the page title
   and its coverage line are, and the removed pair is held absent below. */
const INHERITED_CHROME = [
  '.workbench-back', '.workbench-topbar-title', '.workbench-title', '.workbench-coverage',
  '.workbench-mode-tabs button', '.workbench-mobile-switch button', '.workbench-new-brief',
  '.workbench-rail-k', '.workbench-recent-title', '.workbench-recent-meta', '.workbench-empty',
  '.workbench-kicker', '.workbench-start h1', '.workbench-start-sub', '.workbench-action-k',
  '.workbench-action-title', '.workbench-action-copy', '.ui-page-hero-eyebrow', '.ui-page-hero-sub',
  '.ui-flowbar-step', '.ui-flowbar-chip', '.ui-flowbar-cta', '.ui-flowbar-count', '.ui-market-chip',
];

test('the chrome the workbench inherits is restated on a scale step under the workbench scope', () => {
  const restated = new Map();
  for (const {sheet, rule} of RULES){
    if (sheet !== CONSOLE_SHEET || rule.parent.type !== 'root') continue;
    for (const selector of rule.selectors){
      const match = selector.match(/^\.workbench\s+(.+)$/);
      if (!match) continue;
      for (const {size} of sizes(rule)) restated.set(match[1], size);
    }
  }
  const findings = [];
  for (const selector of INHERITED_CHROME){
    const size = restated.get(selector);
    if (!size){ findings.push(`${selector} is not restated under .workbench`); continue; }
    const reason = offScale(size);
    if (reason) findings.push(`.workbench ${selector}: ${size} ${reason}`);
  }
  expect(findings).toEqual([]);
});

/* The three colours the addendum rules out of text: faint, rule and the plane
   red. None of them, nor any alias that resolves to one, may be named as a
   foreground on a console rule in any sheet. The resolved check, through the
   package palette and the cascade, is scripts/check_contrast.mjs. */
test('no console rule paints text in faint, rule or the plane red', () => {
  const findings = [];
  for (const {sheet, rule} of RULES){
    rule.walkDecls('color', (declaration) => {
      const value = declaration.value.trim();
      if (/var\(--(?:faint|ink-faint|color-ink-faint|rule|line|line-2|hairline|color-ink-rule|color-rule|color-daylight-red-surface|ogilvy-red|accent|down|neg|neu|steady)\)/.test(value)
        || /#(?:5c554f|2a2521|e41424)\b/i.test(value)){
        findings.push(`${where(sheet, declaration)}: color: ${value}`);
      }
    });
  }
  expect(findings).toEqual([]);
});

/* The workbench reads no token it redefines for its descendants. A scope
   alias on .workbench reaches package components composed inside it, which
   the consumer does not own, so every token move is stated on the rule that
   needs it. The daylight plane and flow strip blocks are the two rulings the
   brief made, and they alias surfaces, never text tokens. */
test('the workbench scope carries no text token alias', () => {
  const aliases = [];
  for (const {sheet, rule} of RULES){
    if (sheet !== CONSOLE_SHEET || !/(?:^|\s)\.workbench$/.test(rule.selector.trim())) continue;
    rule.walkDecls(/^--(?:faint|muted|ink|ink-2|accent|header-muted)$/, (declaration) => {
      aliases.push(`${where(sheet, declaration)}: ${declaration.prop}: ${declaration.value}`);
    });
  }
  expect(aliases).toEqual([]);
});

/* Round 3, Task 18. Two console targets sat under the 48 pixel floor on the
   deployed build: the recent investigation links at 28px tall, and the flow
   strip chips at 37 and 44px wide. Lighthouse lists neither, since the chips
   clear 24 and the links are text, so the floor is held here at source: the
   link rule reserves 48 in the block axis, the chip rule 48 in the inline
   axis. The rendered boxes were measured by hand on the deployed build; the
   console-focus harness answers the investigation list with a 404, so it
   renders the index in its error state and no link box. */
function pixels(value){
  const match = value.trim().match(/^(\d*\.?\d+)px$/);
  return match ? Number(match[1]) : null;
}

test('the recent investigation links and the flow strip chips reserve 48 pixels', () => {
  const floors = {'.investigation-row__question a': 'min-block-size', '.ui-flowbar-chip': 'min-inline-size'};
  const found = {};
  for (const {rule} of RULES){
    for (const [selector, property] of Object.entries(floors)){
      if (!rule.selectors.includes(selector)) continue;
      rule.walkDecls(property, (declaration) => { found[selector] = pixels(declaration.value); });
    }
  }
  for (const [selector, property] of Object.entries(floors)){
    expect(found[selector], `${selector} declares ${property}`).not.toBeUndefined();
    expect(found[selector], `${selector} ${property} is ${found[selector]}px`).toBeGreaterThanOrEqual(48);
  }
});

/* Round 4, task 23. The console and research controls on the motion table.
   The Ask tab had no hover; the inline actions sat at 157 by 32 in the
   browser's own font with no hover; the frame submit snapped; Back and the
   fresh-start actions hovered into a red tint, a second use of the ribbon
   fill; and the workbench remapped the raised step to paper in daylight, so
   the row move on its cards had no panel to step onto. Each is held here in
   the console sheet, and the rendered deltas are measured in the task report. */
function consoleSheetRules(){
  return RULES.filter(({sheet}) => sheet === CONSOLE_SHEET).map(({rule}) => rule);
}

function declarationsOf(selector){
  const found = {};
  for (const rule of consoleSheetRules()){
    if (!rule.selectors.map((s) => s.replace(/\s+/g, ' ')).includes(selector)) continue;
    rule.walkDecls((declaration) => { found[declaration.prop] = declaration.value.trim(); });
  }
  return found;
}

test('the Ask tab and the mobile switch take the control move on hover and focus', () => {
  for (const selector of ['.workbench .workbench-mode-tabs button:hover', '.workbench .workbench-mode-tabs button:focus-visible', '.workbench .workbench-mobile-switch button:hover']){
    const found = declarationsOf(selector);
    expect(found.color, `${selector} lifts the text`).toBe('var(--ink)');
    expect(found['border-color'], `${selector} reddens the boundary`).toBe('var(--ogilvy-red-text)');
  }
});

test('Back and the fresh-start actions run at the row speed and step onto the raised surface, never a red tint', () => {
  for (const selector of ['.workbench .workbench-back', '.workbench .workbench-new-brief']){
    const rest = declarationsOf(selector);
    expect(rest.transition, `${selector} names the row move`).toMatch(/var\(--motion-row\)/);
    /* Task 65. The hover move stays on the row token; every property the hover
       touches is still named there. The one entry on the control token is the
       box-shadow the press mark of section 7 moves, which the Active row puts
       on --motion-control and which no hover rule here writes. */
    for (const entry of rest.transition.split(/,(?![^(]*\))/).map((part) => part.trim())){
      if (/var\(--motion-control\)/.test(entry)){
        expect(entry, `${selector} runs only the press on the control move`).toMatch(/^box-shadow /);
        continue;
      }
      expect(entry, `${selector} runs its hover on the row move`).toMatch(/var\(--motion-row\)/);
    }
    const hover = declarationsOf(`${selector}:hover`);
    expect(hover.background, `${selector}:hover steps onto the raised surface`).toBe('var(--surface-2)');
  }
  for (const rule of consoleSheetRules()){
    rule.walkDecls(/^background(?:-color)?$/, (declaration) => {
      if (/--accent-soft/.test(declaration.value) && /workbench-(?:back|new-brief|mode-tabs)/.test(rule.selector)){
        throw new Error(`${where(CONSOLE_SHEET, declaration)} paints the ribbon tint`);
      }
    });
  }
});

test('the inline actions are controls on the scale with the 48 pixel floor and the control move', () => {
  const rest = declarationsOf('.workbench .research-inline-action');
  expect(rest['font-size']).toBe('var(--type-3)');
  expect(rest['font-family']).toBe('var(--sans)');
  expect(pixels(rest['min-block-size'] || '')).toBeGreaterThanOrEqual(48);
  expect(rest.transition).toMatch(/var\(--motion-control\)/);
  expect(declarationsOf('.workbench .research-inline-action:hover')['border-color']).toBe('var(--ogilvy-red-text)');
});

test('the frame submit moves at the control speed and the market labels keep the 48 pixel floor', () => {
  expect(declarationsOf('.investigation-frame__submit').transition).toMatch(/var\(--motion-control\)/);
  expect(pixels(declarationsOf('.investigation-market')['min-height'] || '')).toBeGreaterThanOrEqual(48);
});

test('the row move has a panel to step onto in both themes: the raised step in midnight, the white sheet in daylight', () => {
  for (const selector of ['.workbench-action-card', '.workbench-back', '.workbench-new-brief', '.workbench-recent-row']){
    expect(declarationsOf(`.workbench ${selector}:hover`).background, `${selector} midnight`).toBe('var(--surface-2)');
    for (const state of [':hover', ':focus-visible']){
      expect(declarationsOf(`[data-dir="daylight"] .workbench ${selector}${state}`).background, `${selector}${state} daylight`).toBe('var(--white)');
    }
  }
});

/* Quiet register, 23 Sept 2026: the kicker was mono at 11px, uppercase and
   tracked 0.18em, the spaced mono capitals the rulebook names as the first
   sign of a machine-made screen. Mono is for ids and codes (rule 7), caps
   and tracking on a label flatten the hierarchy (rules 4 and 5), and nothing
   a reader reads sits under 14px (rule 2), so every console kicker is now a
   quiet label: the sans on its sans axis at type-3, weight 400, sentence
   case with no tracking, in muted, never red. */
test('every console kicker is a quiet label: sans, type-3, 400, no tracking, no capitals, muted, no red', () => {
  for (const selector of ['.workbench .workbench-kicker', '.workbench .ui-page-hero-eyebrow', '.investigation-folio__kicker', '.research-landed-k']){
    const found = declarationsOf(selector);
    expect(found['font-family'], `${selector} family`).toBe('var(--sans)');
    expect(found['font-size'], `${selector} size`).toBe('var(--type-3)');
    expect(found['font-weight'], `${selector} weight`).toBe('400');
    expect(found['letter-spacing'], `${selector} tracking`).toBe('0');
    expect(found['text-transform'], `${selector} case`).toBe('none');
    expect(found['font-variation-settings'], `${selector} axis`).toMatch(/"MONO" 0\b/);
    expect(found.color, `${selector} colour`).toBe('var(--muted)');
  }
});

/* Quiet register, 23 Sept 2026: Build brief names itself in the page head,
   so the framing drops the uppercase "Intelligence Console" eyebrow and is a
   section under that one h1 rather than a second h1 with its own masthead
   rule. */
test('the framing is a section under the Build brief page head, with no eyebrow and no masthead rule', () => {
  const source = readFileSync(join(sourceRoot, 'ui', 'InvestigationFraming.jsx'), 'utf8');
  expect(source).not.toMatch(/investigation-folio__kicker/);
  expect(source).not.toMatch(/<h1/);
  expect(source).toMatch(/<h2 className="investigation-folio__title">Frame an investigation<\/h2>/);
  expect(declarationsOf('.workbench .investigation-framing .investigation-folio')['border-bottom']).toBe('0');
  expect(declarationsOf('.workbench .investigation-framing .investigation-folio__title')['font-size']).toBe('var(--type-6)');
  expect(declarationsOf('.workbench .investigation-framing').margin).toBe('0');
});

/* Round 4, task 26. P30: the four market chips on the flow strip produced no
   hover in either theme and the pressed chip rested on the 14% ribbon tint,
   the last tint on the route beside the progress marker; the primary card
   rested on the raised step in midnight and so had nowhere to go on hover.
   P31: the recent investigation question was the one Newsreader role on the
   route at 600. P24: the topbar title was mono 700 in red, a fourth kicker
   voice on a route that has one. */
/* Quiet register, 23 Sept 2026: the flow strip carried 12px step labels
   with tracking, a tinted plane and red-outlined square markers. It is now a
   row of words on the canvas at type-3 with no tracking, the current step
   marked by the 2px red rule a current filter takes, and no markers. */
test('the flow strip is a row of words: type-3, no tracking, no plane, the current step underlined, no markers', () => {
  for (const selector of ['.workbench .ui-flowbar-step', '.workbench .ui-flowbar-cta', '.workbench .ui-flowbar-chip']){
    expect(declarationsOf(selector)['font-size'], selector).toBe('var(--type-3)');
    expect(declarationsOf(selector)['letter-spacing'], selector).toBe('0');
  }
  expect(declarationsOf('.workbench .ui-flowbar')['--flowbar-bg']).toBe('transparent');
  expect(declarationsOf('.workbench .ui-flowbar-step[data-state="current"]')['box-shadow']).toBe('inset 0 -2px 0 var(--ogilvy-red-text)');
  expect(declarationsOf('.workbench .ui-flowbar-step .ui-progress-rail-marker').display).toBe('none');
});

/* Quiet register, 23 Sept 2026: the market chips were framed buttons and
   the pressed chip rested on the raised plane in a red frame with red text.
   They are now a row of words: no frame at rest, the control move lifts the
   text to ink over a muted rule, and the pressed market is ink over the 2px
   red rule a current filter takes, with no plane. The ribbon tint stays
   ruled out. */
test('the flow strip chips are a row of words: no frame, the control move, and the pressed market underlined in red with no plane', () => {
  const rest = declarationsOf('.workbench .ui-flowbar-chip');
  expect(rest.transition, 'chip names the control move').toMatch(/var\(--motion-control\)/);
  expect(rest['border-color'], 'no frame at rest').toBe('transparent');
  const hover = declarationsOf('.workbench .ui-flowbar-chip:hover');
  expect(hover.color, 'chip hover lifts the text').toBe('var(--ink)');
  expect(hover['box-shadow'], 'chip hover draws the muted rule').toBe('inset 0 -1px 0 var(--muted)');
  const pressed = declarationsOf('.workbench .ui-flowbar-chip[aria-pressed="true"]');
  expect(pressed.background, 'pressed chip has no plane').toBe('transparent');
  expect(pressed.color).toBe('var(--ink)');
  expect(pressed['box-shadow'], 'pressed chip takes the current rule').toBe('inset 0 -2px 0 var(--ogilvy-red-text)');
  for (const rule of consoleSheetRules()){
    rule.walkDecls(/^background(?:-color)?$/, (declaration) => {
      if (/--accent-soft/.test(declaration.value) && /ui-flowbar-chip/.test(rule.selector)) throw new Error(`${where(CONSOLE_SHEET, declaration)} paints the ribbon tint`);
    });
  }
});

test('the primary action card reddens its rule on hover like its sibling', () => {
  for (const state of [':hover', ':focus-visible']){
    expect(declarationsOf(`.workbench .workbench-action-card.primary${state}`)['border-color'], `primary${state}`).toBe('var(--ogilvy-red-text)');
  }
});

test('the recent investigation question sits on type-5 at weight 400 and its link moves on the control move', () => {
  const question = declarationsOf('.investigation-row__question');
  expect(question['font-size']).toBe('var(--type-5)');
  expect(question['font-weight']).toBe('400');
  expect(declarationsOf('.investigation-row__question a').transition).toMatch(/var\(--motion-control\)/);
  expect(declarationsOf('.investigation-row__question a:hover').color).toBe('var(--ogilvy-red-text)');
});

/* Quiet register, 23 Sept 2026: a recent question in the Ask rail wraps
   inside the rail rather than running past it under a clipped ellipsis, so
   every word of it stays readable and inside its row. */
test('a recent question in the rail wraps inside the rail instead of clipping', () => {
  const title = declarationsOf('.workbench .workbench-recent-title');
  expect(title['font-size']).toBe('var(--type-3)');
  expect(title['white-space']).toBe('normal');
  expect(title.overflow).toBe('visible');
});

/* Round 5, task 27. Section 5 gives no role a weight of 700, and every
   Newsreader role is 400. The workbench rendered twelve mono runs at 700 and
   three Newsreader runs at 600 through the shared sheet. */
test('no console rule sets a weight off the type roles', () => {
  const findings = [];
  for (const {sheet, rule} of RULES){
    const declarations = Object.fromEntries(rule.nodes.filter((n) => n.type === 'decl').map((n) => [n.prop, n.value.trim()]));
    const shorthand = declarations.font ? declarations.font.match(/^(\d{3})\b/)?.[1] : null;
    const weight = declarations['font-weight'] || shorthand;
    if (weight === '700' || weight === 'bold') findings.push(`${where(sheet, rule.nodes[0])}: weight ${weight}`);
    const serif = /var\(--serif\)/.test(declarations['font-family'] || declarations.font || '');
    if (serif && weight && weight !== '400') findings.push(`${where(sheet, rule.nodes[0])}: Newsreader at ${weight}`);
  }
  expect(findings).toEqual([]);
});

/* The landing eyebrow is the one place the route says Intelligence console;
   the topbar carries the job and the mode, the rail its own name. */
test('the console says Intelligence console once, on the landing eyebrow', () => {
  const source = readFileSync(join(sourceRoot, 'ConsoleWorkbench.jsx'), 'utf8');
  expect(source.match(/Intelligence console/g)).toHaveLength(1);
  expect(source).toMatch(/eyebrow="Intelligence console"/);
  /* Ask redesign, 23 Sept 2026: the rail no longer names itself Workbench,
     Ask carries a serif page title, and the other modes keep the topbar
     label. */
  expect(source).not.toMatch(/workbench-rail-title|workbench-rail-sub|workbench-rail-head/);
  expect(source).toMatch(/<h1 className="workbench-title">Ask<\/h1>/);
  expect(source).toMatch(/<div className="workbench-topbar-title">\s*<span>Build<\/span>/);
  /* Quiet register, 23 Sept 2026: Build brief takes the same serif page
     title as Ask. Its title was the 18px em inside the topbar label, which
     the phone sheet hides, so at 390 the page had no title at all; the
     topbar label now serves only the landing. */
  expect(source).toMatch(/<h1 className="workbench-title">Build brief<\/h1>/);
  expect(source).not.toMatch(/<em>\{work === 'brief' \? 'Build brief'/);
});

/* Quiet register, 23 Sept 2026: the topbar label heads only the landing.
   Its hidden route word was held as a mono kicker at weight 500 and 0.18em
   tracking in capitals; it is now a quiet label, the muted sans at 14px and
   400 in the case its words are written in, with no tracking and the mono
   axis off (rule 7), never a red mono heading, and the visible mode takes
   the page title step the other tabs' titles use. This one test holds both
   lanes' checks: the APP-2 workbench lane's and the APP-5 sheets lane's. */
test('the topbar title is a quiet label, not a red mono heading, and its mode sits on the page title step', () => {
  const found = declarationsOf('.workbench .workbench-topbar-title');
  expect(found['font-family']).toBe('var(--sans)');
  expect(found['font-size']).toBe('var(--type-3)');
  expect(found['font-weight']).toBe('400');
  expect(found['letter-spacing']).toBe('0');
  expect(found['text-transform']).toBe('none');
  expect(found['font-variation-settings']).toBe('normal');
  expect(found.color).toBe('var(--muted)');
  expect(declarationsOf('.workbench .workbench-topbar-title em').font).toMatch(/var\(--type-7\)/);
});

/* Quiet register, 23 Sept 2026. Three console rules outranked the shared
   sheet's quiet ones because the console sheet lands after it: the plan's
   decorative step figure stayed red, the outcome preview kept a frame, a
   fill and a red rule inside the research controls frame, and the persona
   description sat at 12px. Rules 9, 10 and the 14px floor. */
test('the plan figure is muted, the outcome preview carries no frame of its own, and the persona line is 14px', () => {
  const red = consoleSheetRules().filter((rule) => rule.nodes.some((node) => node.type === 'decl' && node.prop === 'color' && node.value.trim() === 'var(--ogilvy-red-text)'));
  for (const rule of red){
    expect(rule.selectors.map((s) => s.replace(/\s+/g, ' '))).not.toContain('.workbench .research-outcome-flow-signal');
  }
  expect(declarationsOf('.workbench .research-outcome-flow-signal').color).toBe('var(--muted)');

  const card = declarationsOf('.research-outcome-card');
  expect(card.border).toBe('0');
  expect(card['border-top']).toBe('1px solid var(--line)');
  expect(card['border-left']).toBeUndefined();
  expect(card.background).toBe('transparent');

  expect(declarationsOf('.research-persona-card-desc')['font-size']).toBe('var(--type-3)');
});
