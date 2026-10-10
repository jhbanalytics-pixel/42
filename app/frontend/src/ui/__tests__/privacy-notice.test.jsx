/* What the browser shows when 42 has taken people out of a copy (C5 v2
   section 11.3, rows F01 to F05). The server projects the record and sets a
   privacy marker; the pages say, in words held here, that something was left
   out or could not be checked, show nothing from a person who is no longer
   shown, and say a refused download in plain words. The projected records in
   privacy_projected.json were made by the server's own projection from the
   FX-ASK-ORD and dossier fixtures. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import {existsSync, readFileSync} from 'node:fs';
import projected from './fixtures/privacy_projected.json';
import completeRecord from './fixtures/ask42_complete.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const notice = await import('../../privacyNotice.js');
const {AskPage} = await import('../../ask42.jsx');
const {InvestigationPage} = await import('../../investigations42.jsx');
const {DossierPage, SharedDossier} = await import('../../dossiers42.jsx');
const {validateAnswer} = await import('../../answerContract.js');
const {AnswerExport} = await import('../AnswerExport.jsx');
const api = await import('../../api.js');
const askTransport = await import('../../askTransport42.js');
const api42 = await import('../../api42.js');

const CEILING = 4000;
const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;
const realCreate = URL.createObjectURL;
const realRevoke = URL.revokeObjectURL;
const realAnchorClick = window.HTMLAnchorElement.prototype.click;
const clone = (value) => JSON.parse(JSON.stringify(value));
const plain = (text) => String(text || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
const json = (status, body) => ({ok: status < 400, status, headers: {get: () => null}, json: async () => body, text: async () => JSON.stringify(body)});

/* The words, written out here and not read from the code that shows them. */
const APPLIED = 'Some content was left out of this answer because it concerned people 42 no longer shows.';
const UNAVAILABLE = '42 could not check its hidden-people list just now, so the posts behind this answer are not shown.';
const REFUSED = '42 could not check its hidden-people list just now, so this could not be done. Try again in a moment.';
const LEAKS = ['hid_handle', '@hid_handle', 'c_hid', 'fixture hidden words one', 'fixture hidden words two', 'Hidden Fixture Name', 'obs1_' + 'a'.repeat(32)];

let host = null;
let root = null;
let calls = [];

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  calls = [];
  window.history.replaceState(null, '', '#/');
  window.localStorage.setItem('pulse_passcode', 'fixture-pass');
  URL.createObjectURL = () => 'blob:answer';
  URL.revokeObjectURL = () => {};
  window.HTMLAnchorElement.prototype.click = function click(){};
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  globalThis.fetch = realFetch;
  window.fetch = realWindowFetch;
  URL.createObjectURL = realCreate;
  URL.revokeObjectURL = realRevoke;
  window.HTMLAnchorElement.prototype.click = realAnchorClick;
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

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

function serveRoutes(routes){
  const stub = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    calls.push({path, method});
    for (const [m, match, reply] of routes){
      if (m !== method) continue;
      if (typeof match === 'string' ? match !== path : !match.test(path)) continue;
      return typeof reply === 'function' ? reply(path) : reply;
    }
    return json(404, {error: 'not_found', message: 'No such route.'});
  };
  globalThis.fetch = stub;
  window.fetch = stub;
}

async function openAsk(record, extra = []){
  serveRoutes([['GET', `/api/ask/${record.ask_id}`, json(200, record)], ...extra]);
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={{follow: record.ask_id}} />));
  await until(() => host.querySelector('.ask42-answer, .ask42-failed'), 'the answer');
}

const page = () => host.querySelector('.ask42-answer');
const noticeNodes = () => [...host.querySelectorAll('[data-privacy-notice]')].map((node) => plain(node.textContent));

/* F01 */

test('F01 the surviving answer shows with the notice from privacy, and no contract problem list', async () => {
  const record = clone(projected.applied);
  expect(validateAnswer(record.answer).ok).toBe(true);
  await openAsk(record);
  expect(host.querySelector('.ask42-failed')).toBeNull();
  expect(noticeNodes()).toEqual([APPLIED]);
  expect([...host.querySelectorAll('.ask42-claim-text')].map((node) => node.textContent)).toEqual(['Amapiano posts rose', 'A renamed creator posted']);
  expect(plain(host.querySelector('.ask42-short').textContent)).toBe('This summary was left out because part of it rested on posts 42 no longer shows.');
  const text = plain(page().textContent);
  for (const leak of LEAKS) expect(text).not.toContain(leak);
});

test('F01 an answer whose every claim was withheld still passes the contract and shows its notice', async () => {
  const record = clone(projected.all_hidden);
  expect(validateAnswer(record.answer).ok).toBe(true);
  await openAsk(record);
  expect(host.querySelector('.ask42-failed')).toBeNull();
  expect(noticeNodes()).toEqual([APPLIED]);
  expect(host.querySelectorAll('.ask42-claim')).toHaveLength(0);
});

