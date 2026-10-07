/* Ask on the 42 API (task 1.15, Ask half). The transport reads the event
   stream with fetch so the passcode header goes with it, parses server-sent
   events across chunk boundaries, and falls back to polling the record when
   the stream drops. The page shows the research as it happens, then renders
   the finished record only after the answer passes the shared contract
   check. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import {readFileSync} from 'node:fs';
import completeRecord from './fixtures/ask42_complete.json';
import partialRecord from './fixtures/ask42_partial.json';
import failedRecord from './fixtures/ask42_failed.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const transport = await import('../../askTransport42.js');
const {AskPage} = await import('../../ask42.jsx');
const {parseAskQuery} = await import('../../App.jsx');
const {sourceMarketLabel} = await import('../EvidenceChip.jsx');

const CEILING = 4000;
const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;
const realCreate = URL.createObjectURL;
const realRevoke = URL.revokeObjectURL;
const realAnchorClick = window.HTMLAnchorElement.prototype.click;

const clone = (value) => JSON.parse(JSON.stringify(value));
const plain = (text) => String(text || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();

let host = null;
let root = null;
let calls = [];
let saved = [];
let downloads = [];
let findingResponder = null;

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  calls = [];
  saved = [];
  downloads = [];
  findingResponder = null;
  window.history.replaceState(null, '', '#/');
  window.localStorage.setItem('pulse_passcode', 'fixture-pass');
  URL.createObjectURL = (blob) => { saved.push(blob); return 'blob:answer'; };
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
  window.fetch = realWindowFetch;
  URL.createObjectURL = realCreate;
  URL.revokeObjectURL = realRevoke;
  window.HTMLAnchorElement.prototype.click = realAnchorClick;
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

/* A response body whose chunks the test hands over one at a time. */
function sseBody(){
  const encoder = new TextEncoder();
  const queue = [];
  let waiting = null;
  let closed = false;
  let failure = null;
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
    fail(error){
      failure = error;
      if (waiting){ const resolve = waiting; waiting = null; resolve(Promise.reject(error)); }
    },
    body: {
      getReader(){
        return {
          read(){
            if (queue.length) return Promise.resolve(queue.shift());
            if (failure) return Promise.reject(failure);
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

test('only flagged recognized source markets get an assumed market label', () => {
  for (const [source_market, expected] of [
    ['ZA', 'Market assumed: South Africa'],
    ['NG', 'Market assumed: Nigeria'],
    ['KE', 'Market assumed: Kenya'],
  ]){
    expect(sourceMarketLabel({source_market, market: 'ZA', flags: ['market_assumed']})).toBe(expected);
  }
  expect(sourceMarketLabel({source_market: 'KE', market: 'KE', flags: []})).toBe('');
  expect(sourceMarketLabel({source_market: null, market: 'ZA', flags: ['market_assumed']})).toBe('');
  expect(sourceMarketLabel({market: 'ZA', flags: ['market_assumed']})).toBe('');
  expect(sourceMarketLabel({source_market: 'GH', market: 'ZA', flags: ['market_assumed']})).toBe('');
});

/* A fake 42 API: each POST /api/ask takes the next record, each record has
   its own event stream. */
function serve(records){
  const byId = new Map();
  const streams = new Map();
  const order = records.map((record) => record.ask_id);
  let next = 0;
  for (const record of records){
    byId.set(record.ask_id, record);
    streams.set(record.ask_id, sseBody());
  }
  const stub = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    const call = {path, method, headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : null};
    calls.push(call);
    if (method === 'POST' && path === '/api/findings'){
      if (findingResponder) return findingResponder(call);
      return json(201, {finding_id: 'f_fixture_1', source_ask_id: call.body.from.ask_id});
    }
    if (method === 'POST' && path === '/api/ask'){
      const id = order[Math.min(next, order.length - 1)];
      next += 1;
      return json(202, {ask_id: id, status: 'running', events_url: `/api/ask/${id}/events`, url: `/api/ask/${id}`});
    }
    const match = /^\/api\/ask\/([^/?]+)(\/[a-z]+)?(\?.*)?$/.exec(path);
    if (!match || !byId.has(match[1])) return json(404, {error: 'not_found', message: 'No such question.'});
    const id = match[1];
    if (match[2] === '/events') return {ok: true, status: 200, headers: {get: () => 'text/event-stream'}, body: streams.get(id).body};
    if (match[2] === '/stop' && method === 'POST') return json(202, {ask_id: id, status: 'stopping'});
    if (match[2] === '/export') return {ok: true, status: 200, headers: {get: () => 'text/html'}, blob: async () => new Blob(['<html>answer</html>'], {type: 'text/html'})};
    if (!match[2]) return json(200, byId.get(id));
    return json(404, {error: 'not_found', message: 'No such route.'});
  };
  globalThis.fetch = stub;
  window.fetch = stub;
  return {stream: (id) => streams.get(id)};
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
  const headers = call.headers;
  if (typeof headers.get === 'function') return headers.get(name);
  const key = Object.keys(headers).find((k) => k.toLowerCase() === name.toLowerCase());
  return key ? headers[key] : undefined;
};

function typeInto(element, value){
  const proto = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, value);
  element.dispatchEvent(new window.Event('input', {bubbles: true}));
}

async function render(props){
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={{}} {...props} />));
}

for (const kind of ['sounds', 'hashtags']){
test(`a checked ${kind} projection appears before the short answer prose`, async () => {
  const record = clone(completeRecord);
  record.question = `Which ${kind} are rising on TikTok in South Africa this week?`;
  const claim = record.answer.claims[0];
  const title = kind === 'sounds' ? 'Named sound' : '#amapiano';
  claim.text = title + ' appeared in posts.';
  claim.numbers = [
    {value: 11, unit: 'creators', query_id: 'q_ranked', run_id: record.run.run_id, result_hash: 'sha256:' + 'a'.repeat(64)},
    {value: 13, unit: 'posts', query_id: 'q_ranked', run_id: record.run.run_id, result_hash: 'sha256:' + 'a'.repeat(64)},
  ];
  record.run.ranked_list = {kind, scope: 'retained_whole_store_claims', order: 'creators_desc_posts_desc',
    query_id: 'q_ranked', market: 'ZA', platform: 'tiktok', measure_columns: {creators: 'creators', posts: 'posts'},
    window: record.run.window, previous_window: null,
    items: [{claim_id: claim.id, title, usage_handle: null, creators_index: 0, posts_index: 1,
      previous_posts_index: null, tied_with_previous: false}]};
  const server = serve([record]);
  await render();
  await askByTyping(record.question);
  await finish(server, record);
  await until(() => host.querySelector('.ask42-answer .ask42-short'), 'finished answer is visible');
  const ranked = host.querySelector('.ask42-ranked');
  const summary = host.querySelector('.ask42-short');
  expect(ranked).not.toBeNull();
  expect(Boolean(ranked.compareDocumentPosition(summary) & window.Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
  expect(plain(ranked.textContent)).toContain(title);
});
}

test('only draft=1 marks an Ask query as unsubmitted', () => {
  expect(parseAskQuery('#/ask?q=football&draft=1').draft).toBe(true);
  for (const value of ['true', '0', '01', '']){
    expect(parseAskQuery('#/ask?q=football&draft=' + value).draft).toBeUndefined();
  }
});

test('a draft query fills the Ask field without starting a paid request', async () => {
  serve([clone(completeRecord)]);
  const question = 'football & street culture';
  await render({query: {q: question, market: 'ZA', draft: true}});
  expect(host.querySelector('textarea')?.value).toBe(question);
  expect(host.querySelector('select')?.value).toBe('ZA');
  expect(calls.filter((call) => call.method === 'POST' && call.path === '/api/ask')).toEqual([]);
});

test('Ask offers the brand lens and context pack only when /api/health says t2_ready is true', async () => {
  serve([clone(completeRecord)]);
  await render({});
  expect(host.querySelector('[data-skill="brand-lens"]')).toBeNull();
  expect(host.querySelector('[data-skill="context-pack"]')).toBeNull();
  await render({health: {ok: true, t2_ready: false}});
  expect(host.querySelector('[data-skill="brand-lens"]')).toBeNull();
  await render({health: {ok: true, t2_ready: 'true'}});
  expect(host.querySelector('[data-skill="brand-lens"]')).toBeNull();
  await render({health: {ok: true, t2_ready: true}});
  expect(host.querySelector('[data-skill="brand-lens"]')).not.toBeNull();
  expect(host.querySelector('[data-skill="context-pack"]')).not.toBeNull();
});

async function askByTyping(question){
  const box = host.querySelector('textarea');
  expect(box).not.toBeNull();
  await act(async () => typeInto(box, question));
  await act(async () => { button('Ask').click(); });
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the ask to start');
}

/* Run a record to the end through its stream: replay the steps, then done. */
async function finish(server, record){
  const stream = server.stream(record.ask_id);
  for (const step of record.steps) stream.push(frame(step.seq, 'step', step));
  stream.push(frame(99, 'done', {seq: 99, status: record.status, url: `/api/ask/${record.ask_id}`}));
  stream.close();
}

/* ---------------- transport ---------------- */

test('the event parser joins a stream split mid-line and ignores comment lines', () => {
  const messages = [];
  const parser = transport.createSSEParser((message) => messages.push(message));
  parser.push('id: 1\nevent: st');
  parser.push('ep\ndata: {"text":"Reading');
  parser.push(' TikTok"}\n');
  parser.push('\n: keepalive\n\nid: 2\r\nevent: done\r');
  parser.push('\ndata: {"status":"complete"}\r\n\r\n');
  expect(messages).toEqual([
    {id: '1', event: 'step', data: '{"text":"Reading TikTok"}'},
    {id: '2', event: 'done', data: '{"status":"complete"}'},
  ]);
});

test('streamAsk hands each event over parsed, sends the passcode and Last-Event-ID, and resolves on done', async () => {
  const server = serve([clone(completeRecord)]);
  const id = completeRecord.ask_id;
  const stream = server.stream(id);
  const seen = [];
  const running = transport.streamAsk(id, (event) => seen.push(event), {lastEventId: '3'});
  stream.push(frame(4, 'step', completeRecord.steps[2]).slice(0, 17));
  stream.push(frame(4, 'step', completeRecord.steps[2]).slice(17));
  stream.push(': keepalive\n\n');
  stream.push(frame(5, 'evidence', {seq: 5, evidence: completeRecord.answer.evidence[0]}));
  stream.push(frame(12, 'done', {seq: 12, status: 'complete', url: `/api/ask/${id}`}));
  const result = await running;
  expect(result.status).toBe('complete');
  expect(seen.map((event) => event.type)).toEqual(['step', 'evidence', 'done']);
  expect(seen[0].data.text).toBe(completeRecord.steps[2].text);
  expect(seen[1].data.evidence.id).toBe('tt_fixture_1');
  const call = calls.find((entry) => entry.path === `/api/ask/${id}/events`);
  expect(headerOf(call, 'X-Passcode')).toBe('fixture-pass');
  expect(headerOf(call, 'Last-Event-ID')).toBe('3');
});

test('when the stream cannot open, streamAsk polls the record until it stops running', async () => {
  const id = completeRecord.ask_id;
  const running = {...clone(completeRecord), status: 'running', answer: null, finished_at: null, steps: completeRecord.steps.slice(0, 2)};
  let reads = 0;
  const stub = async (url, init = {}) => {
    const path = String(url);
    calls.push({path, method: init.method || 'GET', headers: init.headers || {}});
    if (path.endsWith('/events')) throw new TypeError('network dropped');
    reads += 1;
    return json(200, reads < 3 ? running : clone(completeRecord));
  };
  globalThis.fetch = stub;
  window.fetch = stub;
  const seen = [];
  const result = await transport.streamAsk(id, (event) => seen.push(event), {pollMs: 1});
  expect(result.status).toBe('complete');
  expect(result.record.ask_id).toBe(id);
  expect(reads).toBe(3);
  expect(calls.filter((entry) => entry.path === `/api/ask/${id}`).every((entry) => headerOf(entry, 'X-Passcode') === 'fixture-pass')).toBe(true);
  expect(seen.filter((event) => event.type === 'step').map((event) => event.data.seq)).toEqual(completeRecord.steps.map((step) => step.seq));
});

test('a stream that drops mid-way falls back to polling from where it got to', async () => {
  const server = serve([clone(completeRecord)]);
  const id = completeRecord.ask_id;
  const stream = server.stream(id);
  const seen = [];
  const running = transport.streamAsk(id, (event) => seen.push(event), {pollMs: 1});
  stream.push(frame(1, 'step', completeRecord.steps[0]));
  await until(() => seen.length === 1, 'the first step');
  stream.fail(new TypeError('connection reset'));
  const result = await running;
  expect(result.status).toBe('complete');
  expect(calls.some((entry) => entry.path === `/api/ask/${id}`)).toBe(true);
  const seqs = seen.filter((event) => event.type === 'step').map((event) => event.data.seq);
  expect(seqs).toEqual(completeRecord.steps.map((step) => step.seq));
});

test('transport errors carry status, code and auth', async () => {
  let reply = json(401, {error: 'unauthorized', message: 'Passcode required.'});
  const stub = async () => reply;
  globalThis.fetch = stub;
  window.fetch = stub;
  const auth = await transport.startAsk({question: 'What is moving?'}).catch((error) => error);
  expect(auth.auth).toBe(true);
  expect(auth.status).toBe(401);
  reply = json(429, {error: 'daily_question_limit', message: 'The daily question limit is reached.'});
  const limit = await transport.getAsk('a_x').catch((error) => error);
  expect(limit.status).toBe(429);
  expect(limit.code).toBe('daily_question_limit');
  expect(limit.message).toBe('The daily question limit is reached.');
  expect(limit.auth).toBeFalsy();
});

test('downloadExport fetches the HTML with the passcode header and saves it as 42-answer-<id>.html', async () => {
  serve([clone(completeRecord)]);
  const id = completeRecord.ask_id;
  await transport.downloadExport(id);
  const call = calls.find((entry) => entry.path === `/api/ask/${id}/export?format=html`);
  expect(call).toBeDefined();
  expect(headerOf(call, 'X-Passcode')).toBe('fixture-pass');
  expect(saved.length).toBe(1);
  expect(downloads).toEqual([{href: 'blob:answer', download: `42-answer-${id}.html`}]);
});

/* ---------------- page ---------------- */

test('Ask starts with a usable empty form and no request', async () => {
  await render();
  expect(host.querySelector('[data-ask-state="empty"]')).not.toBeNull();
  expect(host.querySelector('textarea').disabled).toBe(false);
  expect(host.querySelector('select#ask42-market').disabled).toBe(false);
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);
});

test('Ctrl or Cmd with Enter asks the typed question once; Enter alone adds a line', async () => {
  serve([clone(completeRecord)]);
  await render();
  const field = host.querySelector('textarea');
  await act(async () => { typeInto(field, 'What is behind amapiano this week?'); });
  const posts = () => calls.filter((call) => call.method === 'POST' && call.path === '/api/ask');
  await act(async () => { field.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true})); });
  expect(posts()).toHaveLength(0);
  await act(async () => { field.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', ctrlKey: true, bubbles: true, cancelable: true})); });
  expect(posts()).toHaveLength(1);
  await act(async () => { field.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', metaKey: true, bubbles: true, cancelable: true})); });
  expect(posts()).toHaveLength(1);
});

test('Cmd with Enter asks on its own, and not while a character is still being composed', async () => {
  serve([clone(completeRecord)]);
  await render();
  const field = host.querySelector('textarea');
  await act(async () => { typeInto(field, 'What is behind amapiano this week?'); });
  const posts = () => calls.filter((call) => call.method === 'POST' && call.path === '/api/ask');
  await act(async () => { field.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', metaKey: true, isComposing: true, bubbles: true, cancelable: true})); });
  expect(posts()).toHaveLength(0);
  await act(async () => { field.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', metaKey: true, bubbles: true, cancelable: true})); });
  expect(posts()).toHaveLength(1);
});

test('the Ask button names its shortcut and the empty page says how to use it', async () => {
  await render();
  const submit = button('Ask');
  expect(submit.getAttribute('aria-keyshortcuts')).toBe('Control+Enter Meta+Enter');
  expect(plain(host.querySelector('[data-ask-state="empty"]').textContent)).toContain('Ctrl+Enter');
});

test('a running Ask has a polite status outside its busy region and clears busy on failure', async () => {
  let rejectStart;
  const pending = new Promise((_resolve, reject) => { rejectStart = reject; });
  const stub = async (url, init = {}) => {
    const path = String(url);
    calls.push({path, method: String(init.method || 'GET').toUpperCase(), headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : null});
    if (path === '/api/ask') return pending;
    return json(404, {error: 'not_found', message: 'No such question.'});
  };
  globalThis.fetch = stub;
  window.fetch = stub;
  await render();
  await askByTyping('What is moving in South Africa?');

  const status = host.querySelector('[data-ask-status][role="status"]');
  expect(status).not.toBeNull();
  expect(status.textContent).toContain('Starting research');
  expect(status.closest('[aria-busy="true"]')).toBeNull();
  expect(host.querySelector('[aria-busy="true"]')).not.toBeNull();
  expect(button('Try again')).toBeUndefined();

  await act(async () => { rejectStart(new Error('Service timed out.')); });
  await until(() => host.querySelector('.ask42-failed[role="alert"]'), 'the failed start');
  expect(text()).toContain('Service timed out.');
  expect(host.querySelector('[aria-busy="true"]')).toBeNull();
  expect(button('Try again')).not.toBeUndefined();
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(1);
});

test('a full run shows the research as it happens, then the checked answer', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  const id = record.ask_id;
  await render();
  await askByTyping(record.question);
  const posted = calls.find((call) => call.path === '/api/ask');
  expect(posted.body).toEqual({question: record.question, market: 'ZA', parent_id: null});
  expect(headerOf(posted, 'X-Passcode')).toBe('fixture-pass');

  const stream = server.stream(id);
  stream.push(frame(1, 'step', record.steps[0]));
  stream.push(frame(2, 'step', record.steps[1]));
  await until(() => text().includes('Reading TikTok posts tagged #fixture, South Africa, 21 to 27 September: 312 found'), 'the second step');
  expect(host.querySelector('[aria-live]')).not.toBeNull();
  expect(text()).toContain('Planning 3 searches');

  stream.push(frame(3, 'evidence', {seq: 3, evidence: record.answer.evidence[0]}));
  await until(() => host.querySelector('img[src="https://example.invalid/thumbs/tt_fixture_1.jpg"]'), 'a gathered thumbnail');

  stream.push(frame(8, 'claim', {seq: 8, claim: record.answer.claims[0], check: 'checking', reason: null}));
  await until(() => text().includes('Checking'), 'the claim marked checking');
  expect(text()).toContain(record.answer.claims[0].text);
  expect(button('Stop')).toBeDefined();

  stream.push(frame(9, 'claim', {seq: 9, claim: record.answer.claims[0], check: 'verified', reason: null}));
  await until(() => text().includes('Verified') && !text().includes('Checking'), 'the claim marked verified');

  for (const step of record.steps.slice(2)) stream.push(frame(step.seq, 'step', step));
  stream.push(frame(12, 'done', {seq: 12, status: 'complete', url: `/api/ask/${id}`}));
  stream.close();

  await until(() => text().includes('What we do not know'), 'the finished answer');
  expect(calls.some((call) => call.path === `/api/ask/${id}` && call.method === 'GET')).toBe(true);
  expect(button('Stop')).toBeUndefined();

  const heading = [...host.querySelectorAll('h1, h2')].find((node) => plain(node.textContent) === record.question);
  expect(heading).toBeDefined();
  expect(text()).toContain('South Africa · posts from 21 to 27 September 2026 · 214 posts from 2 platforms');
  expect(text()).toContain(record.answer.short_answer);
  expect(text()).toContain('Corroborated');
  expect(text()).toContain('Inferred');
  const chip = buttons().find((node) => plain(node.textContent).includes('@fixture_za_1'));
  expect(chip).toBeDefined();
  expect(chip.getAttribute('aria-label')).toBeTruthy();

  const strip = host.querySelector('[aria-label="Posts"]');
  expect(strip).not.toBeNull();
  const cards = strip.querySelectorAll('li');
  expect(cards.length).toBe(2);
  expect(plain(cards[0].textContent)).toContain('0:21');
  expect(plain(cards[0].textContent)).toContain('184 000 views');
  expect(plain(cards[0].textContent)).toContain('0:04');
  expect(plain(cards[0].textContent)).toContain('kitchen table, one pot, everybody dances');
  expect(strip.querySelector('video')).toBeNull();

  expect(text()).toContain('312 TikTok posts tagged #fixture in 7 days');
  expect(text()).toContain('What it means for a brand');
  expect(text()).toContain(record.answer.so_what[0].text);
  expect(text()).toContain('What to watch');
  expect(text()).toContain(record.answer.watch_next[0].text);
  expect(text()).toContain('No Instagram posts in the window');
  /* QA, 2 Oct 2026, item 6: a gap names the platform, not the route, and says
     "nothing found" once rather than "0 found · nothing found". */
  const gap = [...host.querySelectorAll('#ask42-gaps-title ~ ul li')].map((node) => plain(node.textContent));
  expect(gap).toEqual(['No Instagram posts in the window · Searched Instagram for #fixture, 21 to 27 September']);
  /* Design critique, 2 October 2026: a row never starts with a middot. The
     middot is tied to the words before it and the search is one whole fact. */
  const gapRow = host.querySelector('#ask42-gaps-title ~ ul li');
  expect(gapRow.querySelector('.ask42-muted').firstChild.textContent).toBe('\u00a0· ');
  expect(gapRow.querySelector('.ask42-muted .fact-unit')?.textContent).toBe('Searched Instagram for #fixture, 21 to 27 September');
  expect(text()).toContain('How this was researched · 6 steps');
  expect(text()).toContain('214 credits · 41 s');
  expect(button('Export answer')).toBeDefined();
  /* EXPERIENCE.md, Ask: one red "Add to dossier"; Export answer is quiet. */
  expect([...host.querySelectorAll('.ask42-primary')].map((node) => plain(node.textContent))).toEqual(['Add to dossier']);
  expect(button('Export answer').className).toContain('ask42-quiet');
  for (const followup of record.run.followups) expect(button(followup)).toBeDefined();
  /* Demo run, 2 Oct 2026: the run id, tier, tokens and source routes moved
     from a "Details" toggle to a closed "Technical details" one, and none of
     them shows outside it. */
  const details = [...host.querySelectorAll('details')].find((node) => plain(node.querySelector('summary')?.textContent) === 'Technical details');
  expect(details).toBeDefined();
  expect(details.open).toBe(false);
  /* QA, 2 Oct 2026, item 5: even behind the toggle a reader sees no run id,
     query id, token count or source route; the ids stay on the page as data
     attributes for support. */
  expect(plain(details.textContent)).not.toContain(record.run.run_id);
  expect(details.getAttribute('data-run-id')).toBe(record.run.run_id);
  expect(plain(details.textContent)).not.toContain('Tokens');
  expect(plain(details.textContent)).not.toMatch(/tiktok\/hashtag|x\/search|instagram\/hashtag/);
  expect(plain(details.textContent)).toContain('TikTok · read · 312 items');
  /* UI notes, 2 Oct 2026: the tier reads as words (T1 is a quick scan) and the
     credits and seconds sit inside the closed toggle, not under the answer. */
  expect(plain(details.textContent)).toContain('Quick scan');
  expect(plain(details.textContent)).toContain('214 credits · 41 s');
  const footer = host.querySelector('.ask42-footer').cloneNode(true);
  footer.querySelector('details').remove();
  expect(plain(footer.textContent)).not.toContain(record.run.run_id);
  expect(plain(footer.textContent)).not.toContain('Tier');
  expect(plain(footer.textContent)).not.toContain('Tokens');
  expect(plain(footer.textContent)).not.toContain('credits');
  /* The same day: each figure's query id left the reader's view. QA item 5
     took it out of Technical details too; it stays as each figure's data. */
  const figureQueries = record.answer.claims.flatMap((claim) => claim.numbers || []).map((number) => number.query_id).filter(Boolean);
  expect(figureQueries.length).toBeGreaterThan(0);
  const outside = host.querySelector('.ask42-answer').cloneNode(true);
  outside.querySelector('details.ask42-technical').remove();
  for (const id of figureQueries){
    expect(plain(outside.textContent)).not.toContain(id);
    expect(plain(details.textContent)).not.toContain(id);
    expect(host.querySelector('.ask42-figure[data-query-id="' + id + '"]')).not.toBeNull();
  }
});

test('each figure in The numbers says which claim it counts', async () => {
  /* Albert, 4 October 2026: "424 posts. 296 creators. 18 posts." with no
     subject. Each figure now carries the claim it comes from. */
  const record = clone(completeRecord);
  const server = serve([record]);
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');
  const withFigures = record.answer.claims.filter((claim) => (claim.numbers || []).length > 0);
  expect(withFigures.length).toBeGreaterThan(0);
  const groups = [...host.querySelectorAll('.ask42-figure-group')];
  expect(groups).toHaveLength(withFigures.length);
  groups.forEach((group, index) => {
    /* The claim's figures sit together, and the claim is said once under them. */
    expect(group.querySelectorAll('.ask42-figure')).toHaveLength(withFigures[index].numbers.length);
    expect(group.querySelectorAll('.ask42-figure-about')).toHaveLength(1);
    expect(plain(group.querySelector('.ask42-figure-about').textContent)).toBe(plain(withFigures[index].text));
  });
});

test('The numbers keeps four figures at most, each kept with its own claim', async () => {
  const record = clone(completeRecord);
  const [first, second] = record.answer.claims;
  const figure = (value) => ({...record.answer.claims.flatMap((claim) => claim.numbers || [])[0], value});
  first.numbers = [figure(1), figure(2), figure(3)];
  second.numbers = [figure(4), figure(5)];
  const server = serve([record]);
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');
  const groups = [...host.querySelectorAll('.ask42-figure-group')];
  expect(groups.map((group) => group.querySelectorAll('.ask42-figure').length)).toEqual([3, 1]);
  expect(groups.map((group) => plain(group.querySelector('.ask42-figure-about').textContent)))
    .toEqual([plain(first.text), plain(second.text)]);
});

test('a chip previews its source on focus and pins it in the side panel on click', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');
  const chip = buttons().find((node) => plain(node.textContent).includes('@fixture_za_1'));
  await act(async () => { chip.focus(); });
  const popover = host.querySelector('[role="tooltip"]');
  expect(popover).not.toBeNull();
  expect(plain(popover.querySelector('mark')?.textContent)).toBe('kitchen table, one pot, everybody dances');
  expect(plain(popover.textContent)).toContain('184 000 views');
  expect(popover.querySelector('img')).not.toBeNull();
  await act(async () => { chip.click(); });
  const panel = host.querySelector('aside[aria-label="Source"]');
  expect(panel).not.toBeNull();
  expect(plain(panel.textContent)).toContain('@fixture_za_1');
  expect(panel.querySelector('a[href="https://example.invalid/tiktok/@fixture_za_1/video/7431"]')).not.toBeNull();
  expect(text()).toContain(record.answer.short_answer);
});

test('no source column shows until a source is picked, and closing it hands focus back to the chip', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');
  const layout = host.querySelector('.ask42-answer-layout');
  expect(layout.querySelector('aside[aria-label="Source"]')).toBeNull();
  expect(text()).not.toContain('Pick a source under a claim');
  const chip = buttons().find((node) => plain(node.textContent).includes('@fixture_za_1'));
  await act(async () => { chip.focus(); });
  await act(async () => { chip.click(); });
  const panel = layout.querySelector('aside[aria-label="Source"]');
  expect(panel).not.toBeNull();
  expect(document.activeElement.className).toBe('ask42-source-title');
  const close = [...panel.querySelectorAll('button')].find((node) => node.textContent === 'Close');
  await act(async () => { close.click(); });
  expect(layout.querySelector('aside[aria-label="Source"]')).toBeNull();
  expect(document.activeElement).toBe(chip);
});

test('a source disclosure keeps keyboard focus while expanding and collapsing', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');

  const claim = [...host.querySelectorAll('.ask42-claim')]
    .find((node) => node.querySelector('.ask42-claim-text')?.textContent === record.answer.claims[0].text);
  const sourceChips = () => [...claim.querySelectorAll('button[aria-label^="Source:"]')];
  const disclosure = () => claim.querySelector('button.ask42-chip-more');
  const more = disclosure();

  expect(Boolean(claim)).toBe(true);
  expect(sourceChips().length).toBe(1);
  expect(plain(sourceChips()[0].querySelector('.ask42-chip-handle').textContent)).toBe('@fixture_za_1');
  expect(Boolean(more)).toBe(true);
  expect(more.getAttribute('aria-expanded')).toBe('false');
  expect(more.getAttribute('aria-label')).toBe('Show 1 more source');
  /* The disclosure reads as part of a written citation now, not a boxed "+1". */
  expect(plain(more.textContent)).toBe('and 1 more');

  await act(async () => { more.focus(); more.click(); });
  const fewer = disclosure();
  expect(Boolean(fewer === more)).toBe(true);
  expect(Boolean(document.activeElement === fewer)).toBe(true);
  expect(fewer.getAttribute('aria-expanded')).toBe('true');
  expect(fewer.getAttribute('aria-label')).toBe('Show fewer sources');
  expect(plain(fewer.textContent)).toBe('Fewer');
  expect(sourceChips().length).toBe(2);
  expect(sourceChips().map((chip) => plain(chip.querySelector('.ask42-chip-handle').textContent)))
    .toEqual(['@fixture_za_1', '@fixture_za_2']);

  const second = sourceChips()[1];
  await act(async () => { second.focus(); });
  const sourceQuote = record.answer.claims[0].quotes.find((item) => item.evidence_id === 'x_fixture_2');
  const highlightedQuotes = [...claim.querySelectorAll('[role="tooltip"] mark')]
    .map((mark) => plain(mark.textContent));
  expect(Boolean(sourceQuote)).toBe(true);
  expect(highlightedQuotes.includes(sourceQuote.text)).toBe(true);
  await act(async () => { second.click(); });
  const panel = host.querySelector('aside[aria-label="Source"]');
  expect(Boolean(panel)).toBe(true);
  expect(plain(panel.textContent)).toContain('@fixture_za_2');
  expect(Boolean(panel.querySelector('a[href="https://example.invalid/x/fixture_za_2/status/2210"]'))).toBe(true);

  await act(async () => { fewer.focus(); fewer.click(); });
  const collapsed = disclosure();
  expect(Boolean(collapsed === fewer)).toBe(true);
  expect(Boolean(document.activeElement === collapsed)).toBe(true);
  expect(collapsed.getAttribute('aria-expanded')).toBe('false');
  expect(collapsed.getAttribute('aria-label')).toBe('Show 1 more source');
  expect(plain(collapsed.textContent)).toBe('and 1 more');
  expect(sourceChips().length).toBe(1);
  expect(plain(panel.textContent)).toContain('@fixture_za_2');
  expect(Boolean(panel.querySelector('a[href="https://example.invalid/x/fixture_za_2/status/2210"]'))).toBe(true);
});

test('single-source and source-free answers have no source disclosure toggle', async () => {
  const single = clone(completeRecord);
  single.answer.claims = [single.answer.claims[1]];
  single.answer.claims[0].evidence_ids = ['x_fixture_2'];
  single.answer.so_what = single.answer.so_what.map((item) => ({...item, claim_ids: ['c2']}));
  single.answer.watch_next = [];
  const singleServer = serve([single]);
  await render({query: {q: single.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the single-source ask');
  await finish(singleServer, single);
  await until(() => text().includes('What we do not know'), 'the single-source answer');
  expect(host.querySelectorAll('.ask42-claim button[aria-label^="Source:"]').length).toBe(1);
  expect(host.querySelectorAll('.ask42-chip-more').length).toBe(0);

  await act(async () => root.unmount());
  root = createRoot(host);
  calls = [];
  const noSource = clone(completeRecord);
  noSource.answer.status = 'insufficient_evidence';
  noSource.answer.claims = [];
  noSource.answer.evidence = [];
  noSource.answer.so_what = [];
  noSource.answer.watch_next = [];
  noSource.run.posts = 0;
  noSource.run.platforms = 0;
  const noSourceServer = serve([noSource]);
  await render({query: {q: noSource.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the source-free ask');
  await finish(noSourceServer, noSource);
  await until(() => text().includes('What we do not know'), 'the source-free answer');
  expect(host.querySelectorAll('.ask42-claim button[aria-label^="Source:"]').length).toBe(0);
  expect(host.querySelectorAll('.ask42-chip-more').length).toBe(0);
});

test('an answer that fails the contract check is refused and nothing from it renders', async () => {
  const record = clone(completeRecord);
  record.answer.claims[0].evidence_ids = ['tt_fixture_1', 'missing_post'];
  const server = serve([record]);
  await render({query: {q: record.question}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('This answer did not pass its checks'), 'the refusal');
  expect(text()).toContain("c1: evidence 'missing_post' does not resolve to a post record");
  expect(text()).not.toContain(record.answer.short_answer);
  expect(text()).not.toContain(record.answer.claims[0].text);
  expect(text()).not.toContain('What it means for a brand');
  expect(button('Export answer')).toBeUndefined();
});

test('the contract media fields pass the check; any other extra field is refused', async () => {
  const record = clone(completeRecord);
  record.answer.evidence[0].sponsor = 'unknown';
  const server = serve([record]);
  await render({query: {q: record.question}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('This answer did not pass its checks'), 'the refusal');
  expect(text()).toContain("'sponsor'");
  expect(text()).not.toContain('thumbnail_url');
  expect(text()).not.toContain(record.answer.short_answer);
});

test('a media field off its contract shape is refused, not set aside', async () => {
  const record = clone(completeRecord);
  record.answer.evidence[0].duration_s = -1;
  record.answer.evidence[1].transcript_span = {start_s: 1.0, text: 'no end'};
  const server = serve([record]);
  await render({query: {q: record.question}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('This answer did not pass its checks'), 'the refusal');
  expect(text()).toContain('duration_s');
  expect(text()).toContain("'end_s' is a required property");
  expect(text()).not.toContain(record.answer.short_answer);
});

test('a cut claim citing [unresolved] never becomes a chip or a post', async () => {
  const record = clone(completeRecord);
  const cut = {...record.answer.claims[0], id: 'c9', text: 'A claim the checks cut', evidence_ids: ['[unresolved]'], quotes: []};
  const server = serve([record]);
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  const stream = server.stream(record.ask_id);
  stream.push(frame(8, 'claim', {seq: 8, claim: cut, check: 'cut', reason: 'no post record behind it'}));
  await until(() => text().includes('A claim the checks cut'), 'the cut claim');
  expect(text()).toContain('Cut');
  expect(text()).not.toContain('[unresolved]');
  expect(host.querySelector('.ask42-chip')).toBeNull();
  expect(host.querySelector('[aria-label="Sources gathered"]')).toBeNull();

  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');
  expect(text()).not.toContain('[unresolved]');
  expect(text()).not.toContain('A claim the checks cut');
  const chips = [...host.querySelectorAll('.ask42-chip')].filter((node) => !node.classList.contains('ask42-chip-more'));
  expect(chips.length).toBeGreaterThan(0);
  for (const chip of chips) expect(plain(chip.textContent)).not.toContain('unresolved');
  expect(host.querySelector('[aria-label="Posts"]').querySelectorAll('li').length).toBe(record.answer.evidence.length);
});

test('a claim in the answer citing only [unresolved] is refused, so no chip or post is drawn for it', async () => {
  const record = clone(completeRecord);
  record.answer.claims.push({...record.answer.claims[0], id: 'c9', text: 'A claim the checks cut', evidence_ids: ['[unresolved]'], quotes: [], numbers: []});
  const server = serve([record]);
  await render({query: {q: record.question}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('This answer did not pass its checks'), 'the refusal');
  expect(text()).toContain("c9: evidence '[unresolved]' does not resolve to a post record");
  expect(host.querySelector('.ask42-chip')).toBeNull();
  expect(host.querySelector('[aria-label="Posts"]')).toBeNull();
});

test('Stop sends the stop request for the running question', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  await render();
  await askByTyping(record.question);
  server.stream(record.ask_id).push(frame(1, 'step', record.steps[0]));
  await until(() => button('Stop'), 'the Stop button');
  await act(async () => { button('Stop').click(); });
  await until(() => calls.some((call) => call.path === `/api/ask/${record.ask_id}/stop`), 'the stop request');
  const stop = calls.find((call) => call.path === `/api/ask/${record.ask_id}/stop`);
  expect(stop.method).toBe('POST');
  expect(headerOf(stop, 'X-Passcode')).toBe('fixture-pass');
});

test('suggested follow-ups sit in their own labelled group, apart from the answer actions', async () => {
  serve([clone(completeRecord)]);
  await render({query: {follow: completeRecord.ask_id}});
  await until(() => button(completeRecord.run.followups[0]), 'the follow-ups');
  const group = host.querySelector('[data-ask-followups]');
  expect(group).not.toBeNull();
  expect(group.getAttribute('role')).toBe('group');
  const label = document.getElementById(group.getAttribute('aria-labelledby'));
  expect(plain(label.textContent)).toBe('Ask next');
  expect([...group.querySelectorAll('button')].map((node) => plain(node.textContent))).toEqual(completeRecord.run.followups);
  const actions = host.querySelector('.ask42-actions');
  for (const followup of completeRecord.run.followups){
    expect([...actions.querySelectorAll('button')].some((node) => plain(node.textContent) === followup)).toBe(false);
  }
});

test('a reopened answer lets its question be the visible title, keeping Ask 42 for screen readers', async () => {
  serve([clone(completeRecord)]);
  await render({query: {follow: completeRecord.ask_id}});
  await until(() => host.querySelector('.ask42-answer'), 'the answer');
  const title = host.querySelector('h1.ask42-title');
  expect(plain(title.textContent)).toBe('Ask 42');
  expect(title.classList.contains('sr-only')).toBe(true);
  expect(plain(host.querySelector('.ask42-answer .ask42-question').textContent)).toBe(completeRecord.question);
});

test('a fresh Ask shows its title', async () => {
  await render();
  expect(host.querySelector('h1.ask42-title').classList.contains('sr-only')).toBe(false);
});

test('a follow-up updates the child URL, which a reload reads without posting again', async () => {
  const first = clone(completeRecord);
  const second = {...clone(completeRecord), ask_id: 'a_20260928_0000000d', parent_id: first.ask_id, question: first.run.followups[0]};
  const server = serve([first, second]);
  window.history.replaceState(null, '', '#/ask');
  await render({query: {q: first.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await until(() => window.location.hash === '#/ask?follow=' + first.ask_id, 'the parent follow URL');
  await render({query: {follow: first.ask_id}});
  expect(calls.filter((call) => call.path === '/api/ask/' + first.ask_id + '/events')).toHaveLength(1);
  await finish(server, first);
  await until(() => button(first.run.followups[0]), 'the follow-ups');
  await act(async () => { button(first.run.followups[0]).click(); });
  await until(() => calls.filter((call) => call.path === '/api/ask').length === 2, 'the follow-up ask');
  await until(() => window.location.hash === '#/ask?follow=' + second.ask_id, 'the child follow URL');
  await render({query: {follow: second.ask_id}});
  expect(calls.filter((call) => call.path === '/api/ask/' + second.ask_id + '/events')).toHaveLength(1);
  const posted = calls.filter((call) => call.path === '/api/ask')[1];
  expect(posted.body.question).toBe(first.run.followups[0]);
  expect(posted.body.parent_id).toBe(first.ask_id);
  expect(posted.body.market).toBe('ZA');

  await finish(server, second);
  await until(() => host.querySelector('.ask42-done') && text().includes(second.question), 'the child answer');
  await act(async () => root.unmount());
  root = createRoot(host);
  calls = [];
  const follow = new URLSearchParams(window.location.hash.slice(window.location.hash.indexOf('?') + 1)).get('follow');
  await render({query: {follow}});
  await until(() => host.querySelector('.ask42-done') && text().includes(second.question), 'the restored child answer');
  expect(calls.filter((call) => call.method === 'GET' && call.path === '/api/ask/' + second.ask_id)).toHaveLength(1);
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);
});

/* UI notes, 2 Oct 2026: a reopened answer leads with the answer. The composer
   folds to "Ask a follow-up" under it, and a question asked from there is a
   follow-up of the reopened answer. */
test('a reopened answer folds the composer to Ask a follow-up, which asks a follow-up', async () => {
  const first = clone(completeRecord);
  const second = {...clone(completeRecord), ask_id: 'a_20260928_0000000e', parent_id: first.ask_id, question: 'Is it moving in Durban too?'};
  const server = serve([first, second]);
  await render({query: {follow: first.ask_id}});
  await finish(server, first);
  await until(() => text().includes('What we do not know'), 'the reopened answer');
  const form = host.querySelector('form.ask42-form');
  expect(form.hidden).toBe(true);
  // The form's own display rule would otherwise beat the hidden attribute in a browser.
  expect(readFileSync(new URL('../../styles/ask42.css', import.meta.url), 'utf8')).toContain('.ask42-form[hidden] { display: none; }');
  const open = button('Ask a follow-up');
  expect(open).toBeDefined();
  expect(host.querySelector('.ask42-done').contains(open)).toBe(true);
  await act(async () => { open.click(); });
  expect(form.hidden).toBe(false);
  expect(button('Ask a follow-up')).toBeUndefined();
  const field = host.querySelector('textarea');
  expect(field.value).toBe('');
  expect(document.activeElement).toBe(field);
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(field, second.question);
    field.dispatchEvent(new Event('input', {bubbles: true}));
  });
  await act(async () => { form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})); });
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the follow-up ask');
  const posted = calls.find((call) => call.path === '/api/ask');
  expect(posted.body.question).toBe(second.question);
  expect(posted.body.parent_id).toBe(first.ask_id);
});

test('a Today card starts once and retains its accepted ask URL', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  const query = {q: 'What is behind #fixture in South Africa this week?', market: 'ZA', item: 'it_fixture_1', date: '2026-09-28'};
  const historyState = {source: 'today'};
  window.history.replaceState(historyState, '', '#/ask?' + new URLSearchParams(query).toString());
  const historyLength = window.history.length;
  await render({query});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await until(() => window.location.hash === '#/ask?follow=' + record.ask_id, 'the accepted ask URL');
  await render({query: {follow: record.ask_id}});
  expect(calls.filter((call) => call.path === '/api/ask/' + record.ask_id + '/events')).toHaveLength(1);
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');
  const posts = calls.filter((call) => call.path === '/api/ask');
  expect(posts.length).toBe(1);
  expect(posts[0].body).toEqual({question: query.q, market: 'ZA', parent_id: null, from_card: {item_id: 'it_fixture_1', market: 'ZA', date: '2026-09-28'}});
  expect(window.history.state).toEqual(historyState);
  expect(window.history.length).toBe(historyLength);
  expect(host.querySelector('textarea').value).toBe(query.q);
  expect(host.querySelector('select').value).toBe('ZA');
});

test('a pending ask does not replace a newer Ask route', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  let releaseStart;
  const started = new Promise((resolve) => { releaseStart = resolve; });
  const served = globalThis.fetch;
  const delayed = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    if (method === 'POST' && path === '/api/ask'){
      calls.push({path, method, headers: init.headers || {}, body: JSON.parse(init.body)});
      return started;
    }
    return served(url, init);
  };
  globalThis.fetch = delayed;
  window.fetch = delayed;
  window.history.replaceState(null, '', '#/ask');
  await render();
  await askByTyping(record.question);
  window.history.replaceState(null, '', '#/ask?q=reader-changed-route');
  await act(async () => { releaseStart(json(202, {ask_id: record.ask_id, status: 'running'})); });
  await until(() => calls.some((call) => call.path === '/api/ask/' + record.ask_id + '/events'), 'the accepted ask stream');
  expect(window.location.hash).toBe('#/ask?q=reader-changed-route');
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the answer after navigation');
  expect(window.location.hash).toBe('#/ask?q=reader-changed-route');
});

test('an aborted ask leaves the route it was navigating to alone', async () => {
  const record = clone(completeRecord);
  let releaseAborted;
  const abortedStart = new Promise((resolve) => { releaseAborted = resolve; });
  let decoded = false;
  const abortedFetch = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    if (method === 'POST' && path === '/api/ask'){
      calls.push({path, method, headers: init.headers || {}, body: JSON.parse(init.body)});
      return abortedStart;
    }
    return json(404, {error: 'not_found', message: 'No such route.'});
  };
  globalThis.fetch = abortedFetch;
  window.fetch = abortedFetch;
  window.history.replaceState(null, '', '#/ask');
  await render();
  await askByTyping(record.question);
  window.history.replaceState({source: 'fieldwork'}, '', '#/fieldwork');
  await act(async () => root.unmount());
  root = createRoot(host);
  await act(async () => {
    const response = json(202, {ask_id: record.ask_id, status: 'running'});
    response.json = async () => { decoded = true; return {ask_id: record.ask_id, status: 'running'}; };
    releaseAborted(response);
  });
  await until(() => decoded, 'the accepted response decode');
  await Promise.resolve();
  expect(window.location.hash).toBe('#/fieldwork');
  expect(window.history.state).toEqual({source: 'fieldwork'});
  expect(calls.filter((call) => call.path.includes('/events'))).toHaveLength(0);
});

test('an accepted ask leaves embedded Console and Fieldwork hashes alone', async () => {
  for (const route of ['#/console?work=ask', '#/fieldwork']){
    const record = clone(completeRecord);
    const server = serve([record]);
    calls = [];
    window.history.replaceState({host: route}, '', route);
    await render();
    await askByTyping(record.question);
    await finish(server, record);
    await until(() => text().includes('What we do not know'), 'the embedded host answer');
    expect(window.location.hash).toBe(route);
    expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(1);
    if (route !== '#/fieldwork'){
      await act(async () => root.unmount());
      root = createRoot(host);
    }
  }
});

test('Export answer downloads the HTML export with the passcode header', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => button('Export answer'), 'the export action');
  await act(async () => { button('Export answer').click(); });
  await until(() => downloads.length === 1, 'the download');
  const call = calls.find((entry) => entry.path === `/api/ask/${record.ask_id}/export?format=html`);
  expect(headerOf(call, 'X-Passcode')).toBe('fixture-pass');
  expect(downloads[0].download).toBe(`42-answer-${record.ask_id}.html`);
});

test('Add to dossier starts a draft from the answer and opens it', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  const served = globalThis.fetch;
  const withDossiers = async (url, init = {}) => {
    if (String(url) === '/api/dossiers' && String(init.method || '').toUpperCase() === 'POST'){
      calls.push({path: String(url), method: 'POST', headers: init.headers || {}, body: JSON.parse(init.body)});
      return json(201, {dossier_id: 'd_fixture01', version: 1, state: 'draft'});
    }
    return served(url, init);
  };
  globalThis.fetch = withDossiers;
  window.fetch = withDossiers;
  window.history.replaceState(null, '', '#/ask');
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => button('Add to dossier'), 'the dossier action');
  await act(async () => { button('Add to dossier').click(); });
  await until(() => window.location.hash === '#/dossiers/d_fixture01', 'the dossier to open');
  const post = calls.find((call) => call.path === '/api/dossiers');
  expect(post.method).toBe('POST');
  expect(post.body).toEqual({from: {ask_id: record.ask_id}});
  expect(headerOf(post, 'X-Passcode')).toBe('fixture-pass');
});

test('notices from the run show above a partial answer', async () => {
  const record = clone(partialRecord);
  const server = serve([record]);
  await render({query: {q: record.question, market: 'KE'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');
  const all = text();
  expect(all).toContain('Instagram was rate limited during this run; the answer uses TikTok only');
  expect(all).toContain('Partial answer: part of the picture is missing');
  expect(all.indexOf('Instagram was rate limited during this run')).toBeLessThan(all.indexOf(record.answer.short_answer));
  expect(all).toContain('Kenya · posts from 21 to 27 September 2026 · 9 posts from 1 platform');
  expect(all).toContain('Single source');
});

test('the meta line counts the whole store and calls the posts read a sample', async () => {
  const record = clone(partialRecord);
  record.run = {...record.run, posts: 51, platforms: 1,
    store: {posts: 1108, creators: 852, located_posts: 477, located_creators: 332, platforms: 1, query_id: 'q_9'}};
  const server = serve([record]);
  await render({query: {q: record.question, market: 'KE'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');
  const all = text();
  expect(all).toContain('Kenya · posts from 21 to 27 September 2026 · 1 108 posts by 852 creators on 1 platform in the store · 51 posts read');
  expect(all).not.toContain('51 posts from 1 platform');
});

test('a failed record shows its message and Try again asks again', async () => {
  const record = clone(failedRecord);
  const server = serve([record]);
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes(record.error.message), 'the failure');
  expect(button('Try again')).toBeDefined();
  await act(async () => { button('Try again').click(); });
  await until(() => calls.filter((call) => call.path === '/api/ask').length === 2, 'the retry');
  expect(calls.filter((call) => call.path === '/api/ask')[1].body.question).toBe(record.question);
});

test('safeUrl keeps plain http and https addresses and refuses everything else', async () => {
  const {safeUrl} = await import('../../safeUrl.js');
  expect(safeUrl('https://example.invalid/a?b=1')).toBe('https://example.invalid/a?b=1');
  expect(safeUrl('http://example.invalid/')).toBe('http://example.invalid/');
  for (const bad of ['javascript:alert(1)', ' JavaScript:alert(1)', 'java\nscript:alert(1)', 'data:text/html,<b>x</b>',
    'vbscript:x', '/relative/path', '//example.invalid/x', 'https://user:secret@example.invalid/', 'https://user@example.invalid/',
    '', null, undefined, 42, {}]){
    expect(safeUrl(bad)).toBeNull();
  }
});

const unsafeHref = () => [...host.querySelectorAll('a')].filter((a) => !/^https?:\/\//i.test(a.getAttribute('href') || '') || /javascript/i.test(a.getAttribute('href') || ''));
const unsafeSrc = () => [...host.querySelectorAll('img')].filter((img) => !/^https?:\/\//i.test(img.getAttribute('src') || '') || /javascript/i.test(img.getAttribute('src') || ''));

test('an evidence url or thumbnail that is not http or https never reaches a link or an image', async () => {
  const record = clone(completeRecord);
  const bad = record.answer.evidence[0];
  bad.url = 'javascript:alert(1)';
  /* The contract refuses a non-http thumbnail in a finished answer, so the
     bad thumbnail travels on the streamed event, which is not checked. */
  const streamed = {...bad, thumbnail_url: 'javascript:alert(2)'};
  delete bad.thumbnail_url;
  const server = serve([record]);
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  const stream = server.stream(record.ask_id);
  stream.push(frame(3, 'evidence', {seq: 3, evidence: streamed}));
  await until(() => host.querySelector('[aria-label="Sources gathered"]'), 'the gathered sources');
  const gathered = host.querySelector('[aria-label="Sources gathered"]');
  expect(gathered.querySelector('img')).toBeNull();
  expect(plain(gathered.textContent)).toContain(bad.handle);
  expect(unsafeSrc()).toHaveLength(0);

  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');
  expect(unsafeHref()).toHaveLength(0);
  expect(unsafeSrc()).toHaveLength(0);

  const strip = host.querySelector('[aria-label="Posts"]');
  const card = [...strip.querySelectorAll('li')].find((li) => plain(li.textContent).includes(bad.handle));
  expect(card).toBeDefined();
  expect(card.querySelector('a')).toBeNull();
  expect(card.querySelector('img')).toBeNull();
  expect(card.querySelector('.ask42-post-blank')).not.toBeNull();
  const good = [...strip.querySelectorAll('li')].find((li) => !plain(li.textContent).includes(bad.handle));
  expect(good.querySelector('a').getAttribute('href')).toBe(record.answer.evidence[1].url);

  const chip = buttons().find((node) => plain(node.textContent).includes(bad.handle));
  await act(async () => { chip.focus(); });
  const popover = host.querySelector('[role="tooltip"]');
  expect(popover).not.toBeNull();
  expect(popover.querySelector('img')).toBeNull();
  await act(async () => { chip.click(); });
  const panel = host.querySelector('aside[aria-label="Source"]');
  expect(plain(panel.textContent)).toContain(bad.handle);
  expect(panel.querySelector('a')).toBeNull();
  expect(panel.querySelector('img')).toBeNull();
  expect(unsafeHref()).toHaveLength(0);
  expect(unsafeSrc()).toHaveLength(0);
});

test('a 401 on starting a question hands the reader to the passcode flow', async () => {
  let asked = 0;
  const stub = async (url, init = {}) => {
    calls.push({path: String(url), method: String(init.method || 'GET').toUpperCase()});
    return json(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'});
  };
  globalThis.fetch = stub;
  window.fetch = stub;
  await render({onAuth: () => { asked += 1; }});
  await askByTyping('What is moving in South Africa?');
  await until(() => text().includes('Enter the passcode to ask a question.'), 'the passcode line');
  expect(asked).toBe(1);
});

test('a 401 on reading the record, stopping or exporting also hands over to the passcode flow', async () => {
  let asked = 0;
  const onAuth = () => { asked += 1; };
  const record = clone(completeRecord);
  let server = serve([record]);
  const served = globalThis.fetch;
  const denyStop = async (url, init = {}) => {
    if (String(url).endsWith('/stop')) return json(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'});
    return served(url, init);
  };
  globalThis.fetch = denyStop;
  window.fetch = denyStop;
  await render({onAuth});
  await askByTyping(record.question);
  server.stream(record.ask_id).push(frame(1, 'step', record.steps[0]));
  await until(() => button('Stop') && !button('Stop').disabled, 'the Stop button');
  await act(async () => { button('Stop').click(); });
  await until(() => asked === 1, 'onAuth after the stop 401');

  await act(async () => root.unmount());
  root = createRoot(host);
  calls = [];
  const again = clone(completeRecord);
  server = serve([again]);
  const servedAgain = globalThis.fetch;
  const denyExport = async (url, init = {}) => {
    if (String(url).includes('/export')) return json(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'});
    return servedAgain(url, init);
  };
  globalThis.fetch = denyExport;
  window.fetch = denyExport;
  await render({onAuth, query: {q: again.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, again);
  await until(() => button('Export answer'), 'the export action');
  await act(async () => { button('Export answer').click(); });
  await until(() => asked === 2, 'onAuth after the export 401');

  await act(async () => root.unmount());
  root = createRoot(host);
  calls = [];
  const third = clone(completeRecord);
  server = serve([third]);
  const servedThird = globalThis.fetch;
  const denyRead = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    if (method === 'GET' && path === `/api/ask/${third.ask_id}`) return json(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'});
    return servedThird(url, init);
  };
  globalThis.fetch = denyRead;
  window.fetch = denyRead;
  await render({onAuth, query: {q: third.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, third);
  await until(() => asked === 3, 'onAuth after the record read 401');
  expect(text()).toContain('Enter the passcode to ask a question.');
});

test('the page copy names no age group, no Google Trends and no retired product', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');
  expect(text()).not.toMatch(/gen ?z|millennial|youth|generation|google trends|prompt pulse|nano banana/i);
});

/* Contract section 14.1: a spike ask is started on the topic page, and the
   Ask page follows the ask_id it returned the way it follows its own: the
   record, the live log, then the checked answer. Nothing is posted again. */
test('#/ask?follow=<id> follows an ask already started: no new question, the live log, then the answer', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  const served = globalThis.fetch;
  let reads = 0;
  const running = {...clone(completeRecord), status: 'running', answer: null, finished_at: null, steps: record.steps.slice(0, 1)};
  const firstRunning = async (url, init = {}) => {
    const method = String(init.method || 'GET').toUpperCase();
    if (method === 'GET' && String(url) === `/api/ask/${record.ask_id}` && reads++ === 0){
      calls.push({path: String(url), method, headers: init.headers || {}, body: null});
      return json(200, running);
    }
    return served(url, init);
  };
  globalThis.fetch = firstRunning;
  window.fetch = firstRunning;
  await render({query: {follow: record.ask_id}});
  await until(() => calls.some((call) => call.path === `/api/ask/${record.ask_id}/events`), 'the stream to open');
  await until(() => text().includes(record.steps[0].text), 'the first step from the record');
  expect(button('Stop')).toBeDefined();
  const heading = [...host.querySelectorAll('h2')].find((node) => plain(node.textContent) === record.question);
  expect(heading).toBeDefined();
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the finished answer');
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);
  expect(text()).toContain(record.answer.short_answer);
  expect(host.querySelector('textarea').value).toBe(record.question);
});

test('following an ask that has already finished shows its answer without opening the stream', async () => {
  const record = clone(completeRecord);
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => text().includes('What we do not know'), 'the finished answer');
  expect(calls.some((call) => call.path.endsWith('/events'))).toBe(false);
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);
});

test('opening a saved answer reads it without the research timer or Stop (live audit F15)', async () => {
  const record = clone(completeRecord);
  serve([record]);
  const served = globalThis.fetch;
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const slowRead = async (url, init = {}) => {
    if (String(init.method || 'GET').toUpperCase() === 'GET' && String(url) === `/api/ask/${record.ask_id}`) await held;
    return served(url, init);
  };
  globalThis.fetch = slowRead;
  window.fetch = slowRead;
  await render({query: {follow: record.ask_id}});
  await until(() => host.querySelector('[data-ask-opening]'), 'the opening state');
  expect(text()).toContain('Opening the saved answer.');
  expect(button('Stop')).toBeUndefined();
  expect(text()).not.toContain('Research is in progress.');
  release();
  await until(() => text().includes('What we do not know'), 'the finished answer');
  expect(host.querySelector('[data-ask-opening]')).toBeNull();
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);
});

test('an assumed source market reaches the chip, pinned panel, and post strip without changing claim confidence', async () => {
  const record = clone(completeRecord);
  record.answer.evidence[0].source_market = 'KE';
  record.answer.evidence[0].flags = ['market_assumed'];
  const server = serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => host.querySelector('.ask42-done'), 'the finished answer');

  const claimLabel = record.answer.claims[0].label;
  expect(host.querySelector('.ask42-confidence[data-label]').getAttribute('data-label')).toBe(claimLabel);
  const chip = buttons().find((node) => plain(node.textContent).includes(record.answer.evidence[0].handle));
  expect(chip).toBeDefined();
  expect(chip.getAttribute('aria-label')).toContain('Market assumed: Kenya');
  expect(chip.getAttribute('aria-label')).not.toContain("Seen in Kenya's feeds");
  await act(async () => { chip.focus(); });
  expect(plain(host.querySelector('[role="tooltip"]').textContent)).toContain('Market assumed: Kenya');
  await act(async () => { chip.click(); });
  expect(plain(host.querySelector('aside[aria-label="Source"]').textContent)).toContain('Market assumed: Kenya');
  const card = [...host.querySelectorAll('[aria-label="Posts"] li')]
    .find((node) => plain(node.textContent).includes(record.answer.evidence[0].handle));
  expect(plain(card.textContent)).toContain('Market assumed: Kenya');
  expect(host.querySelector('.ask42-confidence[data-label]').getAttribute('data-label')).toBe(claimLabel);
});

test('following an ask that cannot be read says so and manual retry only reads it again', async () => {
  serve([clone(completeRecord)]);
  const askPath = '/api/ask/a_20260930_00missing';
  await render({query: {follow: 'a_20260930_00missing'}});
  await until(() => host.querySelector('[role="alert"]'), 'the failure');
  expect(text()).toContain('No such question.');
  expect(button('Try again')).toBeDefined();
  const reads = () => calls.filter((call) => call.path === askPath && call.method === 'GET');
  expect(reads()).toHaveLength(1);
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);

  await act(async () => { button('Try again').click(); });
  await until(() => reads().length === 2, 'the manual read retry');
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);
});

/* Contract section 14.2: a finished answer offers a quiet "Ask every
   Monday" that opens the Schedules form with the question and market. The
   one red action stays Add to dossier. */
test('a complete saved Ask is saved explicitly to Findings with only its id', async () => {
  const record = clone(completeRecord);
  record.skin_id = null;
  record.investigation_id = null;
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => button('Save checked claims to Findings'), 'the Findings save action');

  expect(calls.filter((call) => call.path === '/api/findings')).toHaveLength(0);
  expect(calls.filter((call) => call.path === '/api/ask' && call.method === 'POST')).toHaveLength(0);
  expect(text()).toContain(record.answer.short_answer);
  expect(button('Add to dossier')).toBeDefined();

  await act(async () => { button('Save checked claims to Findings').click(); });
  await until(() => host.querySelector('[role="status"]')?.textContent.includes('Checked claims saved to Findings'), 'verified save success');
  const writes = calls.filter((call) => call.path === '/api/findings' && call.method === 'POST');
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toEqual({from: {ask_id: record.ask_id}});
  expect(host.querySelector('[role="status"]').textContent).toContain('Checked claims saved to Findings');
  expect([...host.querySelectorAll('a')].some((link) => link.getAttribute('href') === '#/history?tab=findings')).toBe(true);
  expect(calls.filter((call) => call.path.endsWith('/events'))).toHaveLength(0);
  expect(calls.filter((call) => call.path === '/api/ask' && call.method === 'POST')).toHaveLength(0);
});

test('a partial saved Ask keeps its gaps and evidence and can be saved to Findings', async () => {
  const record = clone(partialRecord);
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => button('Save checked claims to Findings'), 'the Findings save action');

  expect(text()).toContain(record.answer.short_answer);
  expect(text()).toContain('What we do not know');
  expect(text()).toContain(record.answer.gaps[0].what);
  const firstEvidence = record.answer.evidence[0];
  expect([...host.querySelectorAll('.ask42-chip')].some((chip) => chip.getAttribute('aria-label').includes(firstEvidence.handle))).toBe(true);
  expect([...host.querySelectorAll('.ask42-post-frame')].some((frame) => frame.getAttribute('href') === firstEvidence.url)).toBe(true);
  expect(calls.filter((call) => call.path === '/api/findings')).toHaveLength(0);
});

test('saving a Finding has one pending request and verifies an existing result', async () => {
  const record = clone(completeRecord);
  let release;
  findingResponder = () => new Promise((resolve) => { release = resolve; });
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => button('Save checked claims to Findings'), 'the Findings save action');

  await act(async () => { button('Save checked claims to Findings').click(); });
  await until(() => typeof release === 'function', 'the pending save request');
  const pending = button('Saving checked claims…');
  expect(pending.disabled).toBe(true);
  expect(pending.getAttribute('aria-busy')).toBe('true');
  await act(async () => { pending.click(); });
  expect(calls.filter((call) => call.path === '/api/findings' && call.method === 'POST')).toHaveLength(1);
  expect(calls.filter((call) => call.path === '/api/findings' && call.method === 'POST')[0].body).toEqual({from: {ask_id: record.ask_id}});

  await act(async () => { release(json(200, {finding_id: 'f_fixture_existing', source_ask_id: record.ask_id})); });
  await until(() => host.querySelector('[role="status"]')?.textContent.includes('Checked claims saved to Findings'), 'verified existing Finding');
  expect(host.querySelector('[role="status"]').textContent).toContain('Checked claims saved to Findings');
});

test('a failed Finding save shows the API error and never claims success', async () => {
  const record = clone(completeRecord);
  const responses = [
    json(409, {error: 'conflict', message: 'This answer cannot be saved yet.'}),
    json(503, {error: 'unavailable', message: 'Findings are unavailable.'}),
  ];
  findingResponder = () => responses.shift();
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => button('Save checked claims to Findings'), 'the Findings save action');

  for (const message of ['This answer cannot be saved yet.', 'Findings are unavailable.']){
    await act(async () => { button('Save checked claims to Findings').click(); });
    await until(() => host.querySelector('.ask42-error[role="alert"]')?.textContent === message, 'the save error');
    expect(Boolean(host.querySelector('[role="status"]'))).toBe(false);
    expect(Boolean(button('Checked claims saved to Findings'))).toBe(false);
  }
  expect(calls.filter((call) => call.path === '/api/findings' && call.method === 'POST')).toHaveLength(2);
});

test('a malformed Finding result or a result for another Ask is never shown as saved', async () => {
  const record = clone(completeRecord);
  const responses = [
    json(201, {finding_id: '../unsafe', source_ask_id: record.ask_id}),
    json(201, {finding_id: 'f_fixture_wrong_source', source_ask_id: 'a_20260930_0000000b'}),
  ];
  findingResponder = () => responses.shift();
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => button('Save checked claims to Findings'), 'the Findings save action');

  for (let attempt = 0; attempt < 2; attempt += 1){
    await act(async () => { button('Save checked claims to Findings').click(); });
    await until(() => host.querySelector('.ask42-error[role="alert"]'), 'the verification error');
    expect(Boolean(host.querySelector('[role="status"]'))).toBe(false);
    expect(Boolean(button('Checked claims saved to Findings'))).toBe(false);
  }
  expect(calls.filter((call) => call.path === '/api/findings' && call.method === 'POST')).toHaveLength(2);
});

test('a Finding save auth failure hands over to the existing passcode callback', async () => {
  const record = clone(completeRecord);
  let handovers = 0;
  findingResponder = () => json(401, {error: 'unauthorized', message: 'Passcode required.'});
  serve([record]);
  await render({query: {follow: record.ask_id}, onAuth: () => { handovers += 1; }});
  await until(() => button('Save checked claims to Findings'), 'the Findings save action');

  await act(async () => { button('Save checked claims to Findings').click(); });
  await until(() => handovers === 1, 'the passcode handover');
  expect(Boolean(host.querySelector('[role="status"]'))).toBe(false);
  expect(host.querySelector('.ask42-error[role="alert"]').textContent).toContain('Passcode required.');
});

test('a late save response for an old Ask cannot mark the new Ask as saved', async () => {
  const first = clone(completeRecord);
  const second = {...clone(completeRecord), ask_id: 'a_20260930_0000000c', question: 'What changed in the second saved Ask?'};
  let release;
  findingResponder = () => new Promise((resolve) => { release = resolve; });
  serve([first, second]);
  await render({query: {follow: first.ask_id}});
  await until(() => button('Save checked claims to Findings'), 'the first Findings save action');
  await act(async () => { button('Save checked claims to Findings').click(); });
  await until(() => typeof release === 'function', 'the pending first save');

  await render({query: {follow: second.ask_id}});
  await until(() => text().includes(second.question) && button('Save checked claims to Findings'), 'the second saved Ask');
  await act(async () => { release(json(201, {finding_id: 'f_fixture_old', source_ask_id: first.ask_id})); });
  expect(text()).toContain(second.question);
  expect(button('Save checked claims to Findings')).toBeDefined();
  expect(Boolean(button('Checked claims saved to Findings'))).toBe(false);
  expect(calls.filter((call) => call.path === '/api/ask' && call.method === 'POST')).toHaveLength(0);
});

test('an unsuccessful or claimless answer has no Findings save action', async () => {
  const failed = clone(failedRecord);
  serve([failed]);
  await render({query: {follow: failed.ask_id}});
  await until(() => button('Try again'), 'the failed Ask');
  expect(Boolean(button('Save checked claims to Findings'))).toBe(false);
  expect(calls.filter((call) => call.path === '/api/findings')).toHaveLength(0);

  const claimless = clone(completeRecord);
  claimless.answer.claims = [];
  serve([claimless]);
  await render({query: {follow: claimless.ask_id}});
  await until(() => text().includes('This answer did not pass its checks') || button('Save checked claims to Findings'), 'the checked claimless Ask');
  expect(Boolean(button('Save checked claims to Findings'))).toBe(false);
  expect(calls.filter((call) => call.path === '/api/findings')).toHaveLength(0);
});

test('an Ask with skin-specific private context cannot be saved to shared Findings', async () => {
  const record = clone(completeRecord);
  record.skin_id = 'skin_private_fixture';
  record.question = 'PRIVATE SKIN CONTEXT: what changed in the restricted campaign?';
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => text().includes(record.question), 'the skin-specific saved Ask');

  expect(Boolean(button('Save checked claims to Findings'))).toBe(false);
  expect(calls.filter((call) => call.path === '/api/findings')).toHaveLength(0);
});

