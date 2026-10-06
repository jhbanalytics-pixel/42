/* Investigations on the 42 API (task 3.4; contract.md section 13.1;
   EXPERIENCE.md, Investigations). The plan comes first with its estimate
   against what is left today, it can be edited, Start is the one red action
   and every refusal is shown in the server's own words; a started run shows
   its live research log and Stop, and a finished one renders its answer as
   Ask does, only after the shared contract check, and opens as a draft
   dossier with one tap. The shapes are core/api/agent_app.py's. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import completeRecord from './fixtures/ask42_complete.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const api = await import('../../api42.js');
const {InvestigationsPage, InvestigationPage} = await import('../../investigations42.jsx');

const CEILING = 4000;
const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;

const clone = (value) => JSON.parse(JSON.stringify(value));
const plain = (text) => String(text || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();

const ID = 'i_0123456789ab';
const PLAN = {
  sub_questions: [
    {id: 'q1', text: 'Where did it start, and who posted about it first?', platforms: ['tiktok', 'x'], credits: 120},
    {id: 'q2', text: 'Which communities picked it up, in which languages and places?', platforms: ['tiktok', 'instagram'], credits: 100},
    {id: 'q3', text: 'Is it still growing, and what do people say about it?', platforms: ['x', 'reddit_threads', 'youtube'], credits: 80},
  ],
  researchers: 3, gap_round: true, max_credits: 400, max_model_usd: 3.0,
};
const ESTIMATE = {credits: 300, model_usd: 1.5, minutes: 6};
const QUESTION = 'What is behind #fixture in South Africa this month?';

function draft(extra = {}){
  return {investigation_id: ID, version: 1, created_at: '2026-09-29T08:00:00+02:00', updated_at: '2026-09-29T08:00:00+02:00',
    who: 'passcode', status: 'draft', question: QUESTION, market: 'ZA', plan: clone(PLAN), estimate: clone(ESTIMATE),
    ask_id: null, run_id: 'r_plan_1', ...extra};
}
const finishedRecord = () => ({...clone(completeRecord), investigation_id: ID});
function finished(extra = {}){
  return draft({version: 3, status: 'complete', ask_id: completeRecord.ask_id, updated_at: '2026-09-29T08:20:00+02:00', record: finishedRecord(), ...extra});
}

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
  window.localStorage.setItem('pulse_passcode', 'fixture-pass');
  window.location.hash = '#/investigations';
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  globalThis.fetch = realFetch;
  window.fetch = realWindowFetch;
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

/* A response body whose chunks the test hands over one at a time. */
function sseBody(){
  const encoder = new TextEncoder();
  const queue = [];
  let waiting = null;
  let closed = false;
  const settle = (result) => { const resolve = waiting; waiting = null; resolve(result); };
  return {
    push(text){
      const chunk = {value: encoder.encode(text), done: false};
      if (waiting) settle(chunk); else queue.push(chunk);
    },
    close(){
      closed = true;
      if (waiting) settle({value: undefined, done: true});
    },
    body: {
      getReader(){
        return {
          read(){
            if (queue.length) return Promise.resolve(queue.shift());
            if (closed) return Promise.resolve({value: undefined, done: true});
            return new Promise((resolve) => { waiting = resolve; });
          },
          cancel(){ closed = true; if (waiting) settle({value: undefined, done: true}); return Promise.resolve(); },
          releaseLock(){},
        };
      },
    },
  };
}

