import {expect, test} from 'bun:test';
import {existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {EvidenceRoomView, evidenceRoomArtifactRead, resolveEvidenceRoomView} from '../../evidenceRoom.jsx';
import * as researchPresentation from '../../ResearchDocPanel.jsx';
import {buildWorkbenchHash, parseWorkbenchRoute} from '../../workbenchRoute.js';

const status = {
  investigation_id: 'inv_case',
  status: 'plan_ready',
  research_plan: {questions: ['What changed?'], required_source_families: ['social', 'search'], known_gaps: ['history_missing']},
  missing_work: ['history_missing'],
  updated_at: '2026-08-28T08:00:00Z',
};

test('canonical workbench URLs preserve investigation and artifact identity', () => {
  const hash = buildWorkbenchHash({work: 'brief', investigationId: 'inv_case', artifactId: 'ra_doc'});
  expect(hash).toBe('#/console?work=brief&investigation=inv_case&artifact=ra_doc');
  expect(parseWorkbenchRoute(hash)).toMatchObject({investigationId: 'inv_case', artifactId: 'ra_doc'});
});

test('Evidence Room renders plan-ready empty without a fabricated claim', () => {
  const markup = renderToStaticMarkup(<EvidenceRoomView state="empty" status={status} claims={[]} decision={null} />);
  expect(markup).toContain('What changed?');
  expect(markup).toContain('No claims gathered yet');
  expect(markup).toContain('History missing');
  expect(markup).not.toMatch(/approved claim|recommended action/i);
});

/* Task 62. Round 14 read two machine tokens on this page with no Details above
   them: the investigation id `inv_gate_c` in the page lead on 24 of 189 cells,
   and the known gap `closed_evidence` in the missing work list on 12. They are
   closed two different ways. A gap is an enumerated value, so it is written out.
   An id is evidence a reader cites and is worth nothing reworded, so it moves
   into the disclosure the copy law rules for it and stays exactly as issued. */
const surfaceOf = (markup) => markup
  .replace(/<details[\s\S]*?<\/details>/g, '')
  .replace(/<[^>]*>/g, ' ')
  .replace(/\s+/g, ' ')
  .trim();

test('the investigation id is inside a disclosure and nowhere else', () => {
  const markup = renderToStaticMarkup(<EvidenceRoomView state="empty" status={status} claims={[]} decision={null} />);

  expect(markup).toContain('<summary>Investigation reference</summary>');
  expect(markup).toContain('inv_case');
  expect(surfaceOf(markup)).not.toContain('inv_case');
});

test('a known gap reads as words on the surface, not as the token the host sent', () => {
  const gapped = {...status, missing_work: ['closed_evidence', 'second_family_unread']};
  const markup = renderToStaticMarkup(<EvidenceRoomView state="empty" status={gapped} claims={[]} decision={null} />);

  expect(markup).toContain('Evidence from the closed window');
  expect(markup).toContain('Second family unread');
  expect(markup).not.toContain('closed_evidence');
  expect(markup).not.toContain('second_family_unread');
});

test('no machine token reaches the evidence room surface outside a disclosure', () => {
  const gapped = {...status, missing_work: ['closed_evidence']};
  const markup = renderToStaticMarkup(<EvidenceRoomView state="empty" status={gapped} claims={[]} decision={null} />);

  expect(surfaceOf(markup)).not.toMatch(/\b[a-z0-9]+_[a-z0-9_]+\b/);
});

test('package Evidence Room fails closed for malformed citation authority', () => {
  expect(typeof researchPresentation.ResearchEvidenceRoom).toBe('function');
  const ResearchEvidenceRoom = researchPresentation.ResearchEvidenceRoom;
  const markup = renderToStaticMarkup(
    <ResearchEvidenceRoom
      open
      title="Citation review"
      evidenceSummary={{state: 'ready', receipts: [{id: 'ev_1'}]}}
      ribbonModel={{state: 'ready', strands: []}}
      onClose={() => {}}
    />,
  );
  /* Quiet register, 23 Sept 2026: package 2.0.20 writes the evidence state as
     the readiness word it shows beside it, "Unchecked", because no text is
     cased through CSS any more. The state it reports is still unchecked. */
  expect(markup).toContain('Evidence state: Unchecked');
  /* copy-register.json, EvidenceLedger.jsx and EvidenceRoom.jsx: "Evidence
     authority is unchecked." became "Nobody has checked these sources yet."
     Same claim, no status annotation, approved. */
  expect(markup).toContain('Nobody has checked these sources yet.');
});

test('current citation adapter converts hostile authority into explicit unchecked evidence', () => {
  const source = {id: 'post_1', ref_type: 'post', text: 'Hostile citation'};
  Object.defineProperty(source, 'url', {get(){ throw new Error('hostile authority'); }});
  expect(() => researchPresentation.adaptResearchCitation(source, 1)).not.toThrow();
  const model = researchPresentation.adaptResearchCitation(source, 1);
  expect(model.evidenceSummary.state).toBe('unchecked');
  expect(model.selectedReceiptId).toBeNull();
});

test('current citation adapter converts an impossible publication date into unchecked evidence', () => {
  const source = {id: 'post_1', ref_type: 'post', text: 'Impossible date',
    url: 'https://example.test/post_1', receipt: {published_at: '2026-99-99T09:00:00Z'}};
  expect(() => researchPresentation.adaptResearchCitation(source, 1)).not.toThrow();
  expect(researchPresentation.adaptResearchCitation(source, 1).evidenceSummary.state).toBe('unchecked');
});

test('Console and research composition are owned by package surfaces', () => {
  const consoleSource = readFileSync(new URL('../../ConsoleWorkbench.jsx', import.meta.url), 'utf8');
  const researchSource = readFileSync(new URL('../../ResearchDocPanel.jsx', import.meta.url), 'utf8');
  expect(consoleSource).toContain("import {IntelligenceConsole} from 'ogilvy-intelligence-design-system'");
  expect(consoleSource).toContain('<IntelligenceConsole');
  expect(researchSource).toContain('<ResearchEvidenceRoom');
});

function resolveChrome(){
  return [process.env.CHROME_PATH, process.env.CHROME_BIN,
    String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
    String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
  ].filter(Boolean).find((candidate) => existsSync(candidate)) || null;
}

test.skipIf(!resolveChrome())('mounted ResearchPage opens a normal current citation in package Evidence Room', async () => {
  const root = fileURLToPath(new URL('../../../../', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'lp-research-evidence-room-'));
  const bundlePath = join(directory, 'probe.js');
  const htmlPath = join(directory, 'probe.html');
  const profilePath = join(directory, 'chrome-profile');
  const entry = `
    import React from 'react';
    import {createRoot} from 'react-dom/client';
    import {ResearchPage} from './frontend/src/ResearchDocPanel.jsx';
    const response = (body) => Promise.resolve({ok:true,status:200,url:'',headers:{get:()=>null},json:()=>Promise.resolve(body)});
    window.fetch = (url) => String(url).includes('/api/research/personas')
      ? response({personas:[{id:'strategy',label:'Strategy',default_markets:['za'],query_groups:[]}]})
      : response({artifact_id:'art_1',persona_id:'strategy',markets:['za'],signal_quality:'strong',doc:{
          json:{title:'Current research',grounded:{}},markdown:'',sources:[{
            id:'post_1',ref_type:'post',market:'za',platform:'reddit',text:'Repair tutorials recur.',
            url:'https://example.test/post_1',evidence_tier:2,
            receipt:{platform:'reddit',handle:'maker',published_at:'2026-08-20T09:00:00Z'},
            bq_trace:{table:'enriched_content',trend_date:'2026-08-20'}
          }]}});
    const wait = (ms=25) => new Promise((resolve) => setTimeout(resolve, ms));
    const encode = (value) => btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(value))));
    (async () => {
      const root = createRoot(document.getElementById('root'));
      root.render(React.createElement(ResearchPage, {routeState:{work:'brief',artifactId:'art_1',personaId:'strategy',markets:['za']}}));
      const deadline = Date.now() + 10000;
      let citation = null;
      while (Date.now() < deadline && !citation){ citation = document.querySelector('.research-evidence-card'); await wait(); }
      if (!citation) throw new Error('current citation did not render');
      citation.click();
      let room = null;
      while (Date.now() < deadline && !room){ room = document.querySelector('.evidence-room'); await wait(); }
      document.getElementById('result').textContent = encode({
        packageRooms: document.querySelectorAll('.evidence-room').length,
        legacyDrawers: document.querySelectorAll('.research-citation-panel').length,
        text: room ? room.textContent : '',
      });
    })().catch((error) => { document.getElementById('result').textContent = encode({error:String(error.stack || error)}); });
  `;
  try {
    await build({stdin:{contents:entry,loader:'jsx',resolveDir:root,sourcefile:'research-evidence-room.jsx'},
      bundle:true,format:'iife',jsx:'automatic',platform:'browser',outfile:bundlePath,
      define:{'process.env.NODE_ENV':'"production"'},nodePaths:[join(root,'frontend','node_modules')],
      plugins:[{name:'empty-css',setup(builder){builder.onResolve({filter:/\.css$/},()=>({path:'empty',namespace:'css'}));builder.onLoad({filter:/.*/,namespace:'css'},()=>({contents:'',loader:'js'}));}}]});
    writeFileSync(htmlPath, '<!doctype html><div id="root"></div><pre id="result">pending</pre><script src="./probe.js"></script>', 'utf8');
    const run = spawnSync(resolveChrome(), ['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
      '--virtual-time-budget=12000','--dump-dom',`--user-data-dir=${profilePath}`,pathToFileURL(htmlPath).href],
    {encoding:'utf8',timeout:25000,windowsHide:true});
    expect(run.status).toBe(0);
    const encoded = run.stdout.match(/<pre id="result">([^<]+)<\/pre>/)?.[1];
    expect(encoded).toBeTruthy();
    const measured = JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'));
    expect(measured.error).toBeUndefined();
    expect(measured.packageRooms).toBe(1);
    expect(measured.legacyDrawers).toBe(0);
    expect(measured.text).toContain('Repair tutorials recur.');
  } finally {
    rmSync(directory, {recursive:true,force:true});
  }
}, 60000);

test('Evidence Room keeps thin, contradictory and unchecked claims distinct', () => {
  for (const evidenceState of ['thin', 'contradictory', 'unchecked']) {
    const markup = renderToStaticMarkup(<EvidenceRoomView state={evidenceState} status={status} claims={[{
      claim_id: 'clm_1', claim_text: 'A bounded claim.', evidence_state: evidenceState,
      supporting_evidence_ids: ['ev_1'], opposing_evidence_ids: evidenceState === 'contradictory' ? ['ev_2'] : [],
      missing_evidence: ['second_family'], assumptions: [], confidence: null, confidence_reason: 'Unavailable', human_review_required: true,
    }]} decision={null} />);
    expect(markup).toContain(evidenceState);
    expect(markup).toContain('A bounded claim.');
    if (evidenceState === 'contradictory') expect(markup).toContain('Challenge evidence');
    expect(markup).not.toContain('Client ready');
  }
});

test('stale, error and authentication states show no prior content', () => {
  for (const state of ['stale', 'error', 'auth']) {
    const markup = renderToStaticMarkup(<EvidenceRoomView state={state} error={{code: state}} />);
    expect(markup).toContain('Evidence Room');
    expect(markup).not.toContain('A bounded claim.');
  }
});

test('record age alone cannot turn a validated workspace response stale', () => {
  const oldStatus = {...status, updated_at: '2020-01-01T00:00:00Z'};
  expect(resolveEvidenceRoomView(oldStatus, {claims: []}, {decision: null}, null)).toMatchObject({
    state: 'empty',
    status: oldStatus,
    claims: [],
  });
});

test('Evidence Room source has no local scope storage or legacy artifact fallback', async () => {
  const source = await Bun.file(new URL('../../evidenceRoom.jsx', import.meta.url)).text();
  expect(source).not.toMatch(/localStorage|sessionStorage/);
  expect(source).not.toContain('/api/research/');
  expect(source).toContain('/claims/read');
  expect(source).toContain('/decision/read');
  expect(source).toContain('/artifacts/');
});

test('Evidence Room shell keeps every surrounding Console action at least 48 pixels', async () => {
  const appCss = await Bun.file(new URL('../../app.css', import.meta.url)).text();
  const uiCss = await Bun.file(new URL('../ui.css', import.meta.url)).text();
  expect(appCss).toMatch(/\.workbench-back\s*\{[^}]*min-height:\s*48px/s);
  expect(appCss).toMatch(/\.workbench-mode-tabs button,[\s\S]*?\.workbench-mobile-switch button\s*\{[^}]*min-height:\s*48px/s);
  expect(appCss).toMatch(/\.workbench-new-brief\s*\{[^}]*min-height:\s*48px/s);
  expect(uiCss).toMatch(/\.ui-flowbar-cta\s*\{[^}]*min-height:\s*48px/s);
});

/* The Contradiction is a named 42 moment: the product actively surfaces the
   evidence that weakens the leading interpretation. Evidence that only appears
   after a reader expands a disclosure triangle is not surfaced. It is
   available, which is a different and much weaker promise. */

const contradictedStatus = () => ({
  investigation_id: 'inv_case',
  research_plan: {questions: ['What is changing in weekend repair?'], required_source_families: ['reddit']},
  missing_work: [],
});

const contradictedClaims = () => ({
  claims: [
    {
      claim_id: 'clm_1',
      claim_text: 'Repair tutorials are spreading through weekend routines.',
      evidence_state: 'contradictory',
      confidence: null,
      supporting_evidence_ids: ['ev_support'],
      opposing_evidence_ids: ['ev_challenge'],
      missing_evidence: [],
    },
  ],
});

const renderRoom = () =>
  renderToStaticMarkup(
    <EvidenceRoomView
      {...resolveEvidenceRoomView(contradictedStatus(), contradictedClaims(), null, null)}
    />,
  );

test('challenge evidence is surfaced, not hidden behind a disclosure', () => {
  const markup = renderRoom();
  const at = markup.indexOf('ev_challenge');
  expect(at).toBeGreaterThan(-1);
  // Everything before the challenge id must contain no unclosed details
  // element, or the reader has to go looking for what weakens the claim.
  const before = markup.slice(0, at);
  const opened = (before.match(/<details/g) || []).length;
  const closed = (before.match(/<\/details>/g) || []).length;
  expect(opened).toBe(closed);
});

test('a contradicted claim says so in words next to its text', () => {
  const markup = renderRoom();
  const at = markup.indexOf('Repair tutorials are spreading');
  const window = markup.slice(Math.max(0, at - 400), at + 400);
  expect(window.toLowerCase()).toContain('contradictory');
});

test('the claim that is contradicted names what opposes it, not just a count', () => {
  const markup = renderRoom();
  expect(markup).toContain('ev_challenge');
  expect(markup.toLowerCase()).toContain('challenge');
});

test('with mixed claims the worst readiness wins, never the most common', () => {
  /* One contradicted claim among many ready ones still means the room is
     contradicted. Averaging or majority would let a single disagreement be
     outvoted, which is precisely the disagreement worth surfacing. */
  const mixed = (states) => ({
    claims: states.map((state, index) => ({
      claim_id: `clm_${index}`,
      claim_text: `Claim ${index}`,
      evidence_state: state,
      confidence: null,
      supporting_evidence_ids: ['ev_support'],
      opposing_evidence_ids: state === 'contradictory' ? ['ev_challenge'] : [],
      missing_evidence: [],
    })),
  });
  const stateOf = (states) =>
    resolveEvidenceRoomView(contradictedStatus(), mixed(states), null, null).state;

  expect(stateOf(['ready', 'ready', 'ready', 'contradictory'])).toBe('contradictory');
  expect(stateOf(['ready', 'thin', 'contradictory'])).toBe('contradictory');
  expect(stateOf(['ready', 'unchecked', 'thin'])).toBe('thin');
  expect(stateOf(['ready', 'unchecked'])).toBe('unchecked');
  expect(stateOf(['ready', 'ready'])).toBe('ready');
});

test('every class the evidence room renders has a rule', () => {
  /* The contradiction was lifted out of the disclosure so a reader meets it.
     A class with no rule undoes that: it renders, and it reads as nothing. */
  const markup = renderRoom();
  const sheets = ['workspaces.css', 'console.css']
    .map((name) => {
      try {
        return readFileSync(new URL(`../../styles/${name}`, import.meta.url), 'utf8');
      } catch {
        return '';
      }
    })
    .join('\n');
  const classes = new Set();
  for (const m of markup.matchAll(/class="([^"]+)"/g)){
    m[1].split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
  }
  const missing = [...classes].filter((c) => !sheets.includes(`.${c}`));
  expect(missing).toEqual([]);
});

