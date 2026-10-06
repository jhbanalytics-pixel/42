/* One artifact version is read once. The Evidence Room and the Client Read
   panel both need the same exact read, so the page sends it once, shows one
   alert when it fails, and re-reads it once on Retry. Two reads of one
   artifact, and two alerts for one failure, would tell the reader the same
   thing twice and cost the server twice. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {act} = await import('react');
const {clearDossierCache, endReviewSession} = await import('../../dossierResource.js');
const {ConsoleWorkbench} = await import('../../ConsoleWorkbench.jsx');

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const INVESTIGATION = 'inv_shared';
const ARTIFACT = 'ra_shared';
const VERSION = 'c'.repeat(64);
const BASE = '/api/v2/investigations/' + INVESTIGATION;
const READ = BASE + '/artifacts/' + ARTIFACT + '/read?artifact_version=' + VERSION;
const LINK = '#/console?work=brief&investigation=' + INVESTIGATION + '&artifact=' + ARTIFACT + '&artifact_version=' + VERSION;
const priorFetch = globalThis.fetch;

const STATUS = {
  contract_version: 'intelligence_dossier_v1', investigation_id: INVESTIGATION,
  research_plan: {questions: ['What changed in weekend repair?'], required_source_families: ['news'], known_gaps: []},
  missing_work: [],
};
const APPROVED = {
  contract_version: 'dossier_artifact_v1', investigation_id: INVESTIGATION, dossier_version: 'd'.repeat(64),
  artifact_id: ARTIFACT, artifact_version: VERSION, format: 'client_read', state: 'approved',
  html: '<!doctype html><title>Client Read</title><p>Approved words.</p>',
};

let reads = [];
let artifactReply = null;
let statusReply = null;
let host = null;
let root = null;

function respond(body, status = 200){
  return Promise.resolve(new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}}));
}

beforeEach(() => {
  clearDossierCache();
  reads = [];
  artifactReply = () => respond(APPROVED);
  statusReply = () => respond(STATUS);
  globalThis.fetch = (path) => {
    const url = String(path);
    if (url === READ){ reads.push(url); return artifactReply(); }
    if (url === BASE + '/status') return statusReply();
    if (url === BASE + '/claims/read') return respond({claims: []});
    if (url === BASE + '/decision/read') return respond({decision: null});
    /* The rest of the console answers plainly, so any alert is about this read. */
    if (url.startsWith('/api/research/recent')) return respond({artifacts: []});
    if (url === '/api/desk') return respond({topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}});
    if (url === '/api/v2/investigations/list') return respond({contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false});
    if (url === '/api/v2/investigations/scopes') return respond({contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null});
    return respond({detail: {code: 'scope_invalid', message: 'unavailable'}}, 404);
  };
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  globalThis.fetch = priorFetch;
  endReviewSession();
  window.location.hash = '#/console';
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

async function settle(){
  for (let i = 0; i < 8; i += 1) await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

async function open(onAuth = () => {}){
  window.location.hash = LINK;
  await act(async () => { root.render(<ConsoleWorkbench region="ZA" setRegion={() => {}} session={0} onAuth={onAuth} />); });
  await settle();
}

const alerts = () => [...host.querySelectorAll('[role="alert"]')];

test('an approved version is read once and shown in the Client Read frame', async () => {
  await open();
  expect(reads).toEqual([READ]);
  expect(host.querySelector('.dossier-client-read iframe').getAttribute('srcdoc')).toBe(APPROVED.html);
  expect(alerts()).toHaveLength(0);
});

test('a failed read is sent once, shows one alert in plain words, and Retry re-reads once', async () => {
  artifactReply = () => respond({detail: {code: 'scope_invalid', message: 'unavailable'}}, 404);
  await open();
  expect(reads).toHaveLength(1);
  expect(alerts()).toHaveLength(1);
  expect(alerts()[0].textContent).toContain('Requested artifact is unavailable.');
  const retry = [...host.querySelectorAll('button')].filter((button) => button.textContent === 'Retry read');
  expect(retry).toHaveLength(1);
  await act(async () => { retry[0].click(); });
  await settle();
  expect(reads).toHaveLength(2);
  expect(alerts()).toHaveLength(1);
});

test('when the investigation itself is unavailable there is one alert and one read', async () => {
  statusReply = () => respond({detail: {code: 'investigation_store_unavailable', message: 'The investigation store did not answer.'}}, 500);
  await open();
  expect(reads).toHaveLength(1);
  expect(alerts()).toHaveLength(1);
  expect(alerts()[0].textContent).toContain('The investigation is unavailable.');
});

test('an unauthorised read is sent once and hands over to the passcode gate once', async () => {
  artifactReply = () => respond({detail: {code: 'unauthorized'}}, 401);
  let asked = 0;
  await open(() => { asked += 1; });
  expect(reads).toHaveLength(1);
  expect(asked).toBe(1);
  expect(host.querySelector('.dossier-client-read')).toBeNull();
});
