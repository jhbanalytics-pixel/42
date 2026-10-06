import {expect, test} from 'bun:test';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {existsSync, mkdtempSync, readFileSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

import {verifyInstrumentPackageInputs} from '../../../scripts/verify-instrument-package.mjs';
import {cleanupTemporaryPath} from './task4-proof-helpers.js';

const expected = {
  archiveSha256: '2E86002374B1C4BA4127932D22319091EA9FCBC00443776D41E81DDD344276E6',
  contractVersion: '1.2.0',
  cssSha256: 'e39748fca5c56e7ca8a5fff28f566f7defd2e09641952d3757541f8ff077fbca',
  evidenceCommit: 'bf1e45a2e01e814ba47f64b307684d746f3e691f',
  packageSourceCommit: '61c42d1be2658e1f3dacdbf317683e81a86f4b6a',
  specSha256: '15765aebe3fa850f813c50e349699fe7e25f5036e27a74b01901beae0e291886',
  tokenDigest: 'f4fc6fab3594c27b923e5ecb727f8f9f759850a9845b351c36249a15ab482ed2', // gitleaks:allow, pinned non-secret contract digest
};

const manifest = {
  contractVersion: expected.contractVersion,
  cssSha256: expected.cssSha256,
  evidenceCommit: expected.evidenceCommit,
  exportList: ['EvidenceRoom', 'InstrumentBriefing', 'InstrumentShell'],
  files: [{path: 'style.css', sha256: expected.cssSha256}],
  package: {name: 'ogilvy-intelligence-design-system', version: '2.0.22'},
  packageSourceCommit: expected.packageSourceCommit,
  sourceCommit: expected.packageSourceCommit,
  specSha256: expected.specSha256,
  tokenDigest: expected.tokenDigest,
};

const input = {
  actualArchiveSha256: expected.archiveSha256,
  archivedManifest: manifest,
  expected,
  installedManifest: manifest,
  lockText: `"ogilvy-intelligence-design-system": "file:vendor/ogilvy-intelligence-design-system-2.0.22.tgz"\n"ogilvy-intelligence-design-system": ["ogilvy-intelligence-design-system@vendor/ogilvy-intelligence-design-system-2.0.22.tgz", {}, "sha512-approved"]`,
  packageDependency: 'file:vendor/ogilvy-intelligence-design-system-2.0.22.tgz',
  requiredExports: manifest.exportList,
  styleSha256: expected.cssSha256,
};

test('package readback accepts one exact vendored archive and independently hashed CSS', () => {
  expect(verifyInstrumentPackageInputs(input)).toMatchObject({checkCount: 14, failures: []});
});

test('package readback rejects each approved package identity mutation', () => {
  const mutations = [
    ['archive SHA', {actualArchiveSha256: '0'.repeat(64)}],
    ['manifest source commit', {archivedManifest: {...manifest, packageSourceCommit: '0'.repeat(40)}}],
    ['spec SHA', {archivedManifest: {...manifest, specSha256: '0'.repeat(64)}}],
    ['contract version', {archivedManifest: {...manifest, contractVersion: '9.0.0'}}],
    ['token digest', {archivedManifest: {...manifest, tokenDigest: '0'.repeat(64)}}],
    ['export list', {archivedManifest: {...manifest, exportList: manifest.exportList.slice(1)}}],
    ['CSS digest', {styleSha256: '0'.repeat(64)}],
    ['installed manifest', {installedManifest: {...manifest, evidenceCommit: '0'.repeat(40)}}],
  ];

  for (const [name, mutation] of mutations){
    expect(() => verifyInstrumentPackageInputs({...input, ...mutation}), name).toThrow();
  }
});

test('consumer lock rejects every nonexact archive resolution', () => {
  const mutations = [
    input.lockText.replaceAll('vendor/ogilvy-intelligence-design-system-2.0.22.tgz', 'vendor/other.tgz'),
    input.lockText.replace('@vendor/', '@link:vendor/'),
    input.lockText.replace('@vendor/', '@workspace:'),
    `${input.lockText}\n"ogilvy-intelligence-design-system": ["ogilvy-intelligence-design-system@vendor/second.tgz"]`,
  ];

  for (const lockText of mutations){
    expect(() => verifyInstrumentPackageInputs({...input, lockText})).toThrow();
  }
});

const SURFACES = ['briefing', 'discover', 'compare', 'build', 'fieldwork', 'evidence-room'];
const CURRENT_ROUTE_SURFACES = ['today', 'discover', 'compare', 'build', 'fieldwork'];
const WIDTHS = [390, 768, 1024, 1280, 1440];

function assertExactViewport({requestedWidth, viewportWidth}){
  if (requestedWidth !== viewportWidth){
    throw new Error(`Requested width ${requestedWidth} rendered at ${viewportWidth}`);
  }
}

test.each([768, 411])('requested width 390 rejects actual viewport %i', (viewportWidth) => {
  expect(() => assertExactViewport({requestedWidth: 390, viewportWidth})).toThrow(
    `Requested width 390 rendered at ${viewportWidth}`,
  );
});

function assertPackageEvidenceRoom({surface, evidence}){
  if (surface !== 'evidence-room') return;
  if (!evidence || evidence.roomPresent !== true){
    throw new Error('Evidence Room did not render on the Discover route');
  }
  const {boundary} = evidence;
  if (!boundary){
    throw new Error('Evidence Room is host composition: no package dialog boundary owns the room');
  }
  if (!['aside', 'dialog'].includes(boundary.tag) || boundary.mode !== 'dialog' || boundary.ariaModal !== 'true'){
    throw new Error(`Evidence Room boundary ${boundary.tag} in ${boundary.mode} mode is not the package modal`);
  }
  if (boundary.tag === 'aside' && boundary.role !== 'dialog'){
    throw new Error(`Evidence Room aside boundary carries role ${boundary.role || 'none'} rather than dialog`);
  }
  if (boundary.containsRoom !== true){
    throw new Error('Evidence Room sits outside the package dialog boundary');
  }
  if (!boundary.labelledBy || boundary.labelledBy !== boundary.titleId || !boundary.titleText){
    throw new Error(`Evidence Room dialog label ${boundary.labelledBy} resolves to no title`);
  }
  if (evidence.focusContained !== true){
    throw new Error(`Focus left the open Evidence Room dialog at ${evidence.activeAfterOpen}`);
  }
  if (evidence.escapeClosed !== true){
    throw new Error('Escape did not close the Evidence Room dialog');
  }
  const escape = evidence.escape || {};
  const packageOwned = escape.nonBubbling?.closed === true
    || (escape.isolated?.closed === true && escape.isolated.documentReached === false);
  if (!packageOwned){
    throw new Error('Escape close is not attributable to the package dialog handler alone');
  }
  if (evidence.focusReturned !== true){
    throw new Error('Focus did not return to the Open evidence button');
  }
}

const PACKAGE_EVIDENCE_ROOM = {
  surface: 'evidence-room',
  evidence: {
    roomPresent: true,
    boundary: {
      ariaModal: 'true', containsRoom: true, labelledBy: 'evidence-room-title', mode: 'dialog',
      role: '', tag: 'dialog', titleId: 'evidence-room-title', titleText: 'Repair routine',
    },
    escape: {nonBubbling: {closed: false}, isolated: {closed: true, documentReached: false}},
    escapeClosed: true, focusContained: true, focusReturned: true,
  },
};

const HOST_ONLY_EVIDENCE_ROOM = {
  surface: 'evidence-room',
  evidence: {
    roomPresent: true, boundary: null,
    escapeClosed: false, focusContained: false, focusReturned: false,
  },
};

function withBoundary(changes){
  return {
    surface: 'evidence-room',
    evidence: {
      ...PACKAGE_EVIDENCE_ROOM.evidence,
      boundary: {...PACKAGE_EVIDENCE_ROOM.evidence.boundary, ...changes},
    },
  };
}

function withEvidence(changes){
  return {surface: 'evidence-room', evidence: {...PACKAGE_EVIDENCE_ROOM.evidence, ...changes}};
}

test.each([
  ['host only composition without a package dialog', HOST_ONLY_EVIDENCE_ROOM, 'host composition'],
  ['a room outside the dialog boundary', withBoundary({containsRoom: false}), 'outside the package dialog'],
  ['a region boundary rather than a modal', withBoundary({mode: 'region'}), 'not the package modal'],
  ['an aside boundary without the dialog role', withBoundary({tag: 'aside', role: ''}), 'rather than dialog'],
  ['a dialog label that resolves to no title', withBoundary({labelledBy: 'other-title'}), 'resolves to no title'],
  ['focus left outside the open dialog', withEvidence({focusContained: false}), 'Focus left'],
  ['an Escape key that does not close', withEvidence({escapeClosed: false}), 'Escape did not close'],
  ['an Escape close the host document handler also saw', withEvidence({escape: {nonBubbling: {closed: false}, isolated: {closed: true, documentReached: true}}}), 'not attributable'],
  ['focus that never returns to the invoker', withEvidence({focusReturned: false}), 'Focus did not return'],
])('legacy package dialog classifier rejects %s', (_name, fixture, message) => {
  expect(() => assertPackageEvidenceRoom(fixture)).toThrow(message);
});

test('legacy package dialog classifier accepts contained focus escape close and focus return', () => {
  expect(() => assertPackageEvidenceRoom(PACKAGE_EVIDENCE_ROOM)).not.toThrow();
  expect(() => assertPackageEvidenceRoom(withBoundary({tag: 'aside', role: 'dialog'}))).not.toThrow();
  expect(() => assertPackageEvidenceRoom(withEvidence({escape: {nonBubbling: {closed: true}, isolated: null}}))).not.toThrow();
  expect(() => assertPackageEvidenceRoom({surface: 'briefing', evidence: null})).not.toThrow();
});

const TARGET_FLOOR_PX = 47.99;

function stringSet(value){
  return new Set(Array.isArray(value) ? value.filter((item) => typeof item === 'string') : []);
}

// Consumer copy of the package Gate B rule at scripts/design-system/check-geometry.mjs,
// classifyInteractiveTarget. The archive does not ship scripts/, so the rule is carried
// here and pinned by GATE_B_CLASSIFIER_FIXTURES, the package contracts fixture table.
function classifyInteractiveTarget(target){
  const classes = stringSet(target?.classNames);
  const contexts = stringSet(target?.contexts);
  const tagName = typeof target?.tagName === 'string' ? target.tagName.toLowerCase() : '';
  const role = typeof target?.role === 'string' ? target.role.toLowerCase() : null;
  if (classes.has('instrument-skip-link')){
    return {primary: false, classification: 'hidden_skip_link'};
  }
  if (contexts.has('inline_receipt') || classes.has('oi-briefing__receipt-link')){
    return {primary: false, classification: 'inline_receipt_link'};
  }
  if (target?.visible !== true){
    return {primary: false, classification: 'not_visible'};
  }
  if (tagName === 'a' && contexts.has('primary_navigation')){
    return {primary: true, classification: 'primary_navigation'};
  }
  if (
    ['button', 'input', 'select', 'textarea'].includes(tagName)
    || ['button', 'tab', 'checkbox'].includes(role)
    || contexts.has('primary_action')
  ){
    return {primary: true, classification: 'primary_action'};
  }
  return {primary: false, classification: tagName === 'a' ? 'inline_link' : 'secondary_interaction'};
}

const GATE_B_CLASSIFIER_FIXTURES = [
  {
    name: 'primary button',
    target: {
      selector: '#primary', tagName: 'button', role: null, classNames: [], contexts: [],
      visible: true, width: 390, height: 48,
    },
    primary: true,
    classification: 'primary_action',
  },
  {
    name: 'hidden skip link',
    target: {
      selector: 'a.instrument-skip-link', tagName: 'a', role: null,
      classNames: ['instrument-skip-link'], contexts: [], visible: false, width: 143, height: 45,
    },
    primary: false,
    classification: 'hidden_skip_link',
  },
  {
    name: 'inline receipt link',
    target: {
      selector: '.evidence-ledger__row a', tagName: 'a', role: null,
      classNames: [], contexts: ['inline_receipt'], visible: true, width: 91, height: 21,
    },
    primary: false,
    classification: 'inline_receipt_link',
  },
  {
    name: 'Discover candidate',
    target: {
      selector: 'button[aria-label="Candidate fixture"]', tagName: 'button', role: null,
      classNames: [], contexts: ['discover_candidate'], visible: true, width: 212, height: 19,
    },
    primary: true,
    classification: 'primary_action',
  },
  {
    name: 'primary job link',
    target: {
      selector: '.instrument-job-link', tagName: 'a', role: null,
      classNames: ['instrument-job-link'], contexts: ['primary_navigation'], visible: true, width: 120, height: 48,
    },
    primary: true,
    classification: 'primary_navigation',
  },
  {
    name: 'plain inline link',
    target: {
      selector: 'article a', tagName: 'a', role: null,
      classNames: [], contexts: [], visible: true, width: 80, height: 20,
    },
    primary: false,
    classification: 'inline_link',
  },
];

test.each(GATE_B_CLASSIFIER_FIXTURES)('the consumer classifier matches the package Gate B rule for $name', ({target, primary, classification}) => {
  expect(classifyInteractiveTarget(target)).toEqual({primary, classification});
});

test('the consumer classifier reads role case context and visibility the way the package rule does', () => {
  expect(classifyInteractiveTarget({tagName: 'div', role: 'Tab', classNames: [], contexts: [], visible: true, width: 40, height: 40}))
    .toEqual({primary: true, classification: 'primary_action'});
  expect(classifyInteractiveTarget({tagName: 'a', role: null, classNames: [], contexts: ['primary_action'], visible: true, width: 40, height: 40}))
    .toEqual({primary: true, classification: 'primary_action'});
  expect(classifyInteractiveTarget({tagName: 'a', role: null, classNames: ['oi-briefing__receipt-link'], contexts: [], visible: true, width: 40, height: 40}))
    .toEqual({primary: false, classification: 'inline_receipt_link'});
  expect(classifyInteractiveTarget({tagName: 'div', role: null, classNames: [], contexts: [], visible: true, width: 40, height: 40}))
    .toEqual({primary: false, classification: 'secondary_interaction'});
  expect(classifyInteractiveTarget({tagName: 'button', role: null, classNames: [], contexts: [], visible: false, width: 40, height: 40}))
    .toEqual({primary: false, classification: 'not_visible'});
});

function belowFloor({width, height}){
  return !(width >= TARGET_FLOOR_PX && height >= TARGET_FLOOR_PX);
}

test('the below-floor filter treats unmeasured and undersized targets as below the floor', () => {
  expect(belowFloor({width: 48, height: 48})).toBe(false);
  expect(belowFloor({width: 47.99, height: 48})).toBe(false);
  expect(belowFloor({width: 47.98, height: 48})).toBe(true);
  expect(belowFloor({width: 48, height: 19})).toBe(true);
  expect(belowFloor({width: undefined, height: 48})).toBe(true);
});

function chromePath(){
  return [
    process.env.CHROME_PATH,
    process.env.CHROME_BIN,
    String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
    String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
    '/usr/bin/google-chrome',
    '/usr/bin/chromium-browser',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].find((candidate) => candidate && existsSync(candidate));
}

const wait = (milliseconds) => new Promise((resolveWait) => setTimeout(resolveWait, milliseconds));

async function chromePort(profile){
  for (let attempt = 0; attempt < 100; attempt += 1){
    try {
      const port = Number(readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split(/\r?\n/, 1)[0]);
      if (port > 0) return port;
    } catch {}
    await wait(50);
  }
  throw new Error('Gate C Chrome debugging port unavailable');
}

// Gate C used to attach to whichever page target /json/list happened to
// return first. That target belongs to Chrome, not to this harness: the
// startup tab can be discarded while the app tab takes its place, and every
// command on the dead session then fails with "Not attached to an active
// page". The harness now talks to the browser endpoint, creates its own page
// target and drives it over a flat session, so no target it uses can be
// closed by Chrome's own startup bookkeeping.
async function browserSocket(port){
  for (let attempt = 0; attempt < 200; attempt += 1){
    try {
      const version = await fetch(`http://127.0.0.1:${port}/json/version`).then((response) => response.json());
      if (version?.webSocketDebuggerUrl) return version.webSocketDebuggerUrl;
    } catch {}
    await wait(50);
  }
  throw new Error('Gate C Chrome browser endpoint unavailable');
}

function cdpConnection(url){
  return new Promise((resolveConnection, rejectConnection) => {
    const socket = new WebSocket(url);
    const pending = new Map();
    const listeners = new Set();
    let id = 0;
    let failure = null;
    const failAll = (error) => {
      failure = error;
      for (const command of pending.values()) command.rejectCommand(error);
      pending.clear();
    };
    socket.addEventListener('open', () => resolveConnection({
      command(method, params = {}, sessionId){
        if (failure) return Promise.reject(failure);
        const commandId = ++id;
        return new Promise((resolveCommand, rejectCommand) => {
          pending.set(commandId, {resolveCommand, rejectCommand});
          socket.send(JSON.stringify({id: commandId, method, params, ...(sessionId ? {sessionId} : {})}));
        });
      },
      onEvent(listener){ listeners.add(listener); },
      fail(error){ failAll(error); },
      close(){ socket.close(); },
    }), {once: true});
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (!message.id){
        for (const listener of listeners) listener(message);
        return;
      }
      if (!pending.has(message.id)) return;
      const command = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) command.rejectCommand(new Error(message.error.message));
      else command.resolveCommand(message.result);
    });
    socket.addEventListener('error', () => {
      const error = new Error('Gate C Chrome socket failed');
      failAll(error);
      rejectConnection(error);
    }, {once: true});
    socket.addEventListener('close', () => failAll(new Error('Gate C Chrome socket closed before the matrix finished')), {once: true});
  });
}

