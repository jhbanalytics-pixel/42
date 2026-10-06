/* The same question under general 42 and under the Brand South Africa Pulse
   lens, in a real browser against the real app: ask, follow up, reload, reopen
   the stored answer, export it as HTML and PDF, and follow up from the reopened
   answer. Each conversation must keep its own lens on screen, in the requests
   the server binds and in both exports, and a follow-up under the other lens
   must be refused in plain words.

   Nothing on the API is intercepted. The FastAPI app runs under uvicorn from
   tests/journey/lens_journey_server.py with its real Ask, lens roster,
   Fieldwork detail and answer export routes, under the configured bsa_pulse
   scope. The one stand-in on the question path is at the engine boundary,
   tests/journey/lens_journey_engine.py: the worker object the question routes
   call in place of the engine process, and the store reader the Fieldwork
   detail read calls. It answers with the stored reply fixture plus one line
   naming the lens it framed the answer under, and it keeps the engine's rule
   that a follow-up cannot take context from another lens. The shell's desk,
   Ask coverage, storage and PDF renderer are the dossier journey's stand-ins.

   Needs the built page (vite build into ../web/dist) and the app's Python
   environment: LENS_JOURNEY_PYTHON, else DOSSIER_JOURNEY_PYTHON, else
   app/.venv/bin/python, else python3, the first that can import fastapi
   (support/journey-python.mjs), failing at once when none can. */
import {expect, test} from '@playwright/test';
import {spawn} from 'node:child_process';
import {existsSync, readFileSync} from 'node:fs';
import {createServer} from 'node:net';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {resolveJourneyPython} from './support/journey-python.mjs';

const APP_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
let PYTHON = null;

const LENS = 'bsa_pulse_lens';
const DIGEST = 'e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf';
const QUESTION = 'What were people in South Africa saying about amapiano and nightlife?';
const FOLLOW_UP = 'Which of those conversations came from Johannesburg?';
const REOPENED_FOLLOW_UP = 'Did any of it mention Youth Day?';
const LENSES = {
  general: {id: '', label: 'None', framing: 'Stand-in engine framing: general 42, no client lens.', exported: 'Client lens: None (general 42)'},
  bsa: {id: LENS, label: 'Brand South Africa Pulse', framing: 'Stand-in engine framing: Brand South Africa Pulse lens.', exported: 'Client lens: Brand South Africa Pulse'},
};

let server = null;
let base = '';

function freePort(){
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => { const {port} = probe.address(); probe.close(() => resolve(port)); });
  });
}

async function waitForServer(url, child){
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline){
    if (child.exitCode !== null) throw new Error('the journey server exited early');
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch (_error){
      /* not listening yet */
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error('the journey server did not start');
}

test.beforeAll(async () => {
  if (!existsSync(join(APP_ROOT, 'web/dist/index.html'))) throw new Error('Build the page first: vite build writes ../web/dist');
  PYTHON = resolveJourneyPython(['LENS_JOURNEY_PYTHON', 'DOSSIER_JOURNEY_PYTHON']);
  const port = await freePort();
  base = `http://127.0.0.1:${port}`;
  const environment = {...process.env};
  for (const name of ['K_SERVICE', 'K_REVISION', 'K_CONFIGURATION', 'DEPLOYMENT_PROFILE', 'UI_PASSCODE']) delete environment[name];
  server = spawn(PYTHON, ['-m', 'tests.journey.lens_journey_server', String(port)], {cwd: APP_ROOT, env: environment, stdio: ['ignore', 'ignore', 'pipe']});
  server.stderr.on('data', (chunk) => {
    const text = String(chunk);
    if (/Traceback/.test(text)) process.stderr.write(text);
  });
  await waitForServer(`${base}/api/health`, server);
});

test.afterAll(async () => {
  if (server && server.exitCode === null){
    server.kill('SIGTERM');
    await new Promise((resolve) => server.once('exit', resolve));
  }
});

const engine = async () => (await fetch(`${base}/__journey/engine`)).json();

/* Every HTTP failure the page meets must be one the step declared. */
async function openPage(browser){
  const context = await browser.newContext({acceptDownloads: true, viewport: {width: 1280, height: 900}});
  const page = await context.newPage();
  const log = {failures: [], pageErrors: []};
  page.on('pageerror', (error) => log.pageErrors.push(error.message));
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (response.status() >= 400 && path.startsWith('/api/chat')) log.failures.push({status: response.status(), path});
  });
  return {context, page, log};
}

