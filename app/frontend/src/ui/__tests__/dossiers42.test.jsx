/* Dossiers on the 42 API (task 3.5, screen half; contract.md section 13.2;
   EXPERIENCE.md, Dossiers). A draft assembles itself from a finished answer,
   a reviewer keeps, orders, notes and ticks its claims, Single source and
   Inferred claims need a tick, and Freeze is the one red action. A frozen
   version exports as HTML and PDF and opens read-only from its share link.
   The server owns every claim: the client only ever sends keep, order,
   title and notes, never claim content. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import completeRecord from './fixtures/ask42_complete.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const api = await import('../../api42.js');
const {DossiersPage, DossierPage, SharedDossier} = await import('../../dossiers42.jsx');

const CEILING = 4000;
const realFetch = globalThis.fetch;
const realCreate = URL.createObjectURL;
const realRevoke = URL.revokeObjectURL;
const realAnchorClick = window.HTMLAnchorElement.prototype.click;

const clone = (value) => JSON.parse(JSON.stringify(value));
const plain = (text) => String(text || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();

const answer = completeRecord.answer;
const [c1, c2] = answer.claims;
const C3_TEXT = 'One Cape Town poster says the family pot is the whole point of the format.';

function claimOf(claim, extra = {}){
  const {id, ...rest} = claim;
  return {claim_id: id, evidence_ids: [], quotes: [], numbers: [], ...clone(rest), kept: true, note: null, ...extra};
}

/* Version 1 of a draft built from the complete fixture answer, as
   GET /api/dossiers/{id} returns it: the body plus ticks and needs_tick. */
function draft(){
  return {
    dossier_id: 'd_fixture01', version: 1, state: 'draft', created_at: '2026-09-28T06:12:00+02:00', who: 'passcode',
    title: completeRecord.question, source: {ask_id: completeRecord.ask_id}, source_ask_id: completeRecord.ask_id,
    question: completeRecord.question, market: 'ZA', as_of: answer.as_of, answer_status: 'complete',
    summary: answer.short_answer,
    claims: [
      claimOf(c1),
      claimOf(c2),
      claimOf({id: 'c3', text: C3_TEXT, label: 'single_source', kind: 'observation', evidence_ids: ['x_fixture_2'], quotes: [{evidence_id: 'x_fixture_2', text: 'just the family pot'}]}),
    ],
    evidence: clone(answer.evidence),
    gaps: clone(answer.gaps),
    left_out: [],
    content_hash: 'sha256:' + 'a'.repeat(64),
    ticks: {},
    needs_tick: [
      {claim_id: 'c2', reason: 'Inferred claims need a tick before freezing'},
      {claim_id: 'c3', reason: 'Single source claims need a tick before freezing'},
    ],
  };
}

function frozen(){
  const body = draft();
  return {
    ...body, version: 2, state: 'frozen', frozen_from: 1, created_at: '2026-09-28T06:15:00+02:00',
    ticks: {c2: {ticked: true, note: null, who: 'passcode', at: '2026-09-28T06:13:00+02:00'}, c3: {ticked: true, note: null, who: 'passcode', at: '2026-09-28T06:14:00+02:00'}},
    reviews: {c2: {ticked: true, note: null, at: '2026-09-28T06:13:00+02:00'}, c3: {ticked: true, note: null, at: '2026-09-28T06:14:00+02:00'}},
    needs_tick: [],
  };
}

let host = null;
let root = null;
let calls = [];
let saved = [];
let downloads = [];

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  calls = [];
  saved = [];
  downloads = [];
  window.localStorage.setItem('pulse_passcode', 'fixture-pass');
  window.history.replaceState(null, '', '#/dossiers');
  URL.createObjectURL = (blob) => { saved.push(blob); return 'blob:dossier'; };
  URL.revokeObjectURL = () => {};
  window.HTMLAnchorElement.prototype.click = function click(){ downloads.push({href: this.href, download: this.download}); };
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  globalThis.fetch = realFetch;
  window.fetch = realFetch;
  URL.createObjectURL = realCreate;
  URL.revokeObjectURL = realRevoke;
  window.HTMLAnchorElement.prototype.click = realAnchorClick;
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

const json = (status, body) => ({ok: status >= 200 && status < 300, status, headers: {get: () => 'application/json'}, json: async () => body});

/* Answers fetch from a route table: [method, path or RegExp, reply]. A reply
   may be a function of (path, body). Every call is recorded with its parsed
   body. */