async function pageClient(port, url){
  const connection = await cdpConnection(await browserSocket(port));
  const {targetId} = await connection.command('Target.createTarget', {url});
  const {sessionId} = await connection.command('Target.attachToTarget', {targetId, flatten: true});
  connection.onEvent((message) => {
    if (message.method === 'Target.detachedFromTarget' && message.params?.sessionId === sessionId){
      connection.fail(new Error(`Gate C page target detached mid-run: ${message.params.reason || 'unknown reason'}`));
    }
    if (message.method === 'Inspector.targetCrashed' && message.sessionId === sessionId){
      connection.fail(new Error('Gate C page target crashed mid-run'));
    }
  });
  await connection.command('Inspector.enable', {}, sessionId);
  return {
    command(method, params = {}){ return connection.command(method, params, sessionId); },
    async dispose(){
      try { await connection.command('Target.closeTarget', {targetId}); } catch {}
      try { await connection.command('Browser.close'); } catch {}
      connection.close();
    },
  };
}

// The served index reloads itself once per tab to install the gate recorder,
// so a document that reports complete is not necessarily the one that will be
// measured. A cell only starts once the load is complete and that one-time
// reload has already happened. Evaluate errors while a navigation is in flight
// are the expected shape here, not a failure.
async function settleDocument(client){
  const deadline = Date.now() + 30000;
  let lastState = 'no document';
  while (Date.now() < deadline){
    try {
      const settled = await client.command('Runtime.evaluate', {
        returnByValue: true,
        expression: `JSON.stringify({state: document.readyState, reloaded: sessionStorage.getItem('gate-c-hard-reloaded') === '1'})`,
      });
      const value = JSON.parse(settled.result?.value || '{}');
      lastState = `readyState ${value.state}, one-time reload ${value.reloaded ? 'done' : 'pending'}`;
      if (value.state === 'complete' && value.reloaded) return;
    } catch (error) {
      if (/detached|crashed|socket closed/i.test(error.message)) throw error;
      lastState = `evaluate rejected with ${error.message}`;
    }
    await wait(50);
  }
  throw new Error(`Gate C document never settled within 30s: ${lastState}`);
}

