/* A code this page worked out for itself is announced as derived, never under
   the producer's name, and a frame that recorded no code prints none. The
   Fieldwork auth frame and the Evidence Room refusal are reached through their
   real read paths in a document, so the origin they are given is the one the
   reader sees. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {FieldworkPage, FieldworkWorkspace, buildFieldworkState} = await import('../../fieldwork.jsx');
const {EvidenceRoom} = await import('../../evidenceRoom.jsx');
const {HistoricalWorkspace} = await import('../../historicalWorkspace.jsx');
const {parseWorkspaceHash} = await import('../../router.js');

const PRODUCER = 'Reason the producer gave:';
const DERIVED = 'No producer said this; this page derived it from the records it read:';
const priorFetch = globalThis.fetch;

let host = null;
let root = null;

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  globalThis.fetch = priorFetch;
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

async function settled(predicate){
  for (let attempt = 0; attempt < 100; attempt += 1){
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error('the page did not settle: ' + (host ? host.textContent : ''));
}

function visible(markup){
  return markup.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');
}

/* Restated 2 October 2026: the retired Fieldwork read contract versions and
   printed derived and producer codes. 42's Fieldwork has no contract version.
   A payload it cannot read is the page's own reading, said in plain words with
   no code under anyone's name; a failure the service explained keeps the
   service's own words. */
test('a payload fieldwork cannot read is the page reading, said without a code', () => {
  const view = buildFieldworkState({payload: {contract_version: 'fieldwork_workspace_v9'}});
  expect(view).toEqual({state: 'error', message: 'The 42 service answered, but not with a source list this page can read.'});
  const markup = renderToStaticMarkup(<FieldworkPage payload={{contract_version: 'fieldwork_workspace_v9'}} />);
  expect(visible(markup)).toContain('not with a source list this page can read');
  expect(markup).not.toContain(PRODUCER);
  expect(markup).not.toContain(DERIVED);
});

test('a service failure keeps the words the service gave', () => {
  const view = buildFieldworkState({error: {status: 503, code: 'unavailable', message: 'The store is not answering.'}});
  expect(view.state).toBe('error');
  const markup = renderToStaticMarkup(<FieldworkPage error={{status: 503, code: 'unavailable', message: 'The store is not answering.'}} />);
  expect(visible(markup)).toContain('The store is not answering.');
  expect(markup).not.toContain('<code>');
});

test('fieldwork frames that recorded no code print no code', () => {
  for (const payload of [
    'not a payload',
    {contract_version: 'fieldwork_workspace_v1', workspace_state: 'ready'},
    {contract_version: 'fieldwork_workspace_v1', workspace_state: 'ready', source_operations: [], research_operations: null},
  ]){
    const markup = renderToStaticMarkup(<FieldworkPage payload={payload} />);
    expect(markup).toContain('data-fieldwork-state="error"');
    expect(markup, JSON.stringify(payload)).not.toContain('<code>');
    expect(markup).not.toContain('workspace_unavailable');
    expect(markup).not.toContain('The producer sent no reason');
  }
});

/* Restated 2 October 2026: a 401 from 42's API hands over to the passcode
   screen and the page says so in words, with no code. */
test('the fieldwork auth frame hands over to the passcode screen on a 401', async () => {
  globalThis.fetch = async () => ({ok: false, status: 401, url: '', headers: {get: () => null}, json: async () => ({error: 'unauthorised', message: 'Passcode needed.'})});
  let authed = 0;
  mount(<FieldworkWorkspace onAuth={() => { authed += 1; }} />);
  await settled(() => host.querySelector('[data-fieldwork-state="auth"]'));
  expect(authed).toBeGreaterThan(0);
  const text = host.textContent.replace(/\s+/g, ' ');
  expect(text).toContain('Enter the passcode to read Fieldwork.');
  expect(text).not.toContain(PRODUCER);
  expect(text).not.toContain('authentication_required');
});

test('the evidence room refusal of a route with no investigation is announced as derived', async () => {
  let reads = 0;
  globalThis.fetch = async () => { reads += 1; throw new Error('no read should be made'); };
  mount(<EvidenceRoom routeState={{}} onAuth={() => {}} />);
  await settled(() => host.textContent.includes('The investigation is unavailable.'));
  expect(reads).toBe(0);
  const text = host.textContent.replace(/\s+/g, ' ');
  expect(text).toContain('The investigation is unavailable.');
  expect(text).toContain(DERIVED + ' workspace_request_invalid');
  expect(text).not.toContain(PRODUCER);
});

test('the historical refusal of a route it cannot read is announced as derived on first render and after it settles', async () => {
  for (const [hash, code] of [
    ['#/historical/inv_case/analogue/extra', 'workspace_request_invalid'],
    ['#/historical/inv_case/diffusion', 'historical_mode_ineligible'],
  ]){
    const route = parseWorkspaceHash(hash);
    expect(route.error).toBe(code);
    const first = visible(renderToStaticMarkup(<HistoricalWorkspace route={route} onAuth={() => {}} />));
    expect(first, hash).toContain(DERIVED + ' ' + code);
    expect(first).not.toContain(PRODUCER);
    let reads = 0;
    globalThis.fetch = async () => { reads += 1; throw new Error('no read should be made'); };
    mount(<HistoricalWorkspace route={route} onAuth={() => {}} />);
    await settled(() => host.textContent.includes('Historical evidence is unavailable'));
    await new Promise((resolve) => setTimeout(resolve, 20));
    const text = host.textContent.replace(/\s+/g, ' ');
    expect(text, hash).toContain(DERIVED + ' ' + code);
    expect(text).not.toContain(PRODUCER);
    expect(reads).toBe(0);
    flushSync(() => root.unmount());
    root = null;
    host.remove();
    host = null;
  }
});