function serve(routes){
  const stub = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    const body = init.body ? JSON.parse(init.body) : null;
    calls.push({path, method, headers: init.headers || {}, body, raw: init.body || null});
    for (const [m, match, reply] of routes){
      if (m !== method) continue;
      if (typeof match === 'string' ? match !== path : !match.test(path)) continue;
      return typeof reply === 'function' ? reply(path, body) : reply;
    }
    return json(404, {error: 'not_found', message: 'No such route.'});
  };
  globalThis.fetch = stub;
  window.fetch = stub;
}

async function until(check, label){
  const started = Date.now();
  for (;;){
    let ok = false;
    try { ok = Boolean(check()); } catch (_error){ ok = false; }
    if (ok) return;
    if (Date.now() - started > CEILING) throw new Error('timed out waiting for ' + label);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
  }
}

const text = () => plain(host.textContent);
const buttons = (scope = host) => [...scope.querySelectorAll('button')];
const button = (label, scope = host) => buttons(scope).find((node) => plain(node.textContent) === label || node.getAttribute('aria-label') === label);
const claimItem = (words) => [...host.querySelectorAll('li[data-claim]')].find((node) => plain(node.textContent).includes(words));
const headerOf = (call, name) => {
  const key = Object.keys(call.headers).find((k) => k.toLowerCase() === name.toLowerCase());
  return key ? call.headers[key] : undefined;
};

function typeInto(element, value){
  const proto = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, value);
  element.dispatchEvent(new window.Event('input', {bubbles: true}));
}

async function click(node){
  await act(async () => { node.click(); });
}

async function openDraft(body = draft(), extra = []){
  serve([...extra, ['GET', '/api/dossiers/d_fixture01', json(200, body)]]);
  await act(async () => root.render(<DossierPage dossierId="d_fixture01" onAuth={() => {}} />));
  await until(() => text().includes(answer.short_answer), 'the draft');
}

/* ---------------- api42 ---------------- */

test('each dossier call goes to its contract path with its method and the passcode header', async () => {
  serve([
    ['POST', '/api/dossiers', json(201, draft())],
    ['GET', /^\/api\/dossiers\?/, json(200, {dossiers: [], next_before: null})],
    ['GET', '/api/dossiers/d_fixture01', json(200, draft())],
    ['GET', '/api/dossiers/d_fixture01/versions/2', json(200, frozen())],
    ['PUT', '/api/dossiers/d_fixture01', json(200, draft())],
    ['POST', '/api/dossiers/d_fixture01/ticks', json(201, {claim_id: 'c2', ticked: true})],
    ['POST', '/api/dossiers/d_fixture01/freeze', json(201, frozen())],
  ]);
  await api.createDossier(completeRecord.ask_id);
  await api.listDossiers({limit: 20, before: '2026-09-28T06:00:00+02:00'});
  await api.getDossier('d_fixture01');
  await api.getDossierVersion('d_fixture01', 2);
  await api.updateDossier('d_fixture01', {keep: ['c1'], order: ['c1'], title: 'T', notes: {}});
  await api.tickClaim('d_fixture01', 'c2', true);
  await api.freezeDossier('d_fixture01');
  expect(calls.map((call) => call.method + ' ' + call.path)).toEqual([
    'POST /api/dossiers',
    'GET /api/dossiers?limit=20&before=2026-09-28T06%3A00%3A00%2B02%3A00',
    'GET /api/dossiers/d_fixture01',
    'GET /api/dossiers/d_fixture01/versions/2',
    'PUT /api/dossiers/d_fixture01',
    'POST /api/dossiers/d_fixture01/ticks',
    'POST /api/dossiers/d_fixture01/freeze',
  ]);
  expect(calls[0].body).toEqual({from: {ask_id: completeRecord.ask_id}});
  expect(calls[5].body).toEqual({claim_id: 'c2', ticked: true});
  for (const call of calls) expect(headerOf(call, 'X-Passcode')).toBe('fixture-pass');
});

test('updateDossier sends keep, order, title and notes only, whatever else it is handed', async () => {
  serve([['PUT', '/api/dossiers/d_fixture01', json(200, draft())]]);
  const body = draft();
  await api.updateDossier('d_fixture01', {
    keep: ['c1', 'c3'], order: ['c3', 'c1'], title: 'Kitchen-table dance', notes: {c1: 'Lead with this'},
    claims: body.claims, summary: body.summary, evidence: body.evidence, text: c1.text, label: 'observed',
  });
  const put = calls[0];
  expect(Object.keys(put.body).sort()).toEqual(['keep', 'notes', 'order', 'title']);
  expect(put.body).toEqual({keep: ['c1', 'c3'], order: ['c3', 'c1'], title: 'Kitchen-table dance', notes: {c1: 'Lead with this'}});
  expect(put.raw).not.toContain(c1.text);
  expect(put.raw).not.toContain('observed');
});

