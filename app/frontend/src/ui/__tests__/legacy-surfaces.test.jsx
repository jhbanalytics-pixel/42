/* The legacy routes keep layout inline and hand every surface a pointer can
   change to a sheet, because an inline border, background or colour outranks
   the sheet's hover and focus rules and the control then never answers. These
   tests read the route modules and the ui sheet as source. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');
const SURFACE = /\b(?:border|borderColor|background|backgroundColor|color|outline)\s*:/;

/* The inline style object attached to the element that carries the class,
   read from the JSX text: the style={{...}} on the same tag or the named
   constant the tag spreads. */
function inlineStyle(source, marker){
  const at = source.indexOf(marker);
  expect(at, `${marker} is rendered`).toBeGreaterThan(-1);
  const tag = source.slice(source.lastIndexOf('<', at), source.indexOf('>', at));
  const named = tag.match(/style=\{(\w+)\}/);
  if (named){
    const constant = source.match(new RegExp(`const ${named[1]} = \{([\s\S]*?)\n\};`));
    return constant ? constant[1] : '';
  }
  const literal = tag.match(/style=\{\{([\s\S]*?)\}\}/);
  return literal ? literal[1] : '';
}

test('the board close control, the browse search box and the listen search box carry no inline surface', () => {
  const cases = [
    ['board.jsx', 'className="board-close"'],
    ['views.jsx', 'className="brw-search-form"'],
    ['views.jsx', 'className="brw-search-input"'],
    ['listen.jsx', 'className="listen-search"'],
    ['listen.jsx', 'aria-label="Filter mentions"'],
    ['listen.jsx', 'aria-label="Clear filter"'],
  ];
  const findings = [];
  for (const [file, marker] of cases){
    const style = inlineStyle(read(file), marker);
    if (SURFACE.test(style)) findings.push(`${file} ${marker}: ${style.trim().replace(/\s+/g, ' ')}`);
  }
  expect(findings).toEqual([]);
});

test('every one of the three search fields and the close control has a hover and a visible focus ring on the focus width', () => {
  const sheets = read('styles/boardviews.css') + read('styles/lexlisten.css');
  const root = postcss.parse(sheets);
  const seen = {};
  root.walkRules((rule) => {
    for (const selector of rule.selectors){
      const m = selector.match(/^(\.board-close|\.brw-search-input|\.listen-search input|\.listen-clear):(hover|focus-visible)$/);
      if (!m) continue;
      seen[m[1]] = seen[m[1]] || {};
      seen[m[1]][m[2]] = rule.nodes.filter((n) => n.type === 'decl').map((n) => `${n.prop}: ${n.value}`).join('; ');
    }
  });
  for (const subject of ['.board-close', '.brw-search-input', '.listen-search input', '.listen-clear']){
    expect(seen[subject] && seen[subject].hover, `${subject} has a hover`).toBeTruthy();
    expect(seen[subject] && seen[subject]['focus-visible'], `${subject} focus ring`).toMatch(/outline: var\(--focus-width\) solid/);
  }
});

/* Round 4, task 26, P24. One kicker voice on every route: the provenance
   label from section 5, mono at type-1, 500, 0.18em, uppercase, muted, no
   dot. Browse, Board and Network wrote their own in red with a dot; Seeds
   and Explorer carried a breadcrumb above the eyebrow; Historical's kicker
   lost to the workspace's own paragraph rule. The package sheet declares
   .eyebrow as that role, so the three routes name the class and drop the
   inline voice, the breadcrumb becomes a next action row under the hero, and
   the workspace paragraph rule steps aside for the kicker. */
const DOT = /width: '6px', height: '6px', borderRadius: '50%', background: 'var\(--accent\)'/;
const OWN_KICKER = /fontFamily: 'var\(--(?:mono|sans)\)', fontSize: 'var\(--type-1\)', letterSpacing: '0\.1[58]em', textTransform: 'uppercase', color: 'var\(--accent\)'/;

test('Browse, Board and Network kickers take the eyebrow role and no route draws a red dot beside a kicker', () => {
  const findings = [];
  /* Shell consistency, 2 October 2026: Board and Browse are titled with the
     menu's noun, as Network is, so the eyebrow that carried that noun above a
     headline is gone. They name no eyebrow and write no kicker in its place. */
  const titled = {'views.jsx': 'Browse', 'board.jsx': 'Board'};
  for (const file of ['views.jsx', 'board.jsx']){
    const source = read(file);
    if (!source.includes(`<h1 className="page-title">${titled[file]}</h1>`)) findings.push(`${file} is not titled ${titled[file]}`);
    if (/<p className="eyebrow"[^>]*>(?:Board|Browse)/.test(source)) findings.push(`${file} still names its eyebrow`);
    if (OWN_KICKER.test(source)) findings.push(`${file} writes a kicker of its own`);
  }
  /* Quiet register, 23 Sept 2026: Network drops its eyebrow. The page title
     says what the page is and the one sentence lead names the market, so a
     kicker above the title only repeated them. It names no eyebrow and writes
     no kicker of its own in its place. */
  const network = read('network.jsx');
  if (/className="eyebrow"/.test(network)) findings.push('network.jsx still names an eyebrow');
  if (OWN_KICKER.test(network)) findings.push('network.jsx writes a kicker of its own');
  if (!/className="net-lead"[^>]*>[^<]*\{marketLabel\}/.test(network)) findings.push('network.jsx lead does not name the market');
  for (const file of ['views.jsx', 'board.jsx', 'network.jsx', 'seeds.jsx', 'seedpath.jsx', 'listen.jsx']){
    if (DOT.test(read(file))) findings.push(`${file} draws a dot`);
  }
  expect(findings).toEqual([]);
});

