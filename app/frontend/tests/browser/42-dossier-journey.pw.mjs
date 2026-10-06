/* The dossier review round trip in a real browser against the real app.

   Nothing on the API is intercepted. The FastAPI app runs under uvicorn from
   tests/journey/dossier_journey_server.py with its real routes and stores; the
   only stand-ins are the ones a test host cannot avoid, each named in
   tests/journey/dossier_journey_seams.py: the object creator bucket that holds
   the store to the staging grants, the renderer double for the pinned PDF
   runtime, and a verifier that accepts only the credential this test's fake
   Google provider signed with a key made for this run. The shell's desk and
   Ask coverage reads, which are not part of this journey, answer as empty.
   The provider script
   URL is answered with an empty script so no request leaves the machine, and
   the fake provider itself is an init script.

   Every page is watched: a console error, an uncaught error or a failed
   request fails the test unless the step that causes it declared it, and a
   declared failure that never happened fails it too.

   Needs the built page (vite build into ../web/dist) and the app's Python
   environment: DOSSIER_JOURNEY_PYTHON, else app/.venv/bin/python, else
   python3, the first that can import fastapi (support/journey-python.mjs),
   failing at once when none can. Rendered PDF pages are written for inspection to
   DOSSIER_JOURNEY_NOTES_DIR, else a directory under the system temporary
   directory, never inside the repository by default. */
import {expect, test} from '@playwright/test';
import {spawn, execFileSync} from 'node:child_process';
import {createHash, createHmac, randomBytes} from 'node:crypto';
import {existsSync, mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {resolveJourneyPython} from './support/journey-python.mjs';

const APP_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
const REPO_ROOT = fileURLToPath(new URL('../../../../', import.meta.url));
const NOTES_DIR = process.env.DOSSIER_JOURNEY_NOTES_DIR || join(tmpdir(), 'dossier-journey-pages');
let PYTHON = null;
const PROVIDER_SCRIPT = 'https://accounts.google.com/gsi/client';
const SUBJECT = '100000000000000000042';
const AUDIENCE = 'dossier-journey.apps.test';
const HOSTED_DOMAIN = 'journey.test';
const KEY_ID = 'dossier-journey-test-only';

const TEXTS = {
  obs: 'Weekend repair meetups are advertised in three Johannesburg community groups.',
  int: 'Repair is turning into a social weekend activity rather than a chore.',
  chal: 'One hardware chain reports that repair kit sales fell in August.',
  lim: 'The groups sampled are all in Gauteng, so the pattern is not shown elsewhere.',
  rec: 'Internal draft: pitch a repair cafe sponsorship before the holidays.',
  uncited: 'Repair interest is also rising in Accra.',
};
const INTERNAL_QUESTION = 'Is the August dip in kit sales seasonal?';
const DECISION_QUESTION = 'Which emerging behaviour should the brand act on in six weeks?';
const hex = (label) => createHash('sha256').update(label, 'utf8').digest('hex');

let server = null;
let base = '';
let key = null;
let receipts = null;

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
  PYTHON = resolveJourneyPython(['DOSSIER_JOURNEY_PYTHON']);
  key = randomBytes(32);
  const port = await freePort();
  base = `http://127.0.0.1:${port}`;
  const environment = {...process.env, DOSSIER_JOURNEY_SIGNING_KEY: key.toString('hex')};
  for (const name of ['K_SERVICE', 'K_REVISION', 'K_CONFIGURATION', 'DEPLOYMENT_PROFILE', 'UI_PASSCODE']) delete environment[name];
  server = spawn(PYTHON, ['-m', 'tests.journey.dossier_journey_server', '--port', String(port), '--subject', SUBJECT], {cwd: APP_ROOT, env: environment, stdio: ['ignore', 'inherit', 'pipe']});
  server.stderr.on('data', (chunk) => {
    const text = String(chunk);
    if (/Traceback|Error/.test(text) && !/question_activation_missing/.test(text)) process.stderr.write(text);
  });
  await waitForServer(`${base}/api/health`, server);
  receipts = {
    meetups: 'gqctx_' + hex('journey_meetups'),
    groups: 'gqctx_' + hex('journey_groups'),
    sales: 'gqctx_' + hex('journey_sales'),
    accra: 'gqctx_' + hex('journey_accra'),
  };
});

test.afterAll(async () => {
  if (server && server.exitCode === null){
    server.kill('SIGTERM');
    await new Promise((resolve) => server.once('exit', resolve));
  }
});

/* Investigations are created through the real API and their dossiers are
   published through the real store, the producer's step. */