test('an edit and a freeze carry the version they were made from as from_version', async () => {
  serve([
    ['PUT', '/api/dossiers/d_fixture01', json(200, draft())],
    ['POST', '/api/dossiers/d_fixture01/freeze', json(201, frozen())],
  ]);
  await api.updateDossier('d_fixture01', {keep: ['c1'], order: ['c1'], title: 'T', notes: {}, fromVersion: 4});
  await api.freezeDossier('d_fixture01', 4);
  expect(calls[0].body).toEqual({keep: ['c1'], order: ['c1'], title: 'T', notes: {}, from_version: 4});
  expect(calls[1].body).toEqual({from_version: 4});
});

test('a refused freeze keeps the claims the server named on the error', async () => {
  const claims = [{claim_id: 'c3', reason: 'Single source claims need a tick before freezing'}];
  serve([['POST', '/api/dossiers/d_fixture01/freeze', json(409, {error: 'not_ready', message: 'Cannot freeze yet: c3: ...', claims})]]);
  let caught = null;
  try { await api.freezeDossier('d_fixture01'); } catch (error){ caught = error; }
  expect(caught.status).toBe(409);
  expect(caught.code).toBe('not_ready');
  expect(caught.claims).toEqual(claims);
});

test('an export downloads through a blob with the passcode header and the server file name', async () => {
  serve([['GET', /\/export\?format=(html|pdf)$/, {ok: true, status: 200, headers: {get: () => 'application/pdf'}, blob: async () => new Blob(['%PDF'], {type: 'application/pdf'})}]]);
  await api.downloadDossierExport('d_fixture01', 2, 'pdf');
  expect(calls[0].path).toBe('/api/dossiers/d_fixture01/versions/2/export?format=pdf');
  expect(headerOf(calls[0], 'X-Passcode')).toBe('fixture-pass');
  expect(saved.length).toBe(1);
  expect(downloads).toEqual([{href: 'blob:dossier', download: '42-dossier-d_fixture01-v2.pdf'}]);
});

/* ---------------- the list ---------------- */

test('the list shows every dossier newest first with its state and title, each opening its review', async () => {
  const older = {dossier_id: 'd_old', version: 3, state: 'frozen', title: 'Amapiano Sundays', created_at: '2026-09-26T09:00:00+02:00', source_ask_id: 'a_1', question: 'q', market: 'ZA', kept: 2, frozen_version: 3};
  const newer = {dossier_id: 'd_new', version: 1, state: 'draft', title: 'Kitchen-table dance', created_at: '2026-09-28T06:12:00+02:00', source_ask_id: 'a_2', question: 'q', market: 'ZA', kept: 3, frozen_version: null};
  serve([['GET', /^\/api\/dossiers\?/, json(200, {dossiers: [older, newer], next_before: null})]]);
  await act(async () => root.render(<DossiersPage onAuth={() => {}} />));
  await until(() => host.querySelectorAll('li[data-dossier]').length === 2, 'the list');
  expect(host.querySelector('h1').textContent).toBe('Dossiers');
  expect(host.querySelector('section[aria-labelledby="dossiers42-empty-title"]')).toBeNull();
  const rows = [...host.querySelectorAll('li[data-dossier]')];
  expect(plain(rows[0].textContent)).toContain('Kitchen-table dance');
  expect(plain(rows[0].textContent)).toContain('Draft');
  expect(plain(rows[1].textContent)).toContain('Amapiano Sundays');
  expect(plain(rows[1].textContent)).toContain('Frozen');
  expect(rows[0].querySelector('a').getAttribute('href')).toBe('#/dossiers/d_new');
  expect(rows[1].querySelector('a').getAttribute('href')).toBe('#/dossiers/d_old');
});

/* UX pass, 3 October 2026: each page's line under its title leads with the question the page answers (pageQuestions.js). */
test('the lede says in plain words where a dossier comes from and what to do with it', async () => {
  serve([['GET', /^\/api\/dossiers\?/, json(200, {dossiers: [], next_before: null})]]);
  await act(async () => root.render(<DossiersPage onAuth={() => {}} />));
  await until(() => host.querySelector('.dossiers42-lede'), 'the lede');
  expect(plain(host.querySelector('.dossiers42-lede').textContent)).toBe('Which answers have you checked and can share? Start a dossier from any finished answer on Ask, check its claims, freeze it, then export it.');
});

