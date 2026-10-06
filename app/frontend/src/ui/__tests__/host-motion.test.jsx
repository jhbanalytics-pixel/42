/* Every host stylesheet joins the motion law.

   The addendum's motion table has five moves: a reveal at 360ms on the
   handoff easing, a ribbon draw at 900ms that belongs to the package ribbon
   alone, a layer open at 240ms, a row hover at 140ms and a control at 90ms,
   the last three on the standard easing. The package declares each as a
   token, so a host sheet never writes a duration or a curve of its own. These
   tests read every consumer stylesheet as source and hold each transition to
   a package duration paired with its easing, each animation to a reveal or a
   layer open that runs once, and every class the sheets treat as interactive
   to a hover and a focus-visible state, so a second motion language fails here
   rather than in a review. */
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

/* The durations a host sheet may name, each with the easing the table pairs
   it with. The draw and the ribbon spacing stay in the package. */
const MOVES = {
  '--motion-control': '--motion-standard-easing',
  '--motion-row': '--motion-standard-easing',
  '--motion-layer': '--motion-standard-easing',
  '--motion-handoff': '--motion-handoff-easing',
};
const RAIL_MOTION_MOVES = [
  {selector: '.rail42-more-chevron', property: 'transition', value: 'transform 160ms cubic-bezier(0.2, 0, 0.38, 0.9)'},
  {selector: '.rail42-sheet[data-motion-state=open]', property: 'animation', value: 'rail42MoreIn 180ms cubic-bezier(0, 0, 0.38, 0.9) both'},
  {selector: '.rail42-sheet[data-motion-state=closing]', property: 'animation', value: 'rail42MoreOut 120ms cubic-bezier(0.2, 0, 0.38, 0.9) both'},
];
/* Motion pass, 3 October 2026: restated for Albert's ask for proper
   animations. Two data reveals join the law by name, like the rail's moves
   above: Today's market bar grows from zero to its share and the trend line
   draws from its first day to its last. Each runs once on the package draw
   move (the ribbon's 900ms) and the handoff easing, sits inside a
   prefers-reduced-motion: no-preference block, and its keyframes may move
   only the one property its picture needs. Everything else keeps the law. */
const DATA_REVEALS = [
  {sheet: 'styles/today42.css', selector: '.t42 .t42-glance-fill', property: 'animation', value: 't42BarGrow var(--motion-draw) var(--motion-handoff-easing) both', keyframes: 't42BarGrow', may: {transform: /^scaleX\(0\)$/}},
  {sheet: 'styles/today42.css', selector: '.t42 .t42-spark .t42-line', property: 'animation', value: 't42LineDraw var(--motion-draw) var(--motion-handoff-easing) both', keyframes: 't42LineDraw', may: {'stroke-dashoffset': /^[01]$/}},
  /* Charts, 3 October 2026: the shared chart forms (ui/Charts42.jsx) take
     the same two reveals, so every bar grows and every line draws alike. */
  {sheet: 'styles/charts42.css', selector: '.ch42 .ch42-bar-fill', property: 'animation', value: 'ch42BarGrow var(--motion-draw) var(--motion-handoff-easing) both', keyframes: 'ch42BarGrow', may: {transform: /^scaleX\(0\)$/}},
  {sheet: 'styles/charts42.css', selector: '.ch42 .ch42-line', property: 'animation', value: 'ch42LineDraw var(--motion-draw) var(--motion-handoff-easing) both', keyframes: 'ch42LineDraw', may: {'stroke-dashoffset': /^[01]$/}},
];
/* Research view, 4 October 2026: restated for Albert's ask for a striking
   research-in-progress view in Ask. Its live loops join the law by name, like
   the rail's moves and the data reveals above: each runs only while research
   is in progress, sits inside a prefers-reduced-motion: no-preference block,
   and its keyframes move only opacity or a vertical translate (the keyframe
   check below still applies). No other host rule may loop. */