function postInvestigation({runId, clientScopeId = 'ogilvy_default', clientLensId}){
  const body = {
    client_scope_id: clientScopeId, market_scope: ['za', 'ng', 'ke'], brand_config_id: clientScopeId === 'bsa_pulse' ? 'bsa' : null,
    audience_lens_ids: [], theme_id: null, run_id: runId, contract_version: '2.1.0',
    decision_question: DECISION_QUESTION, time_horizon_days: 42,
    brand_context: null, known_assumptions: [], change_my_mind_if: [], research_role_id: null, research_role_version: null,
    output_mode: 'internal_working_paper',
  };
  if (clientLensId !== undefined) body.client_lens_id = clientLensId;
  return fetch(`${base}/api/v2/investigations`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
}

async function createInvestigation({runId, clientScopeId = 'ogilvy_default', clientLensId, kind = 'main', label = ''}){
  const created = await postInvestigation({runId, clientScopeId, clientLensId});
  expect(created.status).toBe(200);
  const response = await created.json();
  const investigationId = response.investigation_id;
  const seeded = await control('/__journey/seed', {investigation_id: investigationId, kind, label});
  return {id: investigationId, version: seeded.dossier_version, response};
}

async function control(path, body){
  const response = await fetch(`${base}${path}`, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  expect(response.status).toBe(200);
  return response.json();
}

const storage = () => control('/__journey/storage');

function signCredential(claims){
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString('base64url');
  const head = encode({alg: 'HS256', kid: KEY_ID, typ: 'JWT'});
  const body = encode(claims);
  return `${head}.${body}.${createHmac('sha256', key).update(`${head}.${body}`).digest('base64url')}`;
}

/* One browser window of the reviewer: its own context, so its own session
   cookie, a fake provider, and a watch on everything that goes wrong. */
async function openReviewer(browser, {acceptDownloads = true} = {}){
  const context = await browser.newContext({acceptDownloads, viewport: {width: 1280, height: 900}});
  await context.route(PROVIDER_SCRIPT, (route) => route.fulfill({status: 200, contentType: 'text/javascript', body: '/* provider stand-in */'}));
  await context.exposeBinding('__journeySign', (_source, {nonce, aud}) => {
    const now = Math.floor(Date.now() / 1000);
    return signCredential({iss: 'https://accounts.google.com', aud, sub: SUBJECT, hd: HOSTED_DOMAIN, nonce, iat: now, exp: now + 600});
  });
  await context.addInitScript(() => {
    const state = {options: null};
    window.google = {accounts: {id: {
      initialize(options){ state.options = options; },
      renderButton(container){
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = 'Sign in with Google';
        button.addEventListener('click', async () => {
          const options = state.options;
          const credential = await window.__journeySign({nonce: options.nonce, aud: options.client_id});
          options.callback({credential, select_by: 'btn'});
        });
        container.appendChild(button);
      },
      prompt(){},
      cancel(){},
    }}};
  });
  const page = await context.newPage();
  const log = {console: [], pageErrors: [], failures: [], aborted: [], expected: [], expectedAborts: [], clientReadOpen: false, harness: []};
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    if (log.clientReadOpen && SRCDOC_HARNESS_SCRIPT.test(message.text()) && message.location().url === 'about:srcdoc'){ log.harness.push(message.text()); return; }
    log.console.push(message.text() + ' @' + JSON.stringify(message.location()));
  });
  page.on('pageerror', (error) => log.pageErrors.push(error.message));
  page.on('response', (response) => {
    if (response.status() >= 400) log.failures.push({status: response.status(), path: new URL(response.url()).pathname});
  });
  page.on('requestfailed', (request) => log.aborted.push({path: new URL(request.url()).pathname, error: request.failure() ? request.failure().errorText : ''}));
  return {context, page, log};
}

/* The one allowance. This harness injects its fake provider and signing
   binding into every frame, and Chromium sometimes reports that it refused to
   run them in the Client Read frame, which is sandboxed with no scripts,
   around a reload. The export itself carries no script (asserted below). The
   line is accepted only from about:srcdoc and only while a Client Read is on
   screen; anything else from that frame, a font request included, fails. */
const SRCDOC_HARNESS_SCRIPT = /^Blocked script execution in 'about:srcdoc' because the document's frame is sandboxed and the 'allow-scripts' permission is not set\.$/;

function expectFailure(log, status, pathPart, count = 1){
  for (let index = 0; index < count; index += 1) log.expected.push({status, pathPart});
}

/* Every observed failure must have been declared, and every declared one
   observed. Chromium reports each HTTP error response as a console error
   naming its status, so those are matched to the declared responses; any
   other console error, uncaught error or failed request fails. */
function verifyClean(log){
  const expected = [...log.expected];
  const unexpected = [];
  const statuses = [];
  for (const failure of log.failures){
    const index = expected.findIndex((item) => item.status === failure.status && failure.path.includes(item.pathPart));
    if (index === -1) unexpected.push(failure);
    else { expected.splice(index, 1); statuses.push(failure.status); }
  }
  const consoleUnexpected = [];
  for (const text of log.console){
    const match = /the server responded with a status of (\d+)/.exec(text);
    const index = match ? statuses.indexOf(Number(match[1])) : -1;
    if (index === -1) consoleUnexpected.push(text);
    else statuses.splice(index, 1);
  }
  const abortsExpected = [...log.expectedAborts];
  const abortsUnexpected = [];
  for (const failed of log.aborted){
    const index = abortsExpected.findIndex((part) => failed.path.includes(part) && failed.error.includes('ERR_ABORTED'));
    if (index === -1) abortsUnexpected.push(failed);
    else abortsExpected.splice(index, 1);
  }
  expect({unexpected, missing: expected, console: consoleUnexpected, pageErrors: log.pageErrors, aborted: abortsUnexpected, abortsMissing: abortsExpected})
    .toEqual({unexpected: [], missing: [], console: [], pageErrors: [], aborted: [], abortsMissing: []});
}

const route = (investigationId, extra = '') => `${base}/#/console?work=brief&investigation=${investigationId}${extra}`;
const review = (page) => page.locator('section.dossier-review');
const claimRow = (page, text) => page.locator('li.dossier-review__claim', {has: page.getByRole('heading', {name: text, exact: true})});
const linkRow = (page, parent, child) => page.locator('li[data-review-relationship]', {hasText: `Links ${parent} to ${child}`});
const notice = (page) => review(page).locator('.dossier-review__notice');

async function signIn(page){
  const button = page.getByRole('button', {name: 'Sign in with Google'});
  await expect(button).toHaveCount(1);
  await button.click();
  await expect(button).toHaveCount(0);
  await expect(page.locator('section.dossier-review[data-dossier-version]')).toBeVisible();
}

