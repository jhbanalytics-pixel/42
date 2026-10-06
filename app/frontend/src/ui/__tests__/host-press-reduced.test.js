/* The host's own reduced motion and press rules.

   The package zeroes every move under prefers-reduced-motion, but the host
   sheet carries moves of its own and a host that relies on a dependency's
   universal rule has no guarantee of its own. app.css therefore states one
   shared reduced motion block that settles every transition and animation
   at once, and one shared press state for every native control and role
   button, drawn through the press token rather than a fixed colour. Both are
   read from the sheet with postcss so a later edit that drops either fails
   here. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

const appCss = readFileSync(fileURLToPath(new URL('../../app.css', import.meta.url)), 'utf8');
const root = postcss.parse(appCss);

function sheet(path){
  return postcss.parse(readFileSync(fileURLToPath(new URL(path, import.meta.url)), 'utf8'));
}

function reducedMotionRulesOf(parsed){
  const rules = [];
  parsed.walkAtRules('media', (media) => {
    if (!/prefers-reduced-motion\s*:\s*reduce/.test(media.params)) return;
    media.walkRules((rule) => rules.push(rule));
  });
  return rules;
}

function reducedMotionRules(){
  return reducedMotionRulesOf(root);
}

const norm = (selector) => selector.replace(/\s+/g, ' ').trim();

/* The reduced-motion rules the host sheets carry beyond app.css. The package's
   own universal rule zeroes motion at runtime and hides these from any
   computed-style browser test, so each is named here: a delete or a weakening
   of the settling declaration fails by the selector's name. Each entry names
   the sheet, the selector inside prefers-reduced-motion: reduce, and the
   property that has to settle to none. */
const HOST_REDUCE_RULES = [
  {sheet: '../../tokens.css', selector: '.preveal', prop: 'animation'},
  {sheet: '../../tokens.css', selector: '.bars i, .signal .ring', prop: 'animation'},
  {sheet: '../../ui/ui.css', selector: '.dStag', prop: 'animation'},
  {sheet: '../../ui/ui.css', selector: '.ui-progress-rail-fill', prop: 'transition'},
  {sheet: '../../ui/general-intelligence.css', selector: '.general-reply :is(button, a, summary)', prop: 'transition'},
  {sheet: '../../ui/source-snapshot.css', selector: '.source-snapshot .source-snapshot__summary', prop: 'transition'},
];

function declarations(rule){
  return Object.fromEntries(rule.nodes.filter((node) => node.type === 'decl').map((node) => [node.prop, {value: node.value.trim(), important: Boolean(node.important)}]));
}

test('app.css carries one shared reduced motion rule that settles every transition and animation at once', () => {
  const shared = reducedMotionRules().find((rule) => rule.selectors.includes('*') && rule.selectors.includes('*::before') && rule.selectors.includes('*::after'));
  expect(shared, 'a universal rule inside prefers-reduced-motion: reduce').toBeDefined();
  const declared = declarations(shared);
  for (const property of ['transition-duration', 'transition-delay', 'animation-duration', 'animation-delay']){
    expect(declared[property], property).toEqual({value: '0s', important: true});
  }
  expect(declared['scroll-behavior']).toEqual({value: 'auto', important: true});
});

test('app.css gives every native control and role button a press state through the press token', () => {
  const press = [];
  root.walkRules((rule) => {
    if (rule.parent.type === 'atrule') return;
    if (/:is\(button, a, summary, \[role="button"\]\):active/.test(rule.selector)) press.push(rule);
  });
  expect(press, 'one shared press rule').toHaveLength(1);
  /* The root prefix and the disabled guard rank it above every package press
     rule that reaches the same control, as press-and-reservation reads. */
  expect(press[0].selector).toBe(':root :is(button, a, summary, [role="button"]):active:not(:disabled)');
  const declared = declarations(press[0]);
  expect(declared['box-shadow'].value).toBe('var(--press-grip)');
  expect(appCss).toMatch(/--press-grip:\s*\n?\s*inset/);
  for (const [, {value}] of Object.entries(declared)) expect(value, 'no fixed colour in the press rule').not.toMatch(/#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/i);
});

test('every host sheet reduced-motion rule settles its named selector to none', () => {
  const bySheet = new Map();
  for (const rule of HOST_REDUCE_RULES){
    if (!bySheet.has(rule.sheet)) bySheet.set(rule.sheet, reducedMotionRulesOf(sheet(rule.sheet)));
    const rules = bySheet.get(rule.sheet);
    const match = rules.find((candidate) => norm(candidate.selector) === norm(rule.selector));
    expect(match, `${rule.sheet} carries a reduced-motion rule for ${rule.selector}`).toBeDefined();
    const declared = declarations(match);
    expect(declared[rule.prop], `${rule.selector} settles ${rule.prop}`).toBeDefined();
    expect(declared[rule.prop].value, `${rule.selector} settles ${rule.prop} to none`).toBe('none');
  }
});

test('the press rule does not disable a control or move its geometry', () => {
  root.walkRules((rule) => {
    if (!/:active/.test(rule.selector)) return;
    for (const node of rule.nodes){
      if (node.type !== 'decl') continue;
      expect(['transform', 'translate', 'scale', 'top', 'left', 'margin', 'pointer-events'].includes(node.prop), `${rule.selector} sets ${node.prop}`).toBe(false);
    }
  });
});

/* Quiet register, 23 Sept 2026: the Details triangle on Fieldwork turns on
   open through a transition on ::before, and a universal selector does not
   reach pseudo-elements, so the reduced-motion rule names ::before too. */
test('Fieldwork reduced motion reaches the Details triangle', () => {
  const rules = reducedMotionRulesOf(sheet('../../styles/fieldwork.css'));
  const settles = rules.find((rule) => rule.selectors.map(norm).includes('.fieldwork-page *::before'));
  expect(settles, 'fieldwork.css settles .fieldwork-page *::before under reduced motion').toBeDefined();
  const declared = declarations(settles);
  expect(declared['transition-duration']).toEqual({value: '0s', important: true});
});

/* The triangle's turn is state, not motion: an open Details points down.
   Reduced motion takes away the transition that animates the turn and keeps
   the turn itself, so no reduced-motion rule reaching ::before overrides its
   transform, and the open rule still rotates it. */
test('under reduced motion an open Fieldwork Details triangle keeps its rotation and loses its transition', () => {
  const parsed = sheet('../../styles/fieldwork.css');
  const reaching = reducedMotionRulesOf(parsed).filter((rule) => rule.selectors.some((selector) => /::before/.test(selector)));
  expect(reaching.length).toBeGreaterThan(0);
  for (const rule of reaching){
    const declared = declarations(rule);
    expect(declared.transform, `${rule.selector} leaves the triangle's transform alone`).toBeUndefined();
    expect(declared['transition-duration']).toEqual({value: '0s', important: true});
    expect(declared['transition-delay']).toEqual({value: '0s', important: true});
  }
  let open = null;
  /* Restated 2 October 2026: 42's Fieldwork names its one disclosure
     fieldwork-method, and its chevron turns from -45deg to 45deg on open. */
  parsed.walkRules((rule) => { if (rule.parent.type === 'root' && rule.selectors.map(norm).includes('.fieldwork-method[open] > summary::before')) open = rule; });
  expect(open, 'the open triangle rule').not.toBeNull();
  expect(declarations(open).transform.value).toBe('rotate(45deg)');
});