const LIVE_RESEARCH_LOOPS = [
  {sheet: 'styles/ask42.css', selector: '.ask42-scan-field', property: 'animation', value: 'ask42GridPulse 3.2s ease-in-out infinite'},
  {sheet: 'styles/ask42.css', selector: '.ask42-scan-sweep', property: 'animation', value: 'ask42Sweep 2.8s cubic-bezier(0.45, 0, 0.25, 1) infinite'},
  {sheet: 'styles/ask42.css', selector: '.ask42-scan-beacon', property: 'animation', value: 'ask42Beacon 1.2s ease-in-out infinite'},
  {sheet: 'styles/ask42.css', selector: '.ask42-scan-cells > span', property: 'animation', value: 'ask42Cell 1.2s ease-in-out infinite'},
];
for (const token of ['--motion-draw', ...Object.keys(MOVES), ...new Set(Object.values(MOVES))]){
  if (!new RegExp(`${token}\\s*:`).test(packageCss)) throw new Error(`${token} is not a package token`);
}

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

const SHEETS = stylesheets(sourceRoot).map((path) => ({
  sheet: relative(sourceRoot, path).replace(/\\/g, '/'),
  root: postcss.parse(readFileSync(path, 'utf8')),
}));

function where(sheet, node){
  const owner = node.type === 'decl' ? node.parent : node;
  const name = owner.selector || `@${owner.name} ${owner.params}`;
  return `${sheet}:${node.source.start.line} ${name.replace(/\s+/g, ' ')}`;
}

/* One layer of a transition or animation shorthand, split on top level commas. */
function layers(value){
  return value.split(/,(?![^(]*\))/).map((layer) => layer.trim()).filter(Boolean);
}

