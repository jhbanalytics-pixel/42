/* Round 9 of the 42 in Black programme, the consumer items of the populated
   audit (round 8, four instruments on the local production build).

   Item 6: the consumer's provenance kickers rendered in sans. The package
   resets font-variation-settings to "MONO" 0 on :root and the form controls
   and sets "MONO" 1 only on its own allowlist, so a consumer rule that names
   the mono family gets the sans cut of the same variable font unless it sets
   the axis itself. Every rule in the sheets the instrument routes load that
   names the mono family carries "MONO" 1.

   Item 12: Continue on the Console was a solid red plane at half opacity
   while disabled. A disabled Continue is the rule-bounded control, ink text
   on the ground inside a 2px rule; the red plane appears only when it can be
   pressed. The boundary is muted, not the hairline rule token: section 12
   holds a boundary that alone identifies a control to 3:1 against its plane,
   and the contrast gate measures the midnight hairline at 1.3:1.

   Item 18: New brief and the empty rail panels drew dashed rules, the one
   boundary style the instrument uses nowhere else. Both become 1px solid,
   New brief on muted for the same section 12 reason and the empty panel on
   the rule token. No consumer sheet draws a dashed border.

   23 Sept 2026 design direction: New brief became a text action whose
   label identifies it, so it no longer needs a boundary at 3:1 (WCAG 1.4.11
   asks for a boundary only where it is what identifies the control); both
   keep a transparent 1px rule so their geometry does not move. */
import {expect, test} from 'bun:test';
import {readFileSync, readdirSync, statSync} from 'node:fs';
import {join, relative} from 'node:path';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

const frontendRoot = fileURLToPath(new URL('../../../', import.meta.url));
const sourceRoot = join(frontendRoot, 'src');

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

function sheet(name){
  return postcss.parse(readFileSync(join(sourceRoot, name), 'utf8'));
}

function declarationsOf(root, selector){
  const found = {};
  root.walkRules((rule) => {
    if (!rule.selectors.map((s) => s.replace(/\s+/g, ' ')).includes(selector)) return;
    rule.walkDecls((declaration) => { found[declaration.prop] = declaration.value.trim(); });
  });
  return found;
}

/* The sheets the instrument routes and the shell load: Discover and Compare,
   the Console and Evidence Room, Fieldwork, the shared ui sheet and boot. */
