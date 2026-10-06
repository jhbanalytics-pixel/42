/* Round 11 of the 42 in Black programme measured the consumer production
   build at design system 2.0.12. This file holds the consumer half of that
   run, one test per class of defect, each written so it fails when its fix is
   taken away.

   Two of the four classes come with a ruling the round made, and the rulings
   are what these tests encode rather than the fixes themselves.

   A token fix is verified at the deepest element that declares it, not at the
   highest. Round 10 moved the dim text tokens onto the muted ink on
   :root[data-dir], measured green there, and shipped. The package bridge is
   written as a bare [data-dir] attribute selector and the package also renders
   a nested product root carrying data-dir, so the bridge re-declared both
   tokens one level below the fix and no consumer route ever saw it. The first
   test therefore resolves the tokens at that nested root, with the same
   specificity and source order the browser uses, and it also holds the
   winning rule to a specificity above the bridge's so a change of sheet order
   cannot undo the fix a second time.

   A width fix is not done until the same cell is re-measured for overflow in
   both directions. Round 10 stopped the browse Search control shrinking, which
   cured a word broken across five lines and left the row overflowing the
   document by 25 pixels at 390. The second test holds both directions at once:
   the field yields and the control keeps its word.

   The other two classes are plain defects with a measured mechanism. A font
   shorthand later in app.css resets font-variation-settings, which undid the
   one shared mono axis rule on two chip classes, and eight uppercase mono
   labels sit on neither of the two tracking roles section 5 defines. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');
const packageCss = readFileSync(
  fileURLToPath(new URL('../../../node_modules/ogilvy-intelligence-design-system/dist/style.css', import.meta.url)),
  'utf8',
);

/* The package sheet first and every consumer sheet a route can load after it,
   which is the order the built pages carry. Order is only a tiebreak here: the
   first test asserts the winner is decided on specificity. */
const CONSUMER_SHEETS = [
  'ui/ui.css',
  'styles/boardviews.css',
  'styles/instrument-route-surfaces.css',
  'app.css',
  'styles/console.css',
  'styles/dossier.css',
  'styles/empty-states.css',
  'styles/fieldwork.css',
  'styles/graphs.css',
  'styles/lexlisten.css',
  'styles/loop.css',
  'styles/topic.css',
  'styles/workspaces.css',
  'ogilvy-intelligence.css',
];
const SHEETS = [['package', packageCss], ...CONSUMER_SHEETS.map((name) => [name, read(name)])];

/* ===== a cascade small enough to read and true enough to trust ===== */

/* Only compound selectors appear on the rules that declare these tokens, so
   the matcher handles exactly that: a tag, a class, an attribute and :root,
   in any order, with no combinator. Anything else returns null and the test
   that needs it fails rather than skipping the rule silently. */
function compound(selector){
  const trimmed = selector.trim();
  if (!trimmed || /[\s>+~,]/.test(trimmed)) return null;
  const pattern = /^(?:([a-z][a-z0-9-]*)|\.([A-Za-z_][\w-]*)|\[([^\]]+)\]|:(root))/;
  const parts = [];
  let rest = trimmed;
  while (rest.length){
    const found = pattern.exec(rest);
    if (!found) return null;
    if (found[1]) parts.push({kind: 'tag', value: found[1]});
    else if (found[2]) parts.push({kind: 'class', value: found[2]});
    else if (found[3]) parts.push({kind: 'attr', value: found[3]});
    else parts.push({kind: 'root'});
    rest = rest.slice(found[0].length);
  }
  return parts;
}

function specificity(parts){
  let classes = 0;
  let types = 0;
  for (const part of parts){
    if (part.kind === 'tag') types += 1;
    else classes += 1;
  }
  return classes * 100 + types;
}

function attributeTest(raw){
  const found = raw.match(/^([A-Za-z_:][-\w:.]*)(?:\s*=\s*"?([^"\]]*)"?)?$/);
  if (!found) return null;
  return {name: found[1], value: found[2] === undefined ? null : found[2]};
}

function matches(parts, element){
  for (const part of parts){
    if (part.kind === 'root' && !element.isRoot) return false;
    if (part.kind === 'tag' && part.value !== element.tag) return false;
    if (part.kind === 'class' && !element.classes.includes(part.value)) return false;
    if (part.kind === 'attr'){
      const test = attributeTest(part.value);
      if (!test) return false;
      const carried = element.attributes[test.name];
      if (carried === undefined) return false;
      if (test.value !== null && test.value !== carried) return false;
    }
  }
  return true;
}