test('an Ask with legacy investigation masking cannot be saved to shared Findings', async () => {
  const record = clone(completeRecord);
  record.investigation_id = 'investigation_private_fixture';
  record.question = 'PRIVATE INVESTIGATION CONTEXT: what changed in the masked study?';
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => text().includes(record.question), 'the investigation-scoped saved Ask');

  expect(Boolean(button('Save checked claims to Findings'))).toBe(false);
  expect(calls.filter((call) => call.path === '/api/findings')).toHaveLength(0);
});

test('Ask every Monday opens the Schedules form filled in with the question and market', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  window.history.replaceState(null, '', '#/ask');
  await render({query: {q: record.question, market: 'ZA'}});
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the auto ask');
  await finish(server, record);
  await until(() => button('Ask every Monday'), 'the schedule action');
  expect(button('Ask every Monday').className).toContain('ask42-quiet');
  expect([...host.querySelectorAll('.ask42-primary')].map((node) => plain(node.textContent))).toEqual(['Add to dossier']);
  await act(async () => { button('Ask every Monday').click(); });
  await until(() => window.location.hash.startsWith('#/schedules?'), 'the Schedules screen');
  const query = new URLSearchParams(window.location.hash.slice(window.location.hash.indexOf('?') + 1));
  expect(query.get('q')).toBe(record.question);
  expect(query.get('market')).toBe('ZA');
});