const INSTRUMENT_SHEETS = [
  'ogilvy-intelligence.css',
  'styles/workspaces.css',
  'styles/console.css',
  'styles/fieldwork.css',
  'styles/boot.css',
  'ui/ui.css',
];
const MONO_FAMILY = /var\(--(?:mono|oi-mono|data-mono|font-provenance)\b|Recursive Mono/;

test('every rule on an instrument sheet that names the mono family carries the mono axis', () => {
  const misses = [];
  let named = 0;
  const walked = new Map();
  const namedSelectors = [];
  const walkedSelectors = new Set();
  for (const name of INSTRUMENT_SHEETS){
    walked.set(name, 0);
    sheet(name).walkRules((rule) => {
      walked.set(name, walked.get(name) + 1);
      for (const selector of rule.selectors) walkedSelectors.add(selector.replace(/\s+/g, ' '));
      let names = false;
      let mono = false;
      rule.walkDecls((declaration) => {
        if (/^font(?:-family)?$/.test(declaration.prop) && MONO_FAMILY.test(declaration.value)) names = true;
        if (declaration.prop === 'font-variation-settings' && /"MONO" 1\b/.test(declaration.value)) mono = true;
      });
      if (!names) return;
      /* Each selector of a grouped rule is its own entry, so a pin matches
         by name inside a list and the count is of selectors, not rules. */
      for (const selector of rule.selectors){
        named += 1;
        namedSelectors.push(selector.replace(/\s+/g, ' '));
      }
      if (!mono) misses.push(`${name}:${rule.source.start.line} ${rule.selector.replace(/\s+/g, ' ')}`);
    });
  }
  /* The law is that nothing names the family without the axis, which is the
     misses list. The count is a live-wire check that the walk still reaches
     the sheets at all, so it carries enough headroom that merging two rules
     into one reads as a tidy rather than as an alarm.

     Quiet register, 23 Sept 2026: labels, counts and figures leave the mono
     family for the sans (rulebook rule 7), so the number of rules that name
     it keeps falling by design and no longer shows whether the walk reached
     the sheets. The live wire is now the walk itself: every sheet is read and
     yields rules, with headroom under the 472 walked when this was written.
     The misses list still requires every rule that names the family to
     carry the axis. */
  /* Quiet register, 23 Sept 2026: the floor of 24 rules that name the family
     counted what the sheets happened to hold before the reviewed lanes moved
     labels, counts and dates to the sans, so it fell to 15 by design and
     failed the rule it was meant to guard. What it stood for, that an id or
     a code stays in mono, is now held by name: each selector below renders a
     run id, a hash, a record reference or a code, and must name the family,
     matched inside a grouped selector list. The floor is the number of those
     pins, counted as selectors, so it can never ask for less than they need.
     The other side is held too: labels, counts and dates that left mono in
     those lanes must not name it again. */
  const ID_AND_CODE_SELECTORS = [
    '.explore-folio__run-reference code',
    '.workspace-row code',
    '.workspace-reference p',
    '.workspace-reason code',
    '.workbench .research-error-details code',
    '.workbench-recent-hash',
    '.investigation-row__identity',
    '.workbench-lens-digest',
    /* Restated 2 October 2026: 42's Fieldwork prints no run id or code (query
       ids ride in a title attribute), so '.fieldwork-details code' left with
       the retired dossier. */
    '.ui-meta-rail-value--mono',
  ];
  const LABEL_SELECTORS_OFF_MONO = [
    '.explore-filters__count',
    '.investigation-folio__kicker',
    '.workbench-recent-day',
    '.workbench-recent-time',
    /* Restated 2 October 2026: the retired strip's counts now sit in the
       market tabs, which stay off mono. */
    '.fieldwork-tab__count',
    '.ui-stat-value',
    '.ui-progress-rail-num',
  ];
  for (const [name, count] of walked) expect(count, `${name} is walked`).toBeGreaterThan(0);
  expect([...walked.values()].reduce((sum, count) => sum + count, 0)).toBeGreaterThanOrEqual(300);
  expect(named).toBeGreaterThanOrEqual(ID_AND_CODE_SELECTORS.length);
  for (const id of ID_AND_CODE_SELECTORS) expect(namedSelectors, id).toContain(id);
  for (const label of LABEL_SELECTORS_OFF_MONO){
    expect(walkedSelectors.has(label), `${label} is still styled`).toBe(true);
    expect(namedSelectors, label).not.toContain(label);
  }
  expect(misses).toEqual([]);
  /* Ask redesign, 23 Sept 2026: the rail's data line repeated the page head
     and is gone, so no rule dresses it any more. */
  const consoleSheet = sheet('styles/console.css');
  expect(declarationsOf(consoleSheet, '.workbench .workbench-rail-sub')).toEqual({});
  /* Navigation on the workbench reads in the body face, in sentence case
     (23 Sept 2026 design direction): capitals in the mono face are kept for
     data provenance, not for the back link, the tabs, the fresh-start
     actions or the rail labels. Each of these names the sans and resets the
     case, the tracking and the mono axis it would otherwise inherit. */
  for (const selector of ['.workbench .workbench-back', '.workbench .workbench-new-brief', '.workbench .workbench-rail-k', '.workbench .workbench-empty', '.workbench .workbench-mode-tabs button']){
    const found = declarationsOf(consoleSheet, selector);
    expect(found['font-family'], selector).toBe('var(--sans)');
    expect(found['text-transform'], selector).toBe('none');
    expect(found['letter-spacing'], selector).toBe('0');
    expect(found['font-variation-settings'], selector).toBe('normal');
  }
});

/* Quiet register, 23 Sept 2026: the Discover kickers are labels a reader
   reads, so they take the sans at 14px and weight 400 in sentence case, with
   no tracking and the MONO axis off. */
test('the Discover kickers take the quiet label: type-3, 400, sans, no tracking, no capitals, sans axis', () => {
  const found = declarationsOf(sheet('ogilvy-intelligence.css'), '.explore-state__kicker');
  expect(found.font).toMatch(/^400 var\(--type-3\)\/[\d.]+ var\(--oi-sans\)/);
  expect(found['letter-spacing']).toBeUndefined();
  expect(found['text-transform']).toBeUndefined();
  expect(found['font-variation-settings']).toMatch(/"MONO" 0\b/);
});

test('a disabled Continue is the rule-bounded control and the red plane appears only when pressable', () => {
  const ui = sheet('ui/ui.css');
  const rest = declarationsOf(ui, '.ui-flowbar-cta');
  expect(rest.background).toBe('var(--color-daylight-red-surface)');
  expect(rest.border).toBe('2px solid var(--color-daylight-red-surface)');
  const disabled = declarationsOf(ui, 'button.ui-flowbar-cta[aria-disabled="true"]');
  expect(disabled.opacity).toBe('1');
  expect(disabled.color).toBe('var(--ink)');
  expect(disabled.background).toBe('transparent');
  expect(disabled['border-color']).toBe('var(--muted)');
  expect(disabled['box-shadow']).toBe('none');
  /* The console sheet paints the plane red under .workbench with more
     specificity than the ui sheet, so it has to release the disabled state
     itself. */
  const consoleSheet = sheet('styles/console.css');
  const plane = declarationsOf(consoleSheet, '.workbench .ui-flowbar-cta');
  expect(plane.background).toBe('var(--color-daylight-red-surface)');
  const held = declarationsOf(consoleSheet, '.workbench .ui-flowbar-cta[aria-disabled="true"]');
  /* Quiet register, 23 Sept 2026: in the workbench the held Continue read as
     live in ink while nothing could continue, so it reads in muted text with
     the not-allowed cursor, as the held Send does. It is still the
     rule-bounded control and is never dimmed. */
  expect(held.color).toBe('var(--muted)');
  expect(held.cursor).toBe('not-allowed');
  expect(held.background).toBe('transparent');
  expect(held['border-color']).toBe('var(--muted)');
  expect(held.opacity).toBeUndefined();
  for (const root of [ui, consoleSheet]){
    root.walkRules((rule) => {
      if (!/ui-flowbar-cta/.test(rule.selector)) return;
      rule.walkDecls('opacity', (declaration) => {
        if (declaration.value.trim() !== '1') throw new Error(`${rule.selector}:${declaration.source.start.line} dims the Continue control`);
      });
    });
  }
});

test('New brief and the empty rail panels keep their 1px rule slot without drawing a box inside the rail', () => {
  /* The rail is one panel. A box around each fresh-start action and each
     empty note put boxes inside it (23 Sept 2026 design direction), so the
     rule is kept for geometry and drawn transparent at rest. */
  const consoleSheet = sheet('styles/console.css');
  expect(declarationsOf(consoleSheet, '.workbench .workbench-new-brief').border).toBe('1px solid transparent');
  expect(declarationsOf(consoleSheet, '.workbench .workbench-empty').border).toBe('1px solid transparent');
});

test('no consumer sheet draws a dashed border', () => {
  const hits = [];
  for (const path of stylesheets(sourceRoot)){
    const name = relative(sourceRoot, path).replace(/\\/g, '/');
    postcss.parse(readFileSync(path, 'utf8')).walkDecls((declaration) => {
      if (/^(?:border(?:-(?:top|right|bottom|left))?(?:-style)?|outline(?:-style)?)$/.test(declaration.prop) && /\bdashed\b/.test(declaration.value)){
        hits.push(`${name}:${declaration.source.start.line} ${declaration.prop}: ${declaration.value}`);
      }
    });
  }
  expect(hits).toEqual([]);
});

/* A sheet level walk cannot see a border written into a JSX style object, and
   two of them survived the round 9 pass: the Ask empty state, which the Build
   job reaches whenever the Ask side has no messages, and the honesty rails
   panel on the standalone map. Same law, so the same census, over the source
   the sheets cannot reach. Chart dash arrays are a different property and are
   left alone. */
function modules(directory){
  const files = [];
  for (const entry of readdirSync(directory)){
    if (entry === '__tests__') continue;
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) files.push(...modules(path));
    else if (/\.(?:jsx|js|mjs)$/.test(entry)) files.push(path);
  }
  return files;
}

test('no inline style in a consumer module draws a dashed border', () => {
  const hits = [];
  const inlineBorder = /\b(?:border|borderTop|borderRight|borderBottom|borderLeft|borderStyle|outline|outlineStyle)\s*:\s*(['"`])([^'"`]*)\1/g;
  for (const path of modules(sourceRoot)){
    const name = relative(sourceRoot, path).replace(/\\/g, '/');
    const source = readFileSync(path, 'utf8');
    for (const match of source.matchAll(inlineBorder)){
      if (!/\bdashed\b/.test(match[2])) continue;
      const line = source.slice(0, match.index).split('\n').length;
      hits.push(`${name}:${line} ${match[0]}`);
    }
  }
  expect(hits).toEqual([]);
});
