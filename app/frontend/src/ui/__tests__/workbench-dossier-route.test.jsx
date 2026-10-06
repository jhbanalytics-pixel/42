/* The workbench reads the dossier by the identities on its URL. A copied link,
   a reload, back and forward all name an investigation and an exact version,
   and the working view read is made for exactly that pair. A link without a
   version reads the current one and pins it in place, so the next reload
   reopens what was read rather than whatever is current by then. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {act} = await import('react');
const {clearDossierCache, endReviewSession, signInReviewer} = await import('../../dossierResource.js');
const {ConsoleWorkbench} = await import('../../ConsoleWorkbench.jsx');

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const V1 = '1'.repeat(64);
const V2 = '2'.repeat(64);
const priorFetch = globalThis.fetch;
let reads = [];
let tokens = [];

async function signIn(){
  const post = (path) => Promise.resolve(path.endsWith('/login')
    ? {contract_version: 'dossier_review_login_v1', nonce: 'n', csrf_token: 'login'}
    : {contract_version: 'dossier_review_session_v1', role: 'claim_approver', roles: ['claim_approver'], csrf_token: 'session-token'});
  await signInReviewer({obtainCredential: () => Promise.resolve('credential'), post});
}

function working(investigationId, version){
  return {
    contract_version: 'dossier_working_v1',
    investigation_id: investigationId,
    dossier_version: version,
    state: 'ready',
    projection: {investigation_id: investigationId, cutoff: '2026-09-01', concise_answer: null, decision: 'pending', claims: [], evidence: [], relationships: [], unanswered_questions: []},
    resource_versions: {claim: {}, relationship: {}},
    artifacts: [],
  };
}

function respond(body, status = 200){
  return Promise.resolve(new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}}));
}

let host = null;
let root = null;

beforeEach(() => {
  clearDossierCache();
  reads = [];
  tokens = [];
  globalThis.fetch = (path, init) => {
    const url = String(path);
    const match = /\/api\/internal\/v2\/investigations\/([^/]+)\/dossier\/working(?:\?dossier_version=([0-9a-f]{64}))?$/.exec(url);
    if (match){
      reads.push([match[1], match[2] || null]);
      tokens.push(init && init.headers && init.headers['X-Review-CSRF']);
      return respond(working(match[1], match[2] || V2));
    }
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
  for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

function shownVersion(){
  const node = host.querySelector('[data-dossier-version]');
  return node ? node.getAttribute('data-dossier-version') : null;
}

test('a copied deep link opens the exact version it names', async () => {
  await signIn();
  window.location.hash = '#/console?work=brief&investigation=inv_alpha&version=' + V1;
  await act(async () => { root.render(<ConsoleWorkbench region="ZA" setRegion={() => {}} session={0} onAuth={() => {}} />); });
  await settle();
  expect(reads).toEqual([['inv_alpha', V1]]);
  expect(tokens).toEqual(['session-token']);
  expect(shownVersion()).toBe(V1);
});

test('a link without a version reads the current one and pins it on the URL', async () => {
  await signIn();
  window.location.hash = '#/console?work=brief&investigation=inv_alpha';
  await act(async () => { root.render(<ConsoleWorkbench region="ZA" setRegion={() => {}} session={0} onAuth={() => {}} />); });
  await settle();
  expect(reads[0]).toEqual(['inv_alpha', null]);
  expect(window.location.hash).toContain('version=' + V2);
  expect(shownVersion()).toBe(V2);
  /* The pinned version was remembered from the current read, so pinning it
     does not read again. */
  expect(reads).toHaveLength(1);
});

test('back and forward reopen the version each entry names', async () => {
  await signIn();
  window.location.hash = '#/console?work=brief&investigation=inv_alpha&version=' + V1;
  await act(async () => { root.render(<ConsoleWorkbench region="ZA" setRegion={() => {}} session={0} onAuth={() => {}} />); });
  await settle();
  await act(async () => {
    window.location.hash = '#/console?work=brief&investigation=inv_alpha&version=' + V2;
    window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  });
  await settle();
  expect(shownVersion()).toBe(V2);
  await act(async () => {
    window.location.hash = '#/console?work=brief&investigation=inv_alpha&version=' + V1;
    window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  });
  await settle();
  expect(shownVersion()).toBe(V1);
  expect(reads).toEqual([['inv_alpha', V1], ['inv_alpha', V2]]);
});

test('without a review session the investigation asks for reviewer sign in and reads nothing', async () => {
  window.location.hash = '#/console?work=brief&investigation=inv_alpha&version=' + V1;
  await act(async () => { root.render(<ConsoleWorkbench region="ZA" setRegion={() => {}} session={0} onAuth={() => {}} />); });
  await settle();
  expect(reads).toEqual([]);
  expect(host.querySelector('[data-review-signin]')).toBeTruthy();
  expect(host.textContent).toMatch(/sign in is not set up/i);
});
