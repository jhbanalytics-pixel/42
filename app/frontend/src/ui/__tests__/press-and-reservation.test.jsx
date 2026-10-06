/* Task 65. The two product defects round 16 of the audit measured.

   The press. Section 7 of the addendum gains an Active row and the base
   specification already named `--motion-control` for press reinforcement. The
   round 16 active pass measured 39 interactive classes across both
   repositories and found no press answer on any of them. This suite holds the
   consumer's half of that law: every interactive class the sweep named and the
   consumer owns carries an `:active` rule taking the press mark, and the mark
   is the one idea rather than a local choice per surface.

   The reservation. The round 16 loading tier mounted 16 of the 17 routes in
   their loading branch and measured where the route stands while its data is
   held against where it stands once the data lands. Six routes held their
   geometry and ten did not. This suite holds the structural half of the four
   the product could fix. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../../', import.meta.url));

function read(relative) {
  return readFileSync(`${root}${relative}`, 'utf8');
}

const SHEETS = [
  'app.css',
  'styles/boardviews.css',
  'styles/console.css',
  'styles/dossier.css',
  'styles/empty-states.css',
  'styles/fieldwork.css',
  'styles/graphs.css',
  'styles/lexlisten.css',
  'styles/loop.css',
  'styles/workspaces.css',
  'ui/ui.css',
];

/* Comments are stripped before any rule is read, so a comment sitting above a
   rule cannot be picked up as part of its selector. */