const askRoute = (lensId, extra = '') => `${base}/#/console?work=ask${lensId ? '&lens=' + lensId : ''}${extra}`;
const framings = (page, lens) => page.getByText(lens.framing, {exact: true});
const threadLens = (page) => page.locator('[data-thread-lens]');

async function ask(page, label, text){
  const field = page.getByLabel(label, {exact: true});
  await field.fill(text);
  await page.getByRole('button', {name: 'Send', exact: true}).click();
}

async function exported(page, name){
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', {name, exact: true}).click(),
  ]);
  return readFileSync(await download.path());
}

for (const [key, lens] of Object.entries(LENSES)){
  test(`under ${key}: ask, follow up, reload, reopen, export and follow up again keep the request's own lens`, async ({browser}) => {
    test.setTimeout(90000);
    const {context, page, log} = await openPage(browser);
    const before = (await engine()).requests.length;

    /* Ask under the lens the route names, then follow up in the same conversation. */
    await page.goto(askRoute(lens.id));
    await expect(page.locator('#workbench-client-lens')).toHaveValue(lens.id);
    await ask(page, 'Your question', QUESTION);
    await expect(framings(page, lens)).toHaveCount(1);
    await expect(threadLens(page)).toHaveAttribute('data-thread-lens', lens.id || 'general');
    await expect(threadLens(page)).toHaveText('Client lens: ' + lens.label);
    await ask(page, 'Ask a follow-up', FOLLOW_UP);
    await expect(framings(page, lens)).toHaveCount(2);

    /* The server bound both requests to this lens, and the follow-up to its parent. */
    const admitted = (await engine()).requests.slice(before);
    expect(admitted.map((item) => [item.question, item.client_lens_id])).toEqual([[QUESTION, lens.id || null], [FOLLOW_UP, lens.id || null]]);
    expect(admitted.map((item) => item.configuration_digest)).toEqual(lens.id ? [DIGEST, DIGEST] : [null, null]);
    const [first, second] = admitted;
    expect([second.parent_request_id, second.thread_anchor_request_id]).toEqual([first.request_id, first.request_id]);
    expect(first.parent_request_id).toBeNull();

    /* A reload keeps the lens the conversation was asked under. A general 42
       reply writes its stored request onto the address, so a reload opens the
       follow-up from the server; a lensed reply keeps the lens route, so a
       reload reopens the saved conversation under it. */
    if (lens.id) expect(page.url()).toContain('lens=' + lens.id);
    else expect(page.url()).toContain('request=' + second.request_id);
    await page.reload();
    await expect(framings(page, lens)).toHaveCount(lens.id ? 2 : 1);
    await expect(threadLens(page)).toHaveAttribute('data-thread-lens', lens.id || 'general');
    await expect(threadLens(page)).toHaveText('Client lens: ' + lens.label);
    await expect(page.locator('[data-thread-lens-crossing]')).toHaveCount(0);

    /* Reopen the first answer from its stored request, as Fieldwork opens it:
       the address names no lens, so the lens shown comes from the server. */
    await page.goto(`${base}/#/console?work=ask&request=${first.request_id}`);
    await page.reload();
    await expect(page.getByRole('heading', {name: QUESTION, exact: true})).toBeVisible();
    await expect(framings(page, lens)).toHaveCount(1);
    await expect(threadLens(page)).toHaveAttribute('data-thread-lens', lens.id || 'general');
    await expect(threadLens(page)).toHaveText('Client lens: ' + lens.label);

    /* Both exports name the stored request's lens, and only that one. */
    const html = (await exported(page, 'Download as HTML')).toString('utf8');
    expect(html).toContain(lens.exported);
    expect(html).toContain(lens.framing);
    expect(html).toContain('Request reference: ' + first.request_id);
    const other = key === 'bsa' ? LENSES.general : LENSES.bsa;
    expect(html).not.toContain(other.exported);
    expect(html).not.toContain(other.framing);
    if (lens.id) expect(html).toContain(`Client lens configuration: ${LENS}, ${DIGEST}`);
    else expect(html).not.toContain('Client lens configuration');
    const pdf = (await exported(page, 'Download as PDF')).toString('latin1');
    expect(pdf.startsWith('%PDF-')).toBe(true);
    expect(pdf).toContain(lens.exported.replace(/[()]/g, (bracket) => '\\' + bracket));
    expect(pdf).not.toContain(other.exported.replace(/[()]/g, (bracket) => '\\' + bracket));

    /* A follow-up from the reopened answer continues under that answer's lens. */
    await ask(page, 'Ask a follow-up', REOPENED_FOLLOW_UP);
    await expect(framings(page, lens)).toHaveCount(2);
    await expect(threadLens(page)).toHaveAttribute('data-thread-lens', lens.id || 'general');
    await expect(page.locator('#workbench-client-lens')).toHaveValue(lens.id);
    const continued = (await engine()).requests.slice(before);
    expect(continued).toHaveLength(3);
    expect(continued[2]).toMatchObject({question: REOPENED_FOLLOW_UP, client_lens_id: lens.id || null, parent_request_id: first.request_id, thread_anchor_request_id: first.request_id});

    expect(log.failures).toEqual([]);
    expect(log.pageErrors).toEqual([]);
    await context.close();
  });
}