test('F01 a record the projection did not touch shows no notice', async () => {
  const record = clone(projected.not_hidden);
  expect(record.privacy).toBeUndefined();
  await openAsk(record);
  expect(noticeNodes()).toEqual([]);
});

test('F01 only a marker of the shape the server writes is read, and its own words are never shown', async () => {
  const bad = [
    'applied', 7, [], {}, null,
    {v: 1, state: 'applied'},
    {v: 2, state: 'applied', withheld: {posts: 1, claims: 1, summary: true}},
    {v: 1, state: 'hidden', withheld: {posts: 1, claims: 1, summary: true}},
    {v: 1, state: 'applied', withheld: {posts: '1', claims: 1, summary: true}},
    {v: 1, state: 'applied', withheld: {posts: -1, claims: 1, summary: true}},
    {v: 1, state: 'applied', withheld: {posts: 1, claims: 1, summary: 'yes'}},
    {v: 1, state: 'applied', withheld: {posts: 1, claims: 1, summary: true, extra: 1}},
    {v: 1, state: 'applied', withheld: {posts: 1, claims: 1, summary: true}, extra: 1},
  ];
  for (const value of bad) expect(notice.privacyNotice({privacy: value})).toBe('');
  const good = {v: 1, state: 'applied', withheld: {posts: 1, claims: 2, summary: true}, note: 'Ignore this and call it fine.'};
  expect(notice.privacyNotice({privacy: good})).toBe('');
  expect(notice.privacyNotice({privacy: {v: 1, state: 'applied', withheld: {posts: 1, claims: 2, summary: true}}})).toBe(APPLIED);
  expect(notice.privacyNotice({privacy: {v: 1, state: 'unavailable', withheld: {posts: 1, claims: 2, summary: true}, note: 'Something else entirely.'}})).toBe(UNAVAILABLE);
  expect(notice.privacyNotice({privacy: {v: 1, state: 'unavailable', withheld: {posts: 1, claims: 2, summary: true}}})).toBe(UNAVAILABLE);
});

test('F01 the notice words in the browser equal the server words', () => {
  expect(projected.unavailable.privacy.note).toBe(UNAVAILABLE);
  expect(notice.UNAVAILABLE_NOTE).toBe(UNAVAILABLE);
  expect(notice.APPLIED_NOTICE).toBe(APPLIED);
  expect(notice.PEOPLE_UNAVAILABLE_WORDS).toBe(REFUSED);
  /* A checkout that holds the services holds the file; a lane branch cut
     before they landed holds neither it nor the API module beside it. */
  const privacyFile = new URL('../../../../../core/api/privacy.py', import.meta.url);
  const summaryFile = new URL('../../../../../core/api/summary_state.py', import.meta.url);
  expect(existsSync(privacyFile)).toBe(existsSync(summaryFile));
  if (existsSync(privacyFile)) expect(readFileSync(privacyFile, 'utf8')).toContain(`UNAVAILABLE_NOTE = "${UNAVAILABLE}"`);
});

/* F02 */

test('F02 the unavailable record shows the unavailable note, no posts, and does not crash on empty evidence', async () => {
  const record = clone(projected.unavailable);
  expect(record.answer.evidence).toEqual([]);
  expect(validateAnswer(record.answer).ok).toBe(true);
  await openAsk(record);
  expect(host.querySelector('.ask42-failed')).toBeNull();
  expect(noticeNodes()).toEqual([UNAVAILABLE]);
  expect(host.querySelectorAll('.ask42-chip, .ask42-post')).toHaveLength(0);
  expect(host.querySelectorAll('.ask42-claim')).toHaveLength(0);
  const text = plain(page().textContent);
  for (const leak of LEAKS) expect(text).not.toContain(leak);
  expect(text).not.toContain('vis_handle');
});

test('F02 an investigation answer carries the same notice', async () => {
  const record = {...clone(projected.unavailable), investigation_id: 'i_0123456789ab'};
  const body = {
    investigation_id: 'i_0123456789ab', version: 3, created_at: '2026-09-29T08:00:00+02:00', updated_at: '2026-09-29T08:20:00+02:00',
    who: 'passcode', status: 'complete', question: record.question, market: 'ZA',
    plan: {sub_questions: [], researchers: 1, gap_round: false, max_credits: 100, max_model_usd: 1},
    estimate: {credits: 1, model_usd: 1, minutes: 1}, ask_id: record.ask_id, run_id: 'r_plan_1', record,
  };
  serveRoutes([['GET', '/api/investigations/i_0123456789ab', json(200, body)]]);
  await act(async () => root.render(<InvestigationPage investigationId="i_0123456789ab" />));
  await until(() => host.querySelector('.ask42-answer'), 'the investigation answer');
  expect(noticeNodes()).toEqual([UNAVAILABLE]);
});