/* A followed ask that failed is not quietly asked again. A spike goes back
   to the confirm on its topic page, with that day open; a scheduled answer
   goes back to Schedules; any other opens a confirm naming its tier's
   ceiling, Cancel first, before anything is posted. While the record still
   reads as running, Try again waits. */
const confirmBox = () => host.querySelector('[role="dialog"]');

async function followFailed(extra){
  const record = {...clone(failedRecord), ...extra};
  serve([record]);
  window.history.replaceState(null, '', '#/ask?follow=' + record.ask_id);
  await render({query: {follow: record.ask_id}});
  await until(() => text().includes(record.error.message), 'the failure');
  return record;
}

test('Try again on a followed spike goes back to the spike confirm on its topic page, that day open', async () => {
  await followFailed({spike: {item_id: 'i_step', market: 'ZA', date: '2026-09-28', series: 'feed_tiktok'}});
  await act(async () => { button('Try again').click(); });
  await until(() => window.location.hash.startsWith('#/t/'), 'the topic page');
  expect(window.location.hash).toBe('#/t/i_step?market=ZA&day=2026-09-28&series=feed_tiktok');
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);
});

test('Try again on a followed scheduled answer goes back to Schedules', async () => {
  await followFailed({schedule_id: 'sch_0001'});
  await act(async () => { button('Try again').click(); });
  await until(() => window.location.hash === '#/schedules', 'the Schedules screen');
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);
});