test('an empty list explains how to start a dossier and links to Ask', async () => {
  serve([['GET', /^\/api\/dossiers\?/, json(200, {dossiers: [], next_before: null})]]);
  await act(async () => root.render(<DossiersPage onAuth={() => {}} />));
  await until(() => host.querySelector('section[aria-labelledby="dossiers42-empty-title"]'), 'the empty state');
  const empty = host.querySelector('section[aria-labelledby="dossiers42-empty-title"]');
  expect(plain(empty.querySelector('h2').textContent)).toBe('No dossiers yet');
  expect(plain(empty.textContent)).toContain('Ask a question, then choose Add to dossier on the answer.');
  const ask = empty.querySelector('a[href="#/ask"]');
  expect(ask).not.toBeNull();
  expect(plain(ask.textContent)).toBe('Go to Ask');
});

test('a failed read of past answers says nothing about them, and only an empty read says there are none', async () => {
  const EMPTY = 'No finished answers yet.';
  const empty = ['GET', /^\/api\/dossiers\?/, json(200, {dossiers: [], next_before: null})];
  const opened = async () => {
    await act(async () => root.render(<DossiersPage onAuth={() => {}} />));
    await until(() => host.querySelector('section[aria-labelledby="dossiers42-empty-title"]'), 'the empty state');
    await until(() => calls.some((call) => call.path.startsWith('/api/history/asks')), 'the past answers read');
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
  };
  serve([empty, ['GET', /^\/api\/history\/asks/, json(503, {error: 'unavailable', message: 'History cannot be read.'})]]);
  await opened();
  expect(text()).not.toContain(EMPTY);
  expect(host.querySelector('.dossiers42-ready')).toBeNull();

  await act(async () => root.unmount());
  root = createRoot(host);
  calls = [];
  serve([empty]);
  const stub = globalThis.fetch;
  globalThis.fetch = window.fetch = async (url, init) => (String(url).startsWith('/api/history/asks')
    ? (calls.push({path: String(url), method: 'GET'}), Promise.reject(new TypeError('Failed to fetch')))
    : stub(url, init));
  await opened();
  expect(text()).not.toContain(EMPTY);

  await act(async () => root.unmount());
  root = createRoot(host);
  calls = [];
  serve([empty, ['GET', /^\/api\/history\/asks/, json(200, {asks: [], next_before: null})]]);
  await opened();
  await until(() => text().includes(EMPTY), 'the empty past answers');
});

test('a list that could not be read offers Try again, which reads it again', async () => {
  serve([['GET', /^\/api\/dossiers\?/, json(503, {error: 'agent_unavailable', message: 'The Ask service cannot be reached.'})]]);
  await act(async () => root.render(<DossiersPage onAuth={() => {}} />));
  await until(() => [...host.querySelectorAll('button')].some((b) => b.textContent === 'Try again'), 'the retry');
  serve([['GET', /^\/api\/dossiers\?/, json(200, {dossiers: [], next_before: null})]]);
  await act(async () => [...host.querySelectorAll('button')].find((b) => b.textContent === 'Try again').click());
  await until(() => host.querySelector('section[aria-labelledby="dossiers42-empty-title"]'), 'the list read again');
});

/* ---------------- the draft review ---------------- */

test('a draft shows its title, summary and claims with their confidence words, chips and quotes', async () => {
  await openDraft();
  expect(host.querySelector('h1').textContent).toBe(completeRecord.question);
  expect(text()).toContain(answer.short_answer);
  const first = claimItem(c1.text);
  expect(plain(first.textContent)).toContain('Corroborated');
  expect(buttons(first).some((node) => plain(node.textContent).includes('@fixture_za_1'))).toBe(true);
  expect(plain(first.querySelector('blockquote').textContent)).toContain('kitchen table, one pot, everybody dances');
  expect(plain(first.textContent)).toContain('312 TikTok posts tagged #fixture in 7 days');
  expect(plain(claimItem(c2.text).textContent)).toContain('Inferred');
  expect(plain(claimItem(C3_TEXT).textContent)).toContain('Single source');
  expect(text()).toContain('No Instagram posts in the window');
});

test('Single source and Inferred claims are marked as needing a tick; the others are not', async () => {
  await openDraft();
  expect(plain(claimItem(c1.text).textContent)).not.toContain('needs a tick');
  expect(plain(claimItem(c2.text).textContent)).toContain('needs a tick');
  expect(plain(claimItem(C3_TEXT).textContent)).toContain('needs a tick');
});

test('Freeze is the one red action on a draft, and a draft offers no export', async () => {
  await openDraft();
  const primary = host.querySelectorAll('.dossiers42-primary');
  expect(primary.length).toBe(1);
  expect(plain(primary[0].textContent)).toBe('Freeze');
  expect(button('Export HTML')).toBeUndefined();
  expect(button('Export PDF')).toBeUndefined();
});