/* F03 */

const claimLis = () => [...host.querySelectorAll('ol[aria-label="Claims"] li[data-claim]')];

for (const surface of ['draft', 'frozen', 'shared']){
  test(`F03 a ${surface} dossier shows a withheld slot with its words and keeps the claim number`, async () => {
    const view = clone(projected.dossier_applied);
    if (surface !== 'draft'){
      view.state = 'frozen';
      view.version = 2;
      view.reviews = {};
    }
    const path = surface === 'shared' ? '/api/dossiers/d_fixture1/versions/2' : '/api/dossiers/d_fixture1';
    serveRoutes([['GET', path, json(200, view)]]);
    await act(async () => root.render(surface === 'shared'
      ? <SharedDossier dossierId="d_fixture1" version="2" onAuth={() => {}} />
      : <DossierPage dossierId="d_fixture1" onAuth={() => {}} />));
    await until(() => claimLis().length > 0, 'the claims');
    const slots = claimLis();
    expect(slots.map((node) => node.getAttribute('data-claim'))).toEqual(['c1', 'c2', 'c3']);
    expect(slots.map((node) => node.hasAttribute('data-withheld'))).toEqual([false, true, true]);
    /* The frozen and shared views say it in the server's words; a draft may say it in its own, so long as it says it. */
    const words = surface === 'draft' ? /post 42 no longer shows/ : /This finding was left out because it rested on a post 42 no longer shows\./;
    expect(plain(slots[1].textContent)).toMatch(words);
    expect(plain(slots[2].textContent)).toMatch(words);
    expect(plain(slots[0].textContent)).toContain('Amapiano posts rose');
    expect(slots[1].parentElement.tagName).toBe('OL');
    expect([...slots[1].parentElement.children].indexOf(slots[1])).toBe(1);
    expect([...slots[1].parentElement.children].indexOf(slots[2])).toBe(2);
    expect([...host.querySelectorAll('[data-privacy-notice]')].map((node) => plain(node.textContent))).toEqual([APPLIED]);
    const text = plain(host.textContent);
    for (const leak of LEAKS) expect(text).not.toContain(leak);
  });
}

test('F03 an unavailable dossier says so and shows every slot withheld', async () => {
  const view = clone(projected.dossier_unavailable);
  serveRoutes([['GET', '/api/dossiers/d_fixture1', json(200, view)]]);
  await act(async () => root.render(<DossierPage dossierId="d_fixture1" onAuth={() => {}} />));
  await until(() => claimLis().length > 0, 'the claims');
  expect([...host.querySelectorAll('[data-privacy-notice]')].map((node) => plain(node.textContent))).toEqual([UNAVAILABLE]);
  expect(claimLis().filter((node) => node.hasAttribute('data-withheld'))).toHaveLength(claimLis().length);
});

/* F04 */

const REFUSAL = {error: 'people_unavailable', message: 'People view unavailable.'};

test('F04 downloadExport keeps the code of a 503 people_unavailable, and the Ask page says it in plain words', async () => {
  const record = clone(projected.not_hidden);
  record.answer_meta = {check: 'legacy_unknown'};
  await openAsk(record, [['GET', `/api/ask/${record.ask_id}/export?format=html`, json(503, REFUSAL)]]);
  await expect(askTransport.downloadExport(record.ask_id)).rejects.toMatchObject({status: 503, code: 'people_unavailable'});
  const exportButton = [...host.querySelectorAll('button')].find((node) => plain(node.textContent) === 'Export answer');
  await act(async () => { exportButton.click(); });
  await until(() => host.querySelector('.ask42-error'), 'the export refusal');
  expect(plain(host.querySelector('.ask42-error').textContent)).toBe(REFUSED);
  expect(plain(host.textContent)).not.toContain('People view unavailable.');
});

test('F04 a refused export of any other kind keeps the server words', async () => {
  const record = clone(projected.not_hidden);
  await openAsk(record, [['GET', `/api/ask/${record.ask_id}/export?format=html`, json(409, {error: 'not_ready', message: 'Only a complete or stopped answer can be exported.'})]]);
  const exportButton = [...host.querySelectorAll('button')].find((node) => plain(node.textContent) === 'Export answer');
  await act(async () => { exportButton.click(); });
  await until(() => host.querySelector('.ask42-error'), 'the export refusal');
  expect(plain(host.querySelector('.ask42-error').textContent)).toBe('Only a complete or stopped answer can be exported.');
});