test('Try again on any other followed record confirms the tier ceiling first; Cancel posts nothing', async () => {
  await followFailed({run: {tier: 'T0'}});
  await act(async () => { button('Try again').click(); });
  await until(() => confirmBox(), 'the confirm');
  expect(plain(confirmBox().textContent)).toContain('up to 10 credits');
  expect(document.activeElement && plain(document.activeElement.textContent)).toBe('Cancel');
  await act(async () => { button('Cancel').click(); });
  expect(confirmBox()).toBeNull();
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);
});

test('confirming the ceiling asks again at the same tier', async () => {
  const record = await followFailed({run: {tier: 'T1'}});
  await act(async () => { button('Try again').click(); });
  await until(() => confirmBox(), 'the confirm');
  expect(plain(confirmBox().textContent)).toContain('up to 60 credits');
  await act(async () => { button('Ask again').click(); });
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the retry');
  const posted = calls.filter((call) => call.path === '/api/ask');
  expect(posted).toHaveLength(1);
  expect(posted[0].body).toEqual({question: record.question, market: 'ZA', parent_id: null, tier: 'T1'});
});

test('a followed record with no tier names the highest ceiling Ask may pick and posts no tier', async () => {
  await followFailed({run: null});
  await act(async () => { button('Try again').click(); });
  await until(() => confirmBox(), 'the confirm');
  expect(plain(confirmBox().textContent)).toContain('up to 60 credits');
  await act(async () => { button('Ask again').click(); });
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the retry');
  expect('tier' in calls.find((call) => call.path === '/api/ask').body).toBe(false);
});