test('removing a claim sends keep, order, title and notes only, with no claim content', async () => {
  const next = draft();
  next.version = 2;
  next.claims = [next.claims[0], next.claims[2], {...next.claims[1], kept: false}];
  await openDraft(draft(), [['PUT', '/api/dossiers/d_fixture01', json(200, next)]]);
  await click(button('Remove', claimItem(c2.text)));
  await until(() => calls.some((call) => call.method === 'PUT'), 'the edit');
  const put = calls.find((call) => call.method === 'PUT');
  expect(Object.keys(put.body).sort()).toEqual(['from_version', 'keep', 'notes', 'order', 'title']);
  expect(put.body.from_version).toBe(1);
  expect(put.body.keep).toEqual(['c1', 'c3']);
  expect(put.body.order).toEqual(['c1', 'c3']);
  expect(put.body.title).toBe(completeRecord.question);
  for (const words of [c1.text, c2.text, C3_TEXT, answer.short_answer, 'corroborated', 'inferred', 'single_source', 'tt_fixture_1', 'kitchen table']){
    expect(put.raw).not.toContain(words);
  }
  await until(() => button('Keep', claimItem(c2.text)), 'the removed claim offered back');
  expect(plain(claimItem(c2.text).textContent)).not.toContain('needs a tick');
});

test('moving a claim down sends the new order', async () => {
  const next = draft();
  next.version = 2;
  next.claims = [next.claims[1], next.claims[0], next.claims[2]];
  await openDraft(draft(), [['PUT', '/api/dossiers/d_fixture01', json(200, next)]]);
  await click(button('Move down', claimItem(c1.text)));
  await until(() => calls.some((call) => call.method === 'PUT'), 'the edit');
  const put = calls.find((call) => call.method === 'PUT');
  expect(put.body.keep).toEqual(['c2', 'c1', 'c3']);
  expect(put.body.order).toEqual(['c2', 'c1', 'c3']);
  await until(() => host.querySelector('li[data-claim]') && plain(host.querySelector('li[data-claim]').textContent).includes(c2.text), 'the new order on screen');
});

test('a note is saved against its claim and shown as the reviewer\'s note', async () => {
  const next = draft();
  next.version = 2;
  next.claims[0].note = 'Lead the deck with this';
  await openDraft(draft(), [['PUT', '/api/dossiers/d_fixture01', json(200, next)]]);
  const item = claimItem(c1.text);
  const box = item.querySelector('textarea');
  expect(box).not.toBeNull();
  await act(async () => typeInto(box, 'Lead the deck with this'));
  await click(button('Save note', claimItem(c1.text)));
  await until(() => calls.some((call) => call.method === 'PUT'), 'the edit');
  const put = calls.find((call) => call.method === 'PUT');
  expect(put.body.notes.c1).toBe('Lead the deck with this');
  expect(Object.keys(put.body).sort()).toEqual(['from_version', 'keep', 'notes', 'order', 'title']);
});

test('a new title is saved through the same edit', async () => {
  const next = draft();
  next.version = 2;
  next.title = 'Kitchen-table dance';
  await openDraft(draft(), [['PUT', '/api/dossiers/d_fixture01', json(200, next)]]);
  const input = host.querySelector('input[name="title"]');
  await act(async () => typeInto(input, 'Kitchen-table dance'));
  await click(button('Save title'));
  await until(() => host.querySelector('h1').textContent === 'Kitchen-table dance', 'the new title');
  const put = calls.find((call) => call.method === 'PUT');
  expect(put.body.title).toBe('Kitchen-table dance');
});

test('saving one note keeps the unsaved note on another claim, and a reorder keeps an unsaved title', async () => {
  const saved = draft();
  saved.version = 2;
  saved.claims[0].note = 'Lead the deck with this';
  const moved = clone(saved);
  moved.version = 3;
  moved.claims = [moved.claims[1], moved.claims[0], moved.claims[2]];
  let release = null;
  await openDraft(draft(), [['PUT', '/api/dossiers/d_fixture01', (path, body) => (body.from_version === 1
    ? new Promise((resolve) => { release = () => resolve(json(200, saved)); })
    : json(200, moved))]]);
  await act(async () => typeInto(claimItem(c1.text).querySelector('textarea'), 'Lead the deck with this'));
  await act(async () => typeInto(claimItem(c2.text).querySelector('textarea'), 'Check the second poster'));
  await click(button('Save note', claimItem(c1.text)));
  await until(() => release, 'the note save in flight');
  /* Typed while the save is still in flight. */
  await act(async () => typeInto(host.querySelector('input[name="title"]'), 'Kitchen-table dance'));
  await act(async () => release());
  await until(() => calls.filter((call) => call.method === 'PUT').length === 1 && !button('Move down', claimItem(c1.text)).disabled, 'the note saved');
  expect(claimItem(c1.text).querySelector('textarea').value).toBe('Lead the deck with this');
  expect(claimItem(c2.text).querySelector('textarea').value).toBe('Check the second poster');
  expect(host.querySelector('input[name="title"]').value).toBe('Kitchen-table dance');
  await click(button('Move down', claimItem(c1.text)));
  await until(() => plain(host.querySelector('li[data-claim]').textContent).includes(c2.text), 'the new order on screen');
  expect(host.querySelector('input[name="title"]').value).toBe('Kitchen-table dance');
  expect(claimItem(c2.text).querySelector('textarea').value).toBe('Check the second poster');
  expect(claimItem(c1.text).querySelector('textarea').value).toBe('Lead the deck with this');
});