async function openSignedIn(page, investigationId, extra = ''){
  await page.goto(route(investigationId, extra));
  await signIn(page);
}

const shownVersion = (page) => page.locator('section.dossier-review[data-dossier-version]').getAttribute('data-dossier-version');
const urlVersion = (page) => new URL(page.url().replace('#/console?', 'x?')).searchParams.get('version');

async function saveVersion(page, from){
  await page.getByRole('button', {name: 'Save as a new version'}).click();
  await expect(notice(page)).toContainText('Saved as a new version.');
  await expect.poll(() => shownVersion(page)).not.toBe(from);
  const version = await shownVersion(page);
  expect(urlVersion(page)).toBe(version);
  return version;
}

async function reviewClaim(page, text, {verdict = 'supported', receipt, action = 'Approve claim', note = 'Checked against the receipt.'} = {}){
  const form = claimRow(page, text).locator('fieldset');
  await form.getByRole('combobox', {name: 'Verdict'}).selectOption(verdict);
  if (receipt) await form.locator(`input[type="checkbox"][value="${receipt}"]`).check();
  await form.getByRole('textbox', {name: 'Note'}).fill(note);
  await form.getByRole('button', {name: action}).click();
}

async function approveClaim(page, text, receipt){
  await reviewClaim(page, text, {receipt});
  await expect(notice(page)).toContainText('Claim review recorded for this version.');
  await expect(claimRow(page, text).locator('.workspace-state')).toContainText('Approved');
}

async function approveLink(page, parent, child){
  await linkRow(page, parent, child).getByRole('button', {name: 'Approve link'}).click();
  await expect(notice(page)).toContainText('Link review recorded for this version.');
  await expect(linkRow(page, parent, child).locator('.workspace-state')).toHaveText('Approved');
}

/* Renders each page of a PDF with the browser's own PDF viewer and saves it. */
async function renderPdfPages(browser, bytes, pages, prefix){
  mkdirSync(NOTES_DIR, {recursive: true});
  const context = await browser.newContext({viewport: {width: 900, height: 1200}});
  const origin = 'http://dossier-journey-pdf.invalid';
  await context.route(`${origin}/**`, (routed) => {
    const path = new URL(routed.request().url()).pathname;
    if (path === '/export.pdf') return routed.fulfill({status: 200, contentType: 'application/pdf', body: bytes});
    return routed.fulfill({status: 200, contentType: 'text/html', body: `<!doctype html><body style="margin:0"><embed src="/export.pdf#page=${new URL(routed.request().url()).searchParams.get('page')}&toolbar=0&navpanes=0&view=Fit" type="application/pdf" style="width:900px;height:1200px"></body>`});
  });
  const page = await context.newPage();
  const written = [];
  for (let number = 1; number <= pages; number += 1){
    await page.goto(`${origin}/view?page=${number}`);
    await page.waitForTimeout(1500);
    const file = join(NOTES_DIR, `${prefix}-page-${number}.png`);
    await page.screenshot({path: file});
    written.push(file);
  }
  await context.close();
  return written;
}

function extractExports(htmlBytes, pdfBytes, artifactRead){
  const directory = join(tmpdir(), `dossier-journey-${randomBytes(6).toString('hex')}`);
  mkdirSync(directory, {recursive: true});
  writeFileSync(join(directory, 'export.html'), htmlBytes);
  writeFileSync(join(directory, 'export.pdf'), pdfBytes);
  writeFileSync(join(directory, 'read.json'), JSON.stringify(artifactRead));
  const output = execFileSync(PYTHON, ['-m', 'tests.journey.dossier_journey_extract', join(directory, 'export.html'), join(directory, 'export.pdf'), join(directory, 'read.json'), DECISION_QUESTION], {cwd: APP_ROOT});
  return JSON.parse(String(output));
}

async function download(page, scope, name){
  const [file] = await Promise.all([page.waitForEvent('download'), scope.getByRole('button', {name, exact: true}).click()]);
  return {name: file.suggestedFilename(), bytes: readFileSync(await file.path())};
}

test.describe.configure({mode: 'serial'});