test('challenge evidence is set apart, not styled as ordinary body text', () => {
  const console_css = readFileSync(new URL('../../styles/console.css', import.meta.url), 'utf8');
  const block = console_css.split('}').find((b) => b.includes('.workspace-challenge'));
  expect(block).toBeTruthy();
  expect(block).toMatch(/color|border|background/);
});

test('an unreadable claim resource is not an investigation with no claims', () => {
  /* Empty is a fact about the investigation: it has been opened and nothing
     has been claimed yet. A claims resource that arrives without its list is a
     fact about the read, and reporting it as empty states something about the
     investigation that was never established. */
  for (const broken of [null, undefined, {}, {claims: null}, {claims: 'none'}, {claims: 42}]) {
    const view = resolveEvidenceRoomView(contradictedStatus(), broken, null, null);
    expect(view.state).toBe('error');
  }
});

test('a genuinely empty claim list is still reported as empty', () => {
  const view = resolveEvidenceRoomView(contradictedStatus(), {claims: []}, null, null);
  expect(view.state).toBe('empty');
});

/* Console recents. This lives here because the Console contract allowlists
   this test file and not a Console-specific one; the subject is the Console
   landing, not the Evidence Room. */
test('recent briefs are not reported as none before they have been read', async () => {
  const {recentsState} = await import('../../ConsoleWorkbench.jsx');
  /* The landing fetched immediately from an empty array with no loading flag,
     so on first paint it stated there were no saved briefs in this session.
     That is a claim about the session made before looking at it. */
  expect(recentsState({loaded: false, error: '', items: []})).toBe('loading');
  expect(recentsState({loaded: true, error: '', items: []})).toBe('empty');
  expect(recentsState({loaded: true, error: 'Recent briefs unavailable.', items: []})).toBe('error');
  expect(recentsState({loaded: true, error: '', items: [{id: 'ra_1'}]})).toBe('ready');
});