test('a follow-up under another lens is refused in plain words, before sending or by the service', async ({browser}) => {
  test.setTimeout(90000);
  const {context, page, log} = await openPage(browser);

  /* A general 42 conversation, then the page switched to the BSA lens. */
  await page.goto(askRoute(''));
  await ask(page, 'Your question', QUESTION);
  await expect(framings(page, LENSES.general)).toHaveCount(1);
  const [general] = (await engine()).requests.slice(-1);
  await page.locator('#workbench-client-lens').selectOption(LENS);
  const crossing = page.locator('[data-thread-lens-crossing]');
  await expect(crossing).toBeVisible();
  await expect(crossing).toHaveAttribute('role', 'alert');
  await expect(crossing).toContainText('This conversation was asked with no client lens.');
  await expect(crossing).toContainText('To ask under Brand South Africa Pulse, start a new question.');
  await page.getByLabel('Ask a follow-up', {exact: true}).fill(FOLLOW_UP);
  await expect(page.getByRole('button', {name: 'Send', exact: true})).toBeDisabled();
  const counted = (await engine()).requests.length;

  /* Choosing the conversation's lens again lifts the refusal. */
  await page.locator('#workbench-client-lens').selectOption('');
  await expect(crossing).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Send', exact: true})).toBeEnabled();

  /* A conversation saved before turns recorded their lens reads as general 42.
     If its answer was in fact asked under BSA, the page cannot know, so the
     service refuses the follow-up, and the page says so in words. */
  const bsaJob = await fetch(`${base}/api/chat/send`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: QUESTION, history: [], market: 'za', client_lens_id: LENS, idempotency_key: 'lens-journey-legacy-0001'})});
  expect(bsaJob.status).toBe(202);
  const {job_id: jobId} = await bsaJob.json();
  const stored = await (await fetch(`${base}/api/chat/status?job_id=${jobId}`)).json();
  expect(stored.intelligence.request_id).toMatch(/^[0-9a-f-]{36}$/);
  const legacy = {id: 'legacy-thread', ts: Date.now() + 60000, messages: [
    {role: 'user', content: QUESTION},
    {role: 'assistant', turnId: 'legacy-turn', content: stored.answer, sources: stored.sources, intelligence: stored.intelligence},
  ]};
  await page.evaluate((thread) => {
    const saved = JSON.parse(localStorage.getItem('pulse-chat') || '[]');
    localStorage.setItem('pulse-chat', JSON.stringify([thread, ...saved]));
  }, legacy);
  await page.goto(askRoute(''));
  await page.reload();
  await expect(framings(page, LENSES.bsa)).toHaveCount(1);
  await expect(threadLens(page)).toHaveAttribute('data-thread-lens', 'general');
  const refusalsBefore = (await engine()).refusals.length;
  await ask(page, 'Ask a follow-up', FOLLOW_UP);
  const refused = page.getByText('This follow-up could not take the earlier answer as its context, so nothing was started. Start a new question to ask it on its own.', {exact: true});
  await expect(refused).toBeVisible();
  await expect(page.getByText('parent_context_invalid')).toHaveCount(0);
  const after = await engine();
  expect(after.refusals.slice(refusalsBefore)).toEqual([expect.objectContaining({code: 'parent_context_invalid'})]);
  expect(after.requests.length).toBe(counted + 1);
  expect(after.requests.some((item) => item.parent_request_id === general.request_id)).toBe(false);

  expect(log.failures).toEqual([{status: 409, path: '/api/chat/send'}]);
  expect(log.pageErrors).toEqual([]);
  await context.close();
});
