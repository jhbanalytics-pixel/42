import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

import {
  CRITICAL_SURFACE_ROOTS,
  DEFERRED_SELECTOR_ROOTS,
  declaredSelectors,
  pointerStateRules,
  splitInstrumentStylesheet,
} from '../../instrumentStylesheetSplit.mjs';

const frontendRoot = fileURLToPath(new URL('../../../', import.meta.url));
const installedStylesheet = join(
  frontendRoot,
  'node_modules',
  'ogilvy-intelligence-design-system',
  'dist',
  'style.css',
);
const css = readFileSync(installedStylesheet, 'utf8');

test('the virtual critical stylesheet loads after Vite normalizes its Windows path', async () => {
  const {default: config} = await import('../../../vite.config.mjs');
  const plugin = config.plugins.find((entry) => entry.name === 'instrument-stylesheet-split');
  const resolved = plugin.resolveId('ogilvy-intelligence-design-system/style.critical.css');
  expect(typeof plugin.load(resolved.replaceAll('\\', '/'))).toBe('string');
});

/* The pointer axis the split reads, restated here so the assertions below do
   not have to trust the module for what they are checking it against. A state
   is read off the subject, so :not(:hover), which is true before any pointer
   arrives, does not count, and a selector naming focus never counts. */
const POINTER_STATE = /:(?:hover|active)(?![a-zA-Z0-9-])/;
const FOCUS_STATE = /:focus(?:-visible|-within)?(?![a-zA-Z0-9-])/;

function needsPointer(selector){
  let previous = null;
  let subject = selector;
  while (subject !== previous){
    previous = subject;
    subject = subject.replace(/\([^()]*\)/g, '');
  }
  return POINTER_STATE.test(subject) && !FOCUS_STATE.test(selector);
}

/* The gate the production build has to clear, asserted in
   briefing-contract.test.jsx against the built entry stylesheet. */
const RENDER_BLOCKING_CEILING = 80_000;
/* What the consumer's own critical CSS costs inside that entry stylesheet:
   boot.css, gate.css and the rest of the statically imported host sheets, after
   minification. Measured as the built entry stylesheet minus this split's
   critical half, and re-checked below whenever a build is present. The width
   law that moved into boot.css in round 6 took the measured figure from 7,359
   to 7,645, read against the 2.0.9 sheet. */
const HOST_CRITICAL_BYTES = 7_700;