test('a reviewer completes review, approval and export of one exact version, and a change needs fresh approval', async ({browser}) => {
  test.setTimeout(240000);
  const investigation = await createInvestigation({runId: 'journey_main'});
  const other = await createInvestigation({runId: 'journey_other_scope', clientScopeId: 'bsa_pulse', kind: 'other', label: 'BSA private'});
  const {context, page, log} = await openReviewer(browser);

  await test.step('open the investigation and sign in with the provider button', async () => {
    await page.goto(route(investigation.id));
    await expect(page.locator('[data-review-signin]')).toBeVisible();
    await signIn(page);
    await expect.poll(() => shownVersion(page)).toBe(investigation.version);
    expect(urlVersion(page)).toBe(investigation.version);
  });

  await test.step('inspect observations and interpretations and reveal contradicting evidence', async () => {
    await expect(review(page).getByRole('heading', {name: 'Observations and interpretations'})).toBeVisible();
    for (const text of [TEXTS.obs, TEXTS.int, TEXTS.chal, TEXTS.rec, TEXTS.uncited]) await expect(claimRow(page, text)).toBeVisible();
    await expect(claimRow(page, TEXTS.obs).locator('.workspace-state')).toContainText('Observation');
    await expect(claimRow(page, TEXTS.int).locator('.workspace-state')).toContainText('Interpretation');
    await expect(claimRow(page, TEXTS.lim).locator('.workspace-state')).toContainText('Limitation');
    const interpretation = claimRow(page, TEXTS.int);
    await expect(interpretation.locator('.workspace-challenge')).toHaveCount(0);
    await interpretation.getByRole('button', {name: 'Show contradicting evidence'}).click();
    await expect(interpretation.locator('.workspace-challenge')).toHaveText(`Contradicting evidence: ${receipts.sales}`);
    await expect(claimRow(page, TEXTS.obs).getByRole('button', {name: 'Show contradicting evidence'})).toHaveCount(0);
  });

  let chosen;
  await test.step('a selection with an uncitable finding cannot be prepared', async () => {
    for (const text of [TEXTS.obs, TEXTS.int, TEXTS.uncited]) await claimRow(page, text).getByLabel('Select for the client').check();
    await expect(claimRow(page, TEXTS.lim).getByLabel(/Kept in the selection/)).toBeChecked();
    const uncitedVersion = await saveVersion(page, investigation.version);
    const before = await storage();
    expectFailure(log, 409, '/artifacts/prepare');
    await page.getByRole('button', {name: 'Prepare internal artifact'}).click();
    await expect(notice(page)).toContainText('A selected finding has no usable citation a client could follow, so nothing was prepared.');
    const after = await storage();
    expect(after.names.filter((name) => name.includes('/artifacts/'))).toEqual([]);
    expect(after.uploads).toBe(before.uploads);
    await claimRow(page, TEXTS.uncited).getByLabel('Select for the client').uncheck();
    chosen = await saveVersion(page, uncitedVersion);
  });

  await test.step('review claims and links separately, with verdicts and receipts', async () => {
    await approveClaim(page, TEXTS.obs, receipts.meetups);
    await approveClaim(page, TEXTS.int, receipts.meetups);
    await approveClaim(page, TEXTS.lim, receipts.groups);
    await reviewClaim(page, TEXTS.rec, {verdict: 'unsupported', action: 'Reject claim', note: 'Not for the client.'});
    await expect(notice(page)).toContainText('Claim review recorded for this version.');
    await expect(claimRow(page, TEXTS.rec).locator('.workspace-state')).toContainText('Rejected');
    await approveLink(page, 'clm_obs', 'clm_int');
    await approveLink(page, 'clm_chal', 'clm_int');
    /* Every link in the version is reviewed before it can go to the client,
       including the one to the rejected recommendation. */
    await approveLink(page, 'clm_int', 'clm_rec');
    expect(await shownVersion(page)).toBe(chosen);
  });

  await test.step('a failed PDF is said in plain words and seals nothing', async () => {
    await control('/__journey/renderer', {refusal: 'pdf_browser_unavailable'});
    expectFailure(log, 503, '/artifacts/prepare');
    await page.getByRole('button', {name: 'Prepare internal artifact'}).click();
    await expect(notice(page)).toContainText('The export could not be created, so nothing was prepared.');
    await expect(review(page)).not.toContainText('pdf_');
    expect((await storage()).names.filter((name) => name.includes('/artifacts/'))).toEqual([]);
    await control('/__journey/renderer', {refusal: null});
  });

  let artifact;
  let approvedRead;
  await test.step('prepare, inspect the exported text and approve that exact version', async () => {
    await expect(review(page).locator('.workspace-lead .workspace-state')).toHaveText('Ready for client approval');
    await page.getByRole('button', {name: 'Prepare internal artifact'}).click();
    await expect(notice(page)).toContainText('Internal artifact prepared.');
    const card = review(page).locator('[data-artifacts="current"] article');
    await expect(card).toHaveCount(1);
    await expect(card.locator('.workspace-state')).toHaveText('Needs approval');
    await expect(card.getByRole('button', {name: 'Approve this exact version'})).toBeDisabled();
    const [inspected] = await Promise.all([
      page.waitForResponse((response) => /\/artifacts\/art_[0-9a-f]{16}\/read/.test(response.url())),
      card.getByRole('button', {name: 'Inspect exported text'}).click(),
    ]);
    const pending = await inspected.json();
    expect(pending.state).toBe('pending_review');
    expect('pdf' in pending).toBe(false);
    artifact = {id: pending.artifact_id, version: pending.artifact_version, dossierVersion: chosen};
    const exported = card.locator('pre[data-export="html"]');
    await expect(exported).toContainText(TEXTS.obs);
    await expect(exported).toContainText(TEXTS.int);
    for (const text of [TEXTS.rec, TEXTS.chal, TEXTS.uncited, INTERNAL_QUESTION]) await expect(exported).not.toContainText(text);
    expect(await exported.textContent()).toBe(pending.html);
    const [approval] = await Promise.all([
      page.waitForResponse((response) => response.url().endsWith('/dossier/review') && response.request().method() === 'POST'),
      card.getByRole('button', {name: 'Approve this exact version'}).click(),
    ]);
    expect(approval.status()).toBe(200);
    expect(approval.request().postDataJSON()).toMatchObject({resource: 'artifact', resource_id: artifact.id, resource_version: artifact.version, dossier_version: chosen, action: 'approve'});
    await expect(notice(page)).toContainText('This exact version is approved for the client.');
    await expect(card.locator('.workspace-state')).toHaveText('Approved');
  });

  await test.step('open Client Read and download HTML and PDF through the page', async () => {
    log.clientReadOpen = true;
    const card = review(page).locator('[data-artifacts="current"] article');
    const [read] = await Promise.all([
      page.waitForResponse((response) => response.url().includes(`/artifacts/${artifact.id}/read?artifact_version=${artifact.version}`)),
      card.getByRole('button', {name: 'Open Client Read'}).click(),
    ]);
    approvedRead = await read.json();
    expect(approvedRead.state).toBe('approved');
    const clientRead = page.locator('section.dossier-client-read');
    await expect(clientRead).toHaveAttribute('data-artifact-version', artifact.version);
    expect(page.url()).toContain(`artifact=${artifact.id}`);
    expect(page.url()).toContain(`artifact_version=${artifact.version}`);
    const html = await download(page, clientRead, 'Download HTML');
    const pdf = await download(page, clientRead, 'Download PDF');
    expect(html.name).toBe(`${artifact.id}.html`);
    expect(pdf.name).toBe(`${artifact.id}.pdf`);
    expect(html.bytes.toString('utf8')).toBe(approvedRead.html);
    /* Self-contained: no script and nothing to fetch but the embedded face. */
    expect(approvedRead.html).not.toMatch(/<script/i);
    expect([...approvedRead.html.matchAll(/url\('([^']*)'\)/g)].map((match) => match[1].slice(0, 23)))
      .toEqual(['data:font/woff2;base64,', 'newsreader-variable-tcB']);
    expect(pdf.bytes.equals(Buffer.from(approvedRead.pdf, 'base64'))).toBe(true);

    const extracted = extractExports(html.bytes, pdf.bytes, approvedRead);
    expect(extracted.html_runs).toEqual(extracted.allowed);
    expect(extracted.pdf_text).toBe(extracted.allowed.join(' '));
    const withheld = [TEXTS.rec, TEXTS.chal, TEXTS.uncited, INTERNAL_QUESTION, receipts.sales, receipts.accra,
      'Awaiting review', 'Internal only', 'Rejected', 'pending', 'rejected', 'internal', 'clm_', 'human:',
      investigation.id, 'Readiness', 'approval_required', 'client_ready', '2026-08-'];
    expect(extracted.allowed[1]).toBe(DECISION_QUESTION);
    expect(extracted.allowed).toContain('20 Aug 2026');
    for (const text of withheld){
      expect(extracted.html_runs.join('\n')).not.toContain(text);
      expect(extracted.pdf_text).not.toContain(text);
    }
    for (const text of [TEXTS.obs, TEXTS.int, TEXTS.lim]) expect(extracted.allowed).toContain(text);
    const metadata = JSON.stringify([extracted.pdf_info, extracted.html_meta, extracted.html_title]).toLowerCase();
    for (const name of extracted.tool_names) expect(metadata).not.toContain(name);
    expect(extracted.pdf_info).toEqual({});
    expect(extracted.pdf_xmp).toBe(false);
    expect(extracted.html_meta.filter((meta) => String(meta.name || '').toLowerCase() === 'generator')).toEqual([]);
    const images = await renderPdfPages(browser, pdf.bytes, extracted.pdf_pages, 'dossier-journey-pdf');
    expect(images).toHaveLength(extracted.pdf_pages);
    test.info().annotations.push({type: 'pdf-pages', description: `${extracted.pdf_pages} page(s): ${images.join(', ')}`});
  });

  let changed;
  await test.step('changing one selected claim makes a new version that needs fresh approval', async () => {
    await claimRow(page, TEXTS.int).getByLabel('Select for the client').uncheck();
    changed = await saveVersion(page, chosen);
    expect(changed).not.toBe(chosen);
    await expect(review(page).locator('.workspace-lead .workspace-state')).toHaveText('Not client ready');
    await expect(claimRow(page, TEXTS.obs).locator('.workspace-state')).toContainText('Awaiting review');
    await expect(review(page).locator('[data-artifacts="current"]')).toContainText('This version needs approval');
    const historical = review(page).locator('[data-artifacts="historical"]');
    await expect(historical).toContainText('Approved for an earlier version of this dossier.');
    await expect(historical).toContainText(artifact.version);
    /* The old exact artifact stays readable at its own version. */
    await historical.getByRole('button', {name: 'Open the earlier Client Read'}).click();
    const clientRead = page.locator('section.dossier-client-read');
    await expect(clientRead).toHaveAttribute('data-artifact-version', artifact.version);
    const exported = await fetch(`${base}/api/v2/investigations/${investigation.id}/artifacts/${artifact.id}/export.html?artifact_version=${artifact.version}`, {method: 'POST'});
    expect(exported.status).toBe(200);
    expect(await exported.text()).toBe(approvedRead.html);
    /* The new version has no approved artifact: preparing one leaves it pending. */
    await page.getByRole('button', {name: 'Prepare internal artifact'}).click();
    await expect(notice(page)).toContainText('Internal artifact prepared.');
    const fresh = review(page).locator('[data-artifacts="current"] article');
    await expect(fresh.locator('.workspace-state')).toHaveText('Needs approval');
    await expect(fresh.getByRole('button', {name: 'Approve this exact version'})).toBeDisabled();
  });

  await test.step('reload and back or forward reopen the same version', async () => {
    const hash = new URL(page.url()).hash;
    expect(hash).toContain(`version=${changed}`);
    await page.reload();
    await signIn(page);
    await expect.poll(() => shownVersion(page)).toBe(changed);
    expect(new URL(page.url()).hash).toBe(hash);
    /* History holds: the approved version with its Client Read, the changed
       version, then the changed version with the earlier Client Read open. */
    await page.goBack();
    await expect.poll(() => urlVersion(page)).toBe(changed);
    await expect(page.locator('section.dossier-client-read')).toHaveCount(0);
    await page.goBack();
    await expect.poll(() => urlVersion(page)).toBe(chosen);
    await expect.poll(() => shownVersion(page)).toBe(chosen);
    await expect(page.locator('section.dossier-client-read')).toHaveAttribute('data-artifact-version', artifact.version);
    await expect(review(page).locator('[data-artifacts="current"] article .workspace-state')).toHaveText('Approved');
    await page.goForward();
    await page.goForward();
    await expect.poll(() => new URL(page.url()).hash).toBe(hash);
    await expect.poll(() => shownVersion(page)).toBe(changed);
    await expect(page.locator('section.dossier-client-read')).toHaveAttribute('data-artifact-version', artifact.version);
  });

  await test.step('another scope\'s investigation and this artifact under it reveal nothing', async () => {
    log.clientReadOpen = false;
    const stored = await storage();
    expectFailure(log, 404, `/api/internal/v2/investigations/${other.id}/dossier/working`);
    /* One read of the artifact version, shared by the Evidence Room and the
       Client Read panel. */
    expectFailure(log, 404, `/api/v2/investigations/${other.id}/artifacts/${artifact.id}/read`);
    for (const part of ['/status', '/claims/read', '/decision/read']) expectFailure(log, 404, `/api/v2/investigations/${other.id}${part}`);
    await page.goto(route(other.id, `&artifact=${artifact.id}&artifact_version=${artifact.version}`));
    /* One failure, one alert: the Evidence Room says the investigation is
       unavailable, and the Client Read panel adds nothing of its own. The
       dossier review is a separate read, and it says so once in its own words. */
    const alerts = page.getByRole('alert');
    await expect(alerts.filter({hasText: 'The investigation is unavailable.'})).toHaveCount(1);
    await expect(page.locator('section.dossier-client-read')).toHaveCount(0);
    await expect(page.getByText('This artifact version is not available to you.')).toHaveCount(0);
    await expect(page.getByRole('heading', {name: 'Client Read'})).toHaveCount(0);
    await expect(page.getByText('This investigation or version is not available to you.')).toBeVisible();
    await expect(alerts).toHaveCount(2);
    const body = await page.locator('body').innerText();
    for (const text of ['BSA private', TEXTS.obs, TEXTS.int, investigation.id, artifact.version]) expect(body).not.toContain(text);
    /* Nothing from either investigation reaches the page, in its text or its
       markup: no artifact bytes, no claim and no receipt. */
    const markup = await page.content();
    await expect(page.locator('iframe')).toHaveCount(0);
    for (const text of [approvedRead.html, 'data:font/woff2', ...Object.values(TEXTS), 'taxi rank traders', ...Object.values(receipts)]){
      expect(body).not.toContain(text);
      expect(markup).not.toContain(text);
    }
    if (approvedRead.pdf) expect(markup).not.toContain(approvedRead.pdf.slice(0, 64));
    expect((await storage()).uploads).toBe(stored.uploads);
  });

  expect((await storage()).overwrites).toEqual([]);
  verifyClean(log);
  await context.close();
});

test('a stale decision is refused in plain words and the latest version is reloaded; concurrent reviews give one success and one conflict', async ({browser}) => {
  test.setTimeout(120000);
  const investigation = await createInvestigation({runId: 'journey_stale'});
  const first = await openReviewer(browser);
  const second = await openReviewer(browser);
  await openSignedIn(first.page, investigation.id);
  await claimRow(first.page, TEXTS.obs).getByLabel('Select for the client').check();
  const selected = await saveVersion(first.page, investigation.version);

  await openSignedIn(second.page, investigation.id, `&version=${selected}`);
  expect(await shownVersion(second.page)).toBe(selected);

  /* Concurrent: the same claim, approved in one window and rejected in the
     other at the same moment. One is recorded, the other is a conflict. */
  const approveForm = claimRow(first.page, TEXTS.obs).locator('fieldset');
  await approveForm.getByRole('combobox', {name: 'Verdict'}).selectOption('supported');
  await approveForm.locator(`input[type="checkbox"][value="${receipts.meetups}"]`).check();
  await approveForm.getByRole('textbox', {name: 'Note'}).fill('Checked.');
  const rejectForm = claimRow(second.page, TEXTS.obs).locator('fieldset');
  await rejectForm.getByRole('combobox', {name: 'Verdict'}).selectOption('unsupported');
  await rejectForm.getByRole('textbox', {name: 'Note'}).fill('Not enough.');
  const statuses = new Map([[first, []], [second, []]]);
  for (const window of [first, second]) window.page.on('response', (response) => { if (response.url().endsWith('/dossier/review')) statuses.get(window).push(response.status()); });
  await Promise.all([
    approveForm.getByRole('button', {name: 'Approve claim'}).click(),
    rejectForm.getByRole('button', {name: 'Reject claim'}).click(),
  ]);
  await expect.poll(() => statuses.get(first).length + statuses.get(second).length).toBe(2);
  expect([...statuses.get(first), ...statuses.get(second)].sort()).toEqual([200, 409]);
  const conflictWords = 'The dossier changed after you opened it, so an earlier approval no longer applies.';
  const loser = statuses.get(first)[0] === 409 ? first : second;
  const winner = loser === first ? second : first;
  expectFailure(loser.log, 409, '/dossier/review');
  await expect(notice(loser.page)).toContainText(conflictWords);
  await expect(notice(winner.page)).toContainText('Claim review recorded for this version.');

  /* Stale: the first window saves a new version; the second, still showing
     the old one, sends a decision against it. */
  await claimRow(first.page, TEXTS.int).getByLabel('Select for the client').check();
  const moved = await saveVersion(first.page, selected);
  expect(await shownVersion(second.page)).toBe(selected);
  expectFailure(second.log, 409, '/dossier/review');
  await linkRow(second.page, 'clm_obs', 'clm_int').getByRole('button', {name: 'Approve link'}).click();
  await expect(notice(second.page)).toContainText(conflictWords);
  await expect(notice(second.page).getByRole('group').or(notice(second.page).locator('details'))).toContainText('review_version_conflict');
  await expect.poll(() => shownVersion(second.page)).toBe(moved);
  await expect.poll(() => urlVersion(second.page)).toBe(moved);

  expect((await storage()).overwrites).toEqual([]);
  verifyClean(first.log);
  verifyClean(second.log);
  await first.context.close();
  await second.context.close();
});

test('a late answer for the previous investigation cannot overwrite the current panel', async ({browser}) => {
  test.setTimeout(90000);
  const earlier = await createInvestigation({runId: 'journey_late_a', kind: 'other', label: 'Earlier investigation'});
  const current = await createInvestigation({runId: 'journey_late_b', kind: 'other', label: 'Current investigation'});
  const {context, page, log} = await openReviewer(browser);
  await openSignedIn(page, current.id);
  await expect(review(page)).toContainText('Current investigation: taxi rank traders');
  await control('/__journey/hold', {contains: `/investigations/${earlier.id}/dossier/working`, seconds: 3});
  log.expectedAborts.push(`/investigations/${earlier.id}/dossier/working`);
  await page.evaluate((id) => { location.hash = `#/console?work=brief&investigation=${id}`; }, earlier.id);
  await expect(review(page)).toHaveAttribute('aria-busy', 'true');
  await page.evaluate((id) => { location.hash = `#/console?work=brief&investigation=${id}`; }, current.id);
  await expect.poll(() => shownVersion(page)).toBe(current.version);
  await page.waitForTimeout(4000);
  expect(await shownVersion(page)).toBe(current.version);
  await expect(review(page)).toContainText('Current investigation: taxi rank traders');
  await expect(page.locator('body')).not.toContainText('Earlier investigation');
  verifyClean(log);
  await context.close();
});

/* The Brand South Africa Pulse lens on an investigation, from creation to the
   approved export. The server reads the investigation under the bsa_pulse
   scope, the one the lens registry authorizes the lens for. */
const LENS = 'bsa_pulse_lens';
const LENS_ENVELOPE = {lens_binding_version: 'client_lens_binding_v1', client_lens_id: LENS, configuration_digest: 'e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf'};
const LENS_LINE = 'Client lens: Brand South Africa Pulse';
const LENS_CONFIGURATION = `Client lens configuration: ${LENS}, ${LENS_ENVELOPE.configuration_digest}`;

/* The canonical digest the app gives a scope: sorted keys, compact, UTF-8. */
function canonical(value){
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map((name) => `${JSON.stringify(name)}:${canonical(value[name])}`).join(',')}}`;
  return JSON.stringify(value);
}
function scopeDigest(response, lens){
  const scope = {};
  for (const name of ['client_scope_id', 'market_scope', 'brand_config_id', 'audience_lens_ids', 'theme_id', 'run_id', 'contract_version']) scope[name] = response[name];
  return hex(canonical(lens ? {...scope, client_lens: lens} : scope));
}

test('a BSA lensed investigation keeps its lens through review, approval and export, and another scope is refused', async ({browser}) => {
  test.setTimeout(240000);

  await test.step('the BSA lens under another client scope is refused, and nothing is made in its place', async () => {
    const before = await storage();
    const crossed = await postInvestigation({runId: 'journey_lens', clientScopeId: 'ogilvy_default', clientLensId: LENS});
    expect(crossed.status).toBe(404);
    expect((await crossed.json()).detail.code).toBe('scope_invalid');
    const after = await storage();
    expect(after.uploads).toBe(before.uploads);
    expect(after.names).toEqual(before.names);
  });

  const investigation = await createInvestigation({runId: 'journey_lens', clientScopeId: 'bsa_pulse', clientLensId: LENS});
  const away = await createInvestigation({runId: 'journey_lens_away', kind: 'other', label: 'Default scope'});
  expect(investigation.response.client_lens).toEqual(LENS_ENVELOPE);
  const twin = await postInvestigation({runId: 'journey_lens', clientScopeId: 'bsa_pulse'});
  expect(twin.status).toBe(200);
  const general = await twin.json();
  expect('client_lens' in general).toBe(false);
  expect(general.investigation_id).not.toBe(investigation.id);
  const lensedDigest = scopeDigest(investigation.response, LENS_ENVELOPE);
  expect(lensedDigest).not.toBe(scopeDigest(general));

  await control('/__journey/scope', {client_scope_id: 'bsa_pulse'});
  const {context, page, log} = await openReviewer(browser);
  let artifact;
  let approvedRead;
  try {
    let chosen;
    await test.step('review the lensed investigation under its own scope', async () => {
      await openSignedIn(page, investigation.id);
      await expect.poll(() => shownVersion(page)).toBe(investigation.version);
      for (const text of [TEXTS.obs, TEXTS.int]) await claimRow(page, text).getByLabel('Select for the client').check();
      chosen = await saveVersion(page, investigation.version);
      await approveClaim(page, TEXTS.obs, receipts.meetups);
      await approveClaim(page, TEXTS.int, receipts.meetups);
      await approveClaim(page, TEXTS.lim, receipts.groups);
      await reviewClaim(page, TEXTS.rec, {verdict: 'unsupported', action: 'Reject claim', note: 'Not for the client.'});
      await expect(claimRow(page, TEXTS.rec).locator('.workspace-state')).toContainText('Rejected');
      await approveLink(page, 'clm_obs', 'clm_int');
      await approveLink(page, 'clm_chal', 'clm_int');
      await approveLink(page, 'clm_int', 'clm_rec');
      await expect(review(page).locator('.workspace-lead .workspace-state')).toHaveText('Ready for client approval');
    });

    await test.step('prepare and approve the artifact bound to the lens', async () => {
      await page.getByRole('button', {name: 'Prepare internal artifact'}).click();
      await expect(notice(page)).toContainText('Internal artifact prepared.');
      const card = review(page).locator('[data-artifacts="current"] article');
      const [inspected] = await Promise.all([
        page.waitForResponse((response) => /\/artifacts\/art_[0-9a-f]{16}\/read/.test(response.url())),
        card.getByRole('button', {name: 'Inspect exported text'}).click(),
      ]);
      const pending = await inspected.json();
      expect(pending.state).toBe('pending_review');
      expect(pending.dossier_version).toBe(chosen);
      expect(pending.manifest.scope_digest).toBe(lensedDigest);
      artifact = {id: pending.artifact_id, version: pending.artifact_version};
      const exported = card.locator('pre[data-export="html"]');
      await expect(exported).toContainText(LENS_LINE);
      await expect(exported).toContainText(LENS_CONFIGURATION);
      await expect(exported).not.toContainText('general 42');
      await card.getByRole('button', {name: 'Approve this exact version'}).click();
      await expect(notice(page)).toContainText('This exact version is approved for the client.');
      await expect(card.locator('.workspace-state')).toHaveText('Approved');
    });

    await test.step('the approved Client Read, HTML and PDF name the lens', async () => {
      log.clientReadOpen = true;
      const card = review(page).locator('[data-artifacts="current"] article');
      const [read] = await Promise.all([
        page.waitForResponse((response) => response.url().includes(`/artifacts/${artifact.id}/read?artifact_version=${artifact.version}`)),
        card.getByRole('button', {name: 'Open Client Read'}).click(),
      ]);
      approvedRead = await read.json();
      expect(approvedRead.state).toBe('approved');
      expect(approvedRead.manifest.scope_digest).toBe(lensedDigest);
      const clientRead = page.locator('section.dossier-client-read');
      await expect(clientRead).toHaveAttribute('data-artifact-version', artifact.version);
      const html = await download(page, clientRead, 'Download HTML');
      const pdf = await download(page, clientRead, 'Download PDF');
      expect(html.bytes.toString('utf8')).toBe(approvedRead.html);
      expect(pdf.bytes.equals(Buffer.from(approvedRead.pdf, 'base64'))).toBe(true);
      const extracted = extractExports(html.bytes, pdf.bytes, approvedRead);
      /* The allowed payload lines, with the lens under the title and its
         configuration last, and nothing else. */
      expect(extracted.html_runs.slice(0, 3)).toEqual(['Client Read', DECISION_QUESTION, LENS_LINE]);
      expect(extracted.html_runs.at(-1)).toBe(LENS_CONFIGURATION);
      expect(extracted.html_runs.filter((line) => line !== LENS_LINE && line !== LENS_CONFIGURATION)).toEqual(extracted.allowed);
      expect(extracted.pdf_text).toBe(extracted.html_runs.join(' '));
      for (const text of [TEXTS.rec, TEXTS.chal, 'Client lens: None', 'general 42']){
        expect(extracted.html_runs.join('\n')).not.toContain(text);
        expect(extracted.pdf_text).not.toContain(text);
      }
    });

    await test.step('reload keeps the lensed version and its approved export', async () => {
      await page.reload();
      await signIn(page);
      await expect.poll(() => shownVersion(page)).toBe(chosen);
      await expect(page.locator('section.dossier-client-read')).toHaveAttribute('data-artifact-version', artifact.version);
      const exported = await fetch(`${base}/api/v2/investigations/${investigation.id}/artifacts/${artifact.id}/export.html?artifact_version=${artifact.version}`, {method: 'POST'});
      expect(exported.status).toBe(200);
      expect(await exported.text()).toBe(approvedRead.html);
    });

    await test.step('under another client scope the lensed investigation and its export reveal nothing', async () => {
      await control('/__journey/scope', {client_scope_id: 'ogilvy_default'});
      log.clientReadOpen = false;
      const stored = await storage();
      for (const extension of ['html', 'pdf']){
        const refused = await fetch(`${base}/api/v2/investigations/${investigation.id}/artifacts/${artifact.id}/export.${extension}?artifact_version=${artifact.version}`, {method: 'POST'});
        expect(refused.status).toBe(404);
        const text = await refused.text();
        for (const secret of [LENS, 'Brand South Africa Pulse', TEXTS.obs]) expect(text).not.toContain(secret);
      }
      expectFailure(log, 404, `/api/internal/v2/investigations/${investigation.id}/dossier/working`);
      expectFailure(log, 404, `/api/v2/investigations/${investigation.id}/artifacts/${artifact.id}/read`);
      for (const part of ['/status', '/claims/read', '/decision/read']) expectFailure(log, 404, `/api/v2/investigations/${investigation.id}${part}`);
      /* Away to a default scope investigation and back, so the page reads the
         lensed address again under the moved scope. */
      await page.goto(route(away.id));
      await expect.poll(() => shownVersion(page)).toBe(away.version);
      await page.goto(route(investigation.id, `&artifact=${artifact.id}&artifact_version=${artifact.version}`));
      const alerts = page.getByRole('alert');
      await expect(alerts.filter({hasText: 'The investigation is unavailable.'})).toHaveCount(1);
      await expect(page.getByText('This investigation or version is not available to you.')).toBeVisible();
      await expect(page.locator('section.dossier-client-read')).toHaveCount(0);
      await expect(page.locator('iframe')).toHaveCount(0);
      const markup = await page.content();
      for (const text of [LENS_LINE, 'Brand South Africa Pulse', LENS_ENVELOPE.configuration_digest, TEXTS.obs, TEXTS.int, approvedRead.html]) expect(markup).not.toContain(text);
      expect((await storage()).uploads).toBe(stored.uploads);
    });
  } finally {
    await control('/__journey/scope', {client_scope_id: null});
  }

  expect((await storage()).overwrites).toEqual([]);
  verifyClean(log);
  await context.close();
});