test('Try again is disabled while the followed record still reads as running', async () => {
  const record = {...clone(failedRecord), status: 'running', error: null};
  const server = serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => calls.some((call) => call.path === `/api/ask/${record.ask_id}/events`), 'the stream to open');
  await finish(server, record);
  await until(() => button('Try again'), 'the Try again button');
  expect(button('Try again').disabled).toBe(true);
  expect(text()).toContain('still reads as running');
  await act(async () => { button('Try again').click(); });
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);
  expect(confirmBox()).toBeNull();
});

/* The record exactly as f42-agent holds and stores a failed spike ask
   (core/api/agent_app.py start): the spike object rides on the record, with
   series null when the chart named none. */
test('Try again on a followed spike record shaped as the server keeps it goes back to that day on its topic page', async () => {
  const item = 'afe2bf2632b65cd9b5354f5bd4272ea81a4f7e004d01b3615fa9b479990a5156';
  const record = {
    ask_id: 'a_20260928_0000spk2', question: 'What made #fixture_za_heritage jump in South Africa on 2026-09-28?',
    parent_id: null, market: 'ZA', status: 'failed',
    created_at: '2026-09-28T08:01:05+02:00', finished_at: '2026-09-28T08:01:09+02:00',
    answer: null, run: null, steps: clone(failedRecord.steps),
    error: {error: 'internal', message: 'The agent hit an error and could not finish (RuntimeError).'},
    spike: {item_id: item, market: 'ZA', date: '2026-09-28', series: null},
  };
  serve([record]);
  window.history.replaceState(null, '', '#/ask?follow=' + record.ask_id);
  await render({query: {follow: record.ask_id}});
  await until(() => text().includes(record.error.message), 'the failure');
  await act(async () => { button('Try again').click(); });
  await until(() => window.location.hash.startsWith('#/t/'), 'the topic page');
  expect(window.location.hash).toBe('#/t/' + item + '?market=ZA&day=2026-09-28');
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(0);
});

