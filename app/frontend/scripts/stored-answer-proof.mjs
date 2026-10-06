/* Stored answer browser proof: the U01 staging proof, pointed at 42's own
   Ask records. It makes no paid submission and starts no question.

   It reads answers that already exist, only through the app's own pages:
   each stored Ask opened from its address `#/ask?follow=<ask id>` (the
   History page's Read answer link, history42.jsx:293) and reloaded, a
   follow-up answer (one with a parent), source inspection from a source chip
   by keyboard, and the HTML export. Ask's follow effect reads a finished
   record with GET /api/ask/<ask id> and asks nothing (ask42.jsx:570-603); it
   reads the event stream only while the record still reads as running
   (ask42.jsx:582-588).

   Before the first navigation a request guard is installed on the browser
   context. On the app's own origin every request to /api/ or /internal/ must
   be on the read only allowlist below (ALLOWED_READ_ENDPOINTS for GET and
   HEAD, ALLOWED_NON_GET for the one POST the gate needs); anything else is
   aborted inside the browser before it leaves. That covers POST /api/ask,
   POST /api/ask/<id>/stop, POST /api/spikes, every write under
   /api/investigations and every other request with a body. GETs of the
   built app's own files, and GETs to other origins (post stills), go through.
   A refused request stops the run, is written to the report and exits 2. The
   guard never fulfils a request and never rewrites a request or a response.
   On top of the guard, the script refuses to type into, focus for a key
   press, or click anything inside the question form (`Your question`,
   ask42.jsx:683-711), the suggested `Ask next` questions (each asks at once,
   ask42.jsx:411-418), the skill forms under Ask (skills42.jsx:43) or the
   starters, and any control that asks, stops, retries or writes (`Ask`,
   `Stop`, `Try again`, `Ask again`, `Ask a follow-up`, `Add to dossier`,
   `Save checked claims to Findings`).

   Read only requests the page makes (f42-api, core/api/app.py), listed in
   the report as guard.allowed_read_endpoints and guard.allowed_non_get:
     GET  /api/health                     core/api/app.py:146   (App.jsx:683)
     POST /api/auth/verify                core/api/app.py:223   (App.jsx:862)
     GET  /api/ask/<ask id>               core/api/app.py:831   (askTransport42.js:38)
     GET  /api/ask/<ask id>/events        core/api/app.py:894   (askTransport42.js:122)
     GET  /api/ask/<ask id>/export        core/api/app.py:907   (askTransport42.js:48)

   An ask id is what f42-api accepts (ASK_ID_RE, core/api/app.py:34): 1 to
   128 letters, digits, `_` or `-`. f42-agent issues `r_<date>_<time>_<hash>_
   <hex>` ids (core/agent/ask.py:858) and `sched-...` ids for scheduled runs
   (core/agent/ask.py:138); neither is a uuid.

   Inputs are named environment fields:
     STAGING_URL             the app URL of the serving revision.
     ACCESS_KEY_FILE         a file holding the access key on one line. The key
                             is typed into the gate field Access key and
                             nowhere else. It is never printed, logged, written
                             to the report or used in a file name, and no
                             screenshot is taken while the gate is on screen.
     STORED_ASK_IDS          ask ids of stored first answers, separated by
                             spaces or commas.
     FOLLOW_UP_ASK_IDS       ask ids of stored answers that have a parent.
     PROOF_OUT_DIR           a directory for report.json, screenshots, the
                             downloaded copies and the visible page text.
     CHROME_PATH             optional browser executable. When unset the
                             script tries the usual Chrome and Chromium paths,
                             then the browser bundled with the test runner. If
                             none is installed, run
                             `npx playwright install chromium` in app/frontend.
   The older STORED_REQUEST_IDS and FOLLOW_UP_REQUEST_IDS named desk request
   ids, which 42 does not store; the script refuses them and says so.

   Checks of the desk version that 42 has no counterpart for are not run and
   not marked NOT SEEN: the report lists them under not_applicable, each with
   the reason (NOT_APPLICABLE_IN_42 below).

   Exit codes: 0 every check SEEN; 1 at least one check NOT SEEN; 2 a request
   or an action refused by the guard (the report names it); 3 the run could
   not start (the browser, its context or the first page failed; the report
   names the error) or the gate refused the key.

   Tier 1 native command block, for the owner's machine. Take each ask id
   from the History page (Asks tab): the Read answer link of a row opens
   `#/ask?follow=<ask id>`. A follow-up is an answer asked from `Ask a
   follow-up` under another answer.

     cd app/frontend
     bun install --frozen-lockfile
     export STAGING_URL='https://<staging app URL of the serving revision>'
     export STORED_ASK_IDS='<ask id of a stored first answer>'
     export FOLLOW_UP_ASK_IDS='<ask id of a stored follow-up answer>'
     export ACCESS_KEY_FILE="$HOME/.config/42/staging-access-key"
     export PROOF_OUT_DIR="$HOME/42-proofs/u01-stored-$(date +%Y%m%d-%H%M%S)"
     node scripts/stored-answer-proof.mjs; echo "exit $?"

   The key file holds only the key (chmod 600). The report is
   $PROOF_OUT_DIR/report.json. */
import {existsSync, mkdirSync, readFileSync, statSync, writeFileSync} from 'node:fs';
import {isAbsolute, join, resolve} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

export const ASK_ID = /^[A-Za-z0-9_-]{1,128}$/; // core/api/app.py:34; ask42.jsx:543
const REDACTED = '[redacted]';
const INSTALL_HINT = 'If no Chrome or Chromium is installed, run `npx playwright install chromium` in app/frontend, or set CHROME_PATH, and run the proof again.';
const ID_PART = '[A-Za-z0-9_-]{1,128}';

/* The only request with a method other than GET or HEAD the guard lets
   through. It checks the key and creates or charges nothing. */
