/* Demo polish, 2 October 2026: an entrance that keeps its end state with
   fill "both" leaves an identity transform on the element after it ends.
   On #main-content that made the fixed Watch dialog backdrop sit inside the
   page instead of covering the screen, and opening it jumped the scroll. An
   entrance only needs its start state before it runs, so every entrance
   here fills backwards and leaves the element untransformed once it ends. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';

const animations = (file) => {
  const css = postcss.parse(readFileSync(new URL(file, import.meta.url), 'utf8'));
  const found = {};
  css.walkDecls('animation', (decl) => { found[decl.parent.selector] = decl.value; });
  return found;
};

test('the route entrance fills backwards, so #main-content keeps no transform', () => {
  expect(animations('../../app.css')['.route-enter']).toBe('routeIn var(--motion-layer) var(--motion-standard-easing) backwards');
});

test('dialog and disclosure entrances fill backwards', () => {
  const alerts = animations('../../styles/alerts42.css');
  expect(alerts['.w42-backdrop']).toBe('w42ScrimIn var(--motion-layer) var(--motion-standard-easing) backwards');
  expect(alerts['.w42-dialog']).toBe('w42DialogIn var(--motion-layer) var(--motion-standard-easing) backwards');
  const today = animations('../../styles/today42.css');
  expect(today['.t42-held-detail,\n.t42-posts:not([hidden]),\n.t42 details[open] > :not(summary)']).toBe('t42Reveal var(--motion-layer) var(--motion-standard-easing) backwards');
  const ask = animations('../../styles/ask42.css');
  expect(ask['.ask42-popover,\n.ask42 details[open] > :not(summary)']).toBe('ask42Reveal var(--motion-layer) var(--motion-standard-easing) backwards');
});
