/* The dossier working sequence, driven in a real document.

   The review surface sits between an internal investigation and something a
   client will read, so two properties are pinned here rather than trusted to
   a render. Identity: the panel only ever shows the investigation and the
   exact version the URL names, and a late answer for an earlier one never
   lands on it. Exactness: every request carries the exact version and the
   exact body the contract defines, and every refusal reads as words. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, describe, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {act} = await import('react');
const {
  clearDossierCache,
  createReviewClient,
  dossierReadPath,
  useDossierResource,
} = await import('../../dossierResource.js');
const {
  artifactPrepareCommand,
  buildDossierWorkingModel,
  claimReviewCommand,
  reviewCommand,
  reviewRefusal,
  selectionCommand,
  signInRefusal,
} = await import('../../intelligenceDossierContract.js');
const {ClientReadExport, DossierReview} = await import('../DossierReview.jsx');
const {readDossier, signInReviewer, endReviewSession, currentReviewSession} = await import('../../dossierResource.js');
const {buildWorkbenchHash, parseWorkbenchRoute} = await import('../../workbenchRoute.js');

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

afterAll(() => {
  GlobalRegistrator.unregister();
});

const V1 = '1'.repeat(64);
const V2 = '2'.repeat(64);
const CLAIM_V = 'c'.repeat(64);
const LINK_V = 'd'.repeat(64);
const ART_V = 'e'.repeat(64);

function working({investigationId = 'inv_alpha', dossierVersion = V1, claims, relationships, questions, artifacts} = {}){
  return {
    contract_version: 'dossier_working_v1',
    investigation_id: investigationId,
    dossier_version: dossierVersion,
    state: 'ready',
    projection: {
      investigation_id: investigationId,
      cutoff: '2026-09-01',
      concise_answer: 'Repair tutorials are rising.',
      decision: 'pending',
      claims: claims || [
        {claim_id: 'clm_1', kind: 'observation', text: 'Repair tutorials recur across three sources.', status: 'pending', selected: true, evidence_state: 'unchecked', citations: ['rcp_1', 'rcp_2'], contradicting_evidence_ids: ['rcp_9']},
        {claim_id: 'clm_2', kind: 'interpretation', text: 'Households are stretching what they own.', status: 'approved', selected: true, evidence_state: 'ready', citations: ['rcp_1']},
        {claim_id: 'clm_3', kind: 'interpretation', text: 'A rejected reading of the same material.', status: 'rejected', selected: false, evidence_state: 'unchecked', citations: ['rcp_2']},
        {claim_id: 'clm_4', kind: 'limitation', text: 'Only two markets were read.', status: 'approved', selected: true, evidence_state: 'unchecked', citations: ['rcp_1']},
      ],
      evidence: [
        {evidence_id: 'rcp_1', client_citable: true, in_scope: true, published_at: '2026-08-01', authority: 'contextual'},
        {evidence_id: 'rcp_2', client_citable: true, in_scope: true, published_at: '2026-08-02', authority: 'contextual'},
      ],
      relationships: relationships || [
        {relationship_id: 'rel_1', parent_claim_id: 'clm_1', claim_id: 'clm_2', status: 'pending'},
      ],
      unanswered_questions: questions || [],
    },
    resource_versions: {
      claim: {clm_1: CLAIM_V, clm_2: CLAIM_V, clm_3: CLAIM_V, clm_4: CLAIM_V},
      relationship: {rel_1: LINK_V},
    },
    artifacts: artifacts || [],
  };
}

function deferred(){
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return {promise, resolve, reject};
}

let host = null;
let root = null;

beforeEach(() => {
  clearDossierCache();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
});

async function settle(){
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

function Probe({investigationId, dossierVersion, read, scope, onAuth, csrfToken = 'tok', onSessionLost}){
  const resource = useDossierResource({investigationId, dossierVersion, onAuth, read, scope, csrfToken, onSessionLost});
  return (
    <div data-status={resource.status}>
      {resource.dossier ? <p data-shown={resource.dossier.investigation_id + '@' + resource.dossier.dossier_version}>shown</p> : null}
      {resource.error ? <p data-error="">{resource.error.words}</p> : null}
    </div>
  );
}

function shown(){
  const node = host.querySelector('[data-shown]');
  return node ? node.getAttribute('data-shown') : null;
}

describe('workbench route carries the exact dossier version', () => {
  test('a deep link round trips the investigation and its version and nothing else', () => {
    const hash = buildWorkbenchHash({work: 'brief', investigationId: 'inv_alpha', dossierVersion: V1});
    expect(hash).toBe('#/console?work=brief&investigation=inv_alpha&version=' + V1);
    const parsed = parseWorkbenchRoute(hash);
    expect(parsed.investigationId).toBe('inv_alpha');
    expect(parsed.dossierVersion).toBe(V1);
    expect(parsed).not.toHaveProperty('dossier');
  });

  test('a client read link names its exact artifact version', () => {
    const hash = buildWorkbenchHash({work: 'brief', investigationId: 'inv_alpha', dossierVersion: V1, artifactId: 'art_1', artifactVersion: ART_V});
    const parsed = parseWorkbenchRoute(hash);
    expect(parsed).toMatchObject({investigationId: 'inv_alpha', dossierVersion: V1, artifactId: 'art_1', artifactVersion: ART_V});
  });

  test('a version that is not a digest is not carried', () => {
    expect(parseWorkbenchRoute('#/console?work=brief&investigation=inv_alpha&version=latest').dossierVersion).toBeUndefined();
    expect(parseWorkbenchRoute('#/console?work=brief&investigation=inv_alpha').dossierVersion).toBeUndefined();
    /* A route without versions keeps its earlier serialised shape. */
    expect(JSON.stringify(parseWorkbenchRoute('#/console?work=brief&investigation=inv_alpha'))).not.toMatch(/Version/);
  });

  test('the read path names the exact version when the route has one', () => {
    expect(dossierReadPath('inv_alpha', V1)).toBe('/api/internal/v2/investigations/inv_alpha/dossier/working?dossier_version=' + V1);
    expect(dossierReadPath('inv_alpha', null)).toBe('/api/internal/v2/investigations/inv_alpha/dossier/working');
  });
});

