/* Wave 8 N48 (R0290, R0305, R0297, R0362): the Ask and answer exports hand the
   browser a blob URL and click a link to it. They release that URL about a
   minute later, as the dossier export does, because some browsers lose the
   file if it goes before the download has begun. A click that throws releases
   it at once. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const transport = await import('../../askTransport42.js');
const {AnswerExport} = await import('../AnswerExport.jsx');

const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;
const realCreate = URL.createObjectURL;
const realRevoke = URL.revokeObjectURL;
const realSetTimeout = globalThis.setTimeout;
const realAnchorClick = window.HTMLAnchorElement.prototype.click;

let revoked = [];
let timers = [];
let downloads = [];
let clickThrows = false;

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  revoked = [];
  timers = [];
  downloads = [];
  clickThrows = false;
  window.localStorage.setItem('pulse_passcode', 'fixture-pass');
  URL.createObjectURL = () => 'blob:answer';
  URL.revokeObjectURL = (url) => { revoked.push(url); };
  window.HTMLAnchorElement.prototype.click = function click(){
    if (clickThrows) throw new Error('click refused');
    downloads.push(this.download);
  };
});

afterEach(() => {
  globalThis.fetch = realFetch;
  window.fetch = realWindowFetch;
  URL.createObjectURL = realCreate;
  URL.revokeObjectURL = realRevoke;
  globalThis.setTimeout = realSetTimeout;
  window.HTMLAnchorElement.prototype.click = realAnchorClick;
});

const holdTimers = () => { globalThis.setTimeout = (callback, delay) => { timers.push({callback, delay}); return timers.length; }; };
function respond(response){
  const stub = async () => response;
  globalThis.fetch = stub;
  window.fetch = stub;
}

test('the Ask export keeps its blob URL for a minute after the click', async () => {
  respond(new Response('<html>answer</html>', {status: 200, headers: {'Content-Type': 'text/html'}}));
  holdTimers();
  await transport.downloadExport('a_20260928_0000000a');
  expect(downloads).toEqual(['42-answer-a_20260928_0000000a.html']);
  expect(revoked).toEqual([]);
  expect(timers.map((timer) => timer.delay)).toEqual([60000]);
  timers[0].callback();
  expect(revoked).toEqual(['blob:answer']);
});

test('an Ask export whose click throws releases the URL at once and rethrows', async () => {
  respond(new Response('<html>answer</html>', {status: 200, headers: {'Content-Type': 'text/html'}}));
  holdTimers();
  clickThrows = true;
  const failure = await transport.downloadExport('a_20260928_0000000a').catch((error) => error);
  expect(failure.message).toBe('click refused');
  expect(revoked).toEqual(['blob:answer']);
  expect(timers).toEqual([]);
});

test('the answer download keeps its blob URL for a minute after the click', async () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  try {
    await act(async () => root.render(<AnswerExport requestId="60a5fd02-0b31-483e-bfba-263d0ba596dc" />));
    respond(new Response('<!doctype html><p>x</p>', {status: 200, headers: {'Content-Type': 'text/html'}}));
    const pressed = [...host.querySelectorAll('button')].find((node) => node.textContent === 'Download as HTML');
    await act(async () => { pressed.click(); });
    const started = Date.now();
    while (downloads.length === 0 && Date.now() - started < 4000) await act(async () => { await new Promise((resolve) => realSetTimeout(resolve, 5)); });
    expect(downloads).toHaveLength(1);
    expect(revoked).toEqual([]);
  } finally {
    await act(async () => root.unmount());
    host.remove();
    globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  }
});