/* A question asked here whose 202 came back before the stream and the
   record read both failed is already running on the server: Try again
   follows that ask_id rather than posting a second question. Only a
   question that never got an ask_id is posted again, as before. */
test('Try again after a started ask lost its stream follows that ask and posts nothing new', async () => {
  const record = clone(completeRecord);
  let reads = 0;
  const stub = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    calls.push({path, method, headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : null});
    if (method === 'POST' && path === '/api/ask'){
      return json(202, {ask_id: record.ask_id, status: 'running', events_url: `/api/ask/${record.ask_id}/events`, url: `/api/ask/${record.ask_id}`});
    }
    if (path === `/api/ask/${record.ask_id}/events`) return json(502, {error: 'agent_unavailable', message: 'The stream broke.'});
    if (path === `/api/ask/${record.ask_id}`){
      reads += 1;
      return reads === 1 ? json(502, {error: 'agent_unavailable', message: 'The agent could not be reached.'}) : json(200, record);
    }
    return json(404, {error: 'not_found', message: 'No such route.'});
  };
  globalThis.fetch = stub;
  window.fetch = stub;
  await render();
  await askByTyping(record.question);
  await until(() => text().includes('The agent could not be reached.'), 'the failure');
  await act(async () => { button('Try again').click(); });
  await until(() => text().includes('What we do not know'), 'the answer read back');
  expect(calls.filter((call) => call.path === '/api/ask')).toHaveLength(1);
  expect(reads).toBe(2);
  expect(confirmBox()).toBeNull();
  expect(text()).toContain(record.answer.short_answer);
});