test('Freeze waits while a note or title is unsaved, says to save first, and returns once each edit is saved or undone', async () => {
  const next = draft();
  next.version = 2;
  next.claims[0].note = 'Lead the deck with this';
  await openDraft(draft(), [['PUT', '/api/dossiers/d_fixture01', json(200, next)]]);
  const freezeHint = () => plain(host.querySelector('.dossiers42-actions').textContent);
  expect(button('Freeze').disabled).toBe(false);
  expect(freezeHint()).not.toContain('Save your edits first');
  await act(async () => typeInto(claimItem(c1.text).querySelector('textarea'), 'Lead the deck with this'));
  expect(button('Freeze').disabled).toBe(true);
  expect(freezeHint()).toContain('Save your edits first');
  await act(async () => typeInto(host.querySelector('input[name="title"]'), 'Kitchen-table dance'));
  await click(button('Save note', claimItem(c1.text)));
  await until(() => calls.some((call) => call.method === 'PUT') && !button('Move down', claimItem(c1.text)).disabled, 'the note saved');
  expect(button('Freeze').disabled).toBe(true);
  expect(freezeHint()).toContain('Save your edits first');
  await act(async () => typeInto(host.querySelector('input[name="title"]'), completeRecord.question));
  expect(button('Freeze').disabled).toBe(false);
  expect(freezeHint()).not.toContain('Save your edits first');
  await click(button('Freeze'));
  await until(() => calls.some((call) => call.path.endsWith('/freeze')), 'the freeze');
});

test('ticking a claim posts the tick by claim_id and clears its need', async () => {
  await openDraft(draft(), [['POST', '/api/dossiers/d_fixture01/ticks', (path, body) => json(201, {dossier_id: 'd_fixture01', claim_id: body.claim_id, ticked: body.ticked, note: null, who: 'passcode', at: '2026-09-28T06:13:00+02:00'})]]);
  const box = claimItem(c2.text).querySelector('input[type="checkbox"]');
  await act(async () => { box.click(); });
  await until(() => calls.some((call) => call.path.endsWith('/ticks')), 'the tick');
  const tick = calls.find((call) => call.path.endsWith('/ticks'));
  expect(tick.method).toBe('POST');
  expect(tick.body).toEqual({claim_id: 'c2', ticked: true});
  await until(() => !plain(claimItem(c2.text).textContent).includes('needs a tick'), 'the need cleared');
  expect(plain(claimItem(C3_TEXT).textContent)).toContain('needs a tick');
});

test('a refused freeze names the claims in words, not ids', async () => {
  const claims = [
    {claim_id: 'c3', reason: 'Single source claims need a tick before freezing'},
    {claim_id: 'c2', reason: 'a quote is not found verbatim in the text of x_fixture_2'},
  ];
  await openDraft(draft(), [['POST', '/api/dossiers/d_fixture01/freeze', json(409, {error: 'not_ready', message: 'Cannot freeze yet: c3: Single source claims need a tick before freezing; c2: a quote is not found verbatim in the text of x_fixture_2', claims})]]);
  await click(button('Freeze'));
  await until(() => host.querySelector('[role="alert"]'), 'the refusal');
  const alert = plain(host.querySelector('[role="alert"]').textContent);
  expect(alert).toContain(C3_TEXT);
  expect(alert).toContain('Single source claims need a tick before freezing');
  expect(alert).toContain(c2.text);
  expect(alert).toContain('a quote is not found verbatim');
  expect(alert).not.toContain('c3:');
  expect(alert).not.toContain('c2:');
  expect(button('Freeze')).toBeDefined();
});