const TIME = /(?:^|[\s(])-?\d*\.?\d+m?s\b/;
const CURVE = /\b(?:ease|ease-in|ease-out|ease-in-out|linear|cubic-bezier\(|steps\()/;

/* A layer is on the law when its duration is one package move and its easing
   is the one that move pairs with. */
function isApprovedRailMove(sheet, declaration){
  if (DATA_REVEALS.some((move) => move.sheet === sheet && move.selector === declaration.parent.selector && move.property === declaration.prop && move.value === declaration.value.trim())) return true;
  if (LIVE_RESEARCH_LOOPS.some((move) => move.sheet === sheet && move.selector === declaration.parent.selector && move.property === declaration.prop && move.value === declaration.value.trim())) return true;
  if (sheet !== 'styles/rail42.css') return false;
  const selector = declaration.parent.selector.split(String.fromCharCode(34)).join('');
  return RAIL_MOTION_MOVES.some((move) => move.selector === selector && move.property === declaration.prop && move.value === declaration.value.trim());
}
function offLaw(layer){
  if (layer === 'none') return null;
  if (TIME.test(layer)) return 'writes a duration of its own';
  if (CURVE.test(layer)) return 'writes a curve of its own';
  const durations = [...layer.matchAll(/var\((--motion-[a-z-]+)\)/g)].map((match) => match[1]).filter((token) => !token.endsWith('-easing'));
  const easings = [...layer.matchAll(/var\((--motion-[a-z-]+-easing)\)/g)].map((match) => match[1]);
  const stray = [...layer.matchAll(/var\((--[a-z0-9-]+)\)/g)].map((match) => match[1]).find((token) => !token.startsWith('--motion-'));
  if (stray) return `${stray} is not a package motion token`;
  if (durations.length !== 1) return 'names no package move';
  if (!MOVES[durations[0]]) return `${durations[0]} is not a host move`;
  if (easings.length !== 1 || easings[0] !== MOVES[durations[0]]) return `${durations[0]} runs on ${easings[0] || 'no easing'}, the table pairs it with ${MOVES[durations[0]]}`;
  return null;
}

/* Inside a reduced motion block a sheet may zero a longhand duration or
   delay; that is the addendum's "final geometry at once", not a move. */
function reducedMotionZero(declaration){
  let node = declaration.parent;
  while (node && node.type !== 'root'){
    if (node.type === 'atrule' && /prefers-reduced-motion/.test(node.params)) return /^(?:0s|0ms|0\.001ms)$/.test(declaration.value.trim());
    node = node.parent;
  }
  return false;
}

test('every transition in every host sheet runs at a package move on its easing', () => {
  const findings = [];
  for (const {sheet, root} of SHEETS){
    root.walkDecls(/^transition(?:-[a-z-]+)?$/, (declaration) => {
      if (isApprovedRailMove(sheet, declaration)) return;
      const value = declaration.value.trim();
      if (declaration.prop !== 'transition'){
        if (reducedMotionZero(declaration)) return;
        findings.push(`${where(sheet, declaration)}: ${declaration.prop} is a longhand; the shorthand names the move and its easing together`);
        return;
      }
      for (const layer of layers(value)){
        const reason = offLaw(layer);
        if (reason) findings.push(`${where(sheet, declaration)}: ${layer} ${reason}`);
      }
    });
  }
  expect(findings).toEqual([]);
});

test('More uses only the approved scoped chevron and panel motions', () => {
  const rail = SHEETS.find(({sheet}) => sheet === 'styles/rail42.css');
  for (const move of RAIL_MOTION_MOVES){
    const values = [];
    rail.root.walkRules((rule) => {
      const normalized = rule.selector.split(String.fromCharCode(34)).join('');
      if (normalized !== move.selector) return;
      let parent = rule.parent;
      while (parent && parent.type !== 'root'){
        if (parent.type === 'atrule' && parent.name === 'media' && parent.params.includes('prefers-reduced-motion')) return;
        parent = parent.parent;
      }
      rule.walkDecls(move.property, (declaration) => values.push(declaration.value.trim()));
    });
    expect(values, move.selector).toEqual([move.value]);
  }
});

test('each data reveal is declared once, only where the reader has not asked for less motion', () => {
  for (const move of DATA_REVEALS){
    const {root} = SHEETS.find(({sheet}) => sheet === move.sheet);
    const found = [];
    root.walkDecls(move.property, (declaration) => {
      if (declaration.parent.selector !== move.selector) return;
      const media = declaration.parent.parent;
      found.push({value: declaration.value.trim(), guarded: media.type === 'atrule' && /prefers-reduced-motion:\s*no-preference/.test(media.params)});
    });
    expect(found, move.selector).toEqual([{value: move.value, guarded: true}]);
  }
});

test('each live research loop is declared once, only where the reader has not asked for less motion', () => {
  for (const move of LIVE_RESEARCH_LOOPS){
    const {root} = SHEETS.find(({sheet}) => sheet === move.sheet);
    const found = [];
    root.walkDecls(move.property, (declaration) => {
      if (declaration.parent.selector !== move.selector) return;
      const media = declaration.parent.parent;
      found.push({value: declaration.value.trim(), guarded: media.type === 'atrule' && /prefers-reduced-motion:\s*no-preference/.test(media.params)});
    });
    expect(found, move.selector).toEqual([{value: move.value, guarded: true}]);
  }
});

/* A reveal moves opacity and a vertical translate to rest; a layer open moves
   opacity and the layer's geometry. Nothing else is a keyframe a host owns. */
const REVEAL_PROPERTIES = new Set(['opacity', 'transform']);

test('every animation in every host sheet is a reveal or a layer open that runs once', () => {
  const findings = [];
  for (const {sheet, root} of SHEETS){
    root.walkAtRules('keyframes', (keyframes) => {
      const reveal = DATA_REVEALS.find((move) => move.sheet === sheet && move.keyframes === keyframes.params);
      keyframes.walkDecls((declaration) => {
        if (reveal){
          const may = reveal.may[declaration.prop];
          if (!may || !may.test(declaration.value.trim())) findings.push(`${where(sheet, keyframes)}: ${declaration.prop}: ${declaration.value} is not this data reveal's one move`);
          return;
        }
        if (!REVEAL_PROPERTIES.has(declaration.prop)) findings.push(`${where(sheet, keyframes)}: animates ${declaration.prop}, which is neither a reveal nor a layer open`);
        if (declaration.prop === 'transform' && /\b(?:scale|rotate|skew|translateX|translate3d)\b/.test(declaration.value)) findings.push(`${where(sheet, keyframes)}: ${declaration.value} is not a reveal or layer move`);
      });
    });
    root.walkDecls(/^animation(?:-[a-z-]+)?$/, (declaration) => {
      if (isApprovedRailMove(sheet, declaration)) return;
      const value = declaration.value.trim();
      if (/\binfinite\b/.test(value)) findings.push(`${where(sheet, declaration)}: loops`);
      if (declaration.prop !== 'animation' && reducedMotionZero(declaration)) return;
      if (declaration.prop === 'animation-delay'){
        /* Reveal delays step by 80ms across the first blocks and stop. */
        if (!/^calc\(min\(var\(--[a-z-]+, 0\), \d\) \* 80ms\)$/.test(value)) findings.push(`${where(sheet, declaration)}: ${value} is not the reveal stagger`);
        return;
      }
      if (declaration.prop !== 'animation'){
        findings.push(`${where(sheet, declaration)}: ${declaration.prop} is a longhand; the shorthand names the move and its easing together`);
        return;
      }
      for (const layer of layers(value)){
        if (layer === 'none') continue;
        const reason = offLaw(layer.replace(/^[a-zA-Z][\w-]*\s*/, ''));
        if (reason) findings.push(`${where(sheet, declaration)}: ${layer} ${reason}`);
        if (/var\(--motion-(?:control|row)\)/.test(layer)) findings.push(`${where(sheet, declaration)}: ${layer} runs at a hover speed; an animation is a reveal or a layer open`);
      }
    });
  }
  expect(findings).toEqual([]);
});

/* A class is interactive when a sheet gives it a pointer state (hover, focus,
   focus-visible, active or a pointer cursor) or styles it as a link, button or
   role button. State modifiers chained onto another class are not subjects. */
const MODIFIERS = new Set(['on', 'active', 'is-active', 'current', 'saved', 'disabled', 'open', 'hide', 'muted', 'pos', 'neg', 'a', 'b', 'primary', 'ghost', 'link', 'topic']);

function interactiveClasses(){
  const classes = new Map();
  const state = (name) => {
    if (!classes.has(name)) classes.set(name, {hover: false, focusVisible: false, interactive: false, sheets: new Set()});
    return classes.get(name);
  };
  for (const {sheet, root} of SHEETS){
    root.walkRules((rule) => {
      if (rule.parent.type === 'atrule' && rule.parent.name === 'keyframes') return;
      const pointer = rule.nodes.some((node) => node.type === 'decl' && node.prop === 'cursor' && /pointer/.test(node.value));
      for (const selector of rule.selectors){
        const compounds = selector.trim().split(/\s*[>+~]\s*|\s+/);
        compounds.forEach((compound, index) => {
          const names = [...compound.matchAll(/\.([A-Za-z_][\w-]*)/g)].map((match) => match[1]);
          if (names.length === 0) return;
          const subject = names[names.length - 1];
          if (MODIFIERS.has(subject)) return;
          const entry = state(subject);
          entry.sheets.add(sheet);
          if (/:hover/.test(compound)){ entry.hover = true; entry.interactive = true; }
          /* A wrapper around a field answers focus-within; that is its focus state. */
          if (/:focus-(?:visible|within)/.test(compound)){ entry.focusVisible = true; entry.interactive = true; }
          if (/:(?:focus|active)(?![-\w])/.test(compound)) entry.interactive = true;
          if (/^(?:a|button|summary)\./.test(compound) || /\[role="?button"?\]/.test(compound)) entry.interactive = true;
          if (pointer && index === compounds.length - 1) entry.interactive = true;
        });
      }
    });
  }
  return classes;
}

/* The row move is rule colour to red and panel to raised; the control move
   is colour and an inset rule. Neither moves geometry, so a hover or focus
   rule that translates or scales its subject is the old host language on the
   new clock and fails here. */
const MAY_MOVE = {
  /* A hidden control arriving on focus is a reveal, not a lift. */
  '.skip-link': 'arrives from off screen on focus',
};

test('no hover or focus rule in any host sheet moves geometry', () => {
  const findings = [];
  for (const {sheet, root} of SHEETS){
    root.walkRules((rule) => {
      if (!/:(?:hover|focus-visible|focus-within|focus|active)\b/.test(rule.selector)) return;
      /* A marker or underline drawn by a pseudo element may grow; the control
         itself stays where it is. */
      if (rule.selectors.every((selector) => /::?(?:before|after)\s*$/.test(selector))) return;
      if (rule.selectors.some((selector) => Object.keys(MAY_MOVE).some((held) => selector.includes(held)))) return;
      rule.walkDecls(/^(?:transform|translate|scale|rotate|top|left|margin(?:-[a-z]+)?)$/, (declaration) => {
        if (/^(?:none|0|0px|translateY\(-50%\))$/.test(declaration.value.trim())) return;
        findings.push(`${where(sheet, declaration)}: ${declaration.prop}: ${declaration.value}`);
      });
    });
  }
  expect(findings).toEqual([]);
});

test('every interactive class in the host sheets has a hover and a focus-visible state', () => {
  const findings = [];
  for (const [name, entry] of interactiveClasses()){
    if (!entry.interactive) continue;
    const missing = [];
    if (!entry.hover) missing.push(':hover');
    if (!entry.focusVisible) missing.push(':focus-visible');
    if (missing.length) findings.push(`.${name} [${[...entry.sheets].join(', ')}] has no ${missing.join(' or ')} rule`);
  }
  expect(findings).toEqual([]);
});

/* Round 4, task 26, P19. A transition is only a move when the subject
   declares one at rest: the empty state actions hovered into red text, a red
   rule and an inset rule at 0s because their base rule never named the
   control move, and the walk read the same on the recent investigation
   links. Every class whose hover or focus rule changes a colour, a rule, a
   plane or an inset rule has to carry a transition on a rule that styles it
   at rest. */
const COLOUR_MOVE = /^(?:color|background(?:-color)?|border(?:-[a-z]+)?(?:-color)?|box-shadow|outline-color|text-decoration(?:-color)?)$/;

function subjectClass(compound){
  const names = [...compound.replace(/:[a-z-]+(?:\([^)]*\))?/g, '').matchAll(/\.([A-Za-z_][\w-]*)/g)].map((match) => match[1]);
  return names.length ? names[names.length - 1] : null;
}

/* Classes that changed a colour on hover at 0s before this test existed
   were held here while they waited for their transition. Round 5, task 27
   brought the last forty-one onto the control or row move, so the list is
   empty and stays empty: no class is ever added. */
const SNAPPED_BEFORE_THIS_TEST = new Set([]);

test('every class whose pointer state changes a colour carries a transition at rest', () => {
  const moved = new Map();
  const transitioned = new Set();
  for (const {sheet, root} of SHEETS){
    root.walkRules((rule) => {
      if (rule.parent.type === 'atrule' && rule.parent.name === 'keyframes') return;
      const declaresTransition = rule.nodes.some((node) => node.type === 'decl' && node.prop === 'transition');
      const declaresMove = rule.nodes.some((node) => node.type === 'decl' && COLOUR_MOVE.test(node.prop));
      for (const selector of rule.selectors){
        const compounds = selector.trim().split(/\s*[>+~]\s*|\s+/);
        const last = compounds[compounds.length - 1];
        const subject = subjectClass(last);
        if (!subject || MODIFIERS.has(subject)) continue;
        if (/::?(?:before|after)\s*$/.test(last)) continue;
        if (/:(?:hover|focus-visible|focus-within)\b/.test(last)){
          if (declaresMove && !moved.has(subject)) moved.set(subject, `${sheet}:${rule.source.start.line}`);
        } else if (!/:[a-z-]+/.test(last.replace(/:not\([^)]*\)/g, '')) && declaresTransition){
          transitioned.add(subject);
        }
      }
    });
  }
  const findings = [...moved].filter(([subject]) => !transitioned.has(subject) && !SNAPPED_BEFORE_THIS_TEST.has(subject)).map(([subject, at]) => `.${subject} (${at}) changes a colour on hover with no transition at rest`);
  for (const subject of SNAPPED_BEFORE_THIS_TEST){
    if (transitioned.has(subject) || !moved.has(subject)) findings.push(`.${subject} no longer snaps; take it off the held list`);
  }
  expect(findings).toEqual([]);
});