describe('useDossierResource', () => {
  test('a deep link reopens the same version through the read', async () => {
    const calls = [];
    const read = ({investigationId, dossierVersion}) => { calls.push([investigationId, dossierVersion]); return Promise.resolve(working({dossierVersion})); };
    const route = parseWorkbenchRoute('#/console?work=brief&investigation=inv_alpha&version=' + V2);
    await act(async () => { root.render(<Probe investigationId={route.investigationId} dossierVersion={route.dossierVersion} read={read} />); });
    await settle();
    expect(calls).toEqual([['inv_alpha', V2]]);
    expect(shown()).toBe('inv_alpha@' + V2);
  });

  test('changing identity aborts the superseded read and clears the previous content at once', async () => {
    const first = deferred();
    const signals = [];
    const read = ({investigationId, signal}) => {
      signals.push(signal);
      return investigationId === 'inv_alpha' ? Promise.resolve(working()) : first.promise;
    };
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={read} />); });
    await settle();
    expect(shown()).toBe('inv_alpha@' + V1);

    const pending = deferred();
    const read2 = ({investigationId, signal}) => { signals.push(signal); return investigationId === 'inv_beta' ? pending.promise : Promise.resolve(working()); };
    await act(async () => { root.render(<Probe investigationId="inv_beta" dossierVersion={V2} read={read2} />); });
    /* The previous investigation is gone on the very render that changed
       identity, before any read has answered. */
    expect(shown()).toBeNull();
    expect(host.querySelector('[data-status]').getAttribute('data-status')).toBe('loading');

    const third = deferred();
    const read3 = ({signal}) => { signals.push(signal); return third.promise; };
    await act(async () => { root.render(<Probe investigationId="inv_gamma" dossierVersion={V1} read={read3} />); });
    expect(signals.at(-2).aborted).toBe(true);
    expect(signals.at(-1).aborted).toBe(false);
  });

  test('a late answer for a previous investigation never overwrites the current panel', async () => {
    const late = deferred();
    const current = deferred();
    const read = ({investigationId}) => (investigationId === 'inv_alpha' ? late.promise : current.promise);
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={read} />); });
    await act(async () => { root.render(<Probe investigationId="inv_beta" dossierVersion={V2} read={read} />); });
    await act(async () => { current.resolve(working({investigationId: 'inv_beta', dossierVersion: V2})); });
    await settle();
    expect(shown()).toBe('inv_beta@' + V2);
    await act(async () => { late.resolve(working({investigationId: 'inv_alpha', dossierVersion: V1})); });
    await settle();
    expect(shown()).toBe('inv_beta@' + V2);
  });

  test('an answer that names another investigation or version is refused in words', async () => {
    const read = () => Promise.resolve(working({investigationId: 'inv_other', dossierVersion: V1}));
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={read} />); });
    await settle();
    expect(shown()).toBeNull();
    expect(host.querySelector('[data-status]').getAttribute('data-status')).toBe('error');
    expect(host.textContent).toMatch(/does not match/i);

    const wrongVersion = () => Promise.resolve(working({dossierVersion: V2}));
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={wrongVersion} scope="other" />); });
    await settle();
    expect(shown()).toBeNull();
  });

  test('an exact version is cached by scope, id and version; a new scope reads again', async () => {
    let count = 0;
    const read = ({dossierVersion}) => { count += 1; return Promise.resolve(working({dossierVersion})); };
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={read} scope="s1" />); });
    await settle();
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V2} read={read} scope="s1" />); });
    await settle();
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={read} scope="s1" />); });
    await settle();
    expect(count).toBe(2);
    expect(shown()).toBe('inv_alpha@' + V1);
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={read} scope="s2" />); });
    await settle();
    expect(count).toBe(3);
  });

  test('a passcode refusal hands over to the gate', async () => {
    let asked = 0;
    const read = () => Promise.reject(Object.assign(new Error('Passcode required'), {auth: true, status: 401}));
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={read} onAuth={() => { asked += 1; }} />); });
    await settle();
    expect(asked).toBe(1);
    expect(host.querySelector('[data-status]').getAttribute('data-status')).toBe('auth');
  });
});

describe('request bodies are exactly the contract', () => {
  test('selection', () => {
    expect(selectionCommand({dossierVersion: V1, selectedClaimIds: ['clm_2', 'clm_1', 'clm_2']})).toEqual({
      contract_version: 'dossier_review_v1',
      expected_dossier_version: V1,
      selected_claim_ids: ['clm_2', 'clm_1'],
    });
  });

  test('claim review carries a verdict, receipts and a note', () => {
    const body = claimReviewCommand({dossierVersion: V1, claimId: 'clm_1', claimVersion: CLAIM_V, action: 'approve', expectedState: 'pending_review', idempotencyKey: 'k1', verdict: 'partial', receiptIds: ['rcp_1'], note: 'Two sources agree.'});
    expect(body).toEqual({
      contract_version: 'dossier_review_v1',
      dossier_version: V1,
      resource: 'claim',
      resource_id: 'clm_1',
      resource_version: CLAIM_V,
      action: 'approve',
      expected_state: 'pending_review',
      idempotency_key: 'k1',
      support_review: {verdict: 'partial', receipt_ids: ['rcp_1'], note: 'Two sources agree.'},
    });
    expect(Object.keys(body)).toEqual(['contract_version', 'dossier_version', 'resource', 'resource_id', 'resource_version', 'action', 'expected_state', 'idempotency_key', 'support_review']);
  });

  test('a claim review without a verdict, receipts or a note is refused before it is sent', () => {
    const base = {dossierVersion: V1, claimId: 'clm_1', claimVersion: CLAIM_V, action: 'approve', expectedState: 'pending_review', idempotencyKey: 'k1'};
    expect(() => claimReviewCommand({...base, verdict: 'maybe', receiptIds: ['rcp_1'], note: 'n'})).toThrow(/verdict/i);
    expect(() => claimReviewCommand({...base, verdict: 'supported', receiptIds: [], note: 'n'})).toThrow(/receipt/i);
    expect(() => claimReviewCommand({...base, verdict: 'supported', receiptIds: ['rcp_1'], note: '  '})).toThrow(/note/i);
  });

  test('relationship and artifact reviews carry no support review', () => {
    expect(reviewCommand({dossierVersion: V1, resource: 'relationship', resourceId: 'rel_1', resourceVersion: LINK_V, action: 'reject', expectedState: 'pending_review', idempotencyKey: 'k2'})).toEqual({
      contract_version: 'dossier_review_v1', dossier_version: V1, resource: 'relationship', resource_id: 'rel_1', resource_version: LINK_V, action: 'reject', expected_state: 'pending_review', idempotency_key: 'k2', support_review: null,
    });
    expect(() => reviewCommand({dossierVersion: V1, resource: 'claim', resourceId: 'clm_1', resourceVersion: CLAIM_V, action: 'approve', expectedState: 'pending_review', idempotencyKey: 'k3'})).toThrow();
  });

  test('artifact preparation', () => {
    expect(artifactPrepareCommand({dossierVersion: V1})).toEqual({contract_version: 'dossier_artifact_v1', dossier_version: V1, format: 'html'});
  });

  test('the review client posts to the contract paths with the session token and reads the exact artifact version', async () => {
    const posts = [];
    const post = (path, body, signal, headers) => { posts.push({path, body, headers}); return Promise.resolve({}); };
    const client = createReviewClient({post, investigationId: 'inv_alpha', csrfToken: 'tok'});
    await client.saveSelection({a: 1});
    await client.review({b: 2});
    await client.prepare({c: 3});
    await client.readArtifact({artifactId: 'art_1', artifactVersion: ART_V});
    expect(posts.map((item) => item.path)).toEqual([
      '/api/internal/v2/investigations/inv_alpha/dossier/selection',
      '/api/internal/v2/investigations/inv_alpha/dossier/review',
      '/api/internal/v2/investigations/inv_alpha/artifacts/prepare',
      '/api/v2/investigations/inv_alpha/artifacts/art_1/read?artifact_version=' + ART_V,
    ]);
    expect(posts.slice(0, 3).every((item) => item.headers && item.headers['X-Review-CSRF'] === 'tok')).toBe(true);
    expect(posts[3].body).toBeUndefined();
  });
});