test('a failed recents read is unavailable, never an empty session', async () => {
  const {recentsState} = await import('../../ConsoleWorkbench.jsx');
  expect(recentsState({loaded: false, error: 'boom', items: []})).toBe('error');
});

test('an unsupported contract says so, rather than showing a bare code', () => {
  /* The audit recorded this as partial: the generic error view displayed the
     code and nothing else. A reader who is told `contract_version_unsupported`
     is being handed engine vocabulary and asked to translate it. */
  const markup = renderToStaticMarkup(
    <EvidenceRoomView state="unsupported_contract" status={null} error={{code: 'contract_version_unsupported'}} />
  );
  const visible = markup.replace(/<[^>]*>/g, ' ');
  expect(visible).toMatch(/version this desk cannot read|unsupported version/i);
  expect(visible).not.toMatch(/[a-z]+_[a-z]+/);
});

test('an unsupported contract shows no investigation content', () => {
  const markup = renderToStaticMarkup(
    <EvidenceRoomView
      state="unsupported_contract"
      status={contradictedStatus()}
      claims={[{claim_id: 'c1', claim_text: 'A claim that must not leak', evidence_state: 'ready'}]}
      error={{code: 'contract_version_unsupported'}}
    />
  );
  expect(markup).not.toContain('A claim that must not leak');
});