test('Try again on a question that never got an ask_id asks it again, with no confirm', async () => {
  const record = clone(completeRecord);
  const server = serve([record]);
  const served = globalThis.fetch;
  let posts = 0;
  const firstRefused = async (url, init = {}) => {
    const method = String(init.method || 'GET').toUpperCase();
    if (method === 'POST' && String(url) === '/api/ask' && posts++ === 0){
      calls.push({path: String(url), method, headers: init.headers || {}, body: JSON.parse(init.body)});
      return json(503, {error: 'agent_unavailable', message: 'The Ask agent is not installed on this service.'});
    }
    return served(url, init);
  };
  globalThis.fetch = firstRefused;
  window.fetch = firstRefused;
  await render();
  await askByTyping(record.question);
  await until(() => text().includes('The Ask agent is not installed on this service.'), 'the refusal');
  await act(async () => { button('Try again').click(); });
  await until(() => calls.filter((call) => call.path === '/api/ask').length === 2, 'the second post');
  expect(confirmBox()).toBeNull();
  expect(calls.filter((call) => call.path === '/api/ask')[1].body.question).toBe(record.question);
  await finish(server, record);
  await until(() => text().includes('What we do not know'), 'the answer');
});

test('an Ask with legacy parent masking cannot be saved to shared Findings', async () => {
  const record = clone(completeRecord);
  record.parent_id = 'parent_private_fixture';
  record.question = 'PRIVATE INVESTIGATION CONTEXT: what changed in the masked study?';
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => text().includes(record.question), 'the parent-scoped saved Ask');

  expect(Boolean(button('Save checked claims to Findings'))).toBe(false);
  expect(calls.filter((call) => call.path === '/api/findings')).toHaveLength(0);
});