const frame = (seq, event, data) => `id: ${seq}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const json = (status, body) => ({ok: status < 400, status, headers: {get: () => null}, json: async () => body, text: async () => JSON.stringify(body)});

/* A fake f42-api: routes is a list of [method, path or RegExp, reply or
   function returning a reply]; the first match answers. */
function serve(routes){
  const stub = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    calls.push({path, method, headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : null});
    for (const [m, match, answer] of routes){
      if (m !== method) continue;
      if (typeof match === 'string' ? match === path : match.test(path)) return typeof answer === 'function' ? answer(path, init) : answer;
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
const buttons = () => [...host.querySelectorAll('button')];
const button = (label) => buttons().find((node) => plain(node.textContent) === label);
const headerOf = (call, name) => {
  const key = Object.keys(call.headers).find((k) => k.toLowerCase() === name.toLowerCase());
  return key ? call.headers[key] : undefined;
};
const alertText = () => plain([...host.querySelectorAll('[role="alert"]')].map((node) => node.textContent).join(' '));

function typeInto(element, value){
  const proto = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype
    : element.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, value);
  element.dispatchEvent(new window.Event(element.tagName === 'SELECT' ? 'change' : 'input', {bubbles: true}));
}

async function press(label){
  const node = button(label);
  if (!node) throw new Error('no button ' + label);
  await act(async () => { node.click(); });
}

async function openDraft(body = draft(), extra = []){
  serve([...extra, ['GET', '/api/investigations/' + ID, json(200, body)]]);
  await act(async () => root.render(<InvestigationPage investigationId={ID} />));
  await until(() => host.querySelector('h1'), 'the investigation to load');
}

/* ---------------- api42 ---------------- */

test('the investigation reads and writes use the contract paths, bodies and the passcode', async () => {
  serve([
    ['POST', '/api/investigations', json(201, draft({budget_left: {credits: 600, model_usd: 19.5}}))],
    ['PUT', '/api/investigations/' + ID + '/plan', json(200, draft({version: 2}))],
    ['POST', '/api/investigations/' + ID + '/start', json(202, {investigation_id: ID, status: 'running', ask_id: 'a_1', ceilings: {max_credits: 400, max_model_usd: 3}, events_url: '/api/investigations/' + ID + '/events', url: '/api/investigations/' + ID})],
    ['POST', '/api/investigations/' + ID + '/stop', json(202, {investigation_id: ID, status: 'stopping'})],
    ['GET', '/api/investigations/' + ID, json(200, draft())],
    ['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})],
    ['POST', '/api/dossiers', json(201, {dossier_id: 'd_1', version: 1})],
  ]);
  expect((await api.createInvestigation({question: QUESTION, market: 'ZA', angles: ['music', 'dance']})).investigation_id).toBe(ID);
  await api.createInvestigation({question: QUESTION, market: '', angles: []});
  await api.updateInvestigationPlan(ID, PLAN);
  expect((await api.startInvestigation(ID)).ask_id).toBe('a_1');
  expect((await api.stopInvestigation(ID)).status).toBe('stopping');
  expect((await api.getInvestigation(ID)).status).toBe('draft');
  await api.listInvestigations({status: 'complete'});
  await api.listInvestigations();
  expect((await api.createInvestigationDossier(ID)).dossier_id).toBe('d_1');
  expect(calls.map((c) => c.method + ' ' + c.path)).toEqual([
    'POST /api/investigations', 'POST /api/investigations', 'PUT /api/investigations/' + ID + '/plan',
    'POST /api/investigations/' + ID + '/start', 'POST /api/investigations/' + ID + '/stop', 'GET /api/investigations/' + ID,
    'GET /api/investigations?status=complete', 'GET /api/investigations', 'POST /api/dossiers',
  ]);
  expect(calls[0].body).toEqual({question: QUESTION, market: 'ZA', angles: ['music', 'dance']});
  expect(calls[1].body).toEqual({question: QUESTION, market: null});
  expect(calls[2].body).toEqual({plan: PLAN});
  expect(calls[8].body).toEqual({from: {investigation_id: ID}});
  expect(calls.every((c) => headerOf(c, 'X-Passcode') === 'fixture-pass')).toBe(true);
});

test('a refused write keeps the server words, code and status, and a 401 is marked auth', async () => {
  serve([
    ['POST', '/api/investigations', json(429, {error: 'rate_limited', message: "Today's model budget is spent; try tomorrow or ask Albert to raise MODEL_DAILY_USD"})],
    ['POST', '/api/investigations/' + ID + '/start', json(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})],
  ]);
  const spent = await api.createInvestigation({question: QUESTION, market: 'ZA'}).catch((error) => error);
  expect(spent.status).toBe(429);
  expect(spent.code).toBe('rate_limited');
  expect(spent.message).toBe("Today's model budget is spent; try tomorrow or ask Albert to raise MODEL_DAILY_USD");
  const locked = await api.startInvestigation(ID).catch((error) => error);
  expect(locked.auth).toBe(true);
});

test('streamInvestigation reads the investigation event stream with the passcode and Last-Event-ID, and resolves on done', async () => {
  const stream = sseBody();
  serve([['GET', '/api/investigations/' + ID + '/events', {ok: true, status: 200, headers: {get: () => 'text/event-stream'}, body: stream.body}]]);
  const seen = [];
  const running = api.streamInvestigation(ID, (event) => seen.push(event), {lastEventId: '2'});
  stream.push(frame(3, 'step', completeRecord.steps[2]).slice(0, 12));
  stream.push(frame(3, 'step', completeRecord.steps[2]).slice(12));
  stream.push(': keepalive\n\n');
  stream.push(frame(4, 'done', {seq: 4, status: 'complete', url: '/api/investigations/' + ID}));
  const result = await running;
  expect(result.status).toBe('complete');
  expect(seen.map((event) => event.type)).toEqual(['step', 'done']);
  expect(seen[0].data.text).toBe(completeRecord.steps[2].text);
  expect(headerOf(calls[0], 'X-Passcode')).toBe('fixture-pass');
  expect(headerOf(calls[0], 'Last-Event-ID')).toBe('2');
});

test('when the stream cannot open, streamInvestigation polls the investigation for new steps until it stops running', async () => {
  let reads = 0;
  serve([
    ['GET', '/api/investigations/' + ID + '/events', json(502, {error: 'agent_unavailable', message: 'Down.'})],
    ['GET', '/api/investigations/' + ID, () => {
      reads += 1;
      if (reads === 1) return json(200, draft({status: 'running', ask_id: completeRecord.ask_id, record: {...finishedRecord(), status: 'running', answer: null, steps: completeRecord.steps.slice(0, 2)}}));
      return json(200, finished());
    }],
  ]);
  const seen = [];
  const result = await api.streamInvestigation(ID, (event) => seen.push(event), {pollMs: 1});
  expect(result.status).toBe('complete');
  expect(seen.map((event) => event.data.seq)).toEqual(completeRecord.steps.map((step) => step.seq));
});

/* ---------------- the list, #/investigations ---------------- */

const LIST = {investigations: [
  draft({investigation_id: 'i_bbbbbbbbbbbb', status: 'running', question: 'How is amapiano travelling in Nigeria?', market: 'NG', created_at: '2026-09-29T09:00:00+02:00'}),
  draft({investigation_id: 'i_aaaaaaaaaaaa', status: 'complete', question: 'What are Kenyan food creators posting?', market: 'KE', created_at: '2026-09-27T09:00:00+02:00'}),
]};

test('the list shows each investigation with its status and a link, and the status tabs read by status', async () => {
  serve([['GET', /^\/api\/investigations(\?.*)?$/, (path) => json(200, path.includes('status=running') ? {investigations: [LIST.investigations[0]]} : LIST)]]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => host.querySelectorAll('[data-investigation]').length === 2, 'the list');
  expect(host.querySelector('h1').textContent).toBe('Investigations');
  const rows = [...host.querySelectorAll('[data-investigation]')];
  expect(plain(rows[0].textContent)).toContain('Running');
  expect(plain(rows[0].textContent)).toContain('Nigeria');
  expect(rows[0].querySelector('a').getAttribute('href')).toBe('#/investigations/i_bbbbbbbbbbbb');
  expect(plain(rows[1].textContent)).toContain('Finished');
  const tabs = [...host.querySelectorAll('[role="tab"]')].map((node) => plain(node.textContent));
  expect(tabs).toEqual(['All', 'Drafts', 'Running', 'Finished', 'Stopped', 'Failed']);
  await act(async () => { [...host.querySelectorAll('[role="tab"]')].find((node) => plain(node.textContent) === 'Running').click(); });
  await until(() => host.querySelectorAll('[data-investigation]').length === 1, 'the running tab');
  expect(calls.map((c) => c.path)).toEqual(['/api/investigations', '/api/investigations?status=running']);
  expect(host.querySelector('[role="tab"][aria-selected="true"]').textContent).toBe('Running');
});

test('an empty tab says so, and a list that fails says so in its own words', async () => {
  serve([['GET', /^\/api\/investigations/, json(200, {investigations: []})]]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => text().includes('No investigations yet'), 'the empty line');
  await act(async () => root.unmount());
  root = createRoot(host);
  serve([['GET', /^\/api\/investigations/, json(503, {error: 'agent_unavailable', message: 'The agent service is not reachable.'})]]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => alertText().includes('The agent service is not reachable.'), 'the failure');
});

test('a list that fails offers Try again, which reads the list again', async () => {
  serve([['GET', /^\/api\/investigations/, json(503, {error: 'agent_unavailable', message: 'The agent service is not reachable.'})]]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => [...host.querySelectorAll('button')].some((b) => b.textContent === 'Try again'), 'the retry');
  serve([['GET', /^\/api\/investigations/, json(200, {investigations: []})]]);
  await act(async () => [...host.querySelectorAll('button')].find((b) => b.textContent === 'Try again').click());
  await until(() => text().includes('No investigations yet'), 'the list read again');
});

/* Shell consistency, 2 October 2026: the row is the Today tab row (.t42-tabs)
   rather than a boxed segmented control; its keyboard and ARIA are unchanged. */
test('the status filter is one tab row: arrow keys move the choice, one tab stop, and it names the list it filters', async () => {
  serve([['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})]]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => text().includes('No investigations yet'), 'the empty line');
  const tabs = () => [...host.querySelectorAll('[role="tab"]')];
  const list = host.querySelector('[role="tablist"]');
  expect(list.classList.contains('t42-tabs')).toBe(true);
  expect(tabs().map((tab) => tab.getAttribute('tabindex'))).toEqual(['0', '-1', '-1', '-1', '-1', '-1']);
  const panel = host.querySelector('#' + tabs()[0].getAttribute('aria-controls'));
  expect(panel).not.toBeNull();
  expect(panel.getAttribute('role')).toBe('tabpanel');
  await act(async () => { tabs()[0].dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true})); });
  await until(() => tabs()[1].getAttribute('aria-selected') === 'true', 'the next tab');
  expect(document.activeElement).toBe(tabs()[1]);
  expect(tabs().map((tab) => tab.getAttribute('tabindex'))).toEqual(['-1', '0', '-1', '-1', '-1', '-1']);
  await act(async () => { tabs()[1].dispatchEvent(new KeyboardEvent('keydown', {key: 'End', bubbles: true})); });
  await until(() => tabs()[5].getAttribute('aria-selected') === 'true', 'the last tab');
  await act(async () => { tabs()[5].dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true})); });
  await until(() => tabs()[0].getAttribute('aria-selected') === 'true', 'wrapping to the first tab');
});

test('an empty list says what to do next, and an empty status offers the whole list back', async () => {
  serve([['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})]]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => text().includes('No investigations yet'), 'the empty line');
  expect(text()).toContain('No investigations yet. Write what 42 should find out above, then choose Draft a plan.');
  expect(button('Show all investigations')).toBeUndefined();
  await act(async () => { [...host.querySelectorAll('[role="tab"]')].find((node) => plain(node.textContent) === 'Stopped').click(); });
  await until(() => text().includes('No stopped investigations.'), 'the empty status');
  await act(async () => { button('Show all investigations').click(); });
  await until(() => host.querySelector('[role="tab"][aria-selected="true"]').textContent === 'All', 'the whole list');
});

test('Show all investigations hands keyboard focus to the All tab rather than dropping it', async () => {
  serve([['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})]]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => text().includes('No investigations yet'), 'the empty line');
  await act(async () => { [...host.querySelectorAll('[role="tab"]')].find((node) => plain(node.textContent) === 'Failed').click(); });
  await until(() => text().includes('No failed investigations.'), 'the empty status');
  await act(async () => { button('Show all investigations').focus(); button('Show all investigations').click(); });
  await until(() => host.querySelector('[role="tab"][aria-selected="true"]').textContent === 'All', 'the whole list');
  expect(document.activeElement).toBe(host.querySelector('[role="tab"][aria-selected="true"]'));
});

test('the market control sits in a field with its label above, like the other fields', async () => {
  serve([['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})]]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => host.querySelector('#inv42-market'), 'the market control');
  const field = host.querySelector('#inv42-market').parentElement;
  expect(field.classList.contains('inv42-field')).toBe(true);
  expect(field.querySelector('label').getAttribute('for')).toBe('inv42-market');
});

test('investigations copy says drafting uses model budget before Start', async () => {
  serve([['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})]]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => host.querySelector('.inv42-lede'), 'the page lede');
  const copy = plain(host.querySelector('.inv42-lede').textContent);
  /* Demo polish, 2 October 2026: the lede is one plain account of the steps
     instead of four clipped sentences. It still says that drafting the plan
     uses model spend before anything is started, and that research begins
     only when Start is pressed, so both facts are matched in full. */
  /* UX pass, 3 October 2026: the lede now opens with the question the page
     answers (pageQuestions.js); the account of the steps follows unchanged. */
  expect(copy).toBe("What is 42 researching for you in depth? 42 drafts a research plan first, and drafting uses some of today's model spend. Check the plan, then press Start and the research runs in the background.");
  expect(copy).toContain("drafting uses some of today's model spend");
  expect(copy).toContain('press Start and the research runs');
  expect(copy).not.toContain('nothing is spent until you start it');
});

test('New investigation drafts a plan from the question, market and angles, then opens it', async () => {
  serve([
    ['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})],
    ['POST', '/api/investigations', json(201, draft({market: 'NG', budget_left: {credits: 600, model_usd: 19.46}}))],
  ]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => host.querySelector('form'), 'the form');
  await act(async () => typeInto(host.querySelector('#inv42-question'), QUESTION));
  await act(async () => typeInto(host.querySelector('#inv42-market'), 'NG'));
  await act(async () => typeInto(host.querySelector('#inv42-angles'), 'music scenes\n\n  street food  \n'));
  await press('Draft a plan');
  await until(() => window.location.hash === '#/investigations/' + ID, 'the draft to open');
  expect(calls.find((c) => c.method === 'POST').body).toEqual({question: QUESTION, market: 'NG', angles: ['music scenes', 'street food']});
});

test('New investigation holds more than eight angles until the list is within the reviewed limit', async () => {
  serve([
    ['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})],
    ['POST', '/api/investigations', json(201, draft())],
  ]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => host.querySelector('form'), 'the form');
  await act(async () => typeInto(host.querySelector('#inv42-question'), QUESTION));
  const angleInput = host.querySelector('#inv42-angles');
  const angles = (count) => Array.from({length: count}, (_, index) => 'angle ' + (index + 1)).join('\n');
  await act(async () => typeInto(angleInput, angles(9)));
  expect(button('Draft a plan').disabled).toBe(true);
  expect(alertText()).toContain('Use at most 8 angles');
  expect(calls.some((call) => call.method === 'POST')).toBe(false);
  await act(async () => typeInto(angleInput, angles(8)));
  expect(button('Draft a plan').disabled).toBe(false);
  await press('Draft a plan');
  await until(() => window.location.hash === '#/investigations/' + ID, 'the draft to open');
  expect(calls.find((call) => call.method === 'POST').body.angles).toEqual(Array.from({length: 8}, (_, index) => 'angle ' + (index + 1)));
});

test('a draft refused for money or for want of the agent shows the server words and opens nothing', async () => {
  for (const [status, body] of [
    [429, {error: 'rate_limited', message: "Today's model budget is spent; try tomorrow or ask Albert to raise MODEL_DAILY_USD"}],
    [503, {error: 'agent_unavailable', message: 'Investigations need the T3 agent'}],
  ]){
    serve([['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})], ['POST', '/api/investigations', json(status, body)]]);
    await act(async () => root.render(<InvestigationsPage key={status} />));
    await until(() => host.querySelector('form'), 'the form');
    await act(async () => typeInto(host.querySelector('#inv42-question'), QUESTION));
    await press('Draft a plan');
    await until(() => alertText().includes(body.message), 'the refusal ' + status);
    expect(window.location.hash).toBe('#/investigations');
  }
});

/* ---------------- a draft, #/investigations/<id> ---------------- */

test('a draft shows the plan first: sub-questions with platforms and credits, researchers, gap round and the estimate', async () => {
  await openDraft();
  expect(host.querySelector('h1').textContent).toBe(QUESTION);
  const subs = [...host.querySelectorAll('[data-sub]')];
  expect(subs.map((node) => node.getAttribute('data-sub'))).toEqual(['q1', 'q2', 'q3']);
  expect(plain(subs[0].textContent)).toContain('Where did it start, and who posted about it first?');
  expect(plain(subs[0].textContent)).toContain('TikTok, X');
  expect(plain(subs[0].textContent)).toContain('120 credits');
  expect(plain(subs[2].textContent)).toContain('X, Reddit and Threads, YouTube');
  expect(text()).toContain('3 researchers');
  expect(text()).toContain('A gap round after the first pass');
  expect(text()).toContain('About 300 credits, USD 1.50 of model spend and 6 minutes');
  expect(text()).toContain('Stops at 400 credits or USD 3.00 of model spend');
  expect(text()).toContain('checked again when you start');
  const plan = host.querySelector('[data-section="plan"]');
  const estimate = host.querySelector('[data-section="estimate"]');
  expect(plan.compareDocumentPosition(estimate) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(button('Edit plan')).toBeDefined();
  const primaries = [...host.querySelectorAll('.ask42-primary')];
  expect(primaries.map((node) => plain(node.textContent))).toEqual(['Start']);
});

test('a draft opened from New investigation shows its estimate against the budget left today', async () => {
  serve([
    ['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})],
    ['POST', '/api/investigations', json(201, draft({budget_left: {credits: 1200, model_usd: 19.46}}))],
    ['GET', '/api/investigations/' + ID, json(200, draft())],
  ]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => host.querySelector('form'), 'the form');
  await act(async () => typeInto(host.querySelector('#inv42-question'), QUESTION));
  await press('Draft a plan');
  await until(() => window.location.hash === '#/investigations/' + ID, 'the draft to open');
  await act(async () => root.render(<InvestigationPage investigationId={ID} />));
  await until(() => text().includes('Left today'), 'the budget line');
  expect(text()).toContain('Left today: 1 200 credits and USD 19.46 of model spend');
});

test('a draft read again after a reload shows the budget left today that the read carries', async () => {
  await openDraft(draft({budget_left: {credits: 590, model_usd: 17.25}}));
  await until(() => text().includes('Left today'), 'the budget line');
  expect(text()).toContain('Left today: 590 credits and USD 17.25 of model spend. Both are checked again when you start.');
  expect(text()).not.toContain('What is left today is checked again when you start.');
});

test('a draft read whose budget left is null keeps the plain line and no stale figure', async () => {
  serve([
    ['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})],
    ['POST', '/api/investigations', json(201, draft({budget_left: {credits: 1200, model_usd: 19.46}}))],
  ]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => host.querySelector('form'), 'the form');
  await act(async () => typeInto(host.querySelector('#inv42-question'), QUESTION));
  await press('Draft a plan');
  await until(() => window.location.hash === '#/investigations/' + ID, 'the draft to open');
  await act(async () => root.unmount());
  root = createRoot(host);
  await openDraft(draft({budget_left: null}));
  await until(() => text().includes('The estimate'), 'the estimate');
  expect(text()).toContain('What is left today is checked again when you start.');
  expect(text()).not.toContain('Left today:');
});

test('Edit plan changes sub-question text, platforms and credits, keeps researchers per question, and saves through PUT', async () => {
  const saved = draft({version: 2, estimate: {credits: 250, model_usd: 1.5, minutes: 6}, budget_left: {credits: 580, model_usd: 18}});
  await openDraft(draft(), [['PUT', '/api/investigations/' + ID + '/plan', json(200, saved)]]);
  await press('Edit plan');
  expect(button('Start')).toBeUndefined();
  expect(host.querySelector('#inv42-researchers')).toBeNull();
  expect(text()).toContain('3 researchers, one per sub-question');
  const q1 = host.querySelector('[data-edit-sub="q1"]');
  await act(async () => typeInto(q1.querySelector('textarea'), 'Who posted it first?'));
  await act(async () => { q1.querySelector('input[type="checkbox"][value="x"]').click(); });
  await act(async () => { q1.querySelector('input[type="checkbox"][value="reddit_threads"]').click(); });
  await act(async () => typeInto(q1.querySelector('input[type="number"]'), '70'));
  await act(async () => { host.querySelector('#inv42-gap-round').click(); });
  await press('Save plan');
  await until(() => text().includes('About 250 credits'), 'the fresh estimate');
  const put = calls.find((c) => c.method === 'PUT');
  expect(put.body).toEqual({plan: {
    sub_questions: [{id: 'q1', text: 'Who posted it first?', platforms: ['tiktok', 'reddit_threads'], credits: 70}, PLAN.sub_questions[1], PLAN.sub_questions[2]],
    gap_round: false, max_credits: 400, max_model_usd: 3,
  }});
  expect(text()).toContain('Left today: 580 credits and USD 18.00 of model spend');
  expect(button('Start')).toBeDefined();
});

test('a plan the server will not take keeps the edit open with its words, and Cancel leaves the plan as it was', async () => {
  await openDraft(draft(), [['PUT', '/api/investigations/' + ID + '/plan', json(400, {error: 'bad_request', message: 'Sub-question q1 needs at least one platform.'})]]);
  await press('Edit plan');
  const q1 = host.querySelector('[data-edit-sub="q1"]');
  await act(async () => { q1.querySelector('input[value="tiktok"]').click(); });
  await act(async () => { q1.querySelector('input[value="x"]').click(); });
  await press('Save plan');
  await until(() => alertText().includes('Sub-question q1 needs at least one platform.'), 'the refusal');
  expect(button('Save plan')).toBeDefined();
  await press('Cancel');
  expect(plain(host.querySelector('[data-sub="q1"]').textContent)).toContain('TikTok, X');
});

const REFUSALS = [
  [409, 'This plan needs about 300 credits but only 120 are left today under ASK_DAILY. Trim the plan, try tomorrow or ask Albert to raise ASK_DAILY for today.'],
  [409, 'This plan needs about USD 1.50 of model spend but only USD 0.40 is left today under MODEL_DAILY_USD. Trim the plan, try tomorrow or ask Albert to raise MODEL_DAILY_USD for today.'],
  [409, "Today's spend could not be read, so 42 will not start an investigation that might overspend; try again in a few minutes."],
  [429, "Today's model budget is spent; try tomorrow or ask Albert to raise MODEL_DAILY_USD"],
  [503, 'Investigations need the T3 agent'],
];

test('every refusal at Start is shown in its own words and the plan stays a draft', async () => {
  for (const [status, message] of REFUSALS){
    await act(async () => root.unmount());
    root = createRoot(host);
    calls = [];
    await openDraft(draft(), [['POST', '/api/investigations/' + ID + '/start', json(status, {error: status === 503 ? 'agent_unavailable' : status === 429 ? 'rate_limited' : 'not_ready', message})]]);
    await press('Start');
    await until(() => alertText().includes(message), 'the refusal ' + message.slice(0, 30));
    expect(button('Start')).toBeDefined();
    expect(host.querySelector('[data-section="plan"]')).not.toBeNull();
    expect(calls.some((c) => c.path.endsWith('/events'))).toBe(false);
  }
});

/* ---------------- running, then finished ---------------- */

test('Start runs the investigation in the background with its live research log and Stop, then shows the checked answer', async () => {
  const stream = sseBody();
  let started = false;
  await openDraft(draft(), [
    ['POST', '/api/investigations/' + ID + '/start', () => { started = true; return json(202, {investigation_id: ID, status: 'running', ask_id: completeRecord.ask_id, ceilings: {max_credits: 400, max_model_usd: 3}, events_url: '/api/investigations/' + ID + '/events', url: '/api/investigations/' + ID}); }],
    ['GET', '/api/investigations/' + ID + '/events', {ok: true, status: 200, headers: {get: () => 'text/event-stream'}, body: stream.body}],
    ['POST', '/api/investigations/' + ID + '/stop', json(202, {investigation_id: ID, status: 'stopping'})],
    ['GET', '/api/investigations/' + ID, () => json(200, started ? finished() : draft())],
  ]);
  calls = [];
  await press('Start');
  await until(() => text().includes('Researching'), 'the live log');
  stream.push(frame(1, 'step', completeRecord.steps[0]));
  stream.push(frame(2, 'step', completeRecord.steps[1]));
  await until(() => text().includes(completeRecord.steps[1].text), 'the second step');
  expect(host.querySelector('.ask42-log-steps').getAttribute('aria-live')).toBe('polite');
  expect(text()).toContain('a notice arrives in Today');
  await press('Stop');
  await until(() => calls.some((c) => c.method === 'POST' && c.path === '/api/investigations/' + ID + '/stop'), 'the stop request');
  expect(button('Stopping')).toBeDefined();
  stream.push(frame(9, 'done', {seq: 9, status: 'complete', url: '/api/investigations/' + ID}));
  stream.close();
  await until(() => text().includes(completeRecord.answer.short_answer), 'the answer');
  expect(text()).toContain('How this was researched · 6 steps');
  const events = calls.find((c) => c.path.endsWith('/events'));
  expect(headerOf(events, 'X-Passcode')).toBe('fixture-pass');
});

test('when the finish row lands just after done, the page reads again rather than stay on a running run', async () => {
  const stream = sseBody();
  let reads = 0;
  const running = draft({status: 'running', ask_id: completeRecord.ask_id, record: {...finishedRecord(), status: 'running', answer: null, steps: []}});
  await openDraft(running, [
    ['GET', '/api/investigations/' + ID + '/events', {ok: true, status: 200, headers: {get: () => 'text/event-stream'}, body: stream.body}],
    ['GET', '/api/investigations/' + ID, () => { reads += 1; return json(200, reads <= 2 ? running : finished()); }],
  ]);
  await until(() => calls.some((c) => c.path.endsWith('/events')), 'the stream to open');
  stream.push(frame(9, 'done', {seq: 9, status: 'complete', url: '/api/investigations/' + ID}));
  stream.close();
  await until(() => text().includes(completeRecord.answer.short_answer), 'the answer after the finish row');
  expect(reads).toBe(3);
});

test('an investigation already running opens straight onto its live log', async () => {
  const stream = sseBody();
  await openDraft(draft({status: 'running', ask_id: completeRecord.ask_id, record: {...finishedRecord(), status: 'running', answer: null, steps: []}}), [
    ['GET', '/api/investigations/' + ID + '/events', {ok: true, status: 200, headers: {get: () => 'text/event-stream'}, body: stream.body}],
  ]);
  await until(() => calls.some((c) => c.path.endsWith('/events')), 'the stream to open');
  stream.push(frame(1, 'step', completeRecord.steps[0]));
  await until(() => text().includes(completeRecord.steps[0].text), 'the first step');
  expect(button('Stop')).toBeDefined();
  expect(button('Start')).toBeUndefined();
});

test('a finished investigation renders its answer as Ask does: short answer, labelled claims, gaps and the folded log', async () => {
  await openDraft(finished());
  await until(() => text().includes(completeRecord.answer.short_answer), 'the answer');
  const claims = [...host.querySelectorAll('.ask42-claim')];
  expect(claims).toHaveLength(completeRecord.answer.claims.length);
  expect(plain(claims[0].textContent)).toContain('Corroborated');
  expect(plain(claims[1].textContent)).toContain('Inferred');
  expect(text()).toContain('What we do not know');
  expect(text()).toContain('How this was researched · 6 steps');
  expect(host.querySelector('.ask42-short').textContent).toBe(completeRecord.answer.short_answer);
  const primaries = [...host.querySelectorAll('.ask42-primary')];
  expect(primaries.map((node) => plain(node.textContent))).toEqual(['Open as dossier']);
});

test('cited source disclosure keeps focus when expanding and collapsing', async () => {
  await openDraft(finished());
  await until(() => text().includes(completeRecord.answer.short_answer), 'the answer');
  const claim = host.querySelector('.ask42-claim');
  const sourceHandles = () => [...claim.querySelectorAll('.ask42-chip-handle')].map((node) => plain(node.textContent));
  const expectedHandles = completeRecord.answer.claims[0].evidence_ids.map((id) => completeRecord.answer.evidence.find((record) => record.id === id).handle);
  const disclosure = claim.querySelector('.ask42-chip-more');
  expect(sourceHandles()).toEqual([expectedHandles[0]]);
  expect(disclosure.getAttribute('aria-expanded')).toBe('false');
  expect(disclosure.getAttribute('aria-label')).toBe('Show 1 more source');
  expect(plain(disclosure.textContent)).toBe('+1');

  disclosure.focus();
  expect(document.activeElement === disclosure).toBe(true);
  await act(async () => disclosure.click());
  expect(claim.querySelector('.ask42-chip-more') === disclosure).toBe(true);
  expect(document.activeElement === disclosure).toBe(true);
  expect(disclosure.getAttribute('aria-expanded')).toBe('true');
  expect(disclosure.getAttribute('aria-label')).toBe('Show fewer sources');
  expect(plain(disclosure.textContent)).toBe('Fewer');
  expect(sourceHandles()).toEqual(expectedHandles);

  disclosure.focus();
  await act(async () => disclosure.click());
  expect(claim.querySelector('.ask42-chip-more') === disclosure).toBe(true);
  expect(document.activeElement === disclosure).toBe(true);
  expect(disclosure.getAttribute('aria-expanded')).toBe('false');
  expect(disclosure.getAttribute('aria-label')).toBe('Show 1 more source');
  expect(plain(disclosure.textContent)).toBe('+1');
  expect(sourceHandles()).toEqual([expectedHandles[0]]);
});

test('a single cited source and an answer with no claims have no source disclosure', async () => {
  const single = finished();
  for (const claim of single.record.answer.claims){
    const firstEvidenceId = claim.evidence_ids[0];
    claim.evidence_ids = [firstEvidenceId];
    claim.quotes = claim.quotes.filter((quote) => quote.evidence_id === firstEvidenceId);
  }
  await openDraft(single);
  await until(() => text().includes(completeRecord.answer.short_answer), 'the single source answer');
  expect(host.querySelectorAll('.ask42-chip-more').length).toBe(0);
  expect(host.querySelector('.ask42-claim').querySelectorAll('.ask42-chip:not(.ask42-chip-more)').length).toBe(1);

  await act(async () => root.unmount());
  root = createRoot(host);
  const noClaims = finished();
  noClaims.record.answer.status = 'partial';
  noClaims.record.answer.claims = [];
  noClaims.record.answer.so_what = [];
  noClaims.record.answer.watch_next = [];
  await openDraft(noClaims);
  await until(() => host.querySelector('.ask42-answer'), 'the answer without claims');
  expect(host.querySelectorAll('.ask42-claim').length).toBe(0);
  expect(host.querySelectorAll('.ask42-chip-more').length).toBe(0);
});

test('an answer that fails the contract check is refused and nothing from it renders', async () => {
  const broken = finished();
  broken.record.answer.claims[0].label = 'certain';
  await openDraft(broken);
  await until(() => text().includes('This answer did not pass its checks'), 'the refusal');
  expect(host.querySelector('.ask42-claim')).toBeNull();
  expect(text()).not.toContain(completeRecord.answer.short_answer);
  expect(button('Open as dossier')).toBeUndefined();
});

test('a failed investigation shows its own message', async () => {
  await openDraft(finished({status: 'failed', record: {...finishedRecord(), status: 'failed', answer: null, error: {error: 'internal', message: 'The agent stopped with an error before any claim passed its checks.'}}}));
  await until(() => alertText().includes('The agent stopped with an error before any claim passed its checks.'), 'the failure');
  expect(button('Open as dossier')).toBeUndefined();
});

test('Open as dossier creates a draft dossier from the investigation and opens it', async () => {
  await openDraft(finished(), [['POST', '/api/dossiers', json(201, {dossier_id: 'd_inv01', version: 1, state: 'draft'})]]);
  await until(() => button('Open as dossier'), 'the button');
  await press('Open as dossier');
  await until(() => window.location.hash === '#/dossiers/d_inv01', 'the dossier to open');
  expect(calls.find((c) => c.method === 'POST').body).toEqual({from: {investigation_id: ID}});
});

test('a 401 anywhere hands the reader to the passcode flow', async () => {
  let asked = 0;
  serve([['GET', '/api/investigations/' + ID, json(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})]]);
  await act(async () => root.render(<InvestigationPage investigationId={ID} onAuth={() => { asked += 1; }} />));
  await until(() => asked === 1, 'the handover on read');
  await act(async () => root.unmount());
  root = createRoot(host);
  await openDraft(draft(), [['POST', '/api/investigations/' + ID + '/start', json(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})]]);
  await act(async () => root.render(<InvestigationPage investigationId={ID} onAuth={() => { asked += 1; }} />));
  await press('Start');
  await until(() => asked === 2, 'the handover on start');
});

test('the copy names no age group, no Google Trends and no generated evidence', async () => {
  await openDraft();
  const words = text().toLowerCase();
  for (const banned of ['gen z', 'millennial', 'youth', 'google trends', 'generated', 'nano banana']) expect(words).not.toContain(banned);
});

/* Audit fixes, 4 October 2026: the plan editor offers the platform groups the
   agent's researchers search (core/agent/ask.py PLATFORM_GROUPS), credits take
   cents as the planner drafts them, and a stored plan with older names reads
   plainly. */
const GROUPS = [['tiktok', 'TikTok'], ['x', 'X'], ['instagram', 'Instagram'], ['youtube', 'YouTube'],
  ['facebook', 'Facebook'], ['reddit_threads', 'Reddit and Threads'], ['news', 'News and web search']];

test('the plan editor offers the agent platform groups with readable names and nothing it cannot search', async () => {
  await openDraft();
  await press('Edit plan');
  const q1 = host.querySelector('[data-edit-sub="q1"]');
  const boxes = [...q1.querySelectorAll('.inv42-platforms input[type="checkbox"]')];
  expect(boxes.map((box) => box.value)).toEqual(GROUPS.map(([id]) => id));
  expect(boxes.map((box) => plain(box.parentElement.textContent))).toEqual(GROUPS.map(([, word]) => word));
  expect(plain(q1.textContent)).not.toContain('Telegram');
  expect(plain(q1.textContent)).not.toContain('Apple Music');
});

test('credits take cents: the editor steps by 0.01 and saves the number to two decimals', async () => {
  await openDraft(draft(), [['PUT', '/api/investigations/' + ID + '/plan', json(200, draft({version: 2}))]]);
  await press('Edit plan');
  const q1 = host.querySelector('[data-edit-sub="q1"]');
  const credits = q1.querySelector('input[type="number"]');
  expect(credits.getAttribute('step')).toBe('0.01');
  expect(credits.getAttribute('min')).toBe('0');
  await act(async () => typeInto(credits, '64.2857'));
  await press('Save plan');
  await until(() => calls.some((c) => c.method === 'PUT'), 'the save');
  expect(calls.find((c) => c.method === 'PUT').body.plan.sub_questions[0].credits).toBe(64.29);
});

test('a drafted plan with cents shows them as drafted', async () => {
  const plan = clone(PLAN);
  plan.sub_questions[0].credits = 64.28;
  await openDraft(draft({plan}));
  expect(plain(host.querySelector('[data-sub="q1"]').textContent)).toContain('64.28 credits');
});

test('a stored plan with older platform names reads plainly and saves as the agent groups', async () => {
  const plan = clone(PLAN);
  plan.sub_questions[0].platforms = ['twitter', 'reddit', 'threads'];
  plan.sub_questions[1].platforms = ['tiktok', 'telegram', 'apple_music'];
  await openDraft(draft({plan}), [['PUT', '/api/investigations/' + ID + '/plan', json(200, draft({version: 2}))]]);
  expect(plain(host.querySelector('[data-sub="q1"]').textContent)).toContain('X, Reddit, Threads');
  expect(plain(host.querySelector('[data-sub="q2"]').textContent)).toContain('TikTok, Telegram, Apple Music');
  await press('Edit plan');
  const q1 = host.querySelector('[data-edit-sub="q1"]');
  expect(q1.querySelector('input[value="x"]').checked).toBe(true);
  expect(q1.querySelector('input[value="reddit_threads"]').checked).toBe(true);
  const q2 = host.querySelector('[data-edit-sub="q2"]');
  const telegram = q2.querySelector('input[value="telegram"]');
  expect(telegram.checked).toBe(true);
  expect(plain(telegram.parentElement.textContent)).toBe('Telegram (42 cannot search it)');
  await act(async () => { telegram.click(); });
  await act(async () => { q2.querySelector('input[value="apple_music"]').click(); });
  await press('Save plan');
  await until(() => calls.some((c) => c.method === 'PUT'), 'the save');
  const subs = calls.find((c) => c.method === 'PUT').body.plan.sub_questions;
  expect(subs[0].platforms).toEqual(['x', 'reddit_threads']);
  expect(subs[1].platforms).toEqual(['tiktok']);
});

test('a follow-up on a finished investigation fills Ask with the question and does not start a paid ask', async () => {
  await openDraft(finished());
  await until(() => text().includes(completeRecord.answer.short_answer), 'the answer');
  const label = completeRecord.run.followups[0];
  const node = buttons().find((b) => plain(b.textContent) === plain(label));
  expect(node).toBeDefined();
  await act(async () => { node.click(); });
  const [path, query] = window.location.hash.split('?');
  expect(path).toBe('#/ask');
  const params = new URLSearchParams(query);
  expect(params.get('q')).toBe(label);
  expect(params.get('market')).toBe(completeRecord.market);
  expect(params.get('draft')).toBe('1');
});

test('the drafting hint says drafting uses a little model spend and the research waits for Start', async () => {
  serve([['GET', /^\/api\/investigations(\?.*)?$/, json(200, {investigations: []})]]);
  await act(async () => root.render(<InvestigationsPage />));
  await until(() => host.querySelector('.inv42-hint'), 'the hint');
  const hint = () => plain(host.querySelector('.inv42-hint').textContent);
  expect(hint()).toBe('Write the question first. Drafting the plan uses a little model spend; the research itself waits until you press Start.');
  await act(async () => typeInto(host.querySelector('#inv42-question'), QUESTION));
  await act(async () => typeInto(host.querySelector('#inv42-market'), 'NG'));
  expect(hint()).toBe('42 drafts a plan for Nigeria, which uses a little model spend. The research itself waits until you press Start.');
  expect(text()).not.toContain('before anything runs');
  expect(text()).not.toContain('Nothing runs');
});