test('a frozen version offers Export HTML and Export PDF, and each downloads', async () => {
  await openDraft(draft(), [
    ['POST', '/api/dossiers/d_fixture01/freeze', json(201, frozen())],
    ['GET', /\/versions\/2\/export\?format=(html|pdf)$/, (path) => ({ok: true, status: 200, headers: {get: () => null}, blob: async () => new Blob([path.endsWith('pdf') ? '%PDF' : '<html>'])})],
  ]);
  await click(button('Freeze'));
  await until(() => button('Export PDF'), 'the export actions');
  expect(button('Freeze')).toBeUndefined();
  expect(text()).toContain('Frozen version 2');
  expect(button('Remove')).toBeUndefined();
  expect(host.querySelector('input[type="checkbox"]')).toBeNull();
  expect(host.querySelectorAll('.dossiers42-primary').length).toBe(1);
  const share = [...host.querySelectorAll('a')].find((node) => node.getAttribute('href') === '#/d/d_fixture01/2');
  expect(share).toBeDefined();
  await click(button('Export PDF'));
  await until(() => downloads.length === 1, 'the PDF');
  await click(button('Export HTML'));
  await until(() => downloads.length === 2, 'the HTML');
  const exports = calls.filter((call) => call.path.includes('/export'));
  expect(exports.map((call) => call.path)).toEqual([
    '/api/dossiers/d_fixture01/versions/2/export?format=pdf',
    '/api/dossiers/d_fixture01/versions/2/export?format=html',
  ]);
  expect(exports.every((call) => headerOf(call, 'X-Passcode') === 'fixture-pass')).toBe(true);
  expect(downloads.map((entry) => entry.download)).toEqual(['42-dossier-d_fixture01-v2.pdf', '42-dossier-d_fixture01-v2.html']);
});

test('a frozen version offers Edit again, which starts a new draft version through the edit route and never writes to the frozen one', async () => {
  const next = draft();
  next.version = 3;
  next.created_at = '2026-09-28T06:20:00+02:00';
  next.ticks = frozen().ticks;
  next.needs_tick = [];
  await openDraft(frozen(), [['PUT', '/api/dossiers/d_fixture01', json(200, next)]]);
  expect(text()).toContain('Frozen version 2');
  const again = button('Edit again');
  expect(again).toBeDefined();
  expect(again.className).not.toContain('dossiers42-primary');
  expect(host.querySelectorAll('.dossiers42-primary').length).toBe(1);
  await click(again);
  await until(() => calls.some((call) => call.method === 'PUT'), 'the edit');
  const writes = calls.filter((call) => call.method !== 'GET');
  expect(writes.map((call) => call.method + ' ' + call.path)).toEqual(['PUT /api/dossiers/d_fixture01']);
  const put = writes[0];
  expect(Object.keys(put.body).sort()).toEqual(['from_version', 'keep', 'notes', 'order', 'title']);
  expect(put.body.from_version).toBe(2);
  expect(put.body.keep).toEqual(['c1', 'c2', 'c3']);
  expect(put.body.order).toEqual(['c1', 'c2', 'c3']);
  expect(put.body.title).toBe(completeRecord.question);
  for (const words of [c1.text, c2.text, C3_TEXT, answer.short_answer, 'corroborated', 'inferred', 'single_source']){
    expect(put.raw).not.toContain(words);
  }
  await until(() => button('Freeze'), 'the new draft');
  expect(text()).toContain('Draft version 3');
  expect(button('Edit again')).toBeUndefined();
  expect(button('Export PDF')).toBeUndefined();
});

test('a refused Edit again keeps the frozen version on screen with the server\'s words', async () => {
  await openDraft(frozen(), [['PUT', '/api/dossiers/d_fixture01', json(409, {error: 'not_ready', message: 'The source answer a_fixture can no longer be read.'})]]);
  await click(button('Edit again'));
  await until(() => host.querySelector('[role="alert"]'), 'the refusal');
  expect(plain(host.querySelector('[role="alert"]').textContent)).toContain('The source answer a_fixture can no longer be read.');
  expect(text()).toContain('Frozen version 2');
  expect(button('Freeze')).toBeUndefined();
  expect(button('Edit again')).toBeDefined();
});

const STALE = 'This dossier changed since you opened it. Reload to see the latest version.';