export const ALLOWED_NON_GET = Object.freeze([
  {method: 'POST', path: '/api/auth/verify', where: 'core/api/app.py:223', why: 'gate check (App.jsx:862-868; App.jsx:696 for the read only sign-in mode)'},
]);

/* The only API reads the guard lets through: what the 42 shell and Ask's
   stored view fetch. Each reads, none starts or charges a question. */
export const ALLOWED_READ_ENDPOINTS = Object.freeze([
  {method: 'GET', path: '/api/health', pattern: /^\/api\/health$/, where: 'core/api/app.py:146', why: 'health and gate state on load (App.jsx:683)'},
  {method: 'GET', path: '/api/ask/<ask id>', pattern: new RegExp('^/api/ask/' + ID_PART + '$'), where: 'core/api/app.py:831', why: 'the stored Ask record (askTransport42.js:38, ask42.jsx:576, :586)'},
  {method: 'GET', path: '/api/ask/<ask id>/events', pattern: new RegExp('^/api/ask/' + ID_PART + '/events$'), where: 'core/api/app.py:894', why: 'the event log, read only while a record reads as running; a finished one replays its stored steps (askTransport42.js:122, ask42.jsx:583)'},
  {method: 'GET', path: '/api/ask/<ask id>/export', pattern: new RegExp('^/api/ask/' + ID_PART + '/export$'), where: 'core/api/app.py:907', why: 'the HTML export of the stored record (askTransport42.js:48, ask42.jsx:264)'},
]);

/* Routes that start, charge, stop or write, named so a refusal says which
   one was tried. The guard refuses every other API request outside the
   allowlist as well; this list only labels the refusal. `paid` marks the
   ones that start a question or spend credits. The GET entries name reads
   of the retired desk API that ran a billed search there; f42-api serves
   none of them (its catch-all answers `No such API route.`, or the app shell
   for /card), and they stay refused, HEAD too, in case an older build is
   served. */