test('an unsupported version withholds its claims at the resolver, not just the view', () => {
  /* The view test alone let a mutation through: it rendered the view directly
     and never exercised the resolver that decides what the view receives. */
  const view = resolveEvidenceRoomView(
    {...contradictedStatus(), contract_version_unsupported: true},
    {claims: [{claim_id: 'c1', claim_text: 'A claim that must not leak', evidence_state: 'ready'}]},
    {decision: {state: 'approved'}},
    {artifact: {artifact_id: 'ra_1'}},
  );
  expect(view.state).toBe('unsupported_contract');
  expect(view.claims).toEqual([]);
  expect(view.decision).toBeNull();
  expect(view.artifact).toBeNull();
  expect(JSON.stringify(view)).not.toContain('must not leak');
});

/* Quiet register, 23 Sept 2026: the product writes decimals with a point
   (Ask's US$0.10, the 1.2k counts), so a confidence keeps its point and a
   workspace figure never switches to a decimal comma beside it. */
test('a stated confidence keeps the decimal point the product uses everywhere', async () => {
  const {readerFigure} = await import('../../api.js');
  expect(readerFigure(1.5)).toBe('1.5');
  expect(readerFigure('0.72')).toBe('0.72');
  expect(readerFigure(25000.25)).toBe('25 000.25');
  const markup = renderToStaticMarkup(<EvidenceRoomView state="ready" status={status} claims={[{
    claim_id: 'clm_1', claim_text: 'A bounded claim.', evidence_state: 'ready', confidence: 0.72,
    supporting_evidence_ids: [], opposing_evidence_ids: [], missing_evidence: [],
  }]} decision={null} />);
  expect(markup).toContain('Confidence 0.72');
  expect(markup).not.toContain('0,72');
});