test('Edit again on a frozen version another tab has moved past shows the server\'s words and Reload, which opens the latest version', async () => {
  const theirs = draft();
  theirs.version = 3;
  theirs.title = 'Their draft';
  let reads = 0;
  serve([
    ['PUT', '/api/dossiers/d_fixture01', json(409, {error: 'stale_version', message: STALE})],
    ['GET', '/api/dossiers/d_fixture01', () => { reads += 1; return json(200, reads === 1 ? frozen() : theirs); }],
  ]);
  await act(async () => root.render(<DossierPage dossierId="d_fixture01" onAuth={() => {}} />));
  await until(() => text().includes('Frozen version 2'), 'the frozen version');
  await click(button('Edit again'));
  await until(() => host.querySelector('[role="alert"]'), 'the refusal');
  expect(calls.find((call) => call.method === 'PUT').body.from_version).toBe(2);
  expect(plain(host.querySelector('[role="alert"]').textContent)).toContain(STALE);
  expect(text()).toContain('Frozen version 2');
  const reload = button('Reload');
  expect(reload).toBeDefined();
  await click(reload);
  await until(() => text().includes('Draft version 3'), 'the latest version');
  expect(host.querySelector('h1').textContent).toBe('Their draft');
  expect(host.querySelector('[role="alert"]')).toBeNull();
  expect(button('Reload')).toBeUndefined();
  expect(calls.filter((call) => call.method === 'PUT').length).toBe(1);
});

test('a freeze from a draft another tab has edited shows the server\'s words and Reload, and sends from_version', async () => {
  const theirs = draft();
  theirs.version = 2;
  theirs.title = 'Their draft';
  let reads = 0;
  serve([
    ['POST', '/api/dossiers/d_fixture01/freeze', json(409, {error: 'stale_version', message: STALE})],
    ['GET', '/api/dossiers/d_fixture01', () => { reads += 1; return json(200, reads === 1 ? draft() : theirs); }],
  ]);
  await act(async () => root.render(<DossierPage dossierId="d_fixture01" onAuth={() => {}} />));
  await until(() => text().includes('Draft version 1'), 'the draft');
  await click(button('Freeze'));
  await until(() => host.querySelector('[role="alert"]'), 'the refusal');
  expect(calls.find((call) => call.path.endsWith('/freeze')).body).toEqual({from_version: 1});
  expect(plain(host.querySelector('[role="alert"]').textContent)).toContain(STALE);
  await click(button('Reload'));
  await until(() => text().includes('Draft version 2'), 'the latest version');
  expect(host.querySelector('h1').textContent).toBe('Their draft');
  expect(host.querySelector('[role="alert"]')).toBeNull();
});

test('a share link offers no Edit again', async () => {
  serve([['GET', '/api/dossiers/d_fixture01/versions/2', json(200, frozen())]]);
  await act(async () => root.render(<SharedDossier dossierId="d_fixture01" version="2" onAuth={() => {}} />));
  await until(() => text().includes(answer.short_answer), 'the frozen version');
  expect(button('Edit again')).toBeUndefined();
});

test('a 401 hands over to the passcode screen', async () => {
  let asked = 0;
  serve([['GET', '/api/dossiers/d_fixture01', json(401, {error: 'unauthorized', message: 'Passcode required.'})]]);
  await act(async () => root.render(<DossierPage dossierId="d_fixture01" onAuth={() => { asked += 1; }} />));
  await until(() => asked === 1, 'onAuth');
});

/* ---------------- the share link ---------------- */

test('a share link opens a frozen version read-only', async () => {
  serve([['GET', '/api/dossiers/d_fixture01/versions/2', json(200, frozen())]]);
  await act(async () => root.render(<SharedDossier dossierId="d_fixture01" version="2" onAuth={() => {}} />));
  await until(() => text().includes(answer.short_answer), 'the frozen version');
  expect(host.querySelector('h1').textContent).toBe(completeRecord.question);
  expect(text()).toContain(c1.text);
  expect(text()).toContain(C3_TEXT);
  expect(button('Remove')).toBeUndefined();
  expect(button('Move up')).toBeUndefined();
  expect(button('Save note')).toBeUndefined();
  expect(host.querySelector('input, textarea')).toBeNull();
  expect(button('Freeze')).toBeUndefined();
  expect(button('Export PDF')).toBeDefined();
  expect(button('Export HTML')).toBeDefined();
});

test('a share link to a draft version says it is not frozen yet and shows none of it', async () => {
  serve([['GET', '/api/dossiers/d_fixture01/versions/1', json(200, draft())]]);
  await act(async () => root.render(<SharedDossier dossierId="d_fixture01" version="1" onAuth={() => {}} />));
  await until(() => text().includes('This version is not frozen yet'), 'the not-frozen notice');
  expect(host.querySelector('h1')).not.toBeNull();
  expect(text()).not.toContain(answer.short_answer);
  expect(text()).not.toContain(c1.text);
  expect(button('Export PDF')).toBeUndefined();
});

test('the page copy names no age group, no Google Trends and no retired product', async () => {
  await openDraft();
  expect(text()).not.toMatch(/gen ?z|millennial|youth|generation|google trends|prompt pulse|nano banana/i);
});
