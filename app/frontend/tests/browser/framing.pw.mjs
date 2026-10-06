import {expect, test} from '@playwright/test';
import {questionCoverageFixture} from './support/capability-harness.mjs';

const question = 'Which changes in repair culture should a general retailer investigate?';

async function mockWorkspace(page, {holdFirstIndex = false} = {}){
  const writes = [];
  const unknown = [];
  const records = [];
  const buildWrites = [];
  const buildReads = [];
  let indexReads = 0;
  let releaseInitialIndex;
  const initialIndexRelease = new Promise((resolve) => { releaseInitialIndex = resolve; });
  await page.addInitScript(() => {
    localStorage.setItem('pulse_passcode', 'browser-fixture-only');
    localStorage.setItem('pulse-region', 'ZA');
  });
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let json;
    if (path === '/api/health') json = {passcode: true};
    else if (path === '/api/auth/verify') json = {ok: true};
    else if (path === '/api/desk') json = {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}};
    else if (path === '/api/research/recent') json = {artifacts: []};
    else if (path === '/api/research/personas') json = {personas: []};
    else if (path === '/api/research/behaviours') json = {behaviours: []};
    else if (path === '/api/research/availability') json = {brief_writing: {available: true, code: null, message: null}, behaviour_scan: {available: true, code: null, message: null}};
    else if (path === '/api/chat/coverage') json = questionCoverageFixture();
    else if (path === '/api/v2/investigations/scopes') json = {
      contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1',
      default_scope_id: 'ogilvy_default', scopes: [{
        client_scope_id: 'ogilvy_default', market_scope: ['za', 'ng', 'ke'],
        market_labels: ['South Africa', 'Nigeria', 'Kenya'], brand_config_id: null,
        audience_lens_ids: [], theme_id: null, latest_run_id: 'run_browser',
      }],
    };
    else if (path === '/api/v2/investigations/list'){
      const snapshot = [...records];
      indexReads += 1;
      if (indexReads === 1 && holdFirstIndex) await initialIndexRelease;
      json = {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: snapshot, total_count: snapshot.length, truncated: false};
    } else if (path === '/api/v2/investigations' && request.method() === 'POST'){
      const body = request.postDataJSON();
      writes.push(body);
      await new Promise((resolve) => setTimeout(resolve, 300));
      records.push({investigation_id: 'inv_browser', decision_question: body.decision_question, market_labels: ['South Africa'], created_at: '2026-09-06T10:00:00Z', status: 'plan_ready', readiness_state: 'blocked', candidate_artifact_id: null});
      json = {...body, investigation_id: 'inv_browser', status: 'plan_ready', research_plan: {questions: [question]}};
    } else if (path === '/api/investigations' && request.method() === 'POST'){
      const body = request.postDataJSON();
      buildWrites.push(body);
      await new Promise((resolve) => setTimeout(resolve, 300));
      json = {investigation_id: 'inv_browser', question: body.question, market: body.market, status: 'planned'};
    } else if (request.method() === 'POST' && path.startsWith('/api/investigations/')){
      buildWrites.push({path});
      json = {ok: true};
    } else if (path === '/api/investigations' || path === '/api/dossiers' || path === '/api/history/asks' || path === '/api/history/briefs' || path === '/api/schedules' || path === '/api/investigations/inv_browser'){
      buildReads.push(path);
      await route.fulfill({status: 503, json: {detail: 'Not in this fixture'}});
      return;
    } else if (path.endsWith('/status')) json = {contract_version: 'intelligence_dossier_v1', investigation_id: 'inv_browser', research_plan: {questions: [question]}, missing_work: ['evidence']};
    else if (path.endsWith('/claims/read')) json = {claims: []};
    else if (path.endsWith('/decision/read')) json = {decision: null};
    else {
      unknown.push(path);
      await route.fulfill({status: 404, json: {detail: 'Unmatched browser fixture endpoint'}});
      return;
    }
    await route.fulfill({status: 200, json});
  });
  return {writes, unknown, buildWrites, buildReads, indexReads: () => indexReads, releaseInitialIndex};
}

async function frame(page){
  await page.goto('/#/console?work=brief');
  await page.getByLabel(/what decision does this inform/i).fill(question);
  await page.getByRole('checkbox', {name: 'South Africa', exact: true}).check();
  await page.getByRole('button', {name: 'Frame this investigation', exact: true}).dblclick();
  await expect(page).toHaveURL(/investigation=inv_browser/);
}

/* Page port, 3 October 2026: #/console?work=brief no longer opens the older
   framing workbench; it lands on Build (src/legacyRoutes.js), whose "Start an
   investigation" form posts {question, market} to /api/investigations
   (contract section 13.1) and opens #/investigations/<id>. The framing
   journeys below now drive that form with the same holds: one write for a
   double submit, scoped to the chosen market, no generation started, and a
   fresh empty form on return. */
async function draftOnBuild(page){
  await page.goto('/#/console?work=brief');
  await expect(page).toHaveURL(/#\/console$/);
  await page.getByLabel('What should 42 find out?', {exact: true}).fill(question);
  await page.locator('#b42-inv-market').selectOption('ZA');
  await page.getByRole('button', {name: 'Draft a plan', exact: true}).dblclick();
  await expect(page).toHaveURL(/#\/investigations\/inv_browser$/);
}

/* Was: a double submit creates one scoped investigation without starting generation. */
test('a double submit on Build drafts one investigation scoped to its market without starting it', async ({page}) => {
  const state = await mockWorkspace(page);
  await draftOnBuild(page);
  expect(state.buildWrites).toEqual([{question, market: 'ZA'}]);
  expect(state.writes).toEqual([]);
  expect(state.unknown).toEqual([]);
});

test('returning to the landing shows the new investigation despite an older pending index read', async ({page}) => {
  const state = await mockWorkspace(page, {holdFirstIndex: true});
  try {
    await frame(page);
    await page.evaluate(() => { location.hash = '#/console'; });
    await expect(page.getByText(question, {exact: true})).toBeVisible();
    const oldResponse = page.waitForResponse((response) => response.url().includes('/api/v2/investigations/list'));
    state.releaseInitialIndex();
    await oldResponse;
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(page.getByText(question, {exact: true})).toBeVisible();
    expect(state.indexReads()).toBeGreaterThan(1);
    expect(state.unknown).toEqual([]);
  } finally { state.releaseInitialIndex(); }
});

/* Was: New brief clears the previous investigation and opens a fresh form. */
test('returning to Build after a draft opens a fresh empty form with no second write', async ({page}) => {
  const state = await mockWorkspace(page);
  await draftOnBuild(page);
  await page.evaluate(() => { location.hash = '#/console?work=brief'; });
  await expect(page).toHaveURL(/#\/console$/);
  await expect(page.getByLabel('What should 42 find out?', {exact: true})).toHaveValue('');
  await expect(page.getByRole('button', {name: 'Draft a plan', exact: true})).toBeDisabled();
  expect(state.buildWrites).toEqual([{question, market: 'ZA'}]);
  expect(state.writes).toEqual([]);
  expect(state.unknown).toEqual([]);
});