/* The browser round trip found the Evidence Room reading an approved artifact
   without its exact version. The server refuses that read with 404 every
   time, so a Client Read open beside it always showed the artifact as
   unavailable and logged a failed request. The read now names the exact
   version the link carries, and a link without one is refused on the page
   without a request the server could only refuse. */
test('Evidence Room reads an artifact at the exact version the link names', () => {
  const version = 'a'.repeat(64);
  expect(evidenceRoomArtifactRead('inv_case', 'art_0123456789abcdef', version)).toEqual({
    path: `/api/v2/investigations/inv_case/artifacts/art_0123456789abcdef/read?artifact_version=${version}`,
  });
  expect(evidenceRoomArtifactRead('inv case', 'art/x', version).path).toBe(
    `/api/v2/investigations/inv%20case/artifacts/art%2Fx/read?artifact_version=${version}`,
  );
});

test('Evidence Room sends no artifact read without an exact version', () => {
  for (const version of [undefined, null, '', 'latest']){
    expect(evidenceRoomArtifactRead('inv_case', 'art_0123456789abcdef', version)).toEqual({
      path: null, error: {code: 'workspace_request_invalid', origin: 'derived'},
    });
  }
  expect(evidenceRoomArtifactRead('inv_case', null, 'a'.repeat(64))).toEqual({path: null, error: null});
});
