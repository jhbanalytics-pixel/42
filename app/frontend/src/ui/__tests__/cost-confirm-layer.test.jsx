/* Design audit item 1, Release C: the credit confirm behind "Ask about this"
   on Today opened inside its own card. The card rises with ndRise, and an
   animation that holds its end state keeps a transform on the card, which
   makes the card the box a fixed backdrop is placed in. The scrim covered the
   card only and the dialog's title sat above the screen. The confirm now
   renders as a direct child of the route layer, #main-content, which keeps
   no transform once its own entrance ends, and the rises fill backwards so no
   card or lead panel keeps a transform either. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const {AskAboutThis} = await import('../AskAboutThis.jsx');

const plain = (text) => String(text || '').replace(/\s+/g, ' ').trim();

let layer = null;
let card = null;
let root = null;

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  window.history.replaceState(null, '', '#/pulse');
  layer = document.createElement('div');
  layer.id = 'main-content';
  card = document.createElement('article');
  card.className = 't42-card';
  layer.appendChild(card);
  document.body.appendChild(layer);
  root = createRoot(card);
});

afterEach(async () => {
  await act(async () => root.unmount());
  layer.remove();
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

async function until(check, label){
  const started = Date.now();
  for (;;){
    if (check()) return;
    if (Date.now() - started > 4000) throw new Error('timed out waiting for ' + label);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
  }
}

const HREF = '#/ask?q=What%20is%20behind%20this%3F&market=ZA&item=it_1&date=2026-10-01';

test('the confirm opened from a card renders on the route layer, outside the card', async () => {
  await act(async () => root.render(<AskAboutThis href={HREF} className="t42-button" question="What is behind this?" />));
  const link = card.querySelector('a');
  link.focus();
  await act(async () => { link.click(); });
  await until(() => document.querySelector('[role="dialog"]'), 'the confirm');
  const dialog = document.querySelector('[role="dialog"]');
  const scrim = dialog.parentElement;
  expect(scrim.className).toBe('w42-backdrop');
  expect(scrim.parentElement.id).toBe('main-content');
  expect(card.contains(dialog)).toBe(false);
  expect(plain(dialog.querySelector('h2').textContent)).toBe('Ask 42 about this now?');
});

test('the confirm takes focus on Cancel and hands it back to the link on Escape', async () => {
  await act(async () => root.render(<AskAboutThis href={HREF} className="t42-button" question="What is behind this?" />));
  const link = card.querySelector('a');
  link.focus();
  await act(async () => { link.click(); });
  await until(() => document.querySelector('[role="dialog"]'), 'the confirm');
  const dialog = document.querySelector('[role="dialog"]');
  expect(plain(document.activeElement.textContent)).toBe('Cancel');
  await act(async () => {
    dialog.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  });
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(document.activeElement === link).toBe(true);
});

const animations = (file) => {
  const css = postcss.parse(readFileSync(new URL(file, import.meta.url), 'utf8'));
  const found = [];
  css.walkDecls('animation', (decl) => { if (decl.value.startsWith('ndRise ')) found.push(decl.value); });
  return found;
};

test('the card, headline and lead panel rises fill backwards, so nothing keeps a transform', () => {
  const rises = [...animations('../../styles/nightdesk.css'), ...animations('../../styles/today-story.css')];
  expect(rises.length).toBe(2);
  for (const value of rises) expect(value).toBe('ndRise var(--motion-handoff) var(--motion-handoff-easing) backwards');
});