describe('refusals read as words', () => {
  const codes = ['review_request_invalid', 'review_role_required', 'scope_invalid', 'review_version_conflict', 'review_authority_unavailable', 'review_storage_unavailable', 'something_new'];
  test.each(codes)('%s is written in plain words and never as the code alone', (code) => {
    const refusal = reviewRefusal(Object.assign(new Error('x'), {code, status: 400}));
    expect(refusal.words.length).toBeGreaterThan(20);
    expect(refusal.words).not.toContain(code);
    expect(refusal.words).not.toMatch(/\b[a-z]+_[a-z_]+\b/);
  });
});

function fakeApi(responses){
  const posts = [];
  const post = (path, body, signal, headers) => {
    posts.push({path, body, headers});
    const next = responses.shift();
    if (!next) return Promise.resolve({});
    return next.error ? Promise.reject(next.error) : Promise.resolve(next.value);
  };
  return {posts, post};
}

/* A reviewer who holds every role, so each step's control is open. Role
   gating has its own test below. */
const EVERY_ROLE = Object.freeze({csrfToken: 'tok', role: 'dossier_editor', roles: ['dossier_editor', 'claim_approver', 'relationship_approver', 'client_read_approver']});

function renderReview(props){
  return act(async () => {
    root.render(<DossierReview reviewSession={EVERY_ROLE} {...props} />);
  });
}

function button(text){
  return [...host.querySelectorAll('button')].find((node) => node.textContent.trim() === text);
}

async function click(node){
  await act(async () => { node.dispatchEvent(new window.MouseEvent('click', {bubbles: true})); });
  await settle();
}

async function type(node, value){
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node), 'value').set;
    setter.call(node, value);
    node.dispatchEvent(new window.Event('input', {bubbles: true}));
    node.dispatchEvent(new window.Event('change', {bubbles: true}));
  });
}

describe('the working sequence', () => {
  test('observations and interpretations are shown with their state, and contradicting evidence on request', async () => {
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api: fakeApi([])});
    expect(host.textContent).toContain('Repair tutorials recur across three sources.');
    expect(host.textContent).toContain('Households are stretching what they own.');
    expect(host.textContent).not.toContain('rcp_9');
    await click(button('Show contradicting evidence'));
    expect(host.textContent).toContain('rcp_9');
    /* Rejected and pending material is marked internal on the working view. */
    expect(host.textContent).toMatch(/Internal only/);
  });

  test('saving a selection posts the exact body, keeps limitations and moves to the new version', async () => {
    const api = fakeApi([{value: {contract_version: 'dossier_review_v1', investigation_id: 'inv_alpha', dossier_version: V2, decision_id: null, state: 'saved'}}]);
    const moved = [];
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api, onVersionChange: (version) => moved.push(version)});
    const box = host.querySelector('input[type="checkbox"][data-claim="clm_2"]');
    await click(box);
    await click(button('Save as a new version'));
    expect(api.posts).toHaveLength(1);
    expect(api.posts[0].path).toBe('/api/internal/v2/investigations/inv_alpha/dossier/selection');
    expect(api.posts[0].body).toEqual({contract_version: 'dossier_review_v1', expected_dossier_version: V1, selected_claim_ids: ['clm_1', 'clm_4']});
    expect(moved).toEqual([V2]);
    const limitation = host.querySelector('input[type="checkbox"][data-claim="clm_4"]');
    expect(limitation.disabled).toBe(true);
  });

  test('a claim review sends its verdict, receipts and note for the exact claim version', async () => {
    const api = fakeApi([{value: {contract_version: 'dossier_review_v1', investigation_id: 'inv_alpha', dossier_version: V1, decision_id: 'dec_1', state: 'approved'}}]);
    let reloaded = 0;
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => { reloaded += 1; }};
    await renderReview({investigationId: 'inv_alpha', resource, api});
    const form = host.querySelector('[data-review-claim="clm_1"]');
    await type(form.querySelector('select'), 'supported');
    await click(form.querySelector('input[type="checkbox"][value="rcp_2"]'));
    await type(form.querySelector('textarea'), 'Both receipts say it.');
    await click([...form.querySelectorAll('button')].find((node) => node.textContent.trim() === 'Approve claim'));
    expect(api.posts).toHaveLength(1);
    const body = api.posts[0].body;
    expect(api.posts[0].path).toBe('/api/internal/v2/investigations/inv_alpha/dossier/review');
    expect(body).toMatchObject({contract_version: 'dossier_review_v1', dossier_version: V1, resource: 'claim', resource_id: 'clm_1', resource_version: CLAIM_V, action: 'approve', expected_state: 'pending_review', support_review: {verdict: 'supported', receipt_ids: ['rcp_2'], note: 'Both receipts say it.'}});
    expect(typeof body.idempotency_key).toBe('string');
    expect(reloaded).toBe(1);
  });

  test('a claim review with no note is held on the page and nothing is sent', async () => {
    const api = fakeApi([]);
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api});
    const form = host.querySelector('[data-review-claim="clm_1"]');
    await type(form.querySelector('select'), 'supported');
    await click(form.querySelector('input[type="checkbox"][value="rcp_1"]'));
    await click([...form.querySelectorAll('button')].find((node) => node.textContent.trim() === 'Approve claim'));
    expect(api.posts).toHaveLength(0);
    expect(form.textContent).toMatch(/note/i);
  });

  test('a relationship is reviewed on its own with no support review', async () => {
    const api = fakeApi([{value: {contract_version: 'dossier_review_v1', investigation_id: 'inv_alpha', dossier_version: V1, decision_id: 'dec_2', state: 'approved'}}]);
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api});
    const row = host.querySelector('[data-review-relationship="rel_1"]');
    await click([...row.querySelectorAll('button')].find((node) => node.textContent.trim() === 'Approve link'));
    expect(api.posts[0].body).toMatchObject({resource: 'relationship', resource_id: 'rel_1', resource_version: LINK_V, action: 'approve', expected_state: 'pending_review', support_review: null});
  });

  test('a version conflict says a fresh approval is needed and reloads the latest version', async () => {
    const conflict = Object.assign(new Error('x'), {status: 409, code: 'review_version_conflict'});
    const api = fakeApi([{error: conflict}]);
    let latest = 0;
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api, onReloadLatest: () => { latest += 1; }});
    const row = host.querySelector('[data-review-relationship="rel_1"]');
    await click([...row.querySelectorAll('button')].find((node) => node.textContent.trim() === 'Approve link'));
    expect(latest).toBe(1);
    expect(host.querySelector('[role="alert"]').textContent).toMatch(/fresh approval/i);
  });

  test.each([
    [403, 'review_role_required', /role/i],
    [503, 'review_authority_unavailable', /nothing was recorded/i],
    [503, 'review_storage_unavailable', /nothing was recorded/i],
  ])('a %i %s refusal is shown in plain words', async (status, code, words) => {
    const api = fakeApi([{error: Object.assign(new Error('x'), {status, code})}]);
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api});
    const row = host.querySelector('[data-review-relationship="rel_1"]');
    await click([...row.querySelectorAll('button')].find((node) => node.textContent.trim() === 'Approve link'));
    const alert = host.querySelector('[role="alert"]');
    expect(alert.textContent).toMatch(words);
    const surface = alert.cloneNode(true);
    surface.querySelectorAll('details').forEach((node) => node.remove());
    expect(surface.textContent).not.toContain(code);
  });

  test('prepare, inspect the exact export, approve that version, then open the client read', async () => {
    const html = '<!doctype html><html><body><h1>Repair tutorials are rising.</h1></body></html>';
    const api = fakeApi([
      {value: {contract_version: 'dossier_artifact_v1', investigation_id: 'inv_alpha', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, state: 'pending_review'}},
      {value: {contract_version: 'dossier_artifact_v1', investigation_id: 'inv_alpha', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, format: 'html', state: 'pending_review', manifest: {}, client_read: {}, html}},
      {value: {contract_version: 'dossier_review_v1', investigation_id: 'inv_alpha', dossier_version: V1, decision_id: 'dec_3', state: 'approved'}},
    ]);
    const opened = [];
    const approvedClaims = working({
      claims: [
        {claim_id: 'clm_2', kind: 'interpretation', text: 'Households are stretching what they own.', status: 'approved', selected: true, evidence_state: 'ready', citations: ['rcp_1']},
      ],
      relationships: [],
    });
    const resource = {status: 'ready', dossier: approvedClaims, error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api, reviewSession: EVERY_ROLE, onOpenClientRead: (item) => opened.push(item)});
    await click(button('Prepare internal artifact'));
    expect(api.posts[0].body).toEqual({contract_version: 'dossier_artifact_v1', dossier_version: V1, format: 'html'});
    await click(button('Inspect exported text'));
    expect(api.posts[1].path).toBe('/api/v2/investigations/inv_alpha/artifacts/art_1/read?artifact_version=' + ART_V);
    expect(host.querySelector('pre[data-export="html"]').textContent).toBe(html);
    await click(button('Approve this exact version'));
    expect(api.posts[2].body).toMatchObject({resource: 'artifact', resource_id: 'art_1', resource_version: ART_V, action: 'approve', expected_state: 'pending_review', support_review: null, dossier_version: V1});
    await click(button('Open Client Read'));
    expect(opened).toEqual([{artifactId: 'art_1', artifactVersion: ART_V}]);
    expect(button('Download HTML')).toBeTruthy();
    expect(host.textContent).toMatch(/PDF/);
  });

  test('approval cannot be given while exported text has not been inspected', async () => {
    const api = fakeApi([
      {value: {contract_version: 'dossier_artifact_v1', investigation_id: 'inv_alpha', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, state: 'pending_review'}},
    ]);
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api});
    await click(button('Prepare internal artifact'));
    expect(button('Approve this exact version').disabled).toBe(true);
  });

  test('a blocking question or an uncitable finding holds the client ready state', async () => {
    const held = working({
      claims: [{claim_id: 'clm_2', kind: 'interpretation', text: 'Households are stretching what they own.', status: 'approved', selected: true, evidence_state: 'ready', citations: ['rcp_missing']}],
      relationships: [],
      questions: [{question: 'Is the rise seasonal?', blocking: true}],
    });
    const model = buildDossierWorkingModel(held);
    expect(model.clientReady).toBe(false);
    expect(model.holds.join(' ')).toMatch(/open question/i);
    expect(model.holds.join(' ')).toMatch(/citation/i);
    const resource = {status: 'ready', dossier: held, error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api: fakeApi([])});
    expect(host.textContent).toMatch(/Not client ready/);
    expect(host.textContent).toContain('Is the rise seasonal?');
  });

  test('after a selection change the old approved artifact is historical and the new version needs approval', async () => {
    const dossier = working({
      dossierVersion: V2,
      artifacts: [
        {artifact_id: 'art_old', artifact_version: ART_V, dossier_version: V1, state: 'approved'},
      ],
    });
    const model = buildDossierWorkingModel(dossier);
    expect(model.artifacts.historical.map((item) => item.artifactId)).toEqual(['art_old']);
    expect(model.artifacts.current).toEqual([]);
    const resource = {status: 'ready', dossier, error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api: fakeApi([])});
    const history = host.querySelector('[data-artifacts="historical"]');
    expect(history.textContent).toMatch(/earlier version/i);
    expect(history.textContent).toMatch(/does not carry over/i);
    expect(host.querySelector('[data-artifacts="current"]').textContent).toMatch(/needs approval/i);
  });

  test('without a review session the steps ask the reviewer to sign in rather than failing silently', async () => {
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => {}};
    await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={resource} api={fakeApi([])} reviewSession={null} />); });
    expect(host.textContent).toMatch(/sign in as a reviewer/i);
  });
});