function cellExpression(route, width){
  return `(async()=>{
const wait=(ms)=>new Promise(r=>setTimeout(r,ms));
const required=${JSON.stringify(route.selector)};
const composition=${JSON.stringify(route.composition || '')};
const surface=${JSON.stringify(route.surface)};
const expectedText=${JSON.stringify(route.expectedText || '')};
const visible=e=>{const closed=e.closest('details:not([open])');if(closed&&e.tagName!=='SUMMARY')return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'};
const target=e=>e?(e.id?'#'+e.id:e.tagName.toLowerCase()+(e.classList[0]?'.'+e.classList[0]:'')):'none';
const deadline=Date.now()+5000;
while(Date.now()<deadline){if(document.querySelector(required)&&(!composition||document.querySelector(composition))&&(!expectedText||document.body.innerText.includes(expectedText)))break;await wait(50)}
/* The route surfaces load their layout with the deferred stylesheet, so a
   measurement taken before it lands measures an unstyled surface. main.jsx
   stamps data-product-styles once the deferred set resolves; wait for it. */
const styleState=()=>document.documentElement.getAttribute('data-product-styles');
const settledStyles=s=>s==='ready'||s==='failed';
if(!settledStyles(styleState())){
await new Promise(done=>{
const observer=new MutationObserver(()=>{if(settledStyles(styleState())){observer.disconnect();clearTimeout(timer);done()}});
observer.observe(document.documentElement,{attributes:true,attributeFilter:['data-product-styles']});
const timer=setTimeout(()=>{observer.disconnect();done()},60000);
if(settledStyles(styleState())){observer.disconnect();clearTimeout(timer);done()}
})}
if(styleState()!=='ready')throw new Error('deferred styles never became ready: state '+String(styleState())+', document.readyState '+document.readyState+', stylesheets '+document.styleSheets.length);
await wait(100);
const fixtureThumbnailElements=surface==='discover'?[...document.querySelectorAll('img')].filter(image=>{const src=image.getAttribute('src')||image.currentSrc;return src.includes('https://example.invalid/thumb/')||src.includes('/__fixtures/thumbnail/')}):[];
for(const image of fixtureThumbnailElements)image.loading='eager';
await Promise.all(fixtureThumbnailElements.map(image=>image.decode().catch(()=>{})));
const fixtureThumbnails=fixtureThumbnailElements.map(image=>({src:image.currentSrc||image.src,complete:image.complete,naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight}));
const compositionPresent=!composition||!!document.querySelector(composition);
const violations=[];
const add=(id,impact,nodes)=>{if(nodes.length)violations.push({id,impact,targets:nodes.map(target)})};
add('button-name','critical',[...document.querySelectorAll('button')].filter(e=>visible(e)&&!(e.textContent||e.getAttribute('aria-label')||e.title||'').trim()));
add('link-name','serious',[...document.querySelectorAll('a[href]')].filter(e=>visible(e)&&!(e.textContent||e.getAttribute('aria-label')||e.title||'').trim()));
add('label','critical',[...document.querySelectorAll('input,select,textarea')].filter(e=>visible(e)&&!e.labels?.length&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby')));
const ids=[...document.querySelectorAll('[id]')].map(e=>e.id);
add('duplicate-id-active','serious',[...document.querySelectorAll('[id]')].filter((e,i)=>ids.indexOf(e.id)!==i));
add('aria-dialog-name','serious',[...document.querySelectorAll('[role="dialog"],dialog')].filter(e=>visible(e)&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby')));
const interactiveSelector='a[href],button,input,select,textarea,[role="button"],[role="tab"],[role="checkbox"]';
const interactiveElements=[...document.querySelectorAll(interactiveSelector)];
const interactiveTargets=interactiveElements.map(e=>{
const r=e.getBoundingClientRect();
const contexts=[];
if(e.closest('nav'))contexts.push('primary_navigation');
if(e.closest('.evidence-ledger__row, .cited-answer__receipts, .oi-briefing__receipt'))contexts.push('inline_receipt');
if(e.closest('.state-view__actions, .empty-state__actions'))contexts.push('primary_action');
const hiddenSkipLink=e.matches('.instrument-skip-link:not(:focus)');
const receiptContainer=e.closest('.evidence-ledger__row, .cited-answer__receipts, .oi-briefing__receipt');
return{label:target(e),tagName:e.tagName.toLowerCase(),role:e.getAttribute('role'),classNames:[...e.classList],contexts,visible:visible(e)&&!hiddenSkipLink,width:r.width,height:r.height,container:receiptContainer?String(receiptContainer.classList[0]||''):'',text:(e.textContent||e.getAttribute('aria-label')||'').trim().slice(0,60)}});
const focusable=interactiveElements.filter((e,i)=>interactiveTargets[i].visible&&!e.disabled&&!e.closest('[inert]'));
if(focusable[0])focusable[0].focus();
const focusEstablished=focusable.length===0||document.activeElement===focusable[0];
const inViewport=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.left>=-.5&&r.right<=innerWidth+.5};
const scrollClippedByTable=e=>{const region=e.closest('.c42-scroll');if(!region||!inViewport(region))return false;const s=getComputedStyle(region);return region.scrollWidth>region.clientWidth+1&&['auto','scroll'].includes(s.overflowX)};
const clippedSvgChild=e=>{const svg=e.closest('svg');return !!svg&&e!==svg&&inViewport(svg)};
const escaping=[...document.body.querySelectorAll('*')].filter(e=>{if(!visible(e))return false;const r=e.getBoundingClientRect();return(r.left<-.5||r.right>innerWidth+.5)&&!scrollClippedByTable(e)&&!clippedSvgChild(e)}).map(target);
const gate=window.__gateC||{console:['missing gate recorder'],requests:[],resourceFailures:[]};
return{authority:'42-production-build',surface,requestedWidth:${width},viewportWidth:innerWidth,clientWidth:document.documentElement.clientWidth,scrollWidth:Math.max(document.documentElement.scrollWidth,document.body.scrollWidth),surfacePresent:!!document.querySelector(required),compositionPresent,dataPresent:!expectedText||document.body.innerText.includes(expectedText),interactiveTargets,fixtureThumbnails,bodyText:document.body.innerText.slice(0,500),escaping,focusEstablished,focusScope:'document',errors:[...gate.console,...gate.resourceFailures],network:{requests:gate.requests,unfulfilled:gate.requests.filter(r=>!r.ok)},accessibility:{method:'visible-current-route-v1',scope:'visible current route DOM; critical and serious named rules',violations}}})()`;
}