export const GUARDED_ENDPOINTS = Object.freeze([
  {method: 'POST', pattern: /^\/api\/ask\/?$/, where: 'core/api/app.py:741', paid: true, why: 'starts an Ask and holds one of the day\'s questions (core/api/app.py:753-768)'},
  {method: 'POST', pattern: new RegExp('^/api/ask/' + ID_PART + '/stop$'), where: 'core/api/app.py:901', why: 'stops a running Ask'},
  {method: 'POST', pattern: /^\/api\/spikes\/?$/, where: 'core/api/app.py:787', paid: true, why: 'starts an Ask about a spike'},
  {method: 'POST', pattern: /^\/api\/investigations\/?$/, where: 'core/api/app.py:1003', why: 'creates an investigation'},
  {method: 'PUT', pattern: /^\/api\/investigations\/[^/]+\/plan$/, where: 'core/api/app.py:1020', why: 'changes an investigation plan'},
  {method: 'POST', pattern: /^\/api\/investigations\/[^/]+\/start$/, where: 'core/api/app.py:1028', paid: true, why: 'starts an investigation, which spends SocialCrawl credits'},
  {method: 'POST', pattern: /^\/api\/investigations\/[^/]+\/stop$/, where: 'core/api/app.py:1050', why: 'stops an investigation'},
  {method: 'POST', pattern: /^\/api\/skins\/[^/]+\/report$/, where: 'core/api/app.py:709', paid: true, why: 'starts a skin report investigation'},
  {method: 'POST', pattern: /^\/api\/dossiers\/?$/, where: 'core/api/app.py:945', why: 'starts a dossier from an answer (Add to dossier, ask42.jsx:278)'},
  {method: 'POST', pattern: /^\/api\/findings\/?$/, where: 'core/api/app.py:951', why: 'saves checked claims to Findings (ask42.jsx:292)'},
  {method: 'POST', pattern: /^\/api\/feedback\/?$/, where: 'core/api/app.py:721', why: 'writes feedback'},
  {method: 'POST', pattern: /^\/api\/watches(\/|$)/, where: 'core/api/app.py:575', why: 'writes a watch'},
  {method: 'POST', pattern: /^\/api\/schedules(\/|$)/, where: 'core/api/app.py:605', why: 'writes a schedule'},
  {method: 'POST', pattern: /^\/api\/suppressions\/?$/, where: 'core/api/app.py:462', why: 'hides a person'},
  {method: 'GET', pattern: /^\/api\/ask\/?$/, where: 'core/api/app.py:1060', why: 'the retired desk API ran a billed search here; f42-api serves no GET /api/ask and a stored answer never needs it'},
  {method: 'GET', pattern: /^\/api\/listen\/?$/, where: 'core/api/app.py:1060', why: 'the retired desk API ran campaign listening here, a billed search on a cache miss; f42-api serves no such route'},
  {method: 'GET', pattern: /^\/api\/lexicon\//, where: 'core/api/app.py:1060', why: 'the retired desk API ran a query per slang term here; f42-api serves only GET /api/lexicon (core/api/app.py:538), which a stored answer never needs'},
  {method: 'GET', pattern: /^\/api\/prewarm\/?$/, where: 'core/api/app.py:1060', why: 'the retired desk API rebuilt every regional payload here; f42-api serves no such route'},
  {method: 'GET', pattern: /^\/card(\/|$)/, where: 'core/api/app.py:1087', why: 'the retired desk card page ran a billed search; f42-api serves only the app shell here and nothing on Ask needs it'},
]);

/* The path as the server routes it. The browser keeps percent encodings in
   the request address and the server unquotes before routing, so /%63ard/x
   is /card/x there. Decoded until it stops changing, then repeated slashes
   collapsed to one so //api/ask matches /api/ask; null when it cannot be
   decoded, which the guard refuses. */
export function routedPath(pathname){
  let path = String(pathname);
  for (let round = 0; round < 8; round += 1){
    let next;
    try { next = decodeURIComponent(path); } catch { return null; }
    if (next === path) return path.replace(/\/{2,}/g, '/');
    path = next;
  }
  return null;
}

const isApiPath = path => /^\/(api|internal)(\/|$)/.test(path);

/* The guard's decision for one request. Anything but allow is a refusal.
   Named endpoints and the allowlist are matched on the app origin only; the
   guard assumes the app calls its own API with relative addresses. */
export function guardDecision(method, url, appOrigin){
  const verb = String(method || '').toUpperCase();
  let parsed;
  try { parsed = new URL(url); } catch { return {allow: false, method: verb, path: String(url).slice(0, 200), endpoint: 'unparsable request address', where: null}; }
  const sameOrigin = parsed.origin === appOrigin;
  const path = parsed.pathname;
  const routed = routedPath(path);
  if (routed === null) return {allow: false, method: verb, path, origin: sameOrigin ? 'app' : parsed.origin, endpoint: 'request path that cannot be decoded', where: null};
  /* A named GET is refused for HEAD as well, as a precaution. */
  const named = GUARDED_ENDPOINTS.find(item => (item.method === verb || (verb === 'HEAD' && item.method === 'GET')) && item.pattern.test(routed));
  if (named && sameOrigin) return {allow: false, method: verb, path, endpoint: named.why, where: named.where, paid: Boolean(named.paid)};
  const read = verb === 'GET' || verb === 'HEAD';
  if (sameOrigin && isApiPath(routed)){
    if (read && ALLOWED_READ_ENDPOINTS.some(item => item.pattern.test(routed))) return {allow: true};
    if (ALLOWED_NON_GET.some(item => item.method === verb && item.path === routed)) return {allow: true};
    return {allow: false, method: verb, path, origin: 'app', endpoint: read ? 'API read outside the read only allowlist' : 'request with a body outside the read only allowlist', where: null};
  }
  if (read) return {allow: true};
  return {allow: false, method: verb, path, origin: sameOrigin ? 'app' : parsed.origin, endpoint: sameOrigin ? 'non GET request outside the read only allowlist' : 'non GET request to another origin', where: null};
}

/* What the script never touches. The question form and its controls
   (ask42.jsx:683-711), the suggested questions under `Ask next`, which ask
   on a click (ask42.jsx:411-418), the skill forms under Ask (skills42.jsx:43)
   and the starters (ask42.jsx:116). */
export const FORBIDDEN_REGIONS = Object.freeze([
  {selector: '.ask42-form', label: 'Your question'},
  {selector: '.ask42-next', label: 'Ask next'},
  {selector: '.sk42-skills', label: 'Ask with a skill'},
  {selector: '.ask42-starters', label: 'Starters'},
]);
export const QUESTION_FIELD_LABELS = Object.freeze(['Your question', 'Market', 'Brand', 'Brief (up to 2,000 characters)']);
/* ask42.jsx:709 Ask, :484 Stop, :725 and :740 Try again, CostConfirm's
   confirm word Ask again (:763), :753 Ask a follow-up, :399 Add to dossier,
   :402 Save checked claims to Findings. */
export const SEND_CONTROL_NAMES = Object.freeze(['Ask', 'Stop', 'Stopping', 'Try again', 'Ask again', 'Ask a follow-up', 'Add to dossier', 'Save checked claims to Findings', 'Saving checked claims…']);

export function forbiddenTarget(info){
  if (!info) return null;
  if (info.inComposer) return 'inside ' + JSON.stringify(info.composerLabel || 'unlabelled');
  if (QUESTION_FIELD_LABELS.includes(String(info.label || '').trim())) return 'the question field ' + JSON.stringify(info.label);
  if (info.tag === 'BUTTON' && SEND_CONTROL_NAMES.includes(String(info.name || '').trim())) return 'the ' + JSON.stringify(info.name) + ' control';
  return null;
}

/* Runs in the page: what the script is about to act on. */
function describeTarget(element, regions){
  if (!element) return null;
  const region = element.closest ? regions.find(item => element.closest(item.selector)) : null;
  const labelled = element.labels && element.labels[0] ? element.labels[0].textContent : '';
  return {
    tag: element.tagName, label: (labelled || '').trim(), name: (element.getAttribute('aria-label') || element.textContent || '').trim(),
    inComposer: Boolean(region), composerLabel: region ? region.label : '',
  };
}

/* Checks of the desk version that 42's Ask page has nothing to compare
   with. They are listed in the report with the reason, never marked NOT
   SEEN. */
export const NOT_APPLICABLE_IN_42 = Object.freeze([
  {step: '2.6', desk_check: '`Source records` with `<N> records in this reply. Record counts do not establish independent corroboration.`', reason: '42 shows no record count section; each claim names its posts as source chips and the cited posts are listed under `The posts`, which step 2.6 checks instead (PostStrip.jsx:37-41).'},
  {step: '3.1', desk_check: 'the thread shows both turns (two or more question headings)', reason: '42 opens one stored Ask per page. A follow-up shows its own question and answer and does not repeat the parent turn above it (ask42.jsx:324, :729-754); the parent is named by the record\'s parent_id, which step 3.2 checks.'},
  {step: '3.3', desk_check: 'the parent answer shown above, starting `Response: complete.` or `Response: partial.`', reason: 'The parent answer is not shown on a follow-up\'s page in 42, and the `Response:` history text was the desk browser\'s own copy of the earlier turn (generalIntelligence.js:172), which 42 does not keep.'},
  {step: '3.5', desk_check: 'no line `Authenticated parent continuity was not supplied; prior conversation prose is context only.`', reason: 'That line came from the retired desk engine\'s context admission (engine/src/analysis/open_intelligence/general_question_context_admission.py:1332-1335). 42 records the parent as parent_id and writes no such line.'},
  {step: '4.7', desk_check: '`Record reference` disclosure', reason: '42\'s source panel shows no record reference; the evidence id stays in the record only (SourcePanel.jsx:33-49). Step 4.8 checks that Close returns focus to the source chip instead.'},
  {step: '5.1', desk_check: 'button `Download as PDF`', reason: 'f42-api exports HTML only (core/api/app.py:912-913, `Only format=html is available until Stage 3.`), and Ask offers one `Export answer` control (ask42.jsx:405).'},
]);

class Refusal extends Error {
  constructor(record){ super('refused: ' + record.endpoint); this.record = record; }
}

export function parseInputs(environment = process.env){
  const split = value => String(value || '').split(/[\s,]+/).map(item => item.trim()).filter(Boolean);
  const errors = [];
  let url = null;
  try {
    const parsed = new URL(String(environment.STAGING_URL || ''));
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error('scheme');
    url = parsed.origin;
  } catch { errors.push('STAGING_URL must be an http or https address with no user information'); }
  const keyFile = String(environment.ACCESS_KEY_FILE || '');
  if (!keyFile || !existsSync(keyFile) || !statSync(keyFile).isFile()) errors.push('ACCESS_KEY_FILE must name a readable file');
  if (environment.STORED_REQUEST_IDS || environment.FOLLOW_UP_REQUEST_IDS) errors.push('STORED_REQUEST_IDS and FOLLOW_UP_REQUEST_IDS named desk request ids, which 42 does not store; set STORED_ASK_IDS and FOLLOW_UP_ASK_IDS to ask ids from the History page instead');
  const firsts = split(environment.STORED_ASK_IDS);
  const followUps = split(environment.FOLLOW_UP_ASK_IDS);
  for (const id of [...firsts, ...followUps]) if (!ASK_ID.test(id)) errors.push('not an ask id: ' + JSON.stringify(id.slice(0, 40)));
  if (!firsts.length && !followUps.length) errors.push('STORED_ASK_IDS or FOLLOW_UP_ASK_IDS must name at least one ask id');
  const out = String(environment.PROOF_OUT_DIR || '');
  if (!out || !isAbsolute(out)) errors.push('PROOF_OUT_DIR must be an absolute path');
  return {errors, url, keyFile, firsts: [...new Set(firsts)], followUps: [...new Set(followUps.filter(id => !firsts.includes(id)))], out};
}

function chromePath(environment){
  return [
    environment.CHROME_PATH, environment.CHROME_BIN,
    '/usr/bin/google-chrome', '/usr/bin/chromium-browser', '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
  ].filter(Boolean).find(candidate => existsSync(candidate)) || undefined;
}

/* Lines the page must never show on a stored answer. ask42.jsx:47-51 (the
   answer status words other than partial), :327, :724, :737-738, :747. */
const FAILURE_LINES = Object.freeze([
  'Not enough evidence for a full answer', '42 did not answer this question',
  'Stopped early: this answer holds only what had passed its checks',
  'This answer did not pass its checks',
  'This question stopped before any answer passed its checks.',
  'This question still reads as running, so it cannot be asked again yet.',
  'Enter the passcode to ask a question.', 'The question could not be asked.',
]);
const PARTIAL_LINE = 'Partial answer: part of the picture is missing'; // ask42.jsx:48
const POST_LIMIT = 8; // PostStrip.jsx:13

const fileLabel = value => String(value).replace(/[^a-z0-9-]+/gi, '-');

export async function runProof(environment = process.env, io = {out: process.stdout, err: process.stderr}){
  const inputs = parseInputs(environment);
  let key = '';
  /* The key is removed as typed and in its JSON escaped form, so a key with
     a quote, a backslash or a control character cannot pass through a
     serialised report. */
  const redact = value => {
    let text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    if (key) for (const form of [JSON.stringify(key).slice(1, -1), key]) text = text.split(form).join(REDACTED);
    return text;
  };
  const say = line => io.out.write(redact(line) + '\n');
  const warn = line => io.err.write(redact(line) + '\n');
  if (inputs.errors.length){ inputs.errors.forEach(warn); return 3; }
  key = readFileSync(inputs.keyFile, 'utf8').replace(/[\r\n]+$/, '').trim();
  if (!key){ warn('ACCESS_KEY_FILE is empty'); return 3; }
  mkdirSync(inputs.out, {recursive: true});

  const report = {
    contract: 'stored_answer_proof_v2', started_at: new Date().toISOString(), finished_at: null,
    staging_origin: inputs.url, stored_ask_ids: inputs.firsts, follow_up_ask_ids: inputs.followUps,
    paid_submissions_attempted: 0, refusal: null,
    guard: {
      installed_before_navigation: false,
      allowed_read_endpoints: ALLOWED_READ_ENDPOINTS.map(({method, path, where}) => ({method, path, where})),
      allowed_non_get: ALLOWED_NON_GET.map(({method, path, where}) => ({method, path, where})),
      guarded_endpoints: GUARDED_ENDPOINTS.map(({method, pattern, where, why}) => ({method, path: pattern.source, where, why})),
      allowed_non_get_seen: [],
    },
    not_applicable: NOT_APPLICABLE_IN_42.map(item => ({...item, status: 'NOT APPLICABLE IN 42'})),
    checks: [], screenshots: [], downloads: [], page_text: [], outcome: null, error: null, exit_code: null,
  };
  const check = (step, askId, expected, source, seen, actual) => {
    report.checks.push({step, ask_id: askId, expected, source, status: seen ? 'SEEN' : 'NOT SEEN', actual: seen ? null : actual ?? null});
    say(`${seen ? 'SEEN    ' : 'NOT SEEN'} ${step}${askId ? ' ' + askId : ''}: ${expected}`);
    return report.checks[report.checks.length - 1];
  };
  const writeReport = () => { report.finished_at = new Date().toISOString(); writeFileSync(join(inputs.out, 'report.json'), redact(report) + '\n'); };
  for (const item of NOT_APPLICABLE_IN_42) say(`N/A      ${item.step}: ${item.desk_check}; not applicable in 42: ${item.reason}`);

  delete process.env.DEBUG; delete process.env.PWDEBUG;
  /* Never throws: a thrown value whose text cannot be read gets a fixed
     message, so the report is still written. */
  const errorText = error => {
    try { return String(error && error.message || error); }
    catch { try { return String(error); } catch { return 'the error could not be read'; } }
  };
  const firstLine = error => errorText(error).split('\n')[0];
  /* Anything that fails before the first page loads is a run that could not
     start, so it exits 3 as the key and input checks do; exit 1 is kept for a
     run that reached the app and saw a check fail. The report is written
     either way; redact() keeps the key out of the error text. */
  const startFailed = (outcome, message, hint) => {
    warn(message);
    if (hint) warn(hint);
    report.outcome = outcome; report.error = message; if (hint) report.hint = hint; report.exit_code = 3;
    writeReport();
    return 3;
  };
  const executablePath = chromePath(environment);
  const args = typeof process.getuid === 'function' && process.getuid() === 0 ? ['--no-sandbox'] : [];
  let browser;
  try {
    const chromium = io.chromium || (await import('@playwright/test')).chromium;
    browser = await chromium.launch({headless: environment.PROOF_HEADED !== '1', executablePath, args});
  } catch (error){
    const message = 'The browser did not start: ' + firstLine(error);
    const missing = /Executable doesn't exist|executable.*not found|playwright install/i.test(errorText(error));
    return startFailed('browser_unavailable', message, missing ? INSTALL_HINT : null);
  }

  let refusal = null;
  let stop;
  const stopped = new Promise(resolvePromise => { stop = resolvePromise; });
  const refuse = record => {
    if (refusal) return;
    refusal = {...record, at: new Date().toISOString()};
    report.refusal = refusal;
    if (record.kind === 'network' && record.paid) report.paid_submissions_attempted += 1;
    stop(refusal);
  };
  /* The stored records as the page read them, by ask id: the last good
     GET /api/ask/<ask id> response the page received. */
  const records = new Map();
  let context, page;
  try {
    context = await browser.newContext({viewport: {width: 1280, height: 900}, acceptDownloads: true, serviceWorkers: 'block'});
    /* The guard goes on before any page exists, so the first navigation is
       already under it. Allowed requests continue exactly as sent. */
    await context.route('**/*', route => {
      const request = route.request();
      const decision = guardDecision(request.method(), request.url(), inputs.url);
      if (decision.allow){
        if (!['GET', 'HEAD'].includes(request.method())) report.guard.allowed_non_get_seen.push({method: request.method(), path: new URL(request.url()).pathname});
        return route.continue();
      }
      refuse({kind: 'network', ...decision});
      return route.abort('blockedbyclient');
    });
    report.guard.installed_before_navigation = true;
    page = await context.newPage();
    page.on('response', response => {
      const request = response.request();
      if (request.method() !== 'GET' || !response.ok()) return;
      let address;
      try { address = new URL(response.url()); } catch { return; }
      if (address.origin !== inputs.url) return;
      const match = new RegExp('^/api/ask/(' + ID_PART + ')$').exec(routedPath(address.pathname) || '');
      if (!match) return;
      response.json().then(value => { if (value && typeof value === 'object') records.set(match[1], value); }, () => {});
    });
  } catch (error){
    await Promise.race([Promise.resolve().then(() => browser.close()), new Promise(done => setTimeout(done, 5000))]).catch(() => {});
    return startFailed('start_failed', 'The browser context or first page did not open: ' + firstLine(error), null);
  }
  let shot = 0;
  const screenshot = async (target, label) => {
    const name = String(++shot).padStart(2, '0') + '-' + fileLabel(label) + '.png';
    await target.screenshot({path: join(inputs.out, name), fullPage: false});
    report.screenshots.push(name);
    return name;
  };
  const keepText = async (label) => {
    const text = await page.evaluate(() => (document.querySelector('main.ask42') || document.querySelector('#main-content') || document.body).innerText);
    const name = fileLabel(label) + '.txt';
    writeFileSync(join(inputs.out, name), redact(text) + '\n');
    report.page_text.push(name);
  };
  const regions = FORBIDDEN_REGIONS.map(({selector, label}) => ({selector, label}));
  const guarded = async (locator, act) => {
    const info = await locator.evaluate(describeTarget, regions);
    const reason = forbiddenTarget(info);
    if (reason) throw new Refusal({kind: 'script', endpoint: 'refused to act on ' + reason, method: null, path: null, where: 'app/frontend/src/ask42.jsx:683-711, :753'});
    return act();
  };
  const pressEnterOn = async (locator) => guarded(locator, async () => {
    await locator.focus();
    const focused = await page.evaluate(([element, list]) => {
      const region = element && element.closest ? list.find(item => element.closest(item.selector)) : null;
      const labelled = element && element.labels && element.labels[0] ? element.labels[0].textContent : '';
      return element ? {tag: element.tagName, label: (labelled || '').trim(), name: (element.getAttribute('aria-label') || element.textContent || '').trim(), inComposer: Boolean(region), composerLabel: region ? region.label : ''} : null;
    }, [await page.evaluateHandle(() => document.activeElement), regions]);
    const reason = forbiddenTarget(focused);
    if (reason) throw new Refusal({kind: 'script', endpoint: 'refused to press Enter on ' + reason, method: null, path: null, where: 'app/frontend/src/ask42.jsx:694-701'});
    await page.keyboard.press('Enter');
  });
  /* Waits for the page's own read of this record to land. */
  const recordFor = async (id) => {
    for (let waited = 0; waited < 5000 && !records.has(id); waited += 100) await page.waitForTimeout(100);
    return records.get(id) || null;
  };

  const journeys = async () => {
    const ordered = [...inputs.firsts.map(id => ({id, followUp: false})), ...inputs.followUps.map(id => ({id, followUp: true}))];
    const addressOf = id => '#/ask?follow=' + encodeURIComponent(id);
    /* 1. The gate, opened on the first stored answer's own address so the
       page behind it is Ask's stored view. passcode.jsx:51, :60, :71. */
    await page.goto(inputs.url + '/' + addressOf(ordered[0].id), {waitUntil: 'domcontentloaded'});
    const field = page.locator('#gate-passcode');
    await field.waitFor({state: 'visible', timeout: 30000});
    check('1.1', null, 'gate heading `Enter the desk.`', 'passcode.jsx:51', await page.getByRole('heading', {name: 'Enter the desk.'}).count() === 1);
    const label = (await page.locator('label[for="gate-passcode"]').textContent() || '').trim();
    check('1.1', null, 'field labelled `Access key`', 'passcode.jsx:60', label === 'Access key', label);
    if (label !== 'Access key') throw new Refusal({kind: 'script', endpoint: 'refused to type the key into a field not labelled Access key', method: null, path: null, where: 'app/frontend/src/passcode.jsx:60'});
    await guarded(field, () => field.fill(key));
    const enter = page.locator('button.gate-v4-submit');
    check('1.1', null, 'button `Enter 42`', 'passcode.jsx:71', /^Enter 42/.test((await enter.textContent() || '').trim()));
    await guarded(enter, () => enter.click());
    const past = await Promise.race([
      field.waitFor({state: 'detached', timeout: 30000}).then(() => true),
      page.locator('#gate-status.is-error').waitFor({state: 'visible', timeout: 30000}).then(() => false),
    ]).catch(() => false);
    check('1.2', null, 'the app opens past the gate', 'App.jsx:859-885', past, 'the gate stayed on screen');
    if (!past) return 'gate_refused';

    const settled = page.locator('main.ask42 .ask42-done, main.ask42 .ask42-failed').first();
    for (const {id, followUp} of ordered){
      const label = fileLabel(id);
      /* 2. Open the stored answer by its address, then reload. The address
         is History's Read answer link (history42.jsx:293); Ask reads the
         record and asks nothing (ask42.jsx:570-603). */
      await page.goto(inputs.url + '/' + addressOf(id), {waitUntil: 'domcontentloaded'});
      await settled.waitFor({state: 'visible', timeout: 30000}).catch(() => {});
      records.delete(id);
      await page.reload({waitUntil: 'domcontentloaded'});
      await settled.waitFor({state: 'visible', timeout: 30000}).catch(() => {});
      const record = await recordFor(id);
      const address = await page.evaluate(() => location.hash);
      check('2.1', id, 'address `' + addressOf(id) + '` after reload', 'ask42.jsx:597-603; history42.jsx:293', address === addressOf(id), address);
      const view = page.locator('main.ask42 article.ask42-answer').first();
      const question = ((await view.locator('h2.ask42-question').first().textContent().catch(() => '')) || '').trim();
      report.checks.push({step: '2.2', ask_id: id, expected: 'question heading recorded', source: 'ask42.jsx:324', status: question ? 'SEEN' : 'NOT SEEN', actual: question || null});
      say(`${question ? 'SEEN    ' : 'NOT SEEN'} 2.2 ${id}: question heading`);
      const meta = ((await view.locator('p.ask42-meta').first().textContent().catch(() => '')) || '').trim();
      check('2.3', id, 'meta line under the question (market, post window, posts and platforms)', 'ask42.jsx:168-180, :325', meta.length > 0, meta || 'no meta line');
      const answerStatus = record && record.answer ? record.answer.status : null;
      const statusLines = (await view.locator(':scope > p.ask42-status').allTextContents().catch(() => [])).map(text => text.trim());
      const short = ((await view.locator('p.ask42-short').first().textContent().catch(() => '')) || '').trim();
      const statusShown = answerStatus === 'complete' ? statusLines.length === 0 : answerStatus === 'partial' ? statusLines.length === 1 && statusLines[0] === PARTIAL_LINE : false;
      check('2.4', id, 'a finished record, answer `complete` or `partial`, the short answer shown and, for partial, `' + PARTIAL_LINE + '`', 'core/api/app.py:831; ask42.jsx:47-51, :326-328', Boolean(record) && record.status === 'complete' && statusShown && short.length > 0,
        record ? `record ${record.status}, answer ${answerStatus}, status lines ${statusLines.join(' | ') || 'none'}, short answer ${short ? 'shown' : 'missing'}` : 'the page read no record');
      const claimRows = view.locator('ol.ask42-claims > li.ask42-claim');
      const claims = await claimRows.count();
      const chipCounts = await claimRows.evaluateAll(rows => rows.map(row => row.querySelectorAll('button.ask42-chip:not(.ask42-chip-more)').length));
      const chipNames = (await view.locator('ol.ask42-claims button.ask42-chip:not(.ask42-chip-more)').evaluateAll(buttons => buttons.map(button => button.getAttribute('aria-label') || ''))).map(text => text.trim());
      check('2.5', id, 'claims, each with a source chip named `Source: ...`', 'ask42.jsx:330-347; EvidenceChip.jsx:102', claims > 0 && chipCounts.every(count => count > 0) && chipNames.length > 0 && chipNames.every(text => /^Source: .+/.test(text)), `${claims} claims, chips per claim ${chipCounts.join(' ') || 'none'}`);
      const evidence = record && record.answer && Array.isArray(record.answer.evidence) ? record.answer.evidence : [];
      const postsHeading = await view.locator('h3#ask42-posts-title', {hasText: /^The posts$/}).count();
      const posts = await view.locator('ul.ask42-posts > li.ask42-post').count();
      check('2.6', id, '`The posts` listing the cited posts (up to ' + POST_LIMIT + ')', 'PostStrip.jsx:13, :37-41', postsHeading === 1 && posts > 0 && posts === Math.min(POST_LIMIT, evidence.length), `${postsHeading ? 'heading shown' : 'no heading'}, ${posts} posts for ${evidence.length} evidence records`);
      const body = await page.locator('main.ask42').innerText().catch(() => '');
      const failures = FAILURE_LINES.filter(line => body.includes(line));
      const failedBlocks = await page.locator('main.ask42 .ask42-failed').count();
      check('2.7', id, 'no failure or refusal line on the page', 'ask42.jsx:47-51, :327, :722-749', failures.length === 0 && failedBlocks === 0, [...failures, failedBlocks ? failedBlocks + ' failure blocks' : ''].filter(Boolean).join(' | '));
      const followButton = await page.locator('main.ask42 button.ask42-followup-open', {hasText: /^Ask a follow-up$/}).count();
      const formHidden = await page.locator('main.ask42 form.ask42-form').evaluate(form => form.hidden).catch(() => false);
      check('2.8', id, 'button `Ask a follow-up` present (never pressed) with the question form folded away', 'ask42.jsx:500, :683, :753', followButton === 1 && formHidden, `${followButton} follow-up buttons, form ${formHidden ? 'hidden' : 'shown'}`);
      await keepText('text-' + label + '-reloaded');
      await screenshot(page, label + '-stored-answer');

      if (followUp){
        /* 3. A follow-up answer. Its record names the answer it follows as
           parent_id (core/agent/ask.py, contract section 6); the page shows
           the follow-up's own question and answer. */
        const parent = record && typeof record.parent_id === 'string' ? record.parent_id : null;
        const parentKnown = inputs.firsts.length ? inputs.firsts.includes(parent) : true;
        const parentCheck = check('3.2', id, 'the record the page read names its parent ask' + (inputs.firsts.length ? ', one of the stored first answers' : ''), 'core/api/app.py:831; ask42.jsx:249, :636', Boolean(parent) && ASK_ID.test(parent) && parent !== id && parentKnown, parent || 'no parent_id');
        if (parent) parentCheck.parent_ask_id_as_read = parent;
        const saveOffered = await view.locator('.ask42-actions button', {hasText: /^Save checked claims to Findings$/}).count();
        check('3.4', id, '`Save checked claims to Findings` not offered on a follow-up', 'ask42.jsx:249, :400-404', saveOffered === 0, saveOffered + ' save controls');
        const technical = view.locator('details.ask42-technical');
        const runId = await technical.getAttribute('data-run-id').catch(() => null);
        const expectedRun = record && record.run ? record.run.run_id || null : null;
        check('3.6', id, 'the page shows this follow-up\'s own record (its ask id) with `Technical details` carrying its run id', 'ask42.jsx:431-445', Boolean(record) && record.ask_id === id && await technical.count() === 1 && runId === expectedRun, `record ${record ? record.ask_id : 'none'}, run ${runId || 'none'}`);
        await screenshot(page, label + '-follow-up');
      }

      /* 4. Source inspection by keyboard. EvidenceChip.jsx:100-102,
         SourcePanel.jsx:13-49. */
      const chip = view.locator('ol.ask42-claims button.ask42-chip:not(.ask42-chip-more)').first();
      if (await chip.count()){
        const name = ((await chip.getAttribute('aria-label')) || '').trim();
        check('4.1', id, 'source chip accessible name `Source: <platform> post by <handle>, <day>`', 'EvidenceChip.jsx:84-88, :102', /^Source: .+/.test(name), name);
        await pressEnterOn(chip);
        await page.waitForFunction(() => document.activeElement && document.activeElement.classList.contains('ask42-source-title'), null, {timeout: 5000}).catch(() => {});
        const focus = await page.evaluate(() => ({tag: document.activeElement?.tagName, text: (document.activeElement?.textContent || '').trim(), inPanel: Boolean(document.activeElement?.closest('aside.ask42-source[aria-label="Source"]'))}));
        const pressed = await chip.getAttribute('aria-pressed').catch(() => null);
        check('4.2', id, 'focus on the Source panel heading naming the same post, the chip pressed', 'SourcePanel.jsx:17-19, :33; EvidenceChip.jsx:100', focus.tag === 'H3' && focus.inPanel && focus.text.length > 0 && name.startsWith('Source: ' + focus.text) && pressed === 'true', `${focus.tag}: ${focus.text}; pressed ${pressed}`);
        const panel = page.locator('aside.ask42-source[aria-label="Source"]').first();
        const postText = ((await panel.locator('p.ask42-source-text').textContent().catch(() => '')) || '').trim();
        const marks = await panel.locator('p.ask42-source-text mark').count();
        const answer = record && record.answer ? record.answer : {};
        const known = new Set(evidence.map(item => item.id));
        const firstClaim = (answer.claims || []).find(claim => (claim.evidence_ids || []).some(item => known.has(item)));
        const shownId = firstClaim ? firstClaim.evidence_ids.find(item => known.has(item)) : null;
        const quoted = (answer.claims || []).some(claim => (claim.quotes || []).some(quote => quote.evidence_id === shownId && String(quote.text || '').trim()));
        const lines = (await panel.locator(':scope > p').allTextContents().catch(() => [])).map(text => text.trim());
        report.checks.push({step: '4.3', ask_id: id, expected: 'panel lines recorded', source: 'SourcePanel.jsx:39-46', status: lines.length ? 'SEEN' : 'NOT SEEN', actual: lines});
        check('4.3', id, 'the post\'s words, with the quoted words marked when the answer quotes this post', 'SourcePanel.jsx:39; EvidenceChip.jsx:47-69', postText.length > 0 && (!quoted || marks > 0), `${postText ? 'text shown' : 'no text'}, ${marks} marked spans${quoted ? ', the answer quotes this post' : ''}`);
        const facts = ((await panel.locator(':scope > p.ask42-muted').first().textContent().catch(() => '')) || '').trim();
        check('4.4', id, 'the day the post went up `<day> <month> <year>`, with views when known', 'SourcePanel.jsx:41; EvidenceChip.jsx:22-27', /^\d{1,2} [A-Z][a-z]+ \d{4}( · |$)/.test(facts), facts || 'no date line');
        check('4.5', id, 'the panel heading names the platform `<platform> post by <handle>`', 'SourcePanel.jsx:33; EvidenceChip.jsx:84-88', /^\S.* post( by .+)?$/.test(focus.text), focus.text);
        const link = panel.locator('a', {hasText: /^Open the post$/});
        const linkCount = await link.count();
        const noLink = await panel.locator('span.ask42-muted', {hasText: /^No link to this post$/}).count();
        const linkCheck = check('4.6', id, '`Open the post` link, or `No link to this post`', 'SourcePanel.jsx:43-45', linkCount === 1 || noLink === 1);
        if (linkCount === 1) linkCheck.link_target = await link.getAttribute('href');
        else if (noLink === 1) linkCheck.alternative = 'No link to this post';
        await panel.scrollIntoViewIfNeeded().catch(() => {});
        await screenshot(page, label + '-source');
        const close = panel.locator('button', {hasText: /^Close$/});
        if (await close.count()){
          await pressEnterOn(close);
          await page.waitForFunction(() => !document.querySelector('aside.ask42-source'), null, {timeout: 5000}).catch(() => {});
          const back = await page.evaluate(() => ({label: document.activeElement?.getAttribute('aria-label') || '', open: Boolean(document.querySelector('aside.ask42-source'))}));
          check('4.8', id, 'Close by keyboard shuts the panel and returns focus to the source chip', 'SourcePanel.jsx:20-23', !back.open && back.label === name, `${back.open ? 'panel still open' : 'panel closed'}, focus on ${back.label || 'nothing named'}`);
        } else check('4.8', id, 'a Close control on the Source panel', 'SourcePanel.jsx:46', false, 'no Close control');
      } else check('4.1', id, 'a source chip to inspect', 'EvidenceChip.jsx:102', false, 'no source chip');

      /* 5. The HTML export. ask42.jsx:261-270, :405; askTransport42.js:47-62;
         core/api/export.py:193-313. */
      const exportButton = view.locator('.ask42-actions button', {hasText: /^Export answer$/});
      check('5.1', id, 'button `Export answer`', 'ask42.jsx:405', await exportButton.count() === 1);
      if (await exportButton.count() === 1){
        const expectedName = '42-answer-' + id + '.html';
        const waiting = page.waitForEvent('download', {timeout: 30000});
        await guarded(exportButton, () => exportButton.click());
        const download = await waiting.catch(() => null);
        const suggested = download ? download.suggestedFilename() : null;
        check('5.2', id, 'file `' + expectedName + '` saved', 'askTransport42.js:54', suggested === expectedName, suggested || 'no download');
        await page.waitForFunction(() => !document.querySelector('.ask42-actions button[aria-busy="true"]'), null, {timeout: 5000}).catch(() => {});
        const alerts = (await view.locator('p.ask42-error[role="alert"]').allTextContents()).map(text => text.trim());
        check('5.2', id, 'no export refusal line', 'ask42.jsx:266, :425', alerts.length === 0, alerts.join(' | '));
        if (download){
          const target = join(inputs.out, expectedName);
          await download.saveAs(target);
          report.downloads.push({ask_id: id, file: expectedName, bytes: statSync(target).size});
          const copy = await context.newPage();
          await copy.goto(pathToFileURL(target).href);
          const title = await copy.title();
          check('5.3', id, 'title `42 answer`', 'core/api/export.py:312', title === '42 answer', title);
          const heading = ((await copy.locator('h1').first().textContent().catch(() => '')) || '').trim();
          check('5.3', id, 'heading `42 answer`', 'core/api/export.py:198', heading === '42 answer', heading);
          const asked = (await copy.locator('header p').allTextContents().catch(() => [])).map(text => text.trim());
          check('5.3', id, '`Question: <the question on the page>`', 'core/api/export.py:199', Boolean(question) && asked.includes('Question: ' + question), asked.join(' | '));
          check('5.3', id, 'section `Sources`', 'core/api/export.py:279', await copy.locator('h2', {hasText: /^Sources$/}).count() === 1);
          const footer = ((await copy.locator('footer').first().textContent().catch(() => '')) || '');
          check('5.3', id, 'footer names `ask ' + id + '`', 'core/api/export.py:305', new RegExp('(^| )ask ' + id.replace(/[-]/g, '\\-') + '($|[^A-Za-z0-9_-])').test(footer), footer.trim().slice(0, 200));
          await screenshot(copy, label + '-export');
          await copy.close();
        }
      }
    }
    return 'complete';
  };

  let code;
  /* The journeys never reject: a refusal or an error becomes an outcome, so a
     journey still running when the guard stops the run cannot crash it. */
  const running = journeys().then(value => ({value}), error => ({error}));
  const first = await Promise.race([running, stopped.then(() => ({value: 'refused'}))]);
  let outcome = first.value;
  if (first.error instanceof Refusal) refuse(first.error.record);
  else if (first.error && !refusal){ outcome = 'error'; warn('The run stopped: ' + String(first.error && first.error.message).split('\n')[0]); }
  if (refusal) outcome = 'refused';
  if (outcome === 'refused'){
    code = 2;
    warn(`REFUSED ${refusal.kind === 'network' ? refusal.method + ' ' + refusal.path : refusal.endpoint}; the run stopped before it left the browser.`);
  } else if (outcome === 'gate_refused' || outcome === 'error') code = 3;
  else code = report.checks.every(item => item.status === 'SEEN') ? 0 : 1;
  report.outcome = outcome;
  report.exit_code = code;
  writeReport();
  await Promise.race([browser.close(), new Promise(done => setTimeout(done, 5000))]).catch(() => {});
  say(`report ${join(inputs.out, 'report.json')} exit ${code}`);
  return code;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)){
  runProof().then(code => process.exit(code), () => { process.stderr.write('The run could not complete.\n'); process.exit(3); });
}