function builtEntryStylesheet(){
  const output = join(frontendRoot, '..', 'web', 'dist');
  try {
    const index = readFileSync(join(output, 'index.html'), 'utf8');
    const href = index.match(/href="([^"]+\.css)"/)?.[1];
    return href ? readFileSync(join(output, href.replace(/^\//, '')), 'utf8') : null;
  } catch {
    return null;
  }
}

test('split conserves every byte apart from a re-opened wrapper or a shared rule', () => {
  const {critical, deferred, duplicatedBytes} = splitInstrumentStylesheet(css);

  expect(critical.length + deferred.length).toBe(css.length + duplicatedBytes);
  expect(deferred.length).toBeGreaterThan(0);
  /* Only at-rules holding rules for both halves and rules naming families from
     both halves pay anything; a large number would mean the partition is
     shredding blocks rather than separating families. */
  expect(duplicatedBytes).toBeLessThan(1_200);
});

test('no selector is declared in both halves', () => {
  const {critical, deferred} = splitInstrumentStylesheet(css);
  const criticalSelectors = new Set(declaredSelectors(critical));
  const shared = [...new Set(declaredSelectors(deferred))].filter((selector) => criticalSelectors.has(selector));

  expect(shared).toEqual([]);
});

test('a deferred family carries its at-rule overrides into the deferred half', () => {
  const {critical, deferred} = splitInstrumentStylesheet(css);

  /* The case that made this repartition necessary: the base rule is deferred,
     so its 390 override cannot stay in the sheet that loads first. */
  expect(deferred).toContain('.comparison-instrument__field{grid-template-columns:minmax(0,1fr)}');
  expect(deferred).toContain('@media(max-width:390px)');
  expect(critical).not.toContain('.comparison-instrument__field');
  for (const root of DEFERRED_SELECTOR_ROOTS){
    expect(critical, root).not.toContain(`.${root}`);
    expect(deferred, root).toContain(`.${root}`);
  }
});

test('the ribbon data table waits behind its closed disclosure', () => {
  const {critical, deferred} = splitInstrumentStylesheet(css);

  /* The table and its scroll wrapper are children of a details the ribbon
     renders closed, so nothing paints them until a reader opens it. The
     stacking block travels with them, and the disclosure and the summary a
     closed details does paint stay in the critical half.

     From 2.0.13 that stacking block is a container query rather than a media
     query, because the box that overflows is the wrap and not the window: at a
     768 window the Briefing gave the wrap 601px and Compare gave it 261px
     twice, so a viewport arm could not fire for either. The wrap is a named
     container now and the arm reads it at 899px, the last width at which the
     nine columns cannot fit. This assertion follows the ruling rather than
     recording a regression. */
  expect(deferred).toContain('.proof-ribbon__table{');
  expect(deferred).toContain('.proof-ribbon__table-wrap{');
  expect(deferred).toContain('@container proof-ribbon-table (max-width: 899px)');
  expect(deferred).toContain('container-name:proof-ribbon-table');
  expect(deferred).toContain('.proof-ribbon__table td:before');
  expect(critical).not.toContain('.proof-ribbon__table');
  expect(critical).toContain('.proof-ribbon__data-disclosure{');
  expect(critical).toContain('.proof-ribbon__data-summary{');
  /* Quiet register, 23 Sept 2026: package 2.0.20 gives the summary a hover
     underline and a press answer. Those two rules need a pointer, so the
     split defers them by the 2.0.15 axis below. Every other summary rule,
     including the one that paints the closed summary, stays critical. */
  const deferredSummaryRules = [...deferred.matchAll(/[^{}]*\.proof-ribbon__data-summary[^{}]*\{/g)]
    .map((match) => match[0].trim());
  expect(deferredSummaryRules).toEqual(['.proof-ribbon__data-summary:hover{', '.proof-ribbon__data-summary:active{']);
  /* Quiet register, 23 Sept 2026: package 2.0.21 opens the values as a table
     of its own, proof-ribbon__values in proof-ribbon__values-wrap, inside the
     same closed disclosure. Nothing paints them until a reader opens it, so
     they travel late the way the data table does. */
  expect(deferred).toContain('.proof-ribbon__values{');
  expect(deferred).toContain('.proof-ribbon__values-wrap{');
  expect(critical).not.toMatch(/\.proof-ribbon__values(?![a-zA-Z0-9-])/);
  expect(critical).not.toContain('.proof-ribbon__values-wrap');
});

/* The axis 2.0.15 added, asserted on its own rather than through the byte
   budgets it was added to close, so a change that quietly stops moving these
   rules fails here and not only in a size comparison. */
test('a rule only a pointer can satisfy is never render-blocking', () => {
  const {critical, deferred} = splitInstrumentStylesheet(css);

  /* A hover needs a pointer resting on a painted element and a press needs a
     pointer down on one, so the answer can always land after the paint. The
     one exception is the rule the focus carve-out holds, named and explained
     in the test below this one, and the axis moves everything else. */
  expect(pointerStateRules(critical)).toEqual(['.state-view__summary:hover']);
  expect(pointerStateRules(deferred).length).toBeGreaterThan(0);
  for (const rule of [
    '.instrument-masthead:hover{',
    '.instrument-market-option.instrument-rule-boundary:hover:not(:disabled){',
    '.state-view__action:active:not(:disabled){',
    '.digest-chip:active{',
    /* 2.0.15 wrote this hover as .dossier-action:hover,.evidence-drawer__close:hover,
       one rule naming a family from each half, and the split had to copy its
       declarations into both. 2.0.16 gives each family its own rule, so the
       hover travels late on the pointer axis alone and pays nothing. No
       deferred pointer rule names a family from each half now. */
    '.dossier-action:hover{',
  ]){
    expect(deferred, rule).toContain(rule);
    expect(critical, rule).not.toContain(rule);
  }

  /* Focus is not a pointer state. Autofocus or a restored deep link can put it
     on an element the first paint draws, so these stay in the critical half
     even where the same rule also answers a hover. */
  for (const rule of [
    '.metric-tile-link:hover,.metric-tile-link:focus-visible{',
    '.digest-chip:hover,.digest-chip:focus-visible{',
    '[data-dir=midnight] .evidence-column__row:hover,[data-dir=midnight] .evidence-column__row:focus-within{',
  ]){
    expect(critical, rule).toContain(rule);
    expect(deferred, rule).not.toContain(rule);
  }
});

/* The cascade the pointer axis reversed, and the reason the carve-out reads a
   declared property rather than a subject. */
test('a pointer rule that suppresses a focus ring keeps its place in source order', () => {
  const {critical, deferred} = splitInstrumentStylesheet(css);

  /* The pair as the sealed sheet writes it. Both are a class and a
     pseudo-class, so they tie on specificity and source order alone decides,
     and outline is the property they both declare: the hover rule removes the
     ring the focus rule draws. */
  expect(css).toContain('.state-view__summary:hover{border-color:var(--color-daylight-red-surface);outline:none}');
  expect(css).toContain('.state-view__summary:focus-visible{outline:var(--focus-width) solid var(--accent);outline-offset:var(--focus-offset)}');

  /* So the hover rule cannot travel into a sheet that loads later, where it
     would win the tie and leave a keyboard reader with no ring for as long as
     the pointer rests on the summary. */
  expect(critical).toContain('.state-view__summary:hover{');
  expect(deferred).not.toContain('.state-view__summary:hover{');
  expect(critical).toContain('.state-view__summary:focus-visible{');
  expect(critical.indexOf('.state-view__summary:hover{'))
    .toBeLessThan(critical.indexOf('.state-view__summary:focus-visible{'));

  /* The carve-out is narrow, and the same element proves it: the press rule
     sets a background colour, which no focus rule on this subject declares, so
     sharing the subject buys it nothing and it travels late. */
  expect(deferred).toContain('.state-view__summary:active{');
  expect(critical).not.toContain('.state-view__summary:active{');
});

test('a pointer rule that shares only a subject with a focus rule still travels late', () => {
  /* The hover sets a border colour and the focus sets a ring. Two rules on one
     element that cannot reach the same property cannot decide each other, so
     the order they load in does not matter and the hover answer waits. */
  const source = '.oi-briefing__mark:hover{border-color:red}'
    + '.oi-briefing__mark:focus-visible{outline:2px solid blue}';
  const {critical, deferred, duplicatedBytes} = splitInstrumentStylesheet(source, ['evidence-room']);

  expect(deferred).toBe('.oi-briefing__mark:hover{border-color:red}');
  expect(critical).toBe('.oi-briefing__mark:focus-visible{outline:2px solid blue}');
  expect(critical.length + deferred.length).toBe(source.length + duplicatedBytes);
});

test('a shorthand and its longhand are one property to the carve-out', () => {
  /* outline-color is one of the properties the outline shorthand sets, so the
     first hover rule reaches the ring under another name and stays. The border
     pair is the same case, and the third hover rule is the control: a
     background colour reaches nothing the focus rule sets, so it travels. */
  const source = '.oi-briefing__mark:hover{outline-color:transparent}'
    + '.oi-briefing__mark:focus-visible{outline:2px solid blue}'
    + '.oi-briefing__tag:hover{border-color:red}'
    + '.oi-briefing__tag:focus-visible{border:1px solid blue}'
    + '.oi-briefing__cue:hover{background-color:red}'
    + '.oi-briefing__cue:focus-visible{outline:2px solid blue}';
  const {critical, deferred, duplicatedBytes} = splitInstrumentStylesheet(source, ['evidence-room']);

  expect(critical).toBe('.oi-briefing__mark:hover{outline-color:transparent}'
    + '.oi-briefing__mark:focus-visible{outline:2px solid blue}'
    + '.oi-briefing__tag:hover{border-color:red}'
    + '.oi-briefing__tag:focus-visible{border:1px solid blue}'
    + '.oi-briefing__cue:focus-visible{outline:2px solid blue}');
  expect(deferred).toBe('.oi-briefing__cue:hover{background-color:red}');
  expect(critical.length + deferred.length).toBe(source.length + duplicatedBytes);
});

test('the pointer axis defers whole rules and splits none', () => {
  /* The focus rules here set an outline and the pointer rules set a colour, so
     nothing in this fixture reaches the focus carve-out and it measures the
     axis on its own. The carve-out has its own tests above. */
  const source = '.oi-briefing{color:red}'
    + '.oi-briefing:hover{color:blue}'
    + '.oi-briefing:active{color:green}'
    + '.oi-briefing:focus-visible{outline:2px solid teal}'
    + '.oi-briefing:hover,.oi-briefing:focus-visible{outline:2px solid olive}'
    + '.oi-briefing:hover,.oi-briefing__mark{color:navy}'
    + '.oi-briefing:not(:hover){color:grey}';
  const {critical, deferred, duplicatedBytes} = splitInstrumentStylesheet(source, ['evidence-room']);

  expect(deferred).toBe('.oi-briefing:hover{color:blue}.oi-briefing:active{color:green}');
  /* The focus rule and the rule pairing a hover with a focus both stay. So
     does the list mixing a hover with a plain selector, because splitting it
     would copy its declarations into both halves and a gate counts those. And
     :not(:hover) is true before any pointer arrives, so it stays too. */
  expect(critical).toBe('.oi-briefing{color:red}'
    + '.oi-briefing:focus-visible{outline:2px solid teal}'
    + '.oi-briefing:hover,.oi-briefing:focus-visible{outline:2px solid olive}'
    + '.oi-briefing:hover,.oi-briefing__mark{color:navy}'
    + '.oi-briefing:not(:hover){color:grey}');
  expect(duplicatedBytes).toBe(0);
  expect(critical.length + deferred.length).toBe(source.length + duplicatedBytes);
});

test('a pointer state inside a media query travels with its block', () => {
  const source = '@media(max-width:390px){.oi-briefing{gap:4px}.oi-briefing:hover{gap:8px}}';
  const {critical, deferred, duplicatedBytes} = splitInstrumentStylesheet(source, ['evidence-room']);

  expect(critical).toBe('@media(max-width:390px){.oi-briefing{gap:4px}}');
  expect(deferred).toBe('@media(max-width:390px){.oi-briefing:hover{gap:8px}}');
  expect(critical.length + deferred.length).toBe(source.length + duplicatedBytes);
});

test('every first-paint surface stays in the critical half', () => {
  const {critical, deferred} = splitInstrumentStylesheet(css);
  const deferredSelectors = [...new Set(declaredSelectors(deferred))];

  for (const root of CRITICAL_SURFACE_ROOTS){
    /* Every named surface has to exist in the sheet, so a renamed family fails
       here rather than passing as an absent one. */
    expect(css, root).toContain(`.${root}`);
    expect(critical, root).toContain(`.${root}`);
    expect(DEFERRED_SELECTOR_ROOTS, root).not.toContain(root);
    /* A first-paint family may hand a named part to the deferred half, the
       ribbon data table being the one that does, so what the late sheet may
       not hold is a selector naming the family without naming one of those
       parts. Reading it off the deferred half's own selectors rather than off
       the text keeps a part nobody listed from passing as an allowed one.
       From 2.0.15 it may also hand over what only a pointer can reach: the
       family still paints first, and the hover or press answer lands after,
       because neither can be true before the paint it answers. */
    const parts = DEFERRED_SELECTOR_ROOTS.filter((deferredRoot) => deferredRoot.startsWith(`${root}__`));
    /* Quiet register, 23 Sept 2026: package 2.0.21 aligns the Compare columns
       by writing the ribbon's parts under .comparison-instrument__signal, so
       those rules match a ribbon only inside a Compare column. The Compare
       instrument is a deferred family, so a rule whose selector names it
       ahead of the ribbon cannot reach a ribbon that paints first, and it
       travels with that family. The allowance names that one column class
       and nothing else: no other deferred ancestor, and no part written under
       the column, so any other selector naming the family is still held to
       the parts list and the pointer axis. */
    expect(DEFERRED_SELECTOR_ROOTS).toContain('comparison-instrument');
    const insideCompareColumn = (selector) => (
      /\.comparison-instrument__signal(?![\w-])/.test(selector.slice(0, selector.indexOf(`.${root}`)))
    );
    const stray = deferredSelectors.filter((selector) => (
      selector.includes(`.${root}`)
      && !parts.some((part) => selector.includes(`.${part}`))
      && !needsPointer(selector)
      && !insideCompareColumn(selector)
    ));
    expect(stray, root).toEqual([]);
  }
  for (const token of ['--font-serif:', '@font-face', 'prefers-reduced-motion']){
    expect(critical, token).toContain(token);
  }
});

test('the critical half leaves room for the host inside the render-blocking ceiling', () => {
  const {critical} = splitInstrumentStylesheet(css);

  expect(critical.length).toBeLessThan(RENDER_BLOCKING_CEILING - HOST_CRITICAL_BYTES);

  const entry = builtEntryStylesheet();
  if (entry){
    expect(entry.length).toBeLessThan(RENDER_BLOCKING_CEILING);
    expect(entry.length - critical.length).toBeLessThanOrEqual(HOST_CRITICAL_BYTES);
  }
});

test('a shared rule keeps its order inside each half', () => {
  const source = '.evidence-room{color:red}.evidence-room,.oi-briefing{color:blue}.oi-briefing{color:green}';
  const {critical, deferred} = splitInstrumentStylesheet(source, ['evidence-room']);

  expect(deferred).toBe('.evidence-room{color:red}.evidence-room{color:blue}');
  expect(critical).toBe('.oi-briefing{color:blue}.oi-briefing{color:green}');
});

test('an at-rule is emitted into each half that has rules for it', () => {
  const source = '@media(max-width:390px){.oi-briefing{gap:4px}.evidence-room{gap:8px}}';
  const {critical, deferred, duplicatedBytes} = splitInstrumentStylesheet(source, ['evidence-room']);

  expect(critical).toBe('@media(max-width:390px){.oi-briefing{gap:4px}}');
  expect(deferred).toBe('@media(max-width:390px){.evidence-room{gap:8px}}');
  expect(critical.length + deferred.length).toBe(source.length + duplicatedBytes);
});

test('an at-rule with nothing deferred is reproduced byte for byte', () => {
  const source = '@font-face{font-family:X;src:url("a.woff2") format("woff2")}@keyframes fade{0%{opacity:0}to{opacity:1}}';
  const {critical, deferred, duplicatedBytes} = splitInstrumentStylesheet(source, ['evidence-room']);

  expect(critical).toBe(source);
  expect(deferred).toBe('');
  expect(duplicatedBytes).toBe(0);
});

test('a rule naming families from both halves is written into each', () => {
  const source = '.evidence-room,.oi-briefing{font-weight:700}';
  const {critical, deferred, duplicatedBytes} = splitInstrumentStylesheet(source, ['evidence-room']);

  expect(critical).toBe('.oi-briefing{font-weight:700}');
  expect(deferred).toBe('.evidence-room{font-weight:700}');
  expect(critical.length + deferred.length).toBe(source.length + duplicatedBytes);
});

test('comments and quoted braces never break the partition', () => {
  const source = '/* .evidence-room { not a rule } */.oi-briefing{content:"}"}'
    + '.evidence-room{content:"{"}/* trailing */';
  const {critical, deferred, duplicatedBytes} = splitInstrumentStylesheet(source, ['evidence-room']);

  expect(deferred).toBe('.evidence-room{content:"{"}');
  expect(critical).toBe('/* .evidence-room { not a rule } */.oi-briefing{content:"}"}/* trailing */');
  expect(critical.length + deferred.length).toBe(source.length + duplicatedBytes);
});

test('selector lists split on top-level commas only', () => {
  const source = '.a:is(.evidence-room,.x){color:red}.evidence-room:not(.y){color:blue}';
  const {critical, deferred} = splitInstrumentStylesheet(source, ['evidence-room']);

  expect(critical).toBe('.a:is(.evidence-room,.x){color:red}');
  expect(deferred).toBe('.evidence-room:not(.y){color:blue}');
});