async function renderedMatrix(){
  const chrome = chromePath();
  if (!chrome) throw new Error('Chrome executable is required for Gate C geometry');
  const root = mkdtempSync(join(tmpdir(), 'lp-instrument-gate-c-'));
  const frontendRoot = fileURLToPath(new URL('../../../', import.meta.url));
  const repositoryRoot = fileURLToPath(new URL('../../../../', import.meta.url));
  const serverScript = join(frontendRoot, 'scripts', 'gate-c-local-server.mjs');
  const productionRoot = join(repositoryRoot, 'web', 'dist');
  const port = 32000 + (process.pid % 1000);
  const server = spawn(process.execPath, [serverScript, productionRoot, String(port)], {
    cwd: repositoryRoot, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  const serverErrors = [];
  server.stderr.on('data', (chunk) => serverErrors.push(String(chunk)));
  const profile = join(root, 'chrome-profile');
  const chromeProcess = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
  ], {stdio: 'ignore', windowsHide: true});
  let client;
  try {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline){
      if (server.exitCode !== null) throw new Error(`Gate C server exited ${server.exitCode}: ${serverErrors.join('')}`);
      try { if ((await fetch(`http://127.0.0.1:${port}/`)).ok) break; } catch {}
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
    }
    client = await pageClient(await chromePort(profile), `http://127.0.0.1:${port}/`);
    await settleDocument(client);
    await client.command('Runtime.evaluate', {expression: `localStorage.setItem('pulse_passcode','gate-c-local-only')`});
    await client.command('Page.reload', {ignoreCache: true});
    await settleDocument(client);
    const results = [];
    const routes = [
      {surface: 'today', route: 'pulse', selector: '.page.t42', composition: '.t42-card', expectedText: '#fixture_za_step'},
      {surface: 'discover', route: 'explore', selector: '.d42', composition: '.d42-cell', expectedText: '#fixture_za_step'},
      {surface: 'compare', route: 'compare?mode=items&items=i_step,i_cola,i_ring&market=ZA&days=7', selector: '.c42', composition: '.c42-result', expectedText: 'Posts in the window'},
      {surface: 'build', route: 'console?work=brief&investigation=inv_gate_c&artifact=ra_gate_c&artifact_version=' + 'a'.repeat(64), selector: '.intelligence-console', composition: '.workbench', expectedText: 'Gate C local artifact'},
      {surface: 'fieldwork', route: 'fieldwork', selector: '.fieldwork-page', composition: '.fieldwork-body', expectedText: 'Source and research operations'},
    ];
    const selectedRoutes = process.env.GATE_C_SURFACE
      ? routes.filter(({surface}) => surface === process.env.GATE_C_SURFACE)
      : process.env.GATE_C_ONE_CELL ? routes.slice(0, 1) : routes;
    const selectedWidths = process.env.GATE_C_ONE_CELL ? WIDTHS.slice(0, 1) : WIDTHS;
    for (const route of selectedRoutes){
      for (const width of selectedWidths){
        await client.command('Emulation.setDeviceMetricsOverride', {width, height: 1600, deviceScaleFactor: 1, mobile: false});
        await client.command('Page.navigate', {url: `http://127.0.0.1:${port}/#/${route.route}`});
        await settleDocument(client);
        const evaluated = await client.command('Runtime.evaluate', {
          awaitPromise: true, returnByValue: true, expression: cellExpression(route, width),
        });
        if (evaluated.exceptionDetails) throw new Error(evaluated.exceptionDetails.exception?.description || evaluated.exceptionDetails.text);
        results.push(evaluated.result.value);
      }
    }
    return results;
  } finally {
    if (client) await client.dispose();
    if (chromeProcess.exitCode === null){ chromeProcess.kill(); await Promise.race([once(chromeProcess, 'exit'), wait(5000)]); }
    if (server.exitCode === null) server.kill();
    cleanupTemporaryPath(root);
  }
}

