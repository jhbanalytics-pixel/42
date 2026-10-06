/* The answer export asks the server for a copy of the stored answer by its
   request reference alone and saves what comes back. It never posts the answer
   it is showing, so the file can only hold what the store recorded. A refusal
   is shown in the server's plain words. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
/* act drains React's own queue. The scheduler module is shared by every file in
   the run and can hold the timers of a document an earlier file registered, so
   a state update after an await is flushed here rather than left to it. */
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const {AnswerExport} = await import('../AnswerExport.jsx');

const RID = '60a5fd02-0b31-483e-bfba-263d0ba596dc';
/* A ceiling for a heavily loaded machine, never a pace the tests rely on: each
   step waits for the download to settle, however long the response takes. */
const CEILING = 120000;
const realFetch = window.fetch;
const realGlobalFetch = globalThis.fetch;
const realCreate = URL.createObjectURL;
const realRevoke = URL.revokeObjectURL;
let host = null;
let root = null;
let calls = [];
let saved = [];

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(async () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  calls = [];
  saved = [];
  try { window.localStorage.setItem('pulse_passcode', 'fixture-pass'); } catch (_error) { /* no storage */ }
  URL.createObjectURL = (blob) => { saved.push(blob); return 'blob:answer'; };
  URL.revokeObjectURL = () => {};
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(<AnswerExport requestId={RID} />));
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  window.fetch = realFetch;
  globalThis.fetch = realGlobalFetch;
  URL.createObjectURL = realCreate;
  URL.revokeObjectURL = realRevoke;
});

function respond(response){
  const stub = (url, init) => { calls.push({url: String(url), init}); return Promise.resolve(response); };
  window.fetch = stub;
  globalThis.fetch = stub;
}

/* Ask redesign, 23 Sept 2026: the downloads read "Download as HTML" and
   "Download as PDF" beside the cited brief action. */
const settled = () => [...host.querySelectorAll('button')].every((node) => !node.disabled && node.getAttribute('aria-busy') !== 'true');

/* Press a button, then let the download finish: the request has gone out and
   neither button is busy any longer. Nothing counts ticks, so a slow machine or
   a slow response only makes the wait longer. */
async function press(label){
  const button = [...host.querySelectorAll('button')].find((node) => node.textContent === label);
  expect(button).toBeDefined();
  const before = calls.length;
  await act(async () => { button.click(); });
  while (calls.length === before || !settled()){
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
  }
}

test('each download asks for the stored answer by reference only, with the passcode', async () => {
  respond(new Response(new Blob(['%PDF-1.7']), {status: 200, headers: {'Content-Type': 'application/pdf'}}));
  await press('Download as PDF');
  expect(calls).toHaveLength(1);
  expect(calls[0].url).toBe('/api/chat/answer/' + RID + '/export.pdf');
  expect(calls[0].init.method || 'GET').toBe('GET');
  expect(calls[0].init.body).toBeUndefined();
  expect(calls[0].init.headers['X-Passcode']).toBe('fixture-pass');
  expect(saved).toHaveLength(1);
  respond(new Response('<!doctype html><p>x</p>', {status: 200, headers: {'Content-Type': 'text/html'}}));
  await press('Download as HTML');
  expect(calls).toHaveLength(2);
  expect(calls[1].url).toBe('/api/chat/answer/' + RID + '/export.html');
  expect(saved).toHaveLength(2);
  expect(host.querySelector('[role="alert"]')).toBeNull();
}, CEILING);

test('a refusal shows the server reason in plain words and saves nothing', async () => {
  const message = 'A PDF cannot be made on this server right now. Download the HTML copy instead; it carries the same answer and sources.';
  respond(new Response(JSON.stringify({detail: {code: 'pdf_unavailable', message}}), {status: 503, headers: {'Content-Type': 'application/json'}}));
  await press('Download as PDF');
  expect(saved).toHaveLength(0);
  expect(host.querySelector('[role="alert"]').textContent).toBe(message);
  expect(host.textContent).not.toContain('pdf_unavailable');
}, CEILING);

test('a failure without a reason is still plain', async () => {
  respond(new Response('<html>proxy</html>', {status: 502, headers: {'Content-Type': 'text/html'}}));
  await press('Download as HTML');
  expect(host.querySelector('[role="alert"]').textContent).toBe('The download did not complete. Try again in a moment.');
}, CEILING);

test('a lapsed passcode asks the reader to sign in again', async () => {
  respond(new Response('{}', {status: 401, headers: {'Content-Type': 'application/json'}}));
  await press('Download as PDF');
  expect(host.querySelector('[role="alert"]').textContent).toBe('Sign in again to download this answer.');
}, CEILING);
