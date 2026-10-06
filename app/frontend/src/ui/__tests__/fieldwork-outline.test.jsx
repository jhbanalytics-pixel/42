/* Package 2.0.21 sets the Fieldwork instrument's own heading, an h1, at the
   24px chapter step under this page's 32px title. The page used to drop its
   lead to an h2 whenever the instrument was on the page, so the outline read
   upside down: a level two title, the operations under it, and then a level
   one heading near the foot of the page. These tests read the headings a
   screen reader lists, in document order, with the level it announces. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {FieldworkPage} = await import('../../fieldwork.jsx');

const days = JSON.parse(readFileSync(new URL('./fixtures/fieldwork42_days.json', import.meta.url), 'utf8'));

let host = null;
let root = null;

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

function mount(element){
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  flushSync(() => root.render(element));
}

/* The level a reader hears: aria-level where one is set, else the tag's. */
function outline(){
  return [...host.querySelectorAll('h1, h2, h3, h4, h5, h6, [role="heading"]')].map((node) => ({
    level: Number(node.getAttribute('aria-level') || node.tagName.slice(1)),
    text: node.textContent.trim(),
  }));
}

function expectTopDown(headings){
  expect(headings[0]).toEqual({level: 1, text: 'Fieldwork'});
  expect(headings.filter((heading) => heading.level === 1)).toHaveLength(1);
  expect(headings.slice(1).every((heading) => heading.level >= 2)).toBe(true);
}

/* Restated 2 October 2026: these pinned the retired instrument's heading
   levels and its two side by side columns. The roster keeps the rule they
   guarded: one level one heading, the page's sections at level two and each
   market's source groups at level three, in reading order, after a change of
   market as well. */
test('the lead is the one level one heading and everything under it reads lower', () => {
  mount(<FieldworkPage payload={days.ready} />);
  const headings = outline();
  expectTopDown(headings);
  expect(headings.find((h) => h.text === 'Sources by market').level).toBe(2);
  expect(headings.find((h) => h.text === 'Own feeds').level).toBe(3);
  expect(headings.find((h) => h.text === 'Switched off or retired').level).toBe(2);
});

test('the outline stays top down after the reader changes market', () => {
  mount(<FieldworkPage payload={days.ready} />);
  const kenya = [...host.querySelectorAll('[role="tab"]')].find((tab) => tab.textContent.includes('Kenya'));
  expect(kenya).toBeTruthy();
  flushSync(() => kenya.click());
  expect(kenya.getAttribute('aria-selected')).toBe('true');
  expect(host.querySelector('[role="tabpanel"]').getAttribute('data-market')).toBe('KE');
  expectTopDown(outline());
});

test('arrow keys move between markets and focus follows', () => {
  mount(<FieldworkPage payload={days.ready} />);
  const tabs = () => [...host.querySelectorAll('[role="tab"]')];
  tabs()[0].focus();
  flushSync(() => tabs()[0].dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true})));
  expect(tabs()[1].getAttribute('aria-selected')).toBe('true');
  expect(document.activeElement).toBe(tabs()[1]);
  flushSync(() => tabs()[1].dispatchEvent(new KeyboardEvent('keydown', {key: 'End', bubbles: true})));
  expect(tabs()[3].getAttribute('aria-selected')).toBe('true');
  expect(tabs().filter((t) => t.tabIndex === 0)).toHaveLength(1);
});

test('a day with no record keeps the same outline', () => {
  mount(<FieldworkPage payload={days.absent} />);
  expectTopDown(outline());
});

/* The loading and error frames keep the lead's level one heading and hold
   nothing above it. */
test('the loading frame opens on the lead and holds no second level one heading', () => {
  mount(<FieldworkPage loading />);
  const headings = outline();
  expectTopDown(headings);
  expect(headings).toHaveLength(1);
});

test('the error frame names the failure one level under the lead', () => {
  mount(<FieldworkPage error={{status: 503, message: 'Not now.'}} />);
  const headings = outline();
  expectTopDown(headings);
  expect(headings[1]).toEqual({level: 2, text: 'Fieldwork could not load'});
});

test('each market panel is labelled by its own tab', () => {
  mount(<FieldworkPage payload={days.ready} />);
  const panel = host.querySelector('[role="tabpanel"]');
  const tab = host.querySelector('#' + CSS.escape(panel.getAttribute('aria-labelledby')));
  expect(tab.textContent).toContain('South Africa');
  expect(tab.getAttribute('aria-controls')).toBe(panel.id);
});
