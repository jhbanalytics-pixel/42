/* The 2.0.1 instrument stylesheet is 94,233 bytes on its own, which is more
   than the whole render-blocking budget the consumer holds itself to. The route
   surfaces below are never on screen at first paint: the gate, the desk
   skeleton, the shell chrome, the briefing frame and the state frames are, and
   they stay in the critical half.

   The split is by selector family, not by rule position. A family's base rule
   and every at-rule override of it travel into the same half, because the
   deferred half loads after the critical one: leaving a base rule in the late
   sheet while its media override stayed in the early one would invert the
   cascade and let the base rule win at the width the override exists for. So an
   at-rule block is partitioned the same way its top-level neighbours are, and
   emitted into each half that has rules for it. Original order is preserved
   inside each half. A rule naming families from both halves has its selector
   list split and its declarations written into each, so nothing is dropped and
   no family reads its declarations out of the sheet it does not travel in.

   The 2.0.4 sheet reached 99,433 bytes and its utility slot rules pushed the
   critical half over the ceiling, so one more family moved to the late half:
   the legacy oi-shell chrome, which InstrumentShell replaced and nothing on a
   route renders. The 2.0.9 and 2.0.10 sheets are 100,235 bytes each and the
   2.0.11 sheet is 102,755, and that growth pushed the critical half over the
   ceiling again, so a family that paints first gave up one of its parts: the
   proof ribbon's data table and the scroll wrapper around it. Both sit inside
   a details disclosure the ribbon renders closed, so a reader reaches them
   only by opening it and no first paint can show them. The disclosure and its
   summary stay in the critical half, because a closed details still paints its
   summary. A deferred root may name a part of a critical family this way, and
   only that part travels late.

   The 2.0.12 sheet is 102,940 bytes, 185 more than 2.0.11. All of that growth
   lands where the earlier cut already put the room: the critical half goes
   71,510 to 71,651 and the built entry stylesheet 79,155 to 79,296, which is
   704 bytes under the ceiling. No family moved.

   The 2.0.13 sheet is 103,032 bytes, 92 more than 2.0.12, and this time only
   16 of those bytes reach the critical half: the ribbon data table is already
   a deferred part, so the container query that replaced its viewport arm lands
   late with it and the deferred half carries 76 of the 92. The critical half
   goes 71,651 to 71,667 and the built entry stylesheet 79,296 to 79,312, which
   is 688 bytes under the ceiling. No family moved.

   The 2.0.14 sheet is 103,032 bytes, byte identical to 2.0.13: the manifest
   reports the same cssSha256 and the cut moves JavaScript only, a register of
   written phrases and the reader that looks them up. So the split is unchanged
   and the built entry stylesheet holds at 79,312 bytes with the same 688 of
   headroom, measured from web/dist/index.html rather than carried forward.

   The 2.0.15 sheet is 107,226 bytes, 4,194 more than 2.0.14, and the press grip
   it carries lands almost all in families that paint first. The critical half
   went 71,667 to 75,174 and the built entry stylesheet 79,312 to 82,819, over
   both budgets, and no family still in the critical list is large enough to
   close a 2,874 byte gap without a reader waiting for it on every load. So this
   cut moves an axis rather than a family. A rule whose every selector requires
   a pointer state cannot answer a first paint, because a hover needs a pointer
   resting on a painted element and a press needs a pointer down on one, so
   neither can precede the paint it answers. 13 hover-only rules worth 1,780
   bytes and 21 press-only rules worth 1,736 bytes travel late, 3,516 bytes in
   all, and every one of them is a top-level rule, so no at-rule wrapper changed
   halves and no family gave up first paint. The critical half goes 75,174 to
   71,658, which is 642 under budget, and the built entry stylesheet 82,819 to
   79,303, which is 697 under the ceiling. 10 rules that pair a pointer state
   with a focus one stayed, worth 1,192 bytes, because autofocus or a restored
   deep link can put focus on an element the first paint draws. A list mixing
   pointer-state selectors with plain ones would stay whole in the critical half
   for the same reason a shared rule is expensive, and this sheet holds none.

   The duplicated byte budget does not close with it, and the reason is one rule
   the package writes rather than anything this split chooses. The press grip
   added a 197 byte transition to .dossier-action,.evidence-drawer__close, a
   rule naming a family from each half, so the split copies its declarations
   into both and that charge went 296 to 493. Deferring the two shared rules
   only a pointer can satisfy gives back 89, and the total goes 1,356 to 1,267
   against a budget of 1,200. The remaining 67 can only come from deferring
   dossier-action, which is a family losing first paint, or from the sealed
   sheet grouping that rule differently. Both are rulings above this cut, so the
   figure is recorded here and carried up rather than forced.

   Moving a rule later can only change a tie, because a pointer rule already
   outranks on specificity the plain rule it answers, and only a rule of equal
   weight was ever decided by source order. Of the 12 rules that tie or outrank
   a moved rule on a shared property, 6 pair different elements that share only
   the rule boundary class and 2 were already won by the pointer rule. The other
   4 reverse. Hovering the current route in midnight now takes the hover
   background rather than the aria-current one, on the rail link and on the
   strip link, and hovering a selected ribbon receipt takes the hover ring
   rather than the selection ring. The fourth is worth naming, because the focus
   carve-out cannot catch it: .state-view__summary:hover sets outline:none and
   .state-view__summary:focus-visible sets the ring, the two tie at a class and
   a pseudo-class, and from here the hover rule wins, so a summary that is
   focused and hovered at once shows no ring. The carve-out as that cut wrote
   it keeps a rule naming focus in the critical half. It could not keep a
   pointer rule from suppressing what a separate focus rule sets, which is what
   the paragraph below closes.

   That fourth reversal is closed here, by a carve-out that reads a declared
   property rather than a subject. .state-view__summary:hover sets a border
   colour and outline:none, .state-view__summary:focus-visible sets the ring,
   and both are a class and a pseudo-class, so they carry the same specificity
   and nothing but source order separates them. In the sealed sheet the hover
   rule sits at byte 78,382 and the focus rule at 78,955, so the focus rule was
   second and won. Moving the hover rule into the sheet that loads later made
   it second instead, and a keyboard reader lost the ring for as long as the
   pointer rested on the summary. So a pointer rule now stays in the critical
   half where it declares a property that a focus rule on the same subject
   declares too, compared at the shorthand root, which is what makes
   outline-color count against outline and border-color against border.

   Sharing the subject is not the test, because a hover that sets a border
   colour and a focus that sets a ring never meet. 12 pointer rules share a
   subject with a focus rule and travel anyway, one of them the press rule on
   this same summary, which sets a background colour no focus rule on it
   declares. Holding all 13 would put the critical half at 72,407 against a
   budget of 72,300 and raise the duplicated charge to 1,356, which trades a
   focus ring for a slower first paint. Holding only the rule that fights costs
   87 bytes: the critical half goes 71,658 to 71,745, which is 555 under
   budget, and the built entry stylesheet 79,303 to 79,390, which is 610 under
   the ceiling. The duplicated charge is untouched at 1,267 and stays red for
   the reason written above it. Three tests cover the carve-out, one reading
   this pair off the sealed sheet, one holding a pointer rule that shares only
   a subject to its late half, and one meeting a shorthand under a longhand
   name.

   Quiet register, 23 Sept 2026: the 2.0.21 sheet is 110,473 bytes, 7,071 more
   than 2.0.20, and as the split stood the critical half went 69,264 to 72,575
   and the built entry stylesheet 76,735 to 80,046, over both budgets. The
   ribbon now opens its values as a table of its own, proof-ribbon__values in
   proof-ribbon__values-wrap, inside the same closed disclosure that holds the
   data table, so nothing paints them until a reader opens it. They travel late
   as the data table's parts do, and the disclosure and its summary stay
   critical. That moves 1,147 bytes: the critical half goes to 71,428, which is
   872 under budget, and the built entry stylesheet to 78,899, which is 1,101
   under the ceiling. The duplicated charge goes 677 to 702, the cost of one
   at-rule re-opened in each half. The Compare column rules the same sheet
   writes under .comparison-instrument__signal name the ribbon but already
   travel with the Compare family. */
