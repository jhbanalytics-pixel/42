/* The stored answer proof script (scripts/stored-answer-proof.mjs) against
   local fake apps only. Nothing here reaches staging or any other service.

   1. A fake app whose Enter 42 button POSTs /api/ask: the guard aborts the
      POST inside the browser, so the fake server never receives it, the run
      exits 2 and the report records the refusal and the paid attempt.
   2. A fake app serving the built app (app/web/dist) with two stored Ask
      records in 42's shape (the core/api fixtures, as the ask42 unit tests
      use them), one a follow-up of the other, and the HTML export: every
      check is SEEN, the checks 42 has no counterpart for are listed as not
      applicable with a reason, no question is ever sent, the page makes no
      API request outside the read only allowlist, and the access key
      appears in no output.
   3. A fake app whose Enter 42 button GETs /card/<query>: the retired desk
      card page ran a billed search, so the guard aborts it, the fake server
      never receives it and the run exits 2.
   4. A browser whose new context fails, and a browser that is not installed,
      stubbed in process: report.json is still written with the failure, the
      key stays out of it, and a missing browser names the install command. */
import {expect, test} from '@playwright/test';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {existsSync, mkdtempSync, readFileSync, readdirSync, statSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {extname, join, normalize, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {ALLOWED_NON_GET, ALLOWED_READ_ENDPOINTS, ASK_ID, GUARDED_ENDPOINTS, NOT_APPLICABLE_IN_42, forbiddenTarget, guardDecision, parseInputs, routedPath, runProof} from '../../scripts/stored-answer-proof.mjs';

const SCRIPT = fileURLToPath(new URL('../../scripts/stored-answer-proof.mjs', import.meta.url));
const DIST = fileURLToPath(new URL('../../../web/dist/', import.meta.url));
const FIXTURES = new URL('../../src/ui/__tests__/fixtures/', import.meta.url);
const KEY = 'fixture-key-7Qz9-never-shown';
const readRecord = name => JSON.parse(readFileSync(fileURLToPath(new URL(name, FIXTURES)), 'utf8'));
/* The stored records: the complete and partial Ask fixtures (copies of
   core/api/fixtures/ask_complete.json and ask_partial.json), the partial one
   recorded as a follow-up of the complete one. */
const FIRST_RECORD = readRecord('ask42_complete.json');
const FOLLOW_RECORD = {...readRecord('ask42_partial.json'), parent_id: FIRST_RECORD.ask_id};
const FIRST = FIRST_RECORD.ask_id;
const FOLLOW = FOLLOW_RECORD.ask_id;

function listen(handler){
  const hits = [];
  const server = createServer((request, response) => {
    let body = '';
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => { hits.push({method: request.method, path: new URL(request.url, 'http://local').pathname, body}); handler(request, response, body); });
  });
  return new Promise(done => server.listen(0, '127.0.0.1', () => done({server, hits, origin: 'http://127.0.0.1:' + server.address().port})));
}

function runScript(env){
  return new Promise(done => {
    const child = spawn(process.execPath, [SCRIPT], {env: {PATH: process.env.PATH, CHROME_PATH: process.env.CHROME_PATH || '', HOME: process.env.HOME || tmpdir(), ...env}});
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('close', code => done({code, stdout, stderr}));
  });
}

function workspace(){
  const out = mkdtempSync(join(tmpdir(), 'stored-answer-proof-'));
  const keyFile = join(mkdtempSync(join(tmpdir(), 'stored-answer-key-')), 'key');
  writeFileSync(keyFile, KEY + '\n', {mode: 0o600});
  return {out: join(out, 'run'), keyFile};
}

function json(response, status, value){
  response.writeHead(status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
  response.end(JSON.stringify(value));
}

/* A minimal page with the gate the script expects; its Enter 42 button
   checks the key and then POSTs a question, as a regressed app might. */
const SUBMITTING_PAGE = `<!doctype html><html lang="en"><head><title>Fake</title></head><body><main id="main-content">
<h2>Enter the desk.</h2><form id="gate"><label for="gate-passcode">Access key</label><input id="gate-passcode" type="password">
<button type="submit" class="gate-v4-submit">Enter 42</button></form><p id="gate-status"></p></main><script>
document.getElementById('gate').addEventListener('submit', async (event) => {
  event.preventDefault();
  const code = document.getElementById('gate-passcode').value;
  await fetch('/api/auth/verify', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({passcode: code})});
  await fetch('/api/ask', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Passcode': code}, body: JSON.stringify({question: 'xyz', market: 'ZA', parent_id: null})}).catch(() => {});
  document.getElementById('gate').remove();
});
</script></body></html>`;

test('the guard aborts POST /api/ask before it leaves the browser and exits 2', async () => {
  test.setTimeout(120000);
  const app = await listen((request, response) => {
    const path = new URL(request.url, 'http://local').pathname;
    if (path === '/api/auth/verify') return json(response, 200, {ok: true});
    if (path === '/api/ask') return json(response, 202, {ask_id: 'a_should_never_exist'});
    response.writeHead(200, {'Content-Type': 'text/html'}); response.end(SUBMITTING_PAGE);
  });
  try {
    const {out, keyFile} = workspace();
    const result = await runScript({STAGING_URL: app.origin, ACCESS_KEY_FILE: keyFile, STORED_ASK_IDS: FIRST, PROOF_OUT_DIR: out});
    expect(app.hits.filter(hit => hit.path === '/api/ask')).toEqual([]);
    expect(result.code).toBe(2);
    const report = JSON.parse(readFileSync(join(out, 'report.json'), 'utf8'));
    expect(report.outcome).toBe('refused');
    expect(report.exit_code).toBe(2);
    expect(report.refusal).toMatchObject({kind: 'network', method: 'POST', path: '/api/ask', where: 'core/api/app.py:741', paid: true});
    expect(report.paid_submissions_attempted).toBe(1);
    expect(report.guard.installed_before_navigation).toBe(true);
    expect(result.stderr).toContain('REFUSED POST /api/ask');
    for (const text of [result.stdout, result.stderr, readFileSync(join(out, 'report.json'), 'utf8')]) expect(text).not.toContain(KEY);
  } finally { app.server.close(); }
});

test('guard decisions: every route that starts, charges or writes is refused, only the read only requests allowed', () => {
  const origin = 'https://app.local';
  expect(guardDecision('POST', origin + '/api/ask', origin).allow).toBe(false);
  expect(guardDecision('POST', origin + '/api/ask/', origin).allow).toBe(false);
  expect(guardDecision('POST', origin + '/api/ask/' + FIRST + '/stop', origin)).toMatchObject({allow: false, where: 'core/api/app.py:901'});
  expect(guardDecision('POST', origin + '/api/spikes', origin)).toMatchObject({allow: false, where: 'core/api/app.py:787', paid: true});
  expect(guardDecision('POST', origin + '/api/investigations', origin)).toMatchObject({allow: false, where: 'core/api/app.py:1003'});
  expect(guardDecision('POST', origin + '/api/investigations/inv_1/start', origin)).toMatchObject({allow: false, where: 'core/api/app.py:1028', paid: true});
  expect(guardDecision('PUT', origin + '/api/investigations/inv_1/plan', origin).allow).toBe(false);
  expect(guardDecision('POST', origin + '/api/investigations/inv_1/stop', origin).allow).toBe(false);
  expect(guardDecision('POST', origin + '/api/dossiers', origin).allow).toBe(false);
  expect(guardDecision('POST', origin + '/api/findings', origin).allow).toBe(false);
  expect(guardDecision('POST', origin + '/api/chat/send', origin).allow).toBe(false);
  expect(guardDecision('PUT', origin + '/api/ask', origin).allow).toBe(false);
  expect(guardDecision('POST', origin + '/api/anything/new', origin).allow).toBe(false);
  expect(guardDecision('POST', origin + '/api/health', origin).allow).toBe(false);
  expect(guardDecision('POST', origin + '/api/ask/' + FIRST, origin).allow).toBe(false);
  expect(guardDecision('POST', 'https://elsewhere.local/api/ask', origin).allow).toBe(false);
  expect(guardDecision('GET', origin + '/api/ask?q=x', origin).allow).toBe(false);
  /* The reads Ask's stored view makes are allowed, for 42's ask ids. */
  for (const id of [FIRST, FOLLOW, 'r_20260928_061500_1a2b3c4d_' + '0123456789abcdef0123456789abcdef', 'sched-2026-09-28-s_weekly_za']){
    expect(ASK_ID.test(id), id).toBe(true);
    expect(guardDecision('GET', origin + '/api/ask/' + id, origin).allow, id).toBe(true);
    expect(guardDecision('GET', origin + '/api/ask/' + id + '/events', origin).allow, id).toBe(true);
    expect(guardDecision('GET', origin + '/api/ask/' + id + '/export?format=html', origin).allow, id).toBe(true);
  }
  expect(guardDecision('GET', origin + '/api/health', origin).allow).toBe(true);
  /* Any other API read is outside the allowlist. */
  for (const path of ['/api/today', '/api/discover?market=ZA', '/api/desk', '/api/research/recent', '/api/chat/status?job_id=x', '/api/ask/a/b', '/api/ask/' + 'x'.repeat(129), '/internal/general-question/execute']){
    expect(guardDecision('GET', origin + path, origin).allow, path).toBe(false);
  }
  /* The built app's own files and stills on other origins load. */
  for (const url of [origin + '/', origin + '/assets/index-abc.js', 'https://cdn.elsewhere.local/still.jpg']) expect(guardDecision('GET', url, origin).allow, url).toBe(true);
  for (const {method, path} of ALLOWED_NON_GET) expect(guardDecision(method, origin + path, origin).allow).toBe(true);
  expect(ALLOWED_NON_GET.map(item => item.method + ' ' + item.path)).toEqual(['POST /api/auth/verify']);
  expect(GUARDED_ENDPOINTS.every(item => /^core\/api\/app\.py:\d+/.test(item.where))).toBe(true);
  expect(ALLOWED_READ_ENDPOINTS.every(item => /^core\/api\/app\.py:\d+/.test(item.where))).toBe(true);
});

test('the script refuses the question form, the suggested questions and every control that asks or writes', () => {
  expect(forbiddenTarget({tag: 'TEXTAREA', label: 'Your question', name: '', inComposer: true, composerLabel: 'Your question'})).not.toBeNull();
  expect(forbiddenTarget({tag: 'TEXTAREA', label: 'Your question', name: '', inComposer: false, composerLabel: ''})).not.toBeNull();
  expect(forbiddenTarget({tag: 'BUTTON', label: '', name: 'Is #fixture showing up on Instagram Reels yet?', inComposer: true, composerLabel: 'Ask next'})).not.toBeNull();
  for (const name of ['Ask', 'Stop', 'Try again', 'Ask again', 'Ask a follow-up', 'Add to dossier', 'Save checked claims to Findings']){
    expect(forbiddenTarget({tag: 'BUTTON', label: '', name, inComposer: false, composerLabel: ''}), name).not.toBeNull();
  }
  expect(forbiddenTarget({tag: 'BUTTON', label: '', name: 'Read the brand', inComposer: true, composerLabel: 'Ask with a skill'})).not.toBeNull();
  expect(forbiddenTarget({tag: 'INPUT', label: 'Access key', name: '', inComposer: false, composerLabel: ''})).toBeNull();
  expect(forbiddenTarget({tag: 'BUTTON', label: '', name: 'Source: TikTok post by @fixture_za_1, 24 September 2026', inComposer: false, composerLabel: ''})).toBeNull();
  expect(forbiddenTarget({tag: 'BUTTON', label: '', name: 'Export answer', inComposer: false, composerLabel: ''})).toBeNull();
  expect(forbiddenTarget({tag: 'BUTTON', label: '', name: 'Close', inComposer: false, composerLabel: ''})).toBeNull();
});

test('the older desk request id fields are refused with the reason, ask ids are taken as f42-api accepts them', () => {
  const {keyFile} = workspace();
  const base = {STAGING_URL: 'https://app.local', ACCESS_KEY_FILE: keyFile, PROOF_OUT_DIR: '/tmp/x'};
  const old = parseInputs({...base, STORED_REQUEST_IDS: '11111111-1111-4111-8111-111111111111'});
  expect(old.errors.join(' ')).toContain('which 42 does not store');
  const good = parseInputs({...base, STORED_ASK_IDS: FIRST + ', ' + FIRST, FOLLOW_UP_ASK_IDS: FOLLOW + ' ' + FIRST});
  expect(good.errors).toEqual([]);
  expect(good.firsts).toEqual([FIRST]);
  expect(good.followUps).toEqual([FOLLOW]);
  expect(parseInputs({...base, STORED_ASK_IDS: 'a/b'}).errors.join(' ')).toContain('not an ask id');
  expect(parseInputs({...base, STORED_ASK_IDS: 'x'.repeat(129)}).errors.join(' ')).toContain('not an ask id');
});

/* The export page as core/api/export.py:193-313 builds it for a record. */
function exportPage(record){
  const evidence = record.answer.evidence || [];
  const sources = evidence.map((item, index) => `<article id="source-${index + 1}"><h3>[${index + 1}] ${item.platform} · ${item.handle}</h3><blockquote>${item.text}</blockquote></article>`).join('');
  return '<!doctype html><html lang="en-ZA"><head><meta charset="utf-8"><title>42 answer</title></head><body>'
    + `<header><p class="brand">42 Ogilvy Intelligence</p><h1>42 answer</h1><p>Question: ${record.question}</p></header>`
    + `<main><section><h2>Short answer</h2><p>${record.answer.short_answer}</p></section><section><h2>Sources</h2>${sources}</section></main>`
    + `<footer><p class="note">${record.run.credits} credits · ${record.run.seconds} seconds · run ${record.run.run_id} · ask ${record.ask_id}</p></footer></body></html>`;
}

const RECORDS = {[FIRST]: FIRST_RECORD, [FOLLOW]: FOLLOW_RECORD};
const HEALTH = {ok: true, service: 'f42-api', version: 'test', auth_mode: 'passcode', passcode: true, t2_ready: false, checks: {auth: 'ok', bigquery: 'ok', agent: 'ok', today: 'ok'}};

function storedAnswerApp(request, response, body){
  const url = new URL(request.url, 'http://local');
  const path = url.pathname;
  if (path.startsWith('/api/') || path.startsWith('/internal/')){
    if (path === '/api/health') return json(response, 200, HEALTH);
    if (path === '/api/auth/verify'){
      let code = null; try { code = JSON.parse(body).passcode; } catch { /* no body */ }
      return code === KEY ? json(response, 200, {ok: true}) : json(response, 401, {error: 'unauthorized', message: 'Missing or wrong passcode.'});
    }
    if (request.headers['x-passcode'] !== KEY) return json(response, 401, {error: 'unauthorized', message: 'Passcode required'});
    if (path === '/api/ask') return json(response, 202, {ask_id: 'a_should_never_exist'});
    const read = /^\/api\/ask\/([A-Za-z0-9_-]{1,128})(\/events|\/export)?$/.exec(path);
    const record = read && RECORDS[read[1]];
    if (read && !record) return json(response, 404, {error: 'not_found', message: 'No Ask with that id.'});
    if (read && !read[2]) return json(response, 200, record);
    if (read && read[2] === '/events'){
      const seq = Math.max(0, ...(record.steps || []).map(step => step.seq || 0)) + 1;
      response.writeHead(200, {'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store'});
      return response.end(`id: ${seq}\nevent: done\ndata: ${JSON.stringify({seq, status: record.status, url: '/api/ask/' + record.ask_id})}\n\n`);
    }
    if (read && read[2] === '/export'){
      if (url.searchParams.get('format') !== 'html') return json(response, 400, {error: 'bad_request', message: 'Only format=html is available until Stage 3.'});
      response.writeHead(200, {'Content-Type': 'text/html; charset=utf-8', 'Content-Disposition': `attachment; filename="42-answer-${record.ask_id}.html"`});
      return response.end(exportPage(record));
    }
    return json(response, 404, {error: 'not_found', message: 'No such API route.'});
  }
  const relative = path === '/' ? 'index.html' : path.replace(/^\/+/, '');
  let file = normalize(join(DIST, relative));
  if (!file.startsWith(DIST) || !existsSync(file) || !statSync(file).isFile()) file = join(DIST, 'index.html');
  const types = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.json': 'application/json'};
  response.writeHead(200, {'Content-Type': types[extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store'});
  response.end(readFileSync(file));
}

test.describe.serial('stored answers through the built app', () => {
  test.setTimeout(120000);
  let app, run, out;
  test.beforeAll(async () => {
    test.setTimeout(120000);
    if (!existsSync(join(DIST, 'index.html'))) throw new Error('Build the app first: app/web/dist/index.html is missing');
    app = await listen(storedAnswerApp);
    const paths = workspace();
    out = paths.out;
    run = await runScript({STAGING_URL: app.origin, ACCESS_KEY_FILE: paths.keyFile, STORED_ASK_IDS: FIRST, FOLLOW_UP_ASK_IDS: FOLLOW, PROOF_OUT_DIR: out});
  });
  test.afterAll(() => app?.server.close());

  test('every stored answer check is SEEN and nothing is submitted', () => {
    const report = JSON.parse(readFileSync(join(out, 'report.json'), 'utf8'));
    const missing = report.checks.filter(item => item.status !== 'SEEN');
    expect(missing, JSON.stringify(missing, null, 2) + '\n' + run.stderr).toEqual([]);
    expect(run.code).toBe(0);
    expect(report.outcome).toBe('complete');
    expect(report.refusal).toBeNull();
    expect(report.paid_submissions_attempted).toBe(0);
    expect(app.hits.filter(hit => hit.method !== 'GET' && hit.method !== 'HEAD').map(hit => hit.path).filter(path => !ALLOWED_NON_GET.some(item => item.path === path))).toEqual([]);
    expect(app.hits.some(hit => hit.path === '/api/ask' || /\/stop$/.test(hit.path))).toBe(false);
    /* Every API request that reached the server is on the read only
       allowlist, and the stored view read both records. */
    const apiHits = app.hits.filter(hit => hit.path.startsWith('/api/') || hit.path.startsWith('/internal/'));
    expect(apiHits.filter(hit => !guardDecision(hit.method, app.origin + hit.path, app.origin).allow)).toEqual([]);
    for (const id of [FIRST, FOLLOW]){
      expect(apiHits.some(hit => hit.method === 'GET' && hit.path === '/api/ask/' + id), id).toBe(true);
      expect(apiHits.some(hit => hit.method === 'GET' && hit.path === '/api/ask/' + id + '/export'), id).toBe(true);
    }
    /* The guard continues allowed requests unchanged: the gate check reached
       the server with the typed key in its body. */
    expect(app.hits.filter(hit => hit.path === '/api/auth/verify').map(hit => JSON.parse(hit.body).passcode)).toEqual([KEY]);
    const steps = new Set(report.checks.map(item => item.step));
    for (const step of ['1.1', '1.2', '2.1', '2.2', '2.3', '2.4', '2.5', '2.6', '2.7', '2.8', '3.2', '3.4', '3.6', '4.1', '4.2', '4.3', '4.4', '4.5', '4.6', '4.8', '5.1', '5.2', '5.3']) expect(steps.has(step), step).toBe(true);
    for (const id of [FIRST, FOLLOW]) expect(report.checks.filter(item => item.step === '2.1' && item.ask_id === id).length, id).toBe(1);
    expect(report.checks.find(item => item.step === '3.2').parent_ask_id_as_read).toBe(FIRST);
    /* The desk checks 42 has no counterpart for are listed with a reason,
       never as NOT SEEN. */
    expect(report.not_applicable.map(item => item.step)).toEqual(['2.6', '3.1', '3.3', '3.5', '4.7', '5.1']);
    expect(report.not_applicable.map(item => item.step)).toEqual(NOT_APPLICABLE_IN_42.map(item => item.step));
    for (const item of report.not_applicable){
      expect(item.status, item.step).toBe('NOT APPLICABLE IN 42');
      expect(item.reason.length, item.step).toBeGreaterThan(40);
      expect(item.desk_check.length, item.step).toBeGreaterThan(0);
    }
    expect(run.stdout).toContain('N/A      3.1');
    expect(report.downloads.map(item => item.file)).toEqual(['42-answer-' + FIRST + '.html', '42-answer-' + FOLLOW + '.html']);
    for (const name of report.downloads.map(item => item.file)) expect(existsSync(join(out, name))).toBe(true);
    for (const name of report.screenshots) expect(existsSync(join(out, name))).toBe(true);
  });

  test('the access key appears in no output, report, file name or text file', () => {
    expect(run.stdout).not.toContain(KEY);
    expect(run.stderr).not.toContain(KEY);
    for (const name of readdirSync(out)){
      expect(name).not.toContain(KEY);
      if (/\.(json|txt|html)$/.test(name)) expect(readFileSync(join(out, name), 'utf8')).not.toContain(KEY);
    }
    const report = JSON.parse(readFileSync(join(out, 'report.json'), 'utf8'));
    expect(report.screenshots.length).toBeGreaterThan(0);
    expect(report.screenshots.every(name => !name.includes(KEY))).toBe(true);
  });
});

/* Keeps the resolved path helpers honest when the file moves. */
test('the script and the built app live where the tests expect', () => {
  expect(existsSync(SCRIPT)).toBe(true);
  expect(resolve(DIST).endsWith(join('app', 'web', 'dist'))).toBe(true);
});

test('guard decisions: GET /card/<query> is refused like GET /api/ask', () => {
  const origin = 'https://app.local';
  for (const path of ['/card/brands', '/card/South%20Africa%20brands', '/card/a%2Fb?market=za&t=abc', '/card/x?market=all']){
    const decision = guardDecision('GET', origin + path, origin);
    expect(decision.allow, path).toBe(false);
    expect(decision.where, path).toBe('core/api/app.py:1087');
  }
  expect(guardDecision('GET', origin + '/cardinal', origin).allow).toBe(true);
  /* Outside the allowlist, but not mistaken for the card route. */
  expect(guardDecision('GET', origin + '/api/card-link?q=x', origin)).toMatchObject({allow: false, where: null});
});

const CARD_PAGE = SUBMITTING_PAGE.replace(
  "await fetch('/api/ask', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Passcode': code}, body: JSON.stringify({question: 'xyz', market: 'ZA', parent_id: null})}).catch(() => {});",
  "await fetch('/card/South%20Africa%20brands?market=za', {headers: {'X-Passcode': code}}).catch(() => {});",
);

test('the guard aborts GET /card/<query> before it leaves the browser and exits 2', async () => {
  test.setTimeout(120000);
  expect(CARD_PAGE).toContain('/card/South%20Africa%20brands');
  const app = await listen((request, response) => {
    const path = new URL(request.url, 'http://local').pathname;
    if (path === '/api/auth/verify') return json(response, 200, {ok: true});
    if (path.startsWith('/card/')){ response.writeHead(200, {'Content-Type': 'text/html'}); return response.end('<p>card</p>'); }
    response.writeHead(200, {'Content-Type': 'text/html'}); response.end(CARD_PAGE);
  });
  try {
    const {out, keyFile} = workspace();
    const result = await runScript({STAGING_URL: app.origin, ACCESS_KEY_FILE: keyFile, STORED_ASK_IDS: FIRST, PROOF_OUT_DIR: out});
    expect(app.hits.filter(hit => hit.path.startsWith('/card/'))).toEqual([]);
    expect(result.code).toBe(2);
    const report = JSON.parse(readFileSync(join(out, 'report.json'), 'utf8'));
    expect(report.outcome).toBe('refused');
    expect(report.refusal).toMatchObject({kind: 'network', method: 'GET', path: '/card/South%20Africa%20brands', where: 'core/api/app.py:1087'});
    expect(report.paid_submissions_attempted).toBe(0);
    expect(result.stderr).toContain('REFUSED GET /card/');
    for (const text of [result.stdout, result.stderr, readFileSync(join(out, 'report.json'), 'utf8')]) expect(text).not.toContain(KEY);
  } finally { app.server.close(); }
});

function sink(){
  const lines = [];
  return {lines, write: text => { lines.push(String(text)); return true; }};
}

function stubbedRun(chromium, key){
  const {out, keyFile} = workspace();
  if (key !== undefined) writeFileSync(keyFile, key + '\n', {mode: 0o600});
  const io = {out: sink(), err: sink(), chromium};
  const env = {STAGING_URL: 'http://127.0.0.1:9', ACCESS_KEY_FILE: keyFile, STORED_ASK_IDS: FIRST, PROOF_OUT_DIR: out};
  return runProof(env, io).then(code => ({code, out, keyFile, stdout: io.out.lines.join(''), stderr: io.err.lines.join('')}), error => ({code: null, error, out, keyFile, stdout: io.out.lines.join(''), stderr: io.err.lines.join('')}));
}

test('a browser context that fails before the first page still writes report.json, key redacted, exit 3', async () => {
  let closed = false;
  const chromium = {launch: async () => ({
    newContext: async () => { throw new Error('context refused for key ' + KEY + '\nsecond line ' + KEY); },
    close: async () => { closed = true; },
  })};
  const run = await stubbedRun(chromium);
  expect(run.error, String(run.error)).toBeUndefined();
  expect(run.code).toBe(3);
  const file = join(run.out, 'report.json');
  expect(existsSync(file)).toBe(true);
  const text = readFileSync(file, 'utf8');
  const report = JSON.parse(text);
  expect(report.outcome).toBe('start_failed');
  expect(report.exit_code).toBe(3);
  expect(report.error).toContain('context refused for key');
  expect(report.guard.installed_before_navigation).toBe(false);
  expect(closed).toBe(true);
  for (const value of [text, run.stdout, run.stderr]){
    expect(value).not.toContain(KEY);
    expect(value).not.toContain(readFileSync(run.keyFile, 'utf8').trim());
  }
});

test('a missing browser executable writes the report and names the install command', async () => {
  const chromium = {launch: async () => { throw new Error("browserType.launch: Executable doesn't exist at /nowhere/chrome\nmore detail"); }};
  const run = await stubbedRun(chromium);
  expect(run.code).toBe(3);
  expect(run.stderr).toContain('npx playwright install chromium');
  const report = JSON.parse(readFileSync(join(run.out, 'report.json'), 'utf8'));
  expect(report.outcome).toBe('browser_unavailable');
  expect(report.hint).toContain('npx playwright install chromium');
});

test('guard decisions: percent encoded paths are decoded before matching, undecodable ones refused', () => {
  const origin = 'https://app.local';
  for (const path of ['/%63ard/x', '/card%2Fx', '/card%2fx', '/api/%61sk', '/api/%2561sk?q=x', '/card/%E0%A4%A']){
    expect(guardDecision('GET', origin + path, origin).allow, path).toBe(false);
  }
  for (const path of ['/api/%61sk', '/api%2Fask', '/api/%2561sk']) expect(guardDecision('POST', origin + path, origin)).toMatchObject({allow: false, where: 'core/api/app.py:741'});
  expect(guardDecision('POST', origin + '/api/ask/' + FIRST + '/%73top', origin)).toMatchObject({allow: false, where: 'core/api/app.py:901'});
  expect(guardDecision('GET', origin + '/%63ard/x', origin).where).toBe('core/api/app.py:1087');
  expect(guardDecision('GET', origin + '/api/%61sk', origin).where).toBe('core/api/app.py:1060');
  expect(guardDecision('GET', origin + '/cardinal', origin).allow).toBe(true);
  expect(guardDecision('GET', origin + '/api/card-link?q=x', origin).allow).toBe(false);
  expect(guardDecision('GET', origin + '/api/ask/' + FIRST + '/export?format=html', origin).allow).toBe(true);
  expect(guardDecision('GET', origin + '/api/ask/' + FIRST + '%2Fexport?format=html', origin).allow).toBe(true);
});

test('guard decisions: a trailing slash does not get past /api/ask', () => {
  const origin = 'https://app.local';
  for (const path of ['/api/ask/', '/api/ask/?q=x', '/api/ask', '/card/', '/card/x/']){
    expect(guardDecision('GET', origin + path, origin).allow, path).toBe(false);
  }
  for (const path of ['/api/ask/', '/api/ask', '/api/ask//']) expect(guardDecision('POST', origin + path, origin).allow, path).toBe(false);
  expect(guardDecision('GET', origin + '/api/ask/brief', origin).allow).toBe(true);
  expect(guardDecision('GET', origin + '/api/ask/' + FIRST + '/', origin).allow).toBe(false);
});

test('a key with a quote, a backslash and a tab is redacted in its JSON escaped form too', async () => {
  const key = 'ab"c\\d\tSECRET';
  const chromium = {launch: async () => ({
    newContext: async () => { throw new Error('context refused for ' + key); },
    close: async () => {},
  })};
  const run = await stubbedRun(chromium, key);
  expect(run.code).toBe(3);
  const text = readFileSync(join(run.out, 'report.json'), 'utf8');
  for (const value of [text, run.stdout, run.stderr]){
    expect(value).not.toContain(key);
    expect(value).not.toContain(JSON.stringify(key).slice(1, -1));
    expect(value).not.toContain('SECRET');
  }
  expect(JSON.parse(text).error).toContain('[redacted]');
});

test('a thrown value whose text cannot be read still writes report.json with a fixed message', async () => {
  const hostile = {get message(){ throw new Error('no message'); }, toString(){ throw new Error('no text'); }};
  const chromium = {launch: async () => ({newContext: async () => { throw hostile; }, close: async () => {}})};
  const run = await stubbedRun(chromium);
  expect(run.error, String(run.error && run.error.message)).toBeUndefined();
  expect(run.code).toBe(3);
  const report = JSON.parse(readFileSync(join(run.out, 'report.json'), 'utf8'));
  expect(report.outcome).toBe('start_failed');
  expect(report.error).toContain('the error could not be read');
  const launchRun = await stubbedRun({launch: async () => { throw hostile; }});
  expect(launchRun.error, String(launchRun.error && launchRun.error.message)).toBeUndefined();
  expect(launchRun.code).toBe(3);
  expect(JSON.parse(readFileSync(join(launchRun.out, 'report.json'), 'utf8')).outcome).toBe('browser_unavailable');
});

test('the report lists the read only requests the page makes, and the guard lets them through', async () => {
  const origin = 'https://app.local';
  for (const path of ['/api/health', '/api/ask/' + FIRST, '/api/ask/' + FIRST + '/events', '/api/ask/' + FIRST + '/export']) expect(guardDecision('GET', origin + path, origin).allow, path).toBe(true);
  const run = await stubbedRun({launch: async () => { throw new Error('no browser'); }});
  const report = JSON.parse(readFileSync(join(run.out, 'report.json'), 'utf8'));
  expect(report.guard.allowed_read_endpoints.map(item => item.method + ' ' + item.path + ' ' + item.where)).toEqual([
    'GET /api/health core/api/app.py:146',
    'GET /api/ask/<ask id> core/api/app.py:831',
    'GET /api/ask/<ask id>/events core/api/app.py:894',
    'GET /api/ask/<ask id>/export core/api/app.py:907',
  ]);
  expect(report.guard.allowed_non_get.map(item => item.method + ' ' + item.path + ' ' + item.where)).toEqual(['POST /api/auth/verify core/api/app.py:223']);
  expect(report.not_applicable.map(item => item.step)).toEqual(NOT_APPLICABLE_IN_42.map(item => item.step));
});

test('guard decisions: GET /api/listen is refused plain, with a trailing slash and encoded', () => {
  const origin = 'https://app.local';
  for (const path of ['/api/listen?q=x', '/api/listen/?q=x', '/api/%6Cisten?q=x', '/api%2Flisten?q=x', '/api/%256Cisten']){
    const decision = guardDecision('GET', origin + path, origin);
    expect(decision.allow, path).toBe(false);
    expect(decision.where, path).toBe('core/api/app.py:1060');
  }
  /* Outside the allowlist too, but not labelled as the listen route. */
  for (const path of ['/api/listening', '/api/listen-now', '/api/listen/x']) expect(guardDecision('GET', origin + path, origin), path).toMatchObject({allow: false, where: null});
});

test('guard decisions: HEAD on a named endpoint is refused, HEAD on the allowed reads and app files allowed', () => {
  const origin = 'https://app.local';
  for (const path of ['/api/ask?q=x', '/api/ask/', '/card/x', '/%63ard/x', '/api/listen?q=x', '/api/listen/', '/api/desk', '/api/listening']){
    expect(guardDecision('HEAD', origin + path, origin).allow, path).toBe(false);
  }
  for (const path of ['/', '/api/health', '/api/ask/' + FIRST, '/api/ask/' + FIRST + '/export', '/cardinal', '/assets/index-abc.js']){
    expect(guardDecision('HEAD', origin + path, origin).allow, path).toBe(true);
  }
});

test('guard decisions: GET and HEAD /api/prewarm and /api/lexicon/<term> are refused', () => {
  const origin = 'https://app.local';
  const cases = [
    ['/api/prewarm', 'core/api/app.py:1060'], ['/api/prewarm/', 'core/api/app.py:1060'], ['/api/%70rewarm', 'core/api/app.py:1060'],
    ['/api/lexicon/sharp', 'core/api/app.py:1060'], ['/api/lexicon/sharp%20sharp?region=za', 'core/api/app.py:1060'],
    ['/api/lexicon/sharp/', 'core/api/app.py:1060'], ['/api/lexicon%2Fsharp', 'core/api/app.py:1060'],
  ];
  for (const verb of ['GET', 'HEAD']){
    for (const [path, where] of cases){
      const decision = guardDecision(verb, origin + path, origin);
      expect(decision.allow, verb + ' ' + path).toBe(false);
      expect(decision.where, verb + ' ' + path).toBe(where);
    }
  }
  for (const path of ['/api/prewarming', '/api/lexicon', '/api/lexicons/x']) expect(guardDecision('GET', origin + path, origin), path).toMatchObject({allow: false, where: null});
});

test('guard decisions: repeated slashes are collapsed before matching, legitimate paths unchanged', () => {
  const origin = 'https://app.local';
  for (const path of ['//api/listen?q=x', '/api//ask?q=x', '///card/x', '/api/%2Fask', '//api//prewarm']){
    expect(guardDecision('GET', origin + path, origin).allow, path).toBe(false);
  }
  for (const path of ['//api/ask', '/api//ask', '//api//ask//' + FIRST + '//stop']) expect(guardDecision('POST', origin + path, origin).allow, path).toBe(false);
  for (const path of ['/', '/api/health', '/api/ask/' + FIRST + '/export', '/assets/index-abc.js', '/api/auth/verify']){
    expect(routedPath(path), path).toBe(path);
  }
  expect(routedPath('//api///listen/')).toBe('/api/listen/');
  expect(guardDecision('GET', origin + '//api/health', origin).allow).toBe(true);
  expect(guardDecision('GET', origin + '//api//ask//' + FIRST, origin).allow).toBe(true);
});