describe('Client Read of one exact artifact version', () => {
  const artifact = (over = {}) => ({contract_version: 'dossier_artifact_v1', investigation_id: 'inv_alpha', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, format: 'html', state: 'approved', manifest: {}, client_read: {}, html: '<p>Approved words.</p>', ...over});

  test('an approved version is shown as its exact bytes in a sandbox that runs nothing', async () => {
    const api = fakeApi([{value: artifact()}]);
    await act(async () => { root.render(<ClientReadExport investigationId="inv_alpha" artifactId="art_1" artifactVersion={ART_V} api={api} />); });
    await settle();
    expect(api.posts[0].path).toBe('/api/v2/investigations/inv_alpha/artifacts/art_1/read?artifact_version=' + ART_V);
    const frame = host.querySelector('iframe');
    expect(frame.getAttribute('sandbox')).toBe('');
    expect(frame.getAttribute('srcdoc')).toBe('<p>Approved words.</p>');
    expect(button('Download HTML')).toBeTruthy();
  });

  test('a version that is not approved stays internal', async () => {
    await act(async () => { root.render(<ClientReadExport investigationId="inv_alpha" artifactId="art_1" artifactVersion={ART_V} api={fakeApi([{value: artifact({state: 'pending_review'})}])} />); });
    await settle();
    expect(host.querySelector('iframe')).toBeNull();
    expect(host.textContent).toMatch(/not been approved/);
  });

  test('an answer for another version is not shown', async () => {
    await act(async () => { root.render(<ClientReadExport investigationId="inv_alpha" artifactId="art_1" artifactVersion={ART_V} api={fakeApi([{value: artifact({artifact_version: 'f'.repeat(64)})}])} />); });
    await settle();
    expect(host.querySelector('iframe')).toBeNull();
    expect(host.textContent).toMatch(/not the version this link names/);
  });
});