export const DEFERRED_SELECTOR_ROOTS = Object.freeze([
  'audience-unavailable',
  'cited-answer',
  'comparison-instrument',
  'discover-instrument',
  'evidence-drawer',
  'evidence-ledger',
  'evidence-readiness',
  'evidence-room',
  'fieldwork-instrument',
  'intelligence-console',
  'oi-inventory-row',
  'oi-shell',
  'proof-ribbon__table',
  'proof-ribbon__table-wrap',
  'proof-ribbon__values',
  'proof-ribbon__values-wrap',
]);

/* Families that must paint with the first stylesheet: the shell chrome and its
   rail, strip and theme control, the passcode gate, the briefing frame and the
   state frames a route shows before its data lands. The evidence column is
   here because the briefing composes it: the paint window test measured it on
   screen for the whole deferred window when it was in the late half. A family
   here can still hand one of its parts to the deferred list above, as
   proof-ribbon does with its data table, and the split test allows only the
   parts that list names. */
export const CRITICAL_SURFACE_ROOTS = Object.freeze([
  'editorial-lead',
  'es-block',
  'evidence-column',
  'instrument-briefing',
  'instrument-job-rail',
  'instrument-market-option',
  'instrument-menu',
  'instrument-shell',
  'instrument-theme-control',
  'instrument-utility-strip',
  'oi-briefing',
  'oi-briefing-state',
  'proof-ribbon',
  'state-view',
  'ui-skeleton',
]);