/* Every top level rule in the cascade, with the position the browser gives it.
   Rules inside an at-rule are collected separately so a test can assert what
   is in there rather than quietly dropping it. */
function cascade(){
  const top = [];
  const inAtRule = [];
  SHEETS.forEach(([sheet, css], sheetIndex) => {
    let order = 0;
    postcss.parse(css).walkRules((rule) => {
      order += 1;
      const declarations = [];
      rule.walkDecls((declaration) => declarations.push({prop: declaration.prop, value: declaration.value.trim()}));
      const entry = {
        sheet,
        line: rule.source.start.line,
        selector: rule.selector.replace(/\s+/g, ' '),
        selectors: rule.selectors,
        declarations,
        position: sheetIndex * 1e6 + order,
        atRule: rule.parent.type === 'root' ? null : `@${rule.parent.name} ${rule.parent.params}`.trim(),
      };
      (entry.atRule ? inAtRule : top).push(entry);
    });
  });
  return {top, inAtRule};
}

const CASCADE = cascade();
const DIM_TOKENS = ['--muted', '--faint'];
const declaresDim = (entry) => entry.declarations.some((declaration) => DIM_TOKENS.includes(declaration.prop));

const documentElement = (theme) => ({tag: 'html', classes: [], attributes: {'data-dir': theme}, isRoot: true});
const productRoot = (theme) => ({tag: 'div', classes: ['oi-product'], attributes: {'data-dir': theme}, isRoot: false});

/* The declaration that wins on one element: highest specificity, latest
   position among equals, exactly as the cascade resolves a custom property. */
function winnerAt(element, property){
  let winner = null;
  for (const entry of CASCADE.top){
    const declaration = [...entry.declarations].reverse().find((one) => one.prop === property);
    if (!declaration) continue;
    for (const selector of entry.selectors){
      const parts = compound(selector);
      if (!parts || !matches(parts, element)) continue;
      const rank = specificity(parts);
      if (!winner || rank > winner.rank || (rank === winner.rank && entry.position > winner.position)){
        winner = {rank, position: entry.position, sheet: entry.sheet, line: entry.line, selector, value: declaration.value};
      }
    }
  }
  return winner;
}

/* A custom property resolves from the element's own winning declaration, and
   from the document element by inheritance where the element declares none.
   Nothing between the product root and html declares any of these. */
