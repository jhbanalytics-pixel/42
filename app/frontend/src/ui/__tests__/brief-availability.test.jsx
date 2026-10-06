/* Build brief on a deployment that cannot write briefs says so first, in plain
   words, instead of walking the reader through the scan and the lens to a
   failure at the end. A scan that is switched off says what to do instead of
   printing its refusal next to a Retry that cannot help. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, mock, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
/* act drains React's own queue, so updates that land after an await do not
   depend on a scheduler an earlier file in the run may have bound. */
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const realApi = {...(await import('../../api.js'))};

const OFF = 'Cited briefs cannot be written on staging. Staging lets only Ask use the model, so the brief writer is switched off here. Ask your question in Ask to get a cited answer you can download as HTML or PDF.';
const SCAN_OFF = 'The behaviour scan is switched off on this server. Pick topics manually to choose what the brief covers.';
const reads = {availability: null, scan: null};
const posted = [];

function scanError(){
  const error = new Error(SCAN_OFF);
  error.status = 404;
  error.code = 'behaviour_scan_off';
  return error;
}

mock.module('../../api.js', () => ({
  ...realApi,
  apiGet: (path) => {
    if (path === '/api/research/availability') return reads.availability();
    if (path === '/api/research/personas') return Promise.resolve({personas: [{id: 'audience_neutral', label: 'Audience neutral', markets: ['za'], query_groups: []}]});
    return new Promise(() => {});
  },
  apiGetFresh: (path) => (String(path).startsWith('/api/research/behaviours') ? reads.scan() : new Promise(() => {})),
  apiPost: (...args) => { posted.push(args); return new Promise(() => {}); },
}));

const {ResearchPage} = await import('../../ResearchDocPanel.jsx');

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  mock.module('../../api.js', () => realApi);
  GlobalRegistrator.unregister();
});

let host = null;
let root = null;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  posted.length = 0;
  host = document.createElement('div');
  document.body.appendChild(host);
});

afterEach(() => {
  if (root) act(() => root.unmount());
  root = null;
  host.remove();
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

async function mount(props = {}){
  root = createRoot(host);
  await act(async () => {
    root.render(<ResearchPage embedded onAuth={() => {}} {...props} />);
    for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

const availability = (writing, scan) => () => Promise.resolve({
  brief_writing: writing ? {available: true, code: null, message: null} : {available: false, code: 'brief_writing_unavailable', message: OFF},
  behaviour_scan: scan ? {available: true, code: null, message: null} : {available: false, code: 'behaviour_scan_off', message: SCAN_OFF},
});

test('a deployment that cannot write briefs says so before the scan starts', async () => {
  reads.availability = availability(false, false);
  reads.scan = () => Promise.reject(scanError());
  const asked = [];
  await mount({onBuildAsk: () => asked.push(true)});
  expect(host.textContent).toContain('Cited briefs are not available here');
  expect(host.textContent).toContain(OFF);
  expect(host.textContent).not.toContain('See the behaviours first');
  expect(host.textContent).not.toContain('behaviour scan not enabled');
  expect(host.textContent).not.toContain('brief_writing_unavailable');
  const ask = [...host.querySelectorAll('button')].find((node) => node.textContent === 'Go to Ask');
  act(() => ask.click());
  expect(asked).toEqual([true]);
  expect(posted).toEqual([]);
});

test('a scan that is switched off explains the manual route without a Retry', async () => {
  reads.availability = availability(true, false);
  reads.scan = () => Promise.reject(scanError());
  await mount();
  expect(host.textContent).toContain('See the behaviours first');
  expect(host.textContent).toContain(SCAN_OFF);
  expect(host.textContent).not.toContain('behaviour_scan_off');
  const labels = [...host.querySelectorAll('button')].map((node) => node.textContent);
  expect(labels).toContain('Pick topics manually');
  expect(labels).not.toContain('Retry');
});

test('a scan that fails for another reason still offers Retry', async () => {
  reads.availability = availability(true, true);
  reads.scan = () => Promise.reject(Object.assign(new Error('Service unavailable right now.'), {status: 503}));
  await mount();
  expect(host.textContent).toContain('Service unavailable right now.');
  expect([...host.querySelectorAll('button')].map((node) => node.textContent)).toContain('Retry');
});

test('an unreadable availability record does not block the brief flow', async () => {
  reads.availability = () => Promise.reject(new Error('offline'));
  reads.scan = () => new Promise(() => {});
  await mount();
  expect(host.textContent).toContain('See the behaviours first');
  expect(host.textContent).not.toContain('Cited briefs are not available here');
});