test('Seeds and Explorer carry no breadcrumb, and the trail links survive as a next action row', () => {
  for (const file of ['seeds.jsx', 'seedpath.jsx']){
    const source = read(file);
    expect(source, `${file} imports the breadcrumb`).not.toMatch(/SeedTrail/);
    expect(source, `${file} renders the next action row`).toMatch(/<NextActions /);
  }
  const ui = read('ui/ui.css');
  expect(ui).not.toMatch(/\.ui-seedtrail/);
  const root = postcss.parse(ui);
  const seen = {};
  root.walkRules((rule) => {
    for (const selector of rule.selectors){
      const m = selector.match(/^\.ui-next a:(hover|focus-visible)$/);
      if (m) seen[m[1]] = rule.nodes.filter((n) => n.type === 'decl').map((n) => `${n.prop}: ${n.value}`).join('; ');
    }
  });
  expect(seen.hover, 'the next action link answers the pointer').toMatch(/color: var\(--accent\)/);
  expect(seen['focus-visible'], 'the next action link rings on the focus width').toMatch(/outline: var\(--focus-width\) solid/);
});

/* Round 5, task 27. Faint is not a text token: the stage eyebrows and the
   seed legend titles measured 2.76 on the midnight stage plane. */
test('the stage eyebrow and the seed legend titles are muted, never faint', () => {
  const loop = postcss.parse(read('styles/loop.css'));
  let eyebrow = null;
  loop.walkRules((rule) => { if (rule.selectors.includes('.loop-stage-eyebrow')) eyebrow = rule; });
  expect(eyebrow, 'the stage eyebrow rule').not.toBeNull();
  expect(eyebrow.nodes.find((n) => n.type === 'decl' && n.prop === 'color')?.value).toBe('var(--muted)');
  const seeds = read('seeds.jsx');
  /* Quiet register, 23 Sept 2026: the seed label constant is LABEL now that
     it is the sans rather than mono capitals; it is still muted, never faint. */
  const cap = seeds.match(/const LABEL = \{([\s\S]*?)\n\};/);
  expect(cap, 'the seed legend style').not.toBeNull();
  expect(cap[1]).toMatch(/color: 'var\(--muted\)'/);
  expect(cap[1]).not.toMatch(/--faint/);
});

/* One next action row: the component owns the rule, and the Historical
   landing renders the component rather than a second copy of its markup. */
test('the next action row has one rule under one class', () => {
  const workspaces = postcss.parse(read('styles/workspaces.css'));
  const selectors = [];
  workspaces.walkRules((rule) => selectors.push(...rule.selectors));
  expect(selectors.filter((selector) => /workspace-next/.test(selector))).toEqual([]);
  const historical = read('historicalWorkspace.jsx');
  expect(historical).not.toMatch(/workspace-next/);
  expect(historical).toMatch(/<NextActions /);
  const ui = postcss.parse(read('ui/ui.css'));
  const next = [];
  ui.walkRules((rule) => { if (rule.selectors.includes('.ui-next')) next.push(rule); });
  expect(next).toHaveLength(1);
});

/* Task 19 minors. Show more pages the Source Lab inventory only, and the
   Source Lab hand-off inside the absent sentence keeps its 48px box.

   Round three, 24 Sept 2026: this used to refuse any negative margin on the
   hand-off, so that a link wrapping at 390 could not overlap the lines
   beside it. The box then stretched the line that held it, and the sentence
   read as three loose lines. The rule now pinned is the one that keeps both:
   the link is a single inline block that cannot wrap, padded out to the
   48px target, and its negative margin gives back exactly that padding, so
   its margin box is one line of the paragraph's leading. Under a keyboard
   ring both go to 0 together, so the ring, at the package offset, rings the
   words and not the line above. The browser suite measures the leading,
   the target, the ring and that the box covers no other control. */
/* Restated 2 October 2026: the inventory paging and its Source Lab hand-off
   are retired with the old Fieldwork. The roster's own small controls, the
   member disclosure and the latest-day button, keep the 48px target and
   cannot wrap into a broken line. */
test('the roster disclosure and latest-day control keep their 48px box', () => {
  const root = postcss.parse(read('styles/fieldwork.css'));
  const declarations = (selector) => {
    const found = [];
    root.walkRules((rule) => { if (rule.selectors.includes(selector)) found.push(...rule.nodes.filter((n) => n.type === 'decl').map((n) => `${n.prop}: ${n.value}`)); });
    return found;
  };
  expect(declarations('.fieldwork-link-button')).toContain('min-height: var(--target-min)');
  expect(declarations('.fieldwork-link-button')).toContain('white-space: nowrap');
  expect(declarations('.fieldwork-source__more > summary')).toContain('min-height: var(--target-min)');
});

test('the workspace paragraph rule steps aside for the kicker', () => {
  const root = postcss.parse(read('styles/workspaces.css'));
  const selectors = [];
  root.walkRules((rule) => selectors.push(...rule.selectors));
  expect(selectors).not.toContain('.workspace-page > p');
  expect(selectors).toContain('.workspace-page > p:not(.workspace-kicker)');
});