function resolverFor(theme){
  const root = documentElement(theme);
  const product = productRoot(theme);
  const lookup = (name, element) => {
    const own = winnerAt(element, name);
    if (own) return own.value;
    const inherited = element === product ? winnerAt(root, name) : null;
    return inherited ? inherited.value : undefined;
  };
  const evaluate = (raw, element, depth = 0) => {
    if (depth > 12 || !raw) return null;
    const value = raw.trim();
    if (/^#[0-9a-f]{3,8}$/i.test(value)) return hexColour(value);
    if (value === 'transparent') return {r: 0, g: 0, b: 0, alpha: 0};
    const reference = value.match(/^var\(\s*(--[a-z0-9-]+)\s*(?:,\s*([\s\S]*))?\)$/);
    if (reference){
      const declared = lookup(reference[1], element);
      if (declared !== undefined) return evaluate(declared, element, depth + 1);
      return reference[2] ? evaluate(reference[2], element, depth + 1) : null;
    }
    return null;
  };
  return {evaluate, lookup, root, product};
}

function hexColour(value){
  let hex = value.slice(1);
  if (hex.length === 3 || hex.length === 4) hex = [...hex].map((character) => character + character).join('');
  const channel = (at) => parseInt(hex.slice(at, at + 2), 16) / 255;
  return {r: channel(0), g: channel(2), b: channel(4), alpha: hex.length === 8 ? channel(6) : 1};
}

const toHex = (colour) => '#' + ['r', 'g', 'b'].map((key) => Math.round(colour[key] * 255).toString(16).padStart(2, '0')).join('');

function relativeLuminance(colour){
  const channel = (value) => (value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return 0.2126 * channel(colour.r) + 0.7152 * channel(colour.g) + 0.0722 * channel(colour.b);
}

function contrast(a, b){
  const first = relativeLuminance(a);
  const second = relativeLuminance(b);
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
}

const GROUNDS = {
  midnight: ['#070606', '#0B0A09', '#15120F', '#1C1815'],
  daylight: ['#F5EEE4', '#E8DED2', '#FFFAF2'],
};

/* ===== ruling one, the token bridge ===== */

test('every rule that declares a dim text token is one the cascade model can read', () => {
  const unreadable = [];
  for (const entry of CASCADE.top){
    if (!declaresDim(entry)) continue;
    for (const selector of entry.selectors){
      if (!compound(selector)) unreadable.push(`${entry.sheet}:${entry.line} ${selector}`);
    }
  }
  expect(unreadable, 'a selector this test cannot match would hide a declaration from it').toEqual([]);
  const screened = CASCADE.inAtRule.filter((entry) => declaresDim(entry) && !/print/.test(entry.atRule));
  expect(
    screened.map((entry) => `${entry.sheet}:${entry.line} ${entry.atRule} ${entry.selector}`),
    'only the print sheet may redeclare a dim token inside an at-rule',
  ).toEqual([]);
});

test('the dim text tokens clear AA at the nested product root, not only at the document element', () => {
  const thin = [];
  for (const theme of ['midnight', 'daylight']){
    const resolver = resolverFor(theme);
    for (const token of DIM_TOKENS){
      for (const [where, element] of [['document element', resolver.root], ['product root', resolver.product]]){
        const colour = resolver.evaluate(`var(${token})`, element);
        expect(colour, `${theme} resolves ${token} at the ${where}`).toBeTruthy();
        for (const ground of GROUNDS[theme]){
          const ratio = contrast(colour, hexColour(ground));
          if (ratio < 4.5) thin.push(`${theme} ${token} at the ${where} is ${toHex(colour)} on ${ground} = ${ratio.toFixed(2)}`);
        }
      }
    }
  }
  expect(thin).toEqual([]);
});

test('the consumer lane outranks the package bridge on specificity at the product root, so sheet order cannot undo it', () => {
  const bridge = CASCADE.top.find((entry) => entry.sheet === 'package' && entry.selector === '[data-dir]' && declaresDim(entry));
  expect(bridge, 'the package bridge rule is in the cascade').toBeTruthy();
  const bridgeRank = specificity(compound('[data-dir]'));
  for (const theme of ['midnight', 'daylight']){
    for (const token of DIM_TOKENS){
      const winner = winnerAt(productRoot(theme), token);
      expect(winner, `${theme} declares ${token} at the product root`).toBeTruthy();
      expect(winner.sheet, `${theme} ${token} at the product root comes from a consumer sheet`).not.toBe('package');
      expect(
        winner.rank > bridgeRank,
        `${theme} ${token} wins at ${winner.sheet}:${winner.line} on specificity ${winner.rank} against the bridge at ${bridgeRank}`,
      ).toBe(true);
    }
  }
});

/* ===== ruling two, the browse row in both directions ===== */

/* The row is a flex line inside a rule box: an icon, the field, and the Search
   control. Round 10 gave the control flex: 0 0 auto and white-space: nowrap so
   its one word stops breaking, and the field then had nothing to give, because
   an input's automatic minimum size is its own intrinsic width. Both halves are
   held here, because either one alone is a defect the other round measured. */
function declarationsOf(sheet, selector){
  const found = {};
  postcss.parse(read(sheet)).walkRules((rule) => {
    if (rule.parent.type !== 'root') return;
    if (!rule.selectors.some((one) => one.trim() === selector)) return;
    rule.walkDecls((declaration) => { found[declaration.prop] = declaration.value.trim(); });
  });
  return found;
}

test('the browse search row yields in one direction and holds its word in the other', () => {
  const field = declarationsOf('styles/boardviews.css', '.brw-search-input');
  expect(field['min-width'], 'the field may shrink below its intrinsic width').toBe('0');
  const inline = read('views.jsx').match(/className="brw-search-input"[\s\S]{0,320}?style=\{\{([^}]*)\}\}/);
  expect(inline, 'the field carries its flex inline').toBeTruthy();
  expect(inline[1], 'the field takes the free space from a zero basis').toMatch(/flex:\s*1\b/);

  const control = declarationsOf('styles/boardviews.css', '.brw-search-btn');
  expect(control['white-space'], 'the control keeps its one word on one line').toBe('nowrap');
  expect(control.flex, 'the control neither grows nor shrinks').toBe('0 0 auto');
});

/* ===== the mono axis and the font shorthand ===== */

/* The font shorthand resets font-variation-settings to normal, so a rule that
   sets it after the one shared axis rule takes the axis away from every class
   it names. The proof that this is the mechanism is in the run: the classes in
   the same shared rule that carry no later shorthand all resolve the mono cut. */
test('no consumer rule takes the mono axis away from a class the shared rule names', () => {
  const sheets = ['app.css', 'ui/ui.css', 'styles/console.css', 'styles/dossier.css', 'styles/empty-states.css',
    'styles/fieldwork.css', 'styles/graphs.css', 'styles/lexlisten.css', 'styles/loop.css', 'styles/topic.css',
    'styles/workspaces.css', 'styles/boardviews.css', 'ogilvy-intelligence.css'];
  let named = null;
  postcss.parse(read('app.css')).walkRules((rule) => {
    if (rule.parent.type !== 'root') return;
    const carries = rule.nodes.some((node) => node.type === 'decl'
      && node.prop === 'font-variation-settings' && /"MONO"\s+1/.test(node.value));
    if (!carries || !rule.selector.includes('[style*="var(--mono)"]')) return;
    named = rule.selectors.map((selector) => selector.trim()).filter((selector) => selector.startsWith('.'));
  });
  expect(named, 'the shared mono axis rule names its classes').toBeTruthy();
  /* Quiet register, 23 Sept 2026: the legacy next label, the loop stage
     eyebrow, the map node action and the empty state status line now read in
     the sans, so they left the shared rule, and so did the legacy chip and the
     Listen filter chip, which are words a reader reads. The ones that still
     set mono are named here exactly, so no class leaves or joins without this
     test. Quiet register, 23 Sept 2026: the eyebrow and the page hero eyebrow
     are labels a reader reads, and the package sets .eyebrow in the sans, so
     they left the shared rule as well and are held absent from it. */
  expect(named, 'and it names exactly the classes that still set mono').toEqual([
    '.es-sep', '.loop-rail-arrow', '.map-node-route',
  ]);
  expect(named).not.toContain('.eyebrow');
  expect(named).not.toContain('.ui-page-hero-eyebrow');

  const offenders = [];
  for (const sheet of sheets){
    postcss.parse(read(sheet)).walkRules((rule) => {
      const hits = rule.selectors.filter((selector) => named.some((name) => selector.trim().includes(name)));
      if (!hits.length) return;
      rule.walkDecls((declaration) => {
        if (declaration.prop !== 'font') return;
        offenders.push(`${sheet}:${declaration.source.start.line} ${rule.selector.replace(/\s+/g, ' ')} -> font: ${declaration.value.trim()}`);
      });
    });
  }
  expect(offenders).toEqual([]);
});

/* ===== the provenance roles ===== */

/* Section 5 gives Recursive Mono two roles and no third. A provenance label is
   11px at 500 with 0.18em where uppercase; a provenance value is 11px at 400
   with 0.12em where uppercase. The eight runs the sweep measured were on
   neither, at 0.06em, 0.08em, 0.10em and 0.14em, and none of them is new type:
   the round 10 mono axis fix moved them out of a rule that could not see them
   and into one that reads them.

   Each site is named rather than swept, because these are the eight the run
   measured and the routes carry many more mono labels the matrix never
   reached. */
const LABEL = {weight: '500', tracking: '0.18em'};
const VALUE = {weight: '400', tracking: '0.12em'};

function braceBlock(source, open){
  if (open < 0) return null;
  let depth = 0;
  for (let index = open; index < source.length; index += 1){
    if (source[index] === '{') depth += 1;
    if (source[index] === '}'){
      depth -= 1;
      if (depth === 0) return source.slice(open, index + 1);
    }
  }
  return null;
}

/* The block that opens at the anchor, for a style handed to a prop. */
function styleAfter(source, anchor){
  const at = source.indexOf(anchor);
  return at < 0 ? null : braceBlock(source, source.indexOf('{', at));
}

/* The style of the element whose text the marker names, which is the nearest
   style={{ before it. */
function styleBefore(source, marker){
  const at = source.indexOf(marker);
  if (at < 0) return null;
  const open = source.lastIndexOf('style={{', at);
  return open < 0 ? null : braceBlock(source, open + 'style='.length);
}

function roleOfInline(text, where){
  expect(text, `${where} carries an inline style this test can read`).toBeTruthy();
  expect(text, `${where} names the mono family at the provenance size`).toMatch(/var\(--mono\)[\s\S]*var\(--type-1\)/);
  const tracking = text.match(/letterSpacing:\s*'([^']+)'/);
  const weight = text.match(/fontWeight:\s*'?(\d+)'?/);
  return {tracking: tracking ? tracking[1] : null, weight: weight ? weight[1] : '400'};
}