describe('review fixes after the first review', () => {
  test('ClientReadExport without an api prop makes exactly one read and renders the export', async () => {
    const prior = globalThis.fetch;
    let count = 0;
    globalThis.fetch = () => {
      count += 1;
      return Promise.resolve(new Response(JSON.stringify({contract_version: 'dossier_artifact_v1', investigation_id: 'inv_alpha', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, format: 'html', state: 'approved', manifest: {}, client_read: {}, html: '<p>Once.</p>'}), {status: 200, headers: {'Content-Type': 'application/json'}}));
    };
    try {
      await act(async () => { root.render(<ClientReadExport investigationId="inv_alpha" artifactId="art_1" artifactVersion={ART_V} />); });
      for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
      expect(count).toBe(1);
      expect(host.querySelector('iframe')).toBeTruthy();
    } finally {
      globalThis.fetch = prior;
    }
  });

  test('DossierReview without an api prop does not rebuild its client on every render', async () => {
    const prior = globalThis.fetch;
    let count = 0;
    globalThis.fetch = () => { count += 1; return new Promise(() => {}); };
    try {
      const resource = {status: 'ready', dossier: working(), error: null, reload: () => {}};
      await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={resource} reviewSession={{csrfToken: 'tok', role: 'claim_approver'}} />); });
      for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
      expect(count).toBe(0);
    } finally {
      globalThis.fetch = prior;
    }
  });

  const newCodes = ['artifact_citation_missing', 'artifact_approval_required', 'client_purpose_review_override_refused', 'identity_assets_unavailable', 'workspace_unavailable', 'workspace_request_invalid'];
  test.each(newCodes)('%s has its own plain words', (code) => {
    const refusal = reviewRefusal(Object.assign(new Error('x'), {code, status: 409}));
    expect(refusal.code).toBe(code);
    expect(refusal.words).not.toContain(code);
    expect(refusal.words).not.toMatch(/\b[a-z]+_[a-z_]+\b/);
    expect(refusal.words).not.toBe(reviewRefusal(Object.assign(new Error('x'), {status: 409})).words);
  });

  test('the specific words say what each refusal means', () => {
    const words = (code, status = 409) => reviewRefusal(Object.assign(new Error('x'), {code, status})).words;
    expect(words('artifact_citation_missing')).toMatch(/no usable citation/i);
    expect(words('artifact_citation_missing')).toMatch(/nothing was prepared/i);
    expect(words('artifact_approval_required')).toMatch(/not approved for the client/i);
    expect(words('client_purpose_review_override_refused', 403)).toMatch(/prohibited/i);
    expect(words('client_purpose_review_override_refused', 403)).toMatch(/no role can override/i);
  });

  test('generic 403 and 409 fallbacks are neutral', () => {
    const forbidden = reviewRefusal(Object.assign(new Error('x'), {status: 403, code: 'something_else'})).words;
    const conflict = reviewRefusal(Object.assign(new Error('x'), {status: 409, code: 'something_else'})).words;
    expect(forbidden).not.toMatch(/role/i);
    expect(conflict).not.toMatch(/changed after you opened|reloaded|fresh approval/i);
    expect(forbidden).toMatch(/nothing was recorded/i);
    expect(conflict).toMatch(/nothing was recorded/i);
  });

  test('storage unavailable on prepare says nothing was prepared', () => {
    const refusal = reviewRefusal(Object.assign(new Error('x'), {status: 503, code: 'review_storage_unavailable'}), {step: 'prepare'});
    expect(refusal.words).toBe('The export could not be created, so nothing was prepared. Try again in a moment.');
  });

  test('a 409 that is not a version conflict does not reload the latest version', async () => {
    const api = fakeApi([{error: Object.assign(new Error('x'), {status: 409, code: 'artifact_citation_missing'})}]);
    let latest = 0;
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => { latest += 1; }};
    await renderReview({investigationId: 'inv_alpha', resource, api, reviewSession: EVERY_ROLE, onReloadLatest: () => { latest += 1; }});
    await click(button('Prepare internal artifact'));
    expect(latest).toBe(0);
    expect(host.querySelector('[role="alert"]').textContent).toMatch(/no usable citation/i);
  });

  test('prepare storage failure reads as nothing prepared', async () => {
    const api = fakeApi([{error: Object.assign(new Error('x'), {status: 503, code: 'review_storage_unavailable'})}]);
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api, reviewSession: EVERY_ROLE});
    await click(button('Prepare internal artifact'));
    expect(host.querySelector('[role="alert"]').textContent).toContain('The export could not be created, so nothing was prepared.');
  });

  test('after approval the artifact is read again so the approved PDF can be downloaded', async () => {
    const html = '<p>x</p>';
    const read = (over) => ({value: {contract_version: 'dossier_artifact_v1', investigation_id: 'inv_alpha', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, format: 'html', manifest: {}, client_read: {}, html, ...over}});
    const api = fakeApi([
      {value: {contract_version: 'dossier_artifact_v1', investigation_id: 'inv_alpha', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, state: 'pending_review'}},
      read({state: 'pending_review'}),
      {value: {contract_version: 'dossier_review_v1', investigation_id: 'inv_alpha', dossier_version: V1, decision_id: 'dec_3', state: 'approved'}},
      read({state: 'approved', pdf: 'JVBERi0='}),
    ]);
    const approvedClaims = working({claims: [{claim_id: 'clm_2', kind: 'interpretation', text: 'Households are stretching what they own.', status: 'approved', selected: true, evidence_state: 'ready', citations: ['rcp_1']}], relationships: []});
    const resource = {status: 'ready', dossier: approvedClaims, error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api, reviewSession: EVERY_ROLE});
    await click(button('Prepare internal artifact'));
    await click(button('Inspect exported text'));
    await click(button('Approve this exact version'));
    expect(api.posts).toHaveLength(4);
    expect(api.posts[3].path).toBe('/api/v2/investigations/inv_alpha/artifacts/art_1/read?artifact_version=' + ART_V);
    expect(button('Download PDF')).toBeTruthy();
  });

  test('without the approved PDF the page points to the Client Read', async () => {
    const api = fakeApi([
      {value: {contract_version: 'dossier_artifact_v1', investigation_id: 'inv_alpha', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, state: 'pending_review'}},
      {value: {contract_version: 'dossier_artifact_v1', investigation_id: 'inv_alpha', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, state: 'pending_review', html: '<p>x</p>'}},
      {value: {contract_version: 'dossier_review_v1', investigation_id: 'inv_alpha', dossier_version: V1, decision_id: 'dec_3', state: 'approved'}},
      {error: Object.assign(new Error('x'), {status: 503, code: 'workspace_unavailable'})},
    ]);
    const approvedClaims = working({claims: [{claim_id: 'clm_2', kind: 'interpretation', text: 'Households are stretching what they own.', status: 'approved', selected: true, evidence_state: 'ready', citations: ['rcp_1']}], relationships: []});
    const resource = {status: 'ready', dossier: approvedClaims, error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api, reviewSession: EVERY_ROLE});
    await click(button('Prepare internal artifact'));
    await click(button('Inspect exported text'));
    await click(button('Approve this exact version'));
    expect(host.textContent).toContain('Open Client Read to download the PDF.');
    expect(button('Open Client Read')).toBeTruthy();
  });

  test('an artifact read naming another investigation is not shown', async () => {
    const api = fakeApi([
      {value: {contract_version: 'dossier_artifact_v1', investigation_id: 'inv_alpha', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, state: 'pending_review'}},
      {value: {contract_version: 'dossier_artifact_v1', investigation_id: 'inv_beta', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, state: 'pending_review', html: '<p>foreign</p>'}},
    ]);
    const resource = {status: 'ready', dossier: working(), error: null, reload: () => {}};
    await renderReview({investigationId: 'inv_alpha', resource, api, reviewSession: EVERY_ROLE});
    await click(button('Prepare internal artifact'));
    await click(button('Inspect exported text'));
    expect(host.querySelector('pre[data-export="html"]')).toBeNull();
  });

  test('a reject or an unsupported verdict may rest on no receipts; a supported one may not', () => {
    const base = {dossierVersion: V1, claimId: 'clm_1', claimVersion: CLAIM_V, expectedState: 'pending_review', idempotencyKey: 'k', note: 'Nothing supports it.'};
    expect(claimReviewCommand({...base, action: 'reject', verdict: 'contradictory', receiptIds: []}).support_review.receipt_ids).toEqual([]);
    expect(claimReviewCommand({...base, action: 'approve', verdict: 'unsupported', receiptIds: []}).support_review.receipt_ids).toEqual([]);
    expect(() => claimReviewCommand({...base, action: 'approve', verdict: 'supported', receiptIds: []})).toThrow(/receipt/i);
  });

  test('the working read must name its contract', async () => {
    const read = () => Promise.resolve({...working(), contract_version: 'dossier_working_v9'});
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={read} />); });
    await settle();
    expect(shown()).toBeNull();
    expect(host.querySelector('[data-status]').getAttribute('data-status')).toBe('error');
  });

  test('the working read sends the review session token and is not made without one', async () => {
    const prior = globalThis.fetch;
    const seen = [];
    globalThis.fetch = (path, init) => { seen.push({path: String(path), headers: init && init.headers}); return new Promise(() => {}); };
    try {
      readDossier({investigationId: 'inv_alpha', dossierVersion: V1, csrfToken: 'tok'});
      expect(seen[0].headers['X-Review-CSRF']).toBe('tok');
      expect(seen[0].headers['X-Passcode']).toBeDefined();
    } finally {
      globalThis.fetch = prior;
    }
    let reads = 0;
    const read = () => { reads += 1; return Promise.resolve(working()); };
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={read} csrfToken={null} />); });
    await settle();
    expect(reads).toBe(0);
    expect(host.querySelector('[data-status]').getAttribute('data-status')).toBe('signin');
  });

  test('a 403 on the working read asks for reviewer sign in and ends the session', async () => {
    let lost = 0;
    const read = () => Promise.reject(Object.assign(new Error('x'), {status: 403, code: 'review_role_required'}));
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={read} onSessionLost={() => { lost += 1; }} />); });
    await settle();
    expect(host.querySelector('[data-status]').getAttribute('data-status')).toBe('signin');
    expect(host.textContent).toMatch(/sign in as a reviewer/i);
    expect(lost).toBe(1);
  });

  test('losing the passcode clears the cached versions', async () => {
    let count = 0;
    const good = ({dossierVersion}) => { count += 1; return Promise.resolve(working({dossierVersion})); };
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={good} />); });
    await settle();
    const denied = () => Promise.reject(Object.assign(new Error('Passcode required'), {auth: true, status: 401}));
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V2} read={denied} />); });
    await settle();
    await act(async () => { root.render(<Probe investigationId="inv_alpha" dossierVersion={V1} read={good} />); });
    await settle();
    expect(count).toBe(2);
  });

  test('the panel offers reviewer sign in in place of the working view, or says sign in is not set up', async () => {
    const resource = {status: 'signin', dossier: null, error: null, reload: () => {}};
    const flow = {post: () => new Promise(() => {}), provider: () => null, clientId: () => 'client-1'};
    await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={resource} reviewSession={null} signInConfigured signInFlow={flow} />); });
    expect(host.querySelector('[data-review-signin-button]')).toBeTruthy();
    expect(host.textContent).not.toContain('Repair tutorials');
    await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={resource} reviewSession={null} signInConfigured={false} signInFlow={flow} />); });
    expect(host.querySelector('[data-review-signin-button]')).toBeNull();
    expect(host.textContent).toMatch(/sign in is not set up/i);
  });
});

