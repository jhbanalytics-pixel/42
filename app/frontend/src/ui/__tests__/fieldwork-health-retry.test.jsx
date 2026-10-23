/* Audit W26-04: when the day's collection record could not be read, the
   roster stays, each source reads as could not be read, the tally counts
   those sources, and Try again reads the same day again. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import React from 'react';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {FieldworkPage} = await import('../../fieldwork.jsx');

const days = JSON.parse(readFileSync(new URL('./fixtures/fieldwork42_days.json', import.meta.url), 'utf8'));
const clone = (value) => JSON.parse(JSON.stringify(value));

function unreadableHealth(){
  const payload = clone(days.ready);
  payload.health_state = 'unavailable';
  payload.health_note = 'The collection record could not be read, so how each source did is unknown.';
  for (const market of payload.markets){
    const counts = {delivered: 0, partial: 0, failed: 0, no_record: 0, waiting: 0, off: 0, unavailable: 0};
    for (const source of market.sources){
      if (source.status !== 'off'){
        source.status = 'unavailable';
        source.status_words = 'Could not be read';
        source.health = null;
      }
      counts[source.status] += 1;
    }
    market.counts = counts;
  }
  return payload;
}

test('an unreadable collection record keeps the roster, counts the unread sources and offers Try again', () => {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  let retried = 0;
  const payload = unreadableHealth();
  flushSync(() => root.render(<FieldworkPage payload={payload} onRetry={() => { retried += 1; }} />));
  const words = host.textContent.replace(/\s+/g, ' ');
  expect(words).toContain('could not be read');
  expect(host.querySelector('[data-series]')).not.toBeNull();
  const unread = payload.markets[0].counts.unavailable;
  expect(unread).toBeGreaterThan(0);
  expect(host.querySelector('.fieldwork-panel__tally').textContent).toContain(unread + ' could not be read');
  const again = [...host.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Try again');
  expect(again).toBeDefined();
  flushSync(() => again.dispatchEvent(new MouseEvent('click', {bubbles: true})));
  expect(retried).toBe(1);
  flushSync(() => root.unmount());
  host.remove();
});

test('a day whose record was read shows no Try again', () => {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  flushSync(() => root.render(<FieldworkPage payload={clone(days.ready)} onRetry={() => {}} />));
  expect([...host.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Try again')).toBe(false);
  flushSync(() => root.unmount());
  host.remove();
});

/* Audit W26-02: an earlier day's roster is read from today's settings, and
   the page says so above the roster. */
test('an earlier day says its roster is today\'s collection settings', () => {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  const note = 'This roster is the collection settings as they stand today.';
  flushSync(() => root.render(<FieldworkPage payload={{...clone(days.ready), plan_note: note}} />));
  expect(host.querySelector('[data-plan-note]')?.textContent).toBe(note);
  flushSync(() => root.unmount());
  host.remove();
});