test('the eight mono runs the sweep measured take a section 5 provenance role or the quiet label', () => {
  const off = [];
  const hold = (where, measured, role) => {
    if (measured.tracking !== role.tracking || measured.weight !== role.weight){
      off.push(`${where} is ${measured.weight}/${measured.tracking} against ${role.weight}/${role.tracking}`);
    }
  };

  /* Quiet register, 23 Sept 2026: the map key, the map evidence label and the
     Listen sentiment label are labels a reader reads, so they are no longer
     mono capitals on the provenance role. Each is held to the quiet label
     instead: the sans at 14px and weight 400, with no tracking and no case set
     in style, the words written in sentence case at the source. */
  const quietInline = (found, where, source = '') => {
    /* A style that spreads the file's LABEL constant is read with it. */
    const shared = source.match(/const LABEL = \{[\s\S]*?\};/);
    const text = found && /\.\.\.LABEL\b/.test(found) && shared ? found + shared[0] : found;
    if (!text){ off.push(`${where} carries no inline style this test can read`); return; }
    if (!/fontFamily:\s*'var\(--sans\)'/.test(text)) off.push(`${where} is not set in the sans`);
    if (!/fontSize:\s*'var\(--type-3\)'/.test(text)) off.push(`${where} is not set at 14px`);
    const weight = text.match(/fontWeight:\s*'?(\d+)'?/);
    if (weight && weight[1] !== '400') off.push(`${where} is set at weight ${weight[1]}`);
    if (/letterSpacing/.test(text)) off.push(`${where} is tracked`);
    if (/textTransform/.test(text)) off.push(`${where} sets its case in style`);
  };

  /* Every page on the Map, 2 October 2026: the entry desks key did not match
     its dots and the evidence box said nothing about the pages, so both are
     gone with every inline style on the Map. Held instead: neither remains. */
  const map = read('map.jsx');
  if (/style=\{\{/.test(map)) off.push('map.jsx still sets an inline style');
  if (/Entry desks|Reading the evidence/.test(map)) off.push('map.jsx still draws the key or the evidence label');

  const views = read('views.jsx');
  /* Demo polish, 2 October 2026: the method divider meta ("the point of the
     product") was a tracked capital label with nothing to say, so it is gone
     with the one style that drew it. Held instead: no such style and no such
     words remain, and no style in the file sets its case to capitals. */
  if (/methodDividerMeta/.test(views)) off.push('views.jsx still declares the method divider meta');
  if (/point of the product/i.test(views)) off.push('views.jsx still writes the point of the product');
  if (/const methodDivider\w* = \{[^}]*textTransform/.test(views)) off.push('views.jsx a method divider sets its case in style');

  /* Shell consistency, 2 October 2026: the filter labels on Listen are one
     class, .listen-filter-label, rather than an inline style per row, so the
     quiet label is held on that rule below. */
  const listen = read('listen.jsx');
  expect(listen).toContain('<span className="listen-filter-label" id={sentimentLabelId}>Sentiment</span>');

  const seedpath = read('seedpath.jsx');
  /* Seed path rebuild, 2 October 2026: the page moved its styles out of
     inline constants into styles/seedpath.css (no inline style soup), so the
     LABEL and MONO_VALUE constants are gone and the quiet label is held in
     the sheet instead, below. Nothing reaches for a mono label either. */
  expect(seedpath, 'the seedpath mono label is gone').not.toMatch(/MONO_LABEL/);
  expect(seedpath, 'no inline label constant remains').not.toMatch(/const (LABEL|MONO_VALUE) = \{/);
  expect(seedpath, 'nothing still reaches for the one style that served both roles').not.toMatch(/style=\{MONO\}|\.\.\.MONO[,}]/);

  /* Quiet register, 23 Sept 2026: the three stylesheet runs are no longer
     mono capitals. They are labels a reader reads, so each is the sans at 14px
     and weight 400 in sentence case with no tracking. */
  const quiet = (sheet, selector) => {
    const found = declarationsOf(sheet, selector);
    const where = `${sheet} ${selector}`;
    if (found['font-family'] !== 'var(--sans)') off.push(`${where} is set in ${found['font-family']}`);
    if (found['font-size'] !== 'var(--type-3)') off.push(`${where} is set at ${found['font-size']}`);
    if ((found['font-weight'] || '400') !== '400') off.push(`${where} is set at weight ${found['font-weight']}`);
    if (found['letter-spacing'] && found['letter-spacing'] !== '0') off.push(`${where} is tracked at ${found['letter-spacing']}`);
    if (found['text-transform'] && found['text-transform'] !== 'none') off.push(`${where} is set in ${found['text-transform']}`);
  };
  quiet('app.css', '.map-node-go');
  quiet('styles/loop.css', '.loop-stage-eyebrow');
  quiet('styles/empty-states.css', '.es-status');
  quiet('styles/seedpath.css', '.seedpath .sp-label');
  quiet('styles/lexlisten.css', '.listen-filter-label');

  expect(off).toEqual([]);
});