const sheets = SHEETS.map((relative) => ({relative, css: read(relative).replaceAll(/\/\*[\s\S]*?\*\//g, '')}));
const allCss = sheets.map(({css}) => css).join('\n');
const cssBytes = allCss.length;

/* The consumer's half of the 39 interactive classes the round 16 active pass
   measured, each with the family its press rule is written on. The five the
   ruling leaves out are not here: the skip link, which is never a pointer
   target, and the four text entry fields, where a press places the caret and
   the field answers with the caret and the focus ring. */
const PRESS_FAMILIES = [
  '.brw-search-btn',
  '.dossier-action',
  '.es-act',
  '.es-act-ghost',
  '.evidence-drawer__close',
  '.evidence-ledger__row a',
  /* Restated 2 October 2026: 42's Fieldwork names its buttons fieldwork-button
     (the retired fieldwork-action went with the old dossier). */
  '.fieldwork-button',
  '.legacy-action',
  '.legacy-action--primary',
  '.legacy-chip',
  '.legacy-link',
  '.legacy-link--quiet',
  '.listen-filter-chip',
  '.loop-stage',
  '.map-node',
  '.ui-next a',
  '.workbench-ask-suggestion',
  '.workbench-back',
  '.workbench-mobile-switch button',
  '.workbench-mode-tabs button',
  '.workbench-new-brief',
  '.workspace-page button',
  '.workspace-row a',
  '[role="tab"]',
];

test('the press mark and its width are declared once, not written at each rule', () => {
  const app = read('app.css');
  expect(app).toContain('--press-rule-width:');
  expect(app).toMatch(/--press-grip:[\s\S]{0,240}?inset 0 var\(--press-rule-width\) 0/);
  expect(app).toMatch(/--press-grip:[\s\S]{0,240}?calc\(var\(--press-rule-width\) \* -1\)/);
  expect(app).toContain('--press-grip-inverse:');
  /* The press mark is never the five pixel leading inset section 6 reserves
     for selection. */
  const gripBlock = app.slice(app.indexOf('--press-grip:'), app.indexOf('--press-grip:') + 400);
  expect(gripBlock).not.toContain('inset 5px');
  expect(cssBytes).toBeGreaterThan(200000);
});

test('every interactive class the sweep named and the consumer owns answers a press', () => {
  const missing = PRESS_FAMILIES.filter((selector) => {
    const escaped = selector.replaceAll(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return !new RegExp(`${escaped}:active(?![\\w-])`).test(allCss);
  });
  expect({missing, cssBytes}).toEqual({missing: [], cssBytes});
});

test('every consumer press answer takes the declared mark and nothing local', () => {
  const local = [];
  for (const {relative, css} of sheets) {
    for (const match of css.matchAll(/([^{}]*:active[^{}]*)\{([^}]*)\}/g)) {
      const selector = match[1].trim().replaceAll(/\s+/g, ' ');
      const body = match[2];
      const shadow = /box-shadow:\s*([^;]+)/.exec(body);
      if (!shadow) continue;
      const value = shadow[1].trim();
      /* A family whose hover or selection already draws a mark restates that
         mark beside the grip, so the press adds an answer rather than
         replacing the one the reader already has. What is not allowed is a
         press mark invented at the rule. */
      if (value.includes('var(--press-grip')) continue;
      local.push(`${relative}: ${selector} takes ${value}`);
    }
  }
  expect(local).toEqual([]);
});

function hasPressTransition(family, css){
  const candidates = [family, family.replace(/\[[^\]]+\]/g, ''), family.replace(/(\.[\w-]+)\.[\w-]+$/, '$1')];
  const declared = candidates.flatMap((candidate) => {
    const escaped = candidate.replaceAll(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const resting = new RegExp(`${escaped}(?![\\w-])[^{}]*\\{[^}]*transition:[^;}]*`, 'g');
    return [...css.matchAll(resting)].map((hit) => hit[0]);
  }).join(' ');
  return /--motion-control/.test(declared);
}

test('a selected control inherits its base transition and fails when that motion is absent', () => {
  const css = '.card { transition: box-shadow var(--motion-control) ease; }';
  expect(hasPressTransition('.card[aria-pressed="true"]', css)).toBe(true);
  expect(hasPressTransition('.card[aria-pressed="true"]', css.replace('--motion-control', '--motion-row'))).toBe(false);
  expect(hasPressTransition('.card[aria-pressed="true"]', '.card { color: red; }')).toBe(false);
});

test('the press moves on the control token wherever the consumer declares it', () => {
  const off = [];
  for (const {relative, css} of sheets) {
    for (const match of css.matchAll(/([^{}]*:active[^{}]*)\{([^}]*)\}/g)) {
      const selector = match[1].trim().replaceAll(/\s+/g, ' ');
      if (!/box-shadow|background/.test(match[2])) continue;
      const family = selector.split(':active')[0].trim().split(',').pop().trim();
      if (!hasPressTransition(family, allCss)) off.push(`${relative}: ${selector}`);
    }
  }
  expect(off).toEqual([]);
});

/* Task 92. Two rules that reach one property on one element and carry the same
   specificity are separated by nothing but document order. Where the two sheets
   holding them arrive on their own requests, document order is settled by
   whichever request finishes first, and the built CSS declares no layer, so the
   cascade has no other tiebreak to reach for. An answer decided that way is
   drawn on some loads and not on others.

   Round 10 measured exactly that on the next-step link. Its hover rule is in
   ui.css and its press rule was in app.css, both at one class, one class and
   one type, and over 30 loads of one cell the grip was there 20 times and gone
   the other 10. Which of the two sheets came second predicted the answer every
   time.

   A press rule therefore either travels in the sheet holding the rules it ties
   or outranks them. Either clears the fault. What is not allowed is a tie
   across two sheets, because nothing in the cascade settles it. */

/* A state the selector asks of its own subject. One inside :is(), :not() or
   :has() is a condition on that subject rather than a state of it, so the
   walker below leaves those alone. */
const OWN_STATE = /^:(?:hover|active|focus(?:-visible|-within)?|link|visited|any-link)(?![\w-])/;

function stateFreeSubject(selector) {
  let subject = '';
  let depth = 0;
  for (let index = 0; index < selector.length; index += 1) {
    const character = selector[index];
    if (character === '(') depth += 1;
    else if (character === ')') depth -= 1;
    else if (character === ':' && depth === 0) {
      const state = OWN_STATE.exec(selector.slice(index));
      if (state) {
        index += state[0].length - 1;
        continue;
      }
    }
    subject += character;
  }
  return subject.trim().replaceAll(/\s+/g, ' ');
}

/* Commas inside :is(), :not() and friends do not start a new selector. */
function selectorList(prelude) {
  const selectors = [];
  let depth = 0;
  let start = 0;
  for (let index = 0; index < prelude.length; index += 1) {
    const character = prelude[index];
    if (character === '(') depth += 1;
    else if (character === ')') depth -= 1;
    else if (character === ',' && depth === 0) {
      selectors.push(prelude.slice(start, index));
      start = index + 1;
    }
  }
  selectors.push(prelude.slice(start));
  return selectors.map((selector) => selector.trim()).filter(Boolean);
}

/* Specificity as ids, classes and types. :is(), :not() and :has() take the
   highest of their arguments and :where() takes nothing, which is what the
   cascade does with them. */
function specificity(selector) {
  let ids = 0;
  let classes = 0;
  let types = 0;
  let rest = selector;
  for (const name of ['where', 'is', 'not', 'has', 'matches']) {
    for (;;) {
      const at = rest.indexOf(`:${name}(`);
      if (at === -1) break;
      const open = at + name.length + 1;
      let depth = 0;
      let close = -1;
      for (let index = open; index < rest.length; index += 1) {
        if (rest[index] === '(') depth += 1;
        else if (rest[index] === ')') {
          depth -= 1;
          if (depth === 0) { close = index; break; }
        }
      }
      if (close === -1) break;
      if (name !== 'where') {
        let best = [0, 0, 0];
        for (const inner of selectorList(rest.slice(open + 1, close))) {
          const measured = specificity(inner);
          const rank = (value) => value[0] * 1e6 + value[1] * 1e3 + value[2];
          if (rank(measured) > rank(best)) best = measured;
        }
        ids += best[0];
        classes += best[1];
        types += best[2];
      }
      rest = `${rest.slice(0, at)} ${rest.slice(close + 1)}`;
    }
  }
  ids += (rest.match(/#[\w-]+/g) || []).length;
  rest = rest.replaceAll(/#[\w-]+/g, ' ');
  classes += (rest.match(/\[[^\]]*\]/g) || []).length;
  rest = rest.replaceAll(/\[[^\]]*\]/g, ' ');
  types += (rest.match(/::[\w-]+/g) || []).length;
  rest = rest.replaceAll(/::[\w-]+/g, ' ');
  classes += (rest.match(/:[\w-]+\([^)]*\)/g) || []).length;
  rest = rest.replaceAll(/:[\w-]+\([^)]*\)/g, ' ');
  classes += (rest.match(/:[\w-]+/g) || []).length;
  rest = rest.replaceAll(/:[\w-]+/g, ' ');
  classes += (rest.match(/\.[\w-]+/g) || []).length;
  rest = rest.replaceAll(/\.[\w-]+/g, ' ');
  types += (rest.match(/(^|[\s>+~])[a-zA-Z][\w-]*/g) || []).length;
  return [ids, classes, types];
}

/* Every rule in one sheet that sets a box shadow, with the at-rule condition it
   sits under, because two rules only meet where the same condition holds. */
function shadowRules(css, condition = '') {
  const found = [];
  let depth = 0;
  let start = 0;
  let quote = '';
  for (let index = 0; index < css.length; index += 1) {
    const character = css[index];
    if (quote) {
      if (character === '\\') index += 1;
      else if (character === quote) quote = '';
      continue;
    }
    if (character === '"' || character === "'") { quote = character; continue; }
    if (character === '{') depth += 1;
    else if (character === '}') {
      depth -= 1;
      if (depth > 0) continue;
      const rule = css.slice(start, index + 1);
      start = index + 1;
      const brace = rule.indexOf('{');
      const prelude = rule.slice(0, brace).trim();
      const body = rule.slice(brace + 1, rule.lastIndexOf('}'));
      if (prelude.startsWith('@')) {
        if (/^@(?:media|supports|container|scope|layer)\b/.test(prelude)) {
          found.push(...shadowRules(body, `${condition}${prelude.replaceAll(/\s+/g, ' ')} `));
        }
        continue;
      }
      if (!/(?:^|[;\s])box-shadow\s*:/.test(body)) continue;
      const shadow = (/box-shadow\s*:\s*([^;]+)/.exec(body) || [, ''])[1].trim();
      for (const selector of selectorList(prelude)) {
        found.push({selector, condition, subject: stateFreeSubject(selector), spec: specificity(selector).join('-'), value: shadow});
      }
    }
  }
  return found;
}

test('a press answer is never left to which stylesheet arrives first', () => {
  const groups = new Map();
  for (const {relative, css} of sheets) {
    for (const rule of shadowRules(css)) {
      const key = `${rule.subject} under ${rule.condition || 'no condition'} at ${rule.spec}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push({relative, selector: rule.selector});
    }
  }
  const undecided = [];
  for (const [key, group] of groups) {
    if (!group.some((rule) => /:active(?![\w-])/.test(rule.selector))) continue;
    if (new Set(group.map((rule) => rule.relative)).size < 2) continue;
    undecided.push(`${key}: ${group.map((rule) => `${rule.relative} ${rule.selector}`).join(' against ')}`);
  }
  expect({undecided, cssBytes}).toEqual({undecided: [], cssBytes});
});

/* Task 94. The law above reads the consumer's own sheets and stops at their
   edge. The empty state action is contested from outside it. The pinned package
   writes `.es-act:active` taking the inverse grip, the consumer writes the same
   selector taking the grip, the two carry one class and one pseudo-class each,
   and they travel in two chunks that arrive on their own requests. Round 21
   measured what follows: on ten daylight cells the press mark reads red under
   one arrival order and near white under the other, on a control the consumer
   renders as transparent paper where near white is invisible.

   The package ships inside the pin and cannot be moved from here, so task 92's
   remedy of putting the two rules in one sheet is unavailable and the consumer
   has to outrank rather than out-order.

   What this holds is the class and not the one selector. No consumer press
   answer may lose to, or tie at a different value with, a rule in the pinned
   package that reaches the same element under the same condition. A tie whose
   two sides declare the same value is allowed and listed, because nothing a
   reader sees depends on which of them wins; if a later pin moves either value
   that tie stops being permitted and this fails. */
const packageCss = read('../node_modules/ogilvy-intelligence-design-system/dist/style.css')
  .replaceAll(/\/\*[\s\S]*?\*\//g, '');

/* A class written twice in one compound selects exactly the elements it selects
   once, so the repeat is taken off before two subjects are compared. Only the
   comparison is normalised; the specificity is read off the selector as
   written, which is the whole point of writing it twice. */
function collapseRepeats(subject) {
  let previous = null;
  let current = subject;
  while (current !== previous) {
    previous = current;
    current = current.replace(/(\.[\w-]+)\1(?![\w-])/g, '$1');
  }
  return current;
}

/* True where a package selector can match an element the consumer selector
   matches: the same subject, that subject with an ancestor in front of it, or
   that subject with more of its own compound written on. `[data-dir=midnight]
   .es-act` reaches `.es-act` the first way, and nothing shorter reaches
   anything. */
function reaches(rawPackageSubject, rawOwnSubject) {
  const packageSubject = collapseRepeats(rawPackageSubject);
  const ownSubject = collapseRepeats(rawOwnSubject);
  if (packageSubject === ownSubject) return true;
  if (!packageSubject.endsWith(ownSubject)) return false;
  if (/^[.#[]/.test(ownSubject)) return true;
  const before = packageSubject[packageSubject.length - ownSubject.length - 1];
  return before === ' ' || before === '>';
}

const rankOf = (selector) => {
  const [ids, classes, types] = specificity(selector);
  return ids * 1e6 + classes * 1e3 + types;
};

test('Compare padding outranks generic page padding regardless of sheet order', async () => {
  const postcss = (await import('postcss')).default;
  const own = [];
  const generic = [];
  for (const file of ['app.css', 'ogilvy-intelligence.css']){
    postcss.parse(read(file)).walkRules((rule) => {
      if (!rule.nodes.some((node) => node.type === 'decl' && node.prop === 'padding')) return;
      for (const selector of rule.selectors){
        if (/^(?:\.page)?\.compare-page$/.test(selector)) own.push(selector);
        if (selector === '.page') generic.push(selector);
      }
    });
  }
  expect(own).toHaveLength(1);
  expect(generic.length).toBeGreaterThan(0);
  expect(rankOf(own[0])).toBeGreaterThan(Math.max(...generic.map(rankOf)));
});

test('a consumer press answer outranks every package rule that reaches the same control', () => {
  const packagePress = shadowRules(packageCss).filter((rule) => /:active(?![\w-])/.test(rule.selector));
  const beaten = [];
  const tiedOnOneValue = [];
  for (const {relative, css} of sheets) {
    for (const own of shadowRules(css)) {
      if (!/:active(?![\w-])/.test(own.selector)) continue;
      for (const other of packagePress) {
        if (other.condition !== own.condition) continue;
        if (!reaches(other.subject, own.subject)) continue;
        const gap = rankOf(own.selector) - rankOf(other.selector);
        if (gap > 0) continue;
        const line = `${relative} ${own.selector} at ${own.spec} taking ${own.value}`
          + ` against the package ${other.selector} at ${other.spec} taking ${other.value}`;
        if (gap === 0 && own.value === other.value) tiedOnOneValue.push(line);
        else beaten.push(line);
      }
    }
  }
  expect({beaten, packagePress: packagePress.length}).toEqual({beaten: [], packagePress: packagePress.length});
  /* The permitted ties, named so a later pin that moves one of their values is
     read as a change rather than as noise. */
  expect(tiedOnOneValue).toEqual([
    'styles/dossier.css .dossier-action:active at 0-2-0 taking var(--press-grip)'
      + ' against the package .dossier-action:active at 0-2-0 taking var(--press-grip)',
    'styles/empty-states.css .es-act.es-act:active at 0-3-0 taking var(--press-grip)'
      + ' against the package [data-dir=midnight] .es-act:active at 0-3-0 taking var(--press-grip)',
  ]);
});

/* The reservation. Each of these is a structure assertion on the route source,
   because the geometry itself is measured by the sweep and not by a unit
   suite. What a unit suite can hold is the shape the geometry follows from. */

test('Method reads no desk, so it no longer waits for one', () => {
  const app = read('App.jsx');
  const methodStart = app.indexOf("{route === 'method' && (");
  const exploreStart = app.indexOf("{route === 'explore' &&", methodStart);
  const methodBranch = methodStart >= 0 && exploreStart > methodStart
    ? app.slice(methodStart, exploreStart)
    : '';
  expect(methodBranch.includes('<Suspense fallback={<RouteWait />}>') && methodBranch.includes('<MethodPage />')).toBe(true);
  expect(methodBranch.includes('desk.state')).toBe(false);
});

test('Browse paints its own surface while the desk is held', () => {
  const app = read('App.jsx');
  expect(app).toContain("const DESK_OPTIONAL = new Set(['browse', 'method']);");
  /* The generic route wait no longer stands in for either route, so the
     surface the settled page uses is the surface the loading page uses and the
     route's own origin does not move when the data lands. */
  expect(app).not.toMatch(/desk\.state === 'loading'[^\n]*<RouteWait \/>/);
  const browseStart = app.indexOf("{route === 'browse' && (");
  const methodStart = app.indexOf("{route === 'method' && (", browseStart);
  const browseBranch = browseStart >= 0 && methodStart > browseStart
    ? app.slice(browseStart, methodStart)
    : '';
  expect(browseBranch.includes('<BrowsePage ') && !browseBranch.includes("desk.state === 'loading'")).toBe(true);
  expect(browseBranch.includes("desk.state === 'error'")).toBe(false);
  const views = read('views.jsx');
  expect(views).toContain('data-screen-label="Browse · the post archive"');
  expect(views).toContain('data-screen-label="Method · how 42 reads"');
});

/* Restated 2 October 2026: the retired dossier's loading branch and its
   strip are gone. The rule they held stays: loading paints the lead every
   settle shares, under the route's standing title, and prints no count it has
   not read. */
test('Fieldwork loading paints the lead every settle of the route shares', () => {
  const fieldwork = read('fieldwork.jsx');
  const page = fieldwork.slice(fieldwork.indexOf('export function FieldworkPage'), fieldwork.indexOf('function FieldworkBody'));
  expect(page).toContain('fieldwork-lead');
  const body = fieldwork.slice(fieldwork.indexOf('function FieldworkBody'));
  const loading = body.slice(body.indexOf("if (view.state === 'loading')"), body.indexOf("if (view.state === 'auth')"));
  expect(loading).toContain('role="status"');
  expect(loading).not.toContain('DaySummary');
  expect(loading).not.toContain('Roster');
});

test('the Fieldwork loading title is the settled title, not a state report', () => {
  const fieldwork = read('fieldwork.jsx');
  const page = fieldwork.slice(fieldwork.indexOf('export function FieldworkPage'), fieldwork.indexOf('function FieldworkBody'));
  /* One h1 in the lead, printed before any state branch, so the title never
     changes when the data lands. */
  expect((page.match(/<h1 /g) || []).length).toBe(1);
  expect(page).toContain('>Fieldwork</h1>');
  expect(fieldwork.slice(fieldwork.indexOf('function FieldworkBody'))).not.toContain('<h1');
});

test('Source Lab reserves only what every one of its settles shares', () => {
  const workspaces = read('styles/workspaces.css');
  /* The inventory band reserved 2,600 pixels for a verified snapshot the route
     cannot promise. On a desk whose snapshot does not verify the page settled
     at 340 pixels, so the reader watched it collapse by 2,550. A reservation a
     route cannot guarantee is a guess, and a guess that large is worse than
     none: the page moves twice instead of growing once.

     Round 18 measured the shared band still wrong by 230 pixels at both
     widths: the page stood 570.36 loading and 340.36 settled at 1280, and 554
     against 324 at 390, so it still collapsed rather than grew. The 280 pixel
     shared band was a second guess at the same number. What every settle of
     this route actually shares is the title and one sentence, which is what
     the branch reserves now; the band stays declared for Historical and the
     Evidence Room, whose settles hold against it. */
  expect(workspaces).not.toContain('min-height: 2600px');
  expect(workspaces).not.toMatch(/\.workspace-skeleton--inventory\s*\{[^}]*min-height/);
  const sourceLab = read('sourceLab.jsx');
  const loading = sourceLab.slice(sourceLab.indexOf("if (state === 'loading')"));
  const branch = loading.slice(0, loading.indexOf("if (state !== 'ready'"));
  expect(branch).not.toContain('workspace-skeleton');
  expect(branch).toContain('<h1>Source Lab</h1>');
  /* The two routes that keep the band keep it, so this is a Source Lab change
     and not a shared one. */
  expect(read('historicalWorkspace.jsx')).toContain('workspace-skeleton');
  expect(read('evidenceRoom.jsx')).toContain('workspace-skeleton');
});