describe('the reviewer role set', () => {
  const resource = () => ({status: 'ready', dossier: working(), error: null, reload: () => {}});
  const enabled = (name) => !button(name).disabled;
  const claimButton = () => [...host.querySelector('[data-review-claim="clm_1"]').querySelectorAll('button')].find((node) => node.textContent.trim() === 'Approve claim');

  test('a claim approver can review claims and nothing else', async () => {
    await renderReview({investigationId: 'inv_alpha', resource: resource(), api: fakeApi([]), reviewSession: {csrfToken: 'tok', role: 'claim_approver', roles: ['claim_approver']}});
    expect(claimButton().disabled).toBe(false);
    expect(enabled('Approve link')).toBe(false);
    expect(enabled('Save as a new version')).toBe(false);
    expect(enabled('Prepare internal artifact')).toBe(false);
  });

  test('every role in the set opens its own steps', async () => {
    await renderReview({investigationId: 'inv_alpha', resource: resource(), api: fakeApi([]), reviewSession: {csrfToken: 'tok', role: 'dossier_editor', roles: ['dossier_editor', 'relationship_approver']}});
    expect(claimButton().disabled).toBe(true);
    expect(enabled('Approve link')).toBe(true);
    expect(enabled('Save as a new version')).toBe(true);
    expect(enabled('Prepare internal artifact')).toBe(true);
  });

  test('a client read approver alone can approve an artifact and nothing else', async () => {
    const dossier = working({dossierVersion: V1, artifacts: [{artifact_id: 'art_1', artifact_version: ART_V, dossier_version: V1, state: 'pending_review'}],
      claims: [{claim_id: 'clm_2', kind: 'interpretation', text: 'Households are stretching what they own.', status: 'approved', selected: true, evidence_state: 'ready', citations: ['rcp_1']}], relationships: []});
    const api = fakeApi([{value: {contract_version: 'dossier_artifact_v1', investigation_id: 'inv_alpha', dossier_version: V1, artifact_id: 'art_1', artifact_version: ART_V, state: 'pending_review', html: '<p>x</p>'}}]);
    await renderReview({investigationId: 'inv_alpha', resource: {status: 'ready', dossier, error: null, reload: () => {}}, api, reviewSession: {csrfToken: 'tok', role: 'client_read_approver', roles: ['client_read_approver']}});
    expect(enabled('Save as a new version')).toBe(false);
    expect(enabled('Prepare internal artifact')).toBe(false);
    await click(button('Inspect exported text'));
    expect(enabled('Approve this exact version')).toBe(true);
    expect(enabled('Reject this version')).toBe(true);
  });

  test('a session that names one role and no list holds that role', async () => {
    await renderReview({investigationId: 'inv_alpha', resource: resource(), api: fakeApi([]), reviewSession: {csrfToken: 'tok', role: 'relationship_approver'}});
    expect(enabled('Approve link')).toBe(true);
    expect(enabled('Prepare internal artifact')).toBe(false);
  });

  test('a signed in account that is not on the reviewer list is told so plainly', async () => {
    const post = (path) => (path.endsWith('/login')
      ? Promise.resolve({nonce: 'n', csrf_token: 'c'})
      : Promise.reject(Object.assign(new Error('x'), {status: 403, code: 'review_role_required'})));
    let message = '';
    try {
      await signInReviewer({obtainCredential: () => Promise.resolve('cred'), post});
    } catch (error){
      message = reviewRefusal(error).words;
    }
    expect(message).toMatch(/not enrolled as a reviewer yet/);
  });

  test('a session carries every role the server listed', async () => {
    const post = (path) => Promise.resolve(path.endsWith('/login') ? {nonce: 'n', csrf_token: 'c'} : {role: 'claim_approver', roles: ['claim_approver', 'client_read_approver'], csrf_token: 's'});
    const session = await signInReviewer({obtainCredential: () => Promise.resolve('cred'), post});
    expect(session.roles).toEqual(['claim_approver', 'client_read_approver']);
    endReviewSession();
  });
});