/* Splits one level of CSS into its top-level rules. Quoted strings and comments
   are stepped over so a brace inside either never opens or closes a block. Each
   returned slice is contiguous original text, so joining the pieces back in
   order reproduces the input byte for byte. */
function topLevelRules(css){
  const rules = [];
  let depth = 0;
  let start = 0;
  let quote = '';
  for (let index = 0; index < css.length; index += 1){
    const character = css[index];
    if (quote){
      if (character === '\\') index += 1;
      else if (character === quote) quote = '';
      continue;
    }
    if (character === '/' && css[index + 1] === '*'){
      const close = css.indexOf('*/', index + 2);
      index = close === -1 ? css.length : close + 1;
      continue;
    }
    if (character === '"' || character === "'"){
      quote = character;
      continue;
    }
    if (character === '{') depth += 1;
    else if (character === '}'){
      depth -= 1;
      if (depth === 0){
        rules.push(css.slice(start, index + 1));
        start = index + 1;
      }
    }
  }
  if (start < css.length) rules.push(css.slice(start));
  return rules;
}

/* Index of the brace that opens a rule's block, skipping any that sit inside a
   leading comment or a quoted string. Returns -1 when the chunk has no block. */
function blockStart(rule){
  let quote = '';
  for (let index = 0; index < rule.length; index += 1){
    const character = rule[index];
    if (quote){
      if (character === '\\') index += 1;
      else if (character === quote) quote = '';
      continue;
    }
    if (character === '/' && rule[index + 1] === '*'){
      const close = rule.indexOf('*/', index + 2);
      index = close === -1 ? rule.length : close + 1;
      continue;
    }
    if (character === '"' || character === "'") quote = character;
    else if (character === '{') return index;
  }
  return -1;
}

/* A family named inside :is(), :not() or :has() is a condition on the subject,
   not the subject itself, so it must not decide which half the rule joins. */