test('current 42 routes replace retired package 48px and Evidence Room drawer checks with live geometry at five widths', async () => {
  const results = await renderedMatrix();
  if (process.env.GATE_C_RESULTS_PATH) writeFileSync(process.env.GATE_C_RESULTS_PATH, `${JSON.stringify(results, null, 2)}\n`, 'utf8');
  const expectedCells = process.env.GATE_C_ONE_CELL
    ? 1
    : (process.env.GATE_C_SURFACE ? WIDTHS.length : CURRENT_ROUTE_SURFACES.length * WIDTHS.length);
  expect(results).toHaveLength(expectedCells);
  for (const result of results){
    expect(() => assertExactViewport(result), `${result.surface} viewport`).not.toThrow();
    expect(result.authority, `${result.surface} authority`).toBe('42-production-build');
    expect(result.surfacePresent, `${result.surface} current route surface: ${result.bodyText}; ${result.errors.join(' | ')}`).toBe(true);
    expect(result.compositionPresent, `${result.surface} composition at ${result.viewportWidth}`).toBe(true);
    expect(result.dataPresent, `${result.surface} fixture data at ${result.viewportWidth}: ${result.bodyText}`).toBe(true);
    expect(result.accessibility.method, `${result.surface} accessibility method`).toBe('visible-current-route-v1');
    expect(result.accessibility.violations, `${result.surface} accessibility violations`).toEqual([]);
    expect(result.network.unfulfilled, `${result.surface} unfulfilled requests`).toEqual([]);
    expect(result.scrollWidth, `${result.surface} horizontal overflow at ${result.viewportWidth}`).toBeLessThanOrEqual(result.clientWidth);
    expect(result.escaping, `${result.surface} escaping elements at ${result.viewportWidth}`).toEqual([]);
    expect(result.focusEstablished, `${result.surface} focus at ${result.viewportWidth}`).toBe(true);
    expect(result.errors, `${result.surface} console at ${result.viewportWidth}`).toEqual([]);
    if (result.surface === 'discover'){
      expect(result.fixtureThumbnails.length, `Discover fixture thumbnails at ${result.viewportWidth}`).toBeGreaterThan(0);
      expect(result.fixtureThumbnails.every(({src}) => src.includes('/__fixtures/thumbnail/')), `Discover fixture thumbnail source at ${result.viewportWidth}`).toBe(true);
      expect(result.fixtureThumbnails.filter(({complete, naturalWidth}) => complete && naturalWidth > 0), `decoded Discover fixture thumbnails at ${result.viewportWidth}`).toHaveLength(result.fixtureThumbnails.length);
    }
  }
}, 180000);