describe('reviewer sign in through the provider button', () => {
  function fakeProvider({promptShown = false} = {}){
    const calls = {initialize: [], renderButton: [], prompt: 0};
    const provider = {
      initialize: (options) => { calls.initialize.push(options); },
      renderButton: (container, options) => { calls.renderButton.push({container, options}); },
      prompt: (listener) => {
        calls.prompt += 1;
        if (listener) listener({isNotDisplayed: () => !promptShown, isSkippedMoment: () => false, isDismissedMoment: () => false});
      },
    };
    return {provider, calls};
  }

  function signInFlow(provider){
    const posts = [];
    const post = (path, body) => {
      posts.push({path, body});
      if (path.endsWith('/login')) return Promise.resolve({contract_version: 'dossier_review_login_v1', nonce: 'nonce-1', csrf_token: 'login-csrf', expires_in: 300});
      return Promise.resolve({contract_version: 'dossier_review_session_v1', role: 'client_read_approver', roles: ['client_read_approver'], csrf_token: 'session-csrf'});
    };
    return {posts, flow: {post, provider: () => provider, clientId: () => 'client-1'}};
  }

  afterEach(() => { endReviewSession(); });

  test('the button is rendered with the login nonce even when the prompt is not displayed, and its callback signs in', async () => {
    const {provider, calls} = fakeProvider({promptShown: false});
    const {posts, flow} = signInFlow(provider);
    const resource = {status: 'signin', dossier: null, error: null, reload: () => {}};
    await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={resource} reviewSession={null} signInConfigured signInFlow={flow} />); });
    await settle();
    expect(posts[0].path).toBe('/api/internal/v2/dossier/review/login');
    expect(calls.initialize).toHaveLength(1);
    expect(calls.initialize[0].client_id).toBe('client-1');
    expect(calls.initialize[0].nonce).toBe('nonce-1');
    expect(calls.renderButton).toHaveLength(1);
    expect(calls.renderButton[0].container).toBe(host.querySelector('[data-review-signin-button]'));
    expect(calls.renderButton[0].options).toMatchObject({type: 'standard', text: 'signin_with'});
    expect(host.querySelector('[role="alert"]')).toBeNull();
    await act(async () => { calls.initialize[0].callback({credential: 'credential-1'}); });
    await settle();
    expect(posts[1]).toEqual({path: '/api/internal/v2/dossier/review/session', body: {credential: 'credential-1', g_csrf_token: 'login-csrf'}});
    expect(currentReviewSession()).toMatchObject({csrfToken: 'session-csrf', roles: ['client_read_approver']});
  });

  test('an account not on the reviewer list is told so from the button callback', async () => {
    const {provider, calls} = fakeProvider();
    const posts = [];
    const post = (path, body) => {
      posts.push(path);
      if (path.endsWith('/login')) return Promise.resolve({nonce: 'nonce-2', csrf_token: 'c'});
      return Promise.reject(Object.assign(new Error('x'), {status: 403, code: 'review_role_required'}));
    };
    const resource = {status: 'signin', dossier: null, error: null, reload: () => {}};
    await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={resource} reviewSession={null} signInConfigured signInFlow={{post, provider: () => provider, clientId: () => 'client-1'}} />); });
    await settle();
    await act(async () => { calls.initialize[0].callback({credential: 'credential-2'}); });
    await settle();
    expect(host.querySelector('[role="alert"]').textContent).toMatch(/not enrolled as a reviewer yet/);
    expect(currentReviewSession()).toBeNull();
  });
});