/* Charts, 3 October 2026: the posts each platform returned are drawn as a bar
   list beside the source facts, captioned with the run window. A platform with
   no items number says "Not measured"; a status other than ok rides as a note.
   run.posts is not drawn beside it: it disagrees with the per source sum. */
test('the answer draws posts read by platform as a bar list named by its window', async () => {
  const record = clone(completeRecord);
  record.run.source_status.push({platform: 'youtube', route: 'youtube/search', status: 'rate_limited'});
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => host.querySelector('[data-ask-source-chart]'), 'the source chart');
  const chart = host.querySelector('[data-ask-source-chart]');
  expect(plain(chart.querySelector('.ch42-title').textContent)).toBe('Posts read by platform');
  expect(plain(chart.querySelector('.ch42-caption').textContent)).toBe('Posts each platform returned, 21 to 27 September 2026');
  const rows = [...chart.querySelectorAll('.ch42-bar-row')].map((row) => [...row.children].map((cell) => plain(cell.textContent)).filter(Boolean).join(' | '));
  expect(rows).toEqual(['TikTok | 312', 'X | 48', 'Instagram | 0 nothing found', 'YouTube | Not measured | rate limited']);
  expect(chart.querySelectorAll('.ch42-bar-fill')).toHaveLength(3);
  expect(plain(chart.textContent)).not.toContain('214');
  const details = [...host.querySelectorAll('details')].find((node) => plain(node.querySelector('summary')?.textContent) === 'Technical details');
  expect(plain(details.textContent)).toContain('TikTok · read · 312 items');
});

test('live research steps are said in plain words and a run of one step reads once with its count', async () => {
  const {stepWords, collapseSteps} = await import('../ResearchLog.jsx');
  expect(stepWords('Counting in the warehouse: Check canonical_key prefixes for sound items')).toBe('Counting posts in the archive');
  expect(stepWords('Reading the question: South Africa, 28 September to 4 October, tier T1')).toBe('Reading the question: South Africa, 28 September to 4 October');
  // Restated 4 October 2026: a raw search query no longer shows in quotes; its terms are said as a reader would.
  expect(stepWords("Searching stored posts for 'sound OR trend' on TikTok")).toBe('Searching stored TikTok posts about sounds and trends');
  expect(stepWords("Searching stored posts for 'sound OR challenge OR trend' on TikTok, South Africa, 28 September to 4 October")).toBe('Searching stored TikTok posts about sounds, challenges and trends in South Africa, 28 September to 4 October');
  expect(stepWords("Searching stored posts for 'amapiano' on TikTok, Instagram, South Africa, 28 September to 4 October")).toBe('Searching stored TikTok and Instagram posts about amapiano in South Africa, 28 September to 4 October');
  expect(stepWords("Searching stored posts for 'food prices', Nigeria, 1 to 4 October")).toBe('Searching stored posts about food prices in Nigeria, 1 to 4 October');
  expect(stepWords("Searching stored posts for '#fixture_za_step OR Tyla'")).toBe('Searching stored posts about #fixture_za_step and Tyla');
  expect(stepWords("Checking saved findings for 'rising sounds TikTok South Africa'")).toBe('Checking findings already saved on this question');
  expect(stepWords('Reading TikTok posts tagged #fixture_za_step: 312 found')).toBe('Reading TikTok posts tagged #fixture_za_step: 312 found');
  const rows = collapseSteps([
    {seq: 1, kind: 'read', text: 'Counting in the warehouse: item_state'},
    {seq: 2, kind: 'read', text: 'Counting in the warehouse'},
    {seq: 3, kind: 'found', text: '10 posts found on TikTok'},
    {seq: 4, kind: 'read', text: 'Counting in the warehouse: post_items'},
  ]);
  expect(rows.map((row) => [row.text, row.times])).toEqual([
    ['Counting posts in the archive', 2], ['10 posts found on TikTok', 1], ['Counting posts in the archive', 1],
  ]);
});

test('gathered posts show a platform account for a bare numeric id and lead with the asked market', async () => {
  const {gatheredName, gatheredFirst} = await import('../../ask42.jsx');
  expect(gatheredName({handle: '100064794367851', platform: 'Facebook'})).toBe('Facebook account');
  expect(gatheredName({handle: '', platform: 'X'})).toBe('X account');
  expect(gatheredName({handle: 'CytronicsZA', platform: 'TikTok'})).toBe('CytronicsZA');
  const posts = [{id: 'a', market: 'KE'}, {id: 'b', market: 'ZA'}, {id: 'c'}, {id: 'd', market: 'ZA'}];
  expect(gatheredFirst(posts, 'ZA').map((p) => p.id)).toEqual(['b', 'd', 'a', 'c']);
  expect(gatheredFirst(posts, '').map((p) => p.id)).toEqual(['a', 'b', 'c', 'd']);
  expect(gatheredFirst(posts, 'NG').map((p) => p.id)).toEqual(['a', 'b', 'c', 'd']);
});

/* Visual QA, 5 October 2026 (A01, A04, A05): a budget stop is not headed as
   an evidence shortage, a warehouse unit reads as words, and an empty short
   answer says why. */
test('an answer the model budget stopped is headed as a budget stop, not as missing evidence', async () => {
  const record = clone(partialRecord);
  record.answer.status = 'insufficient_evidence';
  record.answer.short_answer = 'The answer was not completed because a model call could not fit within a verified model budget.';
  record.answer.claims = [];
  record.answer.evidence = [];
  record.answer.so_what = [];
  record.answer.watch_next = [];
  record.answer.gaps = [{what: 'The answer stopped because a model call could not be safely reserved',
    searched: 'stored posts and live sources, 29 September to 5 October',
    why: 'model cost or usage could not be verified within the per-question budget'}];
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => host.querySelector('.ask42-answer .ask42-status'), 'the status line');
  expect(plain(host.querySelector('.ask42-answer .ask42-status').textContent)).toBe("Stopped at this question's model budget, not for lack of evidence");
  expect(text()).not.toContain('Not enough evidence for a full answer');
});

test('a claim figure with a warehouse unit reads as words, singular for one', async () => {
  const record = clone(partialRecord);
  record.answer.claims[0].numbers = [
    {...record.answer.claims[0].numbers[0], value: 59, unit: 'located_posts'},
    {...record.answer.claims[0].numbers[0], value: 1, unit: 'located_creators'},
  ];
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => host.querySelector('.ask42-answer .ask42-figure'), 'the figures');
  const figures = [...host.querySelectorAll('.ask42-answer .ask42-figure')].map((node) => plain(node.textContent));
  expect(figures).toContain('59 posts with a known location.');
  expect(figures).toContain('1 creator with a known location.');
  expect(text()).not.toContain('located_');
});

/* Live review, 5 October 2026: the old fallback, "No short answer: too
   little passed the checks", said only what did not pass. It now says what
   did. */
test('a partial answer with no short answer says what passed instead of leaving the space empty', async () => {
  const record = clone(partialRecord);
  record.answer.short_answer = '';
  serve([record]);
  await render({query: {follow: record.ask_id}});
  await until(() => host.querySelector('.ask42-answer'), 'the answer');
  expect(plain(host.querySelector('.ask42-answer .ask42-short').textContent)).toBe('1 checked finding from 2 posts on TikTok is below. The one-line summary did not pass the checks.');
});

test('the no-short-answer words count findings, posts and platforms, and say when nothing passed', async () => {
  const {noShortAnswer} = await import('../../ask42.jsx');
  expect(noShortAnswer({claims: [], evidence: []})).toBe('Nothing passed the checks to sum up.');
  expect(noShortAnswer({claims: [{id: 'c1'}, {id: 'c2'}], evidence: [{platform: 'tiktok'}, {platform: 'youtube'}, {platform: 'x'}]}))
    .toBe('2 checked findings from 3 posts on TikTok, YouTube and X are below. The one-line summary did not pass the checks.');
});

test('a gap why that is a sentence stays whole, and status codes read as one list', async () => {
  const {whyAll} = await import('../../ask42.jsx');
  expect(whyAll("every cited post must fall inside the question's window and market, and text that names a place must cite a post located there"))
    .toBe("every cited post must fall inside the question's window and market, and text that names a place must cite a post located there");
  expect(whyAll('empty, rate_limited, auth_failed')).toBe('nothing found, rate limited and access failed');
  expect(whyAll('rate_limited, rate_limited')).toBe('rate limited');
  expect(whyAll('schema_drift')).toBe('the source changed shape');
  expect(whyAll('')).toBe('');
});