function matchableSelector(selector){
  let previous = null;
  let current = selector.replace(/\/\*[\s\S]*?\*\//g, '');
  while (current !== previous){
    previous = current;
    current = current.replace(/\([^()]*\)/g, '');
  }
  return current;
}

/* Commas inside :is(), :not() and friends do not start a new selector. */
function selectorList(prelude){
  const selectors = [];
  let depth = 0;
  let start = 0;
  for (let index = 0; index < prelude.length; index += 1){
    const character = prelude[index];
    if (character === '(') depth += 1;
    else if (character === ')') depth -= 1;
    else if (character === ',' && depth === 0){
      selectors.push(prelude.slice(start, index));
      start = index + 1;
    }
  }
  selectors.push(prelude.slice(start));
  return selectors.map((selector) => selector.trim()).filter(Boolean);
}

function rootMatchers(roots){
  return roots.map((root) => new RegExp(`\\.${root}(?![a-zA-Z0-9-])`));
}

/* A pointer state cannot be true at first paint. A hover needs a pointer
   resting on a painted element and a press needs a pointer down on one, so
   neither can precede the paint it answers, and a rule whose every selector
   requires one is never render-blocking whatever family it names, unless it
   contests a property with a focus rule on the same subject, which the
   carve-out below reads. Focus is not a pointer state: autofocus or a restored
   deep link can put it on an element the first paint draws, so a rule naming
   :focus, :focus-visible or :focus-within stays in the critical half even
   where it also names a pointer state. A list mixing pointer-state selectors
   with plain ones stays critical whole, because splitting it would copy its
   declarations into both halves and the split pays duplicated bytes for that.
   The states are read off the subject, so :not(:hover), which is true before
   any pointer arrives, keeps its rule critical. */
const POINTER_STATE = /:(?:hover|active)(?![a-zA-Z0-9-])/;
const FOCUS_STATE = /:focus(?:-visible|-within)?(?![a-zA-Z0-9-])/;

function pointerStateOnly(selectors){
  if (selectors.length === 0) return false;
  if (selectors.some((selector) => FOCUS_STATE.test(selector))) return false;
  return selectors.every((selector) => POINTER_STATE.test(matchableSelector(selector)));
}

/* Two rules on one element decide each other only where they reach the same
   property, so the carve-out below compares declarations rather than subjects.
   A property is read at its shorthand root: outline-color is one of the things
   outline sets and border-color one of the things border sets, so a pointer
   rule naming the longhand fights a focus rule naming the shorthand. The roots
   are the shorthands whose longhands carry the root as a prefix, and no root
   is a prefix of another, so the first match is also the shortest and the
   order they are written in does not decide anything. A property no root
   prefixes is its own root: box is not a property, and box-shadow and
   box-sizing are unrelated. */
const SHORTHAND_ROOTS = Object.freeze([
  'animation',
  'background',
  'border',
  'flex',
  'font',
  'grid',
  'inset',
  'list-style',
  'margin',
  'outline',
  'overflow',
  'padding',
  'text-decoration',
  'transition',
]);

function shorthandRoot(property){
  const name = property.trim().toLowerCase();
  if (name.startsWith('--')) return name;
  return SHORTHAND_ROOTS.find((root) => name === root || name.startsWith(`${root}-`)) ?? name;
}

/* The property roots a rule's block declares. A semicolon inside a quoted
   string or a function's arguments does not end a declaration, and the first
   colon separates the name from a value that may hold more of them. */
function declaredProperties(body){
  const declarations = [];
  const properties = [];
  let depth = 0;
  let start = 0;
  let quote = '';
  for (let index = 0; index < body.length; index += 1){
    const character = body[index];
    if (quote){
      if (character === '\\') index += 1;
      else if (character === quote) quote = '';
      continue;
    }
    if (character === '"' || character === "'"){
      quote = character;
      continue;
    }
    if (character === '(') depth += 1;
    else if (character === ')') depth -= 1;
    else if (character === ';' && depth === 0){
      declarations.push(body.slice(start, index));
      start = index + 1;
    }
  }
  declarations.push(body.slice(start));
  for (const declaration of declarations){
    const colon = declaration.indexOf(':');
    if (colon === -1) continue;
    const name = declaration.slice(0, colon).trim();
    if (name) properties.push(shorthandRoot(name));
  }
  return properties;
}

/* The element a state rule is written about: the selector with the states it
   asks of its own subject taken off. A state inside :is(), :not() or :has() is
   a condition rather than the subject's own, so it stays, which is what keeps
   .oi-briefing:not(:hover) a different subject from .oi-briefing:hover. */
const OWN_STATE = /^:(?:hover|active|focus(?:-visible|-within)?)(?![a-zA-Z0-9-])/;

function stateFreeSubject(selector){
  let subject = '';
  let depth = 0;
  for (let index = 0; index < selector.length; index += 1){
    const character = selector[index];
    if (character === '(') depth += 1;
    else if (character === ')') depth -= 1;
    else if (character === ':' && depth === 0){
      const state = OWN_STATE.exec(selector.slice(index));
      if (state){
        index += state[0].length - 1;
        continue;
      }
    }
    subject += character;
  }
  return subject.trim();
}

/* Every subject a focus rule is written about, at any at-rule depth, against
   the property roots that rule declares for it. Built once from the whole
   sheet, because the rule a pointer rule fights is a different rule and can
   sit anywhere in it. */
function focusPropertyIndex(css, index = new Map()){
  for (const rule of topLevelRules(css)){
    const brace = blockStart(rule);
    if (brace === -1) continue;
    const prelude = rule.slice(0, brace).trim();
    const close = rule.lastIndexOf('}');
    if (prelude.startsWith('@')){
      focusPropertyIndex(rule.slice(brace + 1, close), index);
      continue;
    }
    const focused = selectorList(prelude).filter((selector) => FOCUS_STATE.test(selector));
    if (focused.length === 0) continue;
    const properties = declaredProperties(rule.slice(brace + 1, close));
    for (const selector of focused){
      const subject = stateFreeSubject(selector);
      const roots = index.get(subject) ?? new Set();
      for (const property of properties) roots.add(property);
      index.set(subject, roots);
    }
  }
  return index;
}

/* True where a pointer rule declares a property that a focus rule on the same
   subject declares too. That is the only case in which moving the pointer rule
   into the late sheet can change which of the two a reader sees, so it is the
   only case the carve-out holds. Sharing the subject alone is not enough: a
   hover that sets a border colour and a focus that sets a ring never meet. */
function contestsFocus(selectors, body, focusProperties){
  const properties = declaredProperties(body);
  if (properties.length === 0) return false;
  return selectors.some((selector) => {
    const focused = focusProperties.get(stateFreeSubject(selector));
    return focused !== undefined && properties.some((property) => focused.has(property));
  });
}

function partition(css, matchers, focusProperties){
  const critical = [];
  const deferred = [];
  let duplicatedBytes = 0;
  for (const rule of topLevelRules(css)){
    const brace = blockStart(rule);
    if (brace === -1){
      critical.push(rule);
      continue;
    }
    const prelude = rule.slice(0, brace);
    const close = rule.lastIndexOf('}');
    if (prelude.trim().startsWith('@')){
      const inner = partition(rule.slice(brace + 1, close), matchers, focusProperties);
      duplicatedBytes += inner.duplicatedBytes;
      if (inner.critical) critical.push(`${prelude}{${inner.critical}}`);
      if (inner.deferred) deferred.push(`${prelude}{${inner.deferred}}`);
      /* Only a block that landed in both halves re-opens its wrapper. */
      if (inner.critical && inner.deferred) duplicatedBytes += prelude.length + 2;
      continue;
    }
    const selectors = selectorList(prelude.trim());
    if (pointerStateOnly(selectors) && !contestsFocus(selectors, rule.slice(brace + 1, close), focusProperties)){
      deferred.push(rule);
      continue;
    }
    const deferredSelectors = selectors.filter((selector) => {
      const subject = matchableSelector(selector);
      return matchers.some((matcher) => matcher.test(subject));
    });
    if (deferredSelectors.length === 0){
      critical.push(rule);
      continue;
    }
    if (deferredSelectors.length === selectors.length){
      deferred.push(rule);
      continue;
    }
    /* A shared rule names families from both halves, so its selector list is
       split and its declarations are written into each half. Nothing is
       dropped, no family reads its declarations out of the other half, and
       relative order is preserved inside each half, so the copy cannot
       outrank a later rule for the same family. */
    const body = rule.slice(brace);
    const criticalRule = `${selectors.filter((selector) => !deferredSelectors.includes(selector)).join(',')}${body}`;
    const deferredRule = `${deferredSelectors.join(',')}${body}`;
    critical.push(criticalRule);
    deferred.push(deferredRule);
    duplicatedBytes += criticalRule.length + deferredRule.length - rule.length;
  }
  return {critical: critical.join(''), deferred: deferred.join(''), duplicatedBytes};
}

/* Returns the two halves and `duplicatedBytes`, the cost of re-opening an
   at-rule in both halves and of copying a shared rule's declarations into each.
   critical.length + deferred.length === css.length + duplicatedBytes, which is
   the conservation the tests check. */
export function splitInstrumentStylesheet(css, roots = DEFERRED_SELECTOR_ROOTS){
  return partition(css, rootMatchers(roots), focusPropertyIndex(css));
}

/* Every selector the sheet declares, at any at-rule depth. The tests use this
   to prove a family lands in exactly one half. */
export function declaredSelectors(css){
  const found = [];
  for (const rule of topLevelRules(css)){
    const brace = blockStart(rule);
    if (brace === -1) continue;
    const prelude = rule.slice(0, brace).trim();
    if (prelude.startsWith('@')){
      found.push(...declaredSelectors(rule.slice(brace + 1, rule.lastIndexOf('}'))));
      continue;
    }
    found.push(...selectorList(prelude));
  }
  return found;
}

/* Every rule the sheet declares that only a pointer can satisfy, at any at-rule
   depth, returned as its prelude. This reads the axis alone and not the focus
   carve-out, so the tests use it to name exactly which of these rules the
   critical half still holds and to prove the rest of them travel. */
export function pointerStateRules(css){
  const found = [];
  for (const rule of topLevelRules(css)){
    const brace = blockStart(rule);
    if (brace === -1) continue;
    const prelude = rule.slice(0, brace).trim();
    if (prelude.startsWith('@')){
      found.push(...pointerStateRules(rule.slice(brace + 1, rule.lastIndexOf('}'))));
      continue;
    }
    if (pointerStateOnly(selectorList(prelude))) found.push(prelude);
  }
  return found;
}