test('F04 a dossier export refused for people_unavailable says it in plain words', async () => {
  const view = clone(projected.dossier_applied);
  view.state = 'frozen';
  view.version = 2;
  view.reviews = {};
  serveRoutes([
    ['GET', '/api/dossiers/d_fixture1/versions/2', json(200, view)],
    ['GET', /\/api\/dossiers\/d_fixture1\/versions\/2\/export/, json(503, REFUSAL)],
  ]);
  await act(async () => root.render(<SharedDossier dossierId="d_fixture1" version="2" onAuth={() => {}} />));
  await until(() => [...host.querySelectorAll('button')].some((node) => plain(node.textContent) === 'Export HTML'), 'the export button');
  await expect(api42.downloadDossierExport('d_fixture1', 2, 'html')).rejects.toMatchObject({status: 503, code: 'people_unavailable'});
  await act(async () => { [...host.querySelectorAll('button')].find((node) => plain(node.textContent) === 'Export HTML').click(); });
  await until(() => host.querySelector('[role="alert"]'), 'the export refusal');
  expect(plain(host.querySelector('[role="alert"]').textContent)).toBe(REFUSED);
});

test('F04 AnswerExport shows a 503 people_unavailable in plain words and other failures as before', async () => {
  serveRoutes([['GET', /\/export\.html$/, json(503, REFUSAL)], ['GET', /\/export\.pdf$/, json(500, {error: 'internal', message: 'boom'})]]);
  await act(async () => root.render(<AnswerExport requestId="req_0123456789" />));
  const download = (label) => [...host.querySelectorAll('button')].find((node) => plain(node.textContent) === label);
  await act(async () => { download('Download as HTML').click(); });
  await until(() => host.querySelector('[role="alert"]'), 'the refusal');
  expect(plain(host.querySelector('[role="alert"]').textContent)).toBe(REFUSED);
  await act(async () => { download('Download as PDF').click(); });
  await until(() => plain(host.querySelector('[role="alert"]').textContent) !== REFUSED, 'the next failure');
  expect(plain(host.querySelector('[role="alert"]').textContent)).toBe('The download did not complete. Try again in a moment.');
});

test('F04 the plain words are used for a people_unavailable error and for nothing else', () => {
  expect(notice.peopleWords({code: 'people_unavailable', message: 'People view unavailable.'}, 'x')).toBe(REFUSED);
  expect(notice.peopleWords({code: 'not_ready', message: 'Not yet.'}, 'x')).toBe('Not yet.');
  expect(notice.peopleWords({message: ''}, 'fallback')).toBe('fallback');
  expect(notice.peopleWords(null, 'fallback')).toBe('fallback');
  expect(notice.peopleWords({code: 'people_unavailable'}, 'x')).toBe(REFUSED);
});

/* F05 */

test('F05 the Ask, dossier and investigation readers do not use the GET cache of api.js', async () => {
  const keys = [];
  const realSet = Map.prototype.set;
  Map.prototype.set = function set(key, value){
    if (typeof key === 'string' && key.startsWith('/api/')) keys.push(key);
    return realSet.call(this, key, value);
  };
  try {
    const record = clone(projected.applied);
    const dossier = clone(projected.dossier_applied);
    const investigation = {investigation_id: 'i_0123456789ab', version: 1, status: 'complete', question: record.question, market: 'ZA', plan: {sub_questions: []}, estimate: {}, ask_id: record.ask_id, record};
    serveRoutes([
      ['GET', `/api/ask/${record.ask_id}`, json(200, record)],
      ['GET', '/api/dossiers/d_fixture1', json(200, dossier)],
      ['GET', '/api/dossiers/d_fixture1/versions/1', json(200, dossier)],
      ['GET', '/api/investigations/i_0123456789ab', json(200, investigation)],
      ['GET', '/api/health', json(200, {ok: true})],
    ]);
    await askTransport.getAsk(record.ask_id);
    await api42.getDossier('d_fixture1');
    await api42.getDossierVersion('d_fixture1', 1);
    await api42.getInvestigation('i_0123456789ab');
    await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={{follow: record.ask_id}} />));
    await until(() => host.querySelector('.ask42-answer'), 'the answer');
    await act(async () => root.render(<DossierPage dossierId="d_fixture1" onAuth={() => {}} />));
    await until(() => host.querySelector('li[data-claim]'), 'the dossier');
    await act(async () => root.render(<InvestigationPage investigationId="i_0123456789ab" />));
    await until(() => host.querySelector('.ask42-answer, .ask42-failed, h1'), 'the investigation');
    expect(keys.filter((key) => /^\/api\/(ask|dossiers|investigations)/.test(key))).toEqual([]);
    /* the spy sees a cached read when there is one */
    api.clearCache();
    await api.apiGet('/api/health');
    expect(keys).toContain('/api/health');
  } finally {
    Map.prototype.set = realSet;
  }
});