describe('reviewer sign in: refresh, cleanup and refusals', () => {
  function provider(){
    const calls = {initialize: [], renderButton: 0, prompt: 0, cancel: 0};
    const value = {
      initialize: (options) => { calls.initialize.push(options); },
      renderButton: (container) => { calls.renderButton += 1; const node = document.createElement('div'); node.setAttribute('data-fake-button', String(calls.renderButton)); container.appendChild(node); },
      prompt: () => { calls.prompt += 1; },
      cancel: () => { calls.cancel += 1; },
    };
    return {value, calls};
  }

  function flowWith(fake, {logins, session, pollMs, pollLimit, providerCalls} = {}){
    const posts = [];
    let n = 0;
    const post = (path, body) => {
      posts.push({path, body});
      if (path.endsWith('/login')){
        n += 1;
        return Promise.resolve((logins && logins[n - 1]) || {nonce: 'nonce-' + n, csrf_token: 'csrf-' + n, expires_in: 300});
      }
      return session ? session() : Promise.resolve({role: 'claim_approver', roles: ['claim_approver'], csrf_token: 's'});
    };
    const flow = {post, provider: () => { if (providerCalls) providerCalls.count += 1; return fake; }, clientId: () => 'client-1', pollMs, pollLimit};
    return {posts, flow};
  }

  const signinResource = {status: 'signin', dossier: null, error: null, reload: () => {}};
  const wait = (ms) => act(async () => { await new Promise((resolve) => setTimeout(resolve, ms)); });
  afterEach(() => { endReviewSession(); });

  test('a refresh before the nonce expires re-initialises with the new nonce, clears the old button and prompts only once', async () => {
    const fake = provider();
    const {posts, flow} = flowWith(fake.value, {logins: [{nonce: 'nonce-1', csrf_token: 'csrf-1', expires_in: 60.05}, {nonce: 'nonce-2', csrf_token: 'csrf-2', expires_in: 300}]});
    await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={signinResource} reviewSession={null} signInConfigured signInFlow={flow} />); });
    await settle();
    expect(fake.calls.initialize.map((item) => item.nonce)).toEqual(['nonce-1']);
    await wait(150);
    await settle();
    expect(fake.calls.initialize.map((item) => item.nonce)).toEqual(['nonce-1', 'nonce-2']);
    expect(fake.calls.prompt).toBe(1);
    expect(fake.calls.cancel).toBeGreaterThanOrEqual(1);
    const container = host.querySelector('[data-review-signin-button]');
    expect(container.querySelectorAll('[data-fake-button]')).toHaveLength(1);
    /* The callback of the superseded run does nothing. */
    await act(async () => { fake.calls.initialize[0].callback({credential: 'stale'}); });
    await settle();
    expect(posts.filter((item) => item.path.endsWith('/session'))).toHaveLength(0);
    await act(async () => { fake.calls.initialize[1].callback({credential: 'fresh'}); });
    await settle();
    expect(posts.filter((item) => item.path.endsWith('/session')).map((item) => item.body)).toEqual([{credential: 'fresh', g_csrf_token: 'csrf-2'}]);
  });

  test('polling for the provider stops when the panel unmounts', async () => {
    const providerCalls = {count: 0};
    const fake = provider();
    let ready = false;
    const {flow} = flowWith(null, {pollMs: 10, pollLimit: 1000, providerCalls});
    flow.provider = () => { providerCalls.count += 1; return ready ? fake.value : null; };
    await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={signinResource} reviewSession={null} signInConfigured signInFlow={flow} />); });
    await wait(50);
    expect(providerCalls.count).toBeGreaterThan(1);
    flushSync(() => root.unmount());
    root = null;
    const after = providerCalls.count;
    await wait(80);
    expect(providerCalls.count).toBe(after);
    ready = true;
    expect(fake.calls.initialize).toHaveLength(0);
  });

  test('a provider that never loads is reported in words, and Start sign in again begins a fresh login', async () => {
    const fake = provider();
    let ready = false;
    const {posts, flow} = flowWith(null, {pollMs: 5, pollLimit: 3});
    flow.provider = () => (ready ? fake.value : null);
    await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={signinResource} reviewSession={null} signInConfigured signInFlow={flow} />); });
    await wait(60);
    await settle();
    expect(host.querySelector('[role="alert"]').textContent).toMatch(/did not load/);
    ready = true;
    await click(button('Start sign in again'));
    await wait(20);
    await settle();
    expect(posts.filter((item) => item.path.endsWith('/login'))).toHaveLength(2);
    expect(fake.calls.initialize.map((item) => item.nonce)).toEqual(['nonce-2']);
    expect(host.querySelector('[role="alert"]')).toBeNull();
  });

  test('a server refusal during sign in reads in sign in words', async () => {
    const fake = provider();
    const refused = Object.assign(new Error('login_expired'), {status: 400, code: 'review_request_invalid'});
    const {flow} = flowWith(fake.value, {session: () => Promise.reject(refused)});
    await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={signinResource} reviewSession={null} signInConfigured signInFlow={flow} />); });
    await settle();
    await act(async () => { fake.calls.initialize[0].callback({credential: 'c'}); });
    await settle();
    const alert = host.querySelector('[role="alert"]').textContent;
    expect(alert).toMatch(/sign in/i);
    expect(alert).toMatch(/lapsed/);
    expect(alert).not.toMatch(/dossier/i);
  });

  const reasons = [
    ['review_request_invalid', 'login_expired', /lapsed/],
    ['review_request_invalid', 'login_mismatch', /did not match/],
    ['review_request_invalid', 'csrf_mismatch', /could not be checked/],
    ['review_request_invalid', 'csrf_missing', /could not be checked/],
    ['review_request_invalid', 'credential_invalid', /could not be verified/],
    ['review_authority_unavailable', 'configuration_missing', /switched off/],
    ['review_request_invalid', 'login_unknown', /is no longer known to the desk, start sign in again/],
    ['review_role_required', 'hosted_domain_mismatch', /organisation domain/],
  ];
  test.each(reasons)('%s with %s has sign in words', (code, reason, words) => {
    const status = code === 'review_authority_unavailable' ? 503 : code === 'review_role_required' ? 403 : 400;
    const refusal = signInRefusal(Object.assign(new Error(reason), {status, code}));
    expect(refusal.words).toMatch(words);
    expect(refusal.words).toMatch(/sign in/i);
    expect(refusal.words).not.toMatch(/dossier|nothing was recorded/i);
    expect(refusal.words).not.toContain(reason);
  });
});

describe('sign in refusals outside the server checks', () => {
  test.each([
    ['a lost passcode', Object.assign(new Error('Passcode required'), {auth: true, status: 401}), /passcode/i],
    ['a network failure', new TypeError('Failed to fetch'), /^The sign in could not reach the desk\. Check the connection and start sign in again\.$/],
  ])('%s', (_name, error, words) => {
    expect(signInRefusal(error).words).toMatch(words);
  });

  test('a page own sign in sentence is kept as written', () => {
    expect(signInRefusal(new Error('The sign in service did not load. Reload the page and try again.')).words).toBe('The sign in service did not load. Reload the page and try again.');
  });

  test('a hosted domain refusal at the session step says the account is not on the organisation domain', async () => {
    const post = (path) => (path.endsWith('/login')
      ? Promise.resolve({nonce: 'n', csrf_token: 'c'})
      : Promise.reject(Object.assign(new Error('hosted_domain_mismatch'), {status: 403, code: 'review_role_required'})));
    let words = '';
    try {
      await signInReviewer({obtainCredential: () => Promise.resolve('cred'), post});
    } catch (error){
      words = signInRefusal(error).words;
    }
    expect(words).toMatch(/organisation domain/);
    expect(words).not.toMatch(/not enrolled/);
  });

  test('a short lived login refreshes at half its lifetime', async () => {
    const calls = {initialize: []};
    const fake = {initialize: (options) => calls.initialize.push(options), renderButton: () => {}, prompt: () => {}};
    let n = 0;
    const post = (path) => {
      if (!path.endsWith('/login')) return new Promise(() => {});
      n += 1;
      return Promise.resolve(n === 1 ? {nonce: 'nonce-1', csrf_token: 'c', expires_in: 0.1} : {nonce: 'nonce-2', csrf_token: 'c', expires_in: 300});
    };
    const flow = {post, provider: () => fake, clientId: () => 'client-1'};
    await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={{status: 'signin', dossier: null, error: null, reload: () => {}}} reviewSession={null} signInConfigured signInFlow={flow} />); });
    await settle();
    expect(n).toBe(1);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 120)); });
    await settle();
    expect(n).toBe(2);
    expect(calls.initialize.map((item) => item.nonce)).toEqual(['nonce-1', 'nonce-2']);
  });

  test('the provider is looked for one more time than its limit before giving up', async () => {
    let looks = 0;
    const flow = {post: () => Promise.resolve({nonce: 'n', csrf_token: 'c', expires_in: 300}), provider: () => { looks += 1; return null; }, clientId: () => 'client-1', pollMs: 2, pollLimit: 3};
    await act(async () => { root.render(<DossierReview investigationId="inv_alpha" resource={{status: 'signin', dossier: null, error: null, reload: () => {}}} reviewSession={null} signInConfigured signInFlow={flow} />); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 60)); });
    await settle();
    expect(host.querySelector('[role="alert"]').textContent).toMatch(/did not load/);
    /* One look per mount attempt plus the reset at the start of the run. */
    expect(looks).toBe(4 + 1);
  });
});
