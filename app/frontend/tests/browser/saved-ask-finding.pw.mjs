import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';

const ORIGIN = 'http://127.0.0.1:4199';
const FIXTURE_PASSCODE = 'saved-ask-finding-fixture-only';
const readFixture = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'));
const savedAsk = readFixture('../../src/ui/__tests__/fixtures/ask42_complete.json');
savedAsk.run.notices = ['Instagram returned no posts during the selected window.'];
const historyFixture = readFixture('../../src/ui/__tests__/fixtures/history42.json');
const FINDING_ID = 'f_' + 'a'.repeat(64);
const scope = {market: savedAsk.market, window: savedAsk.run.window};
const sourceCaveats = savedAsk.run.source_status.filter((source) => source.status !== 'ok');
const uniqueStrings = (values) => [...new Set(values.filter((value) => typeof value === 'string' && value.length > 0))];
const sourceMarketCitation = {
  ...savedAsk.answer.evidence[0],
  id: 'tt_fixture_market_assumed',
  handle: '@fixture_za_assumed',
  url: 'https://example.invalid/tiktok/@fixture_za_assumed/video/7432',
  source_market: savedAsk.market,
  flags: ['market_assumed'],
};
savedAsk.answer.evidence.push(sourceMarketCitation);
savedAsk.answer.claims[0].evidence_ids.push(sourceMarketCitation.id);
const findingClaims = savedAsk.answer.claims.map((claim) => ({
  text: claim.text,
  label: claim.label,
  item_ids: uniqueStrings(claim.item_ids || []),
  evidence_post_ids: uniqueStrings(claim.evidence_ids || []),
  query_ids: uniqueStrings((claim.numbers || []).map((number) => number.query_id)),
  run_ids: uniqueStrings((claim.numbers || []).map((number) => number.run_id)),
  result_hashes: uniqueStrings((claim.numbers || []).map((number) => number.result_hash)),
}));
const findingAnswer = findingClaims.map((claim) => claim.text).join('\n');
const findingEvidenceIds = new Set(findingClaims.flatMap((claim) => claim.evidence_post_ids));
const findingEvidence = savedAsk.answer.evidence.filter((source) => findingEvidenceIds.has(source.id));
const savedContext = {
  schema_version: 'saved-ask-finding-v1',
  source_ask_id: savedAsk.ask_id,
  source_run_id: savedAsk.run.run_id,
  parent_id: savedAsk.parent_id ?? null,
  answer_status: savedAsk.answer.status,
  context: savedAsk.answer.context ?? null,
  market: savedAsk.market,
  window: savedAsk.run.window,
  notices: savedAsk.run.notices,
  gaps: savedAsk.answer.gaps,
  source_status: savedAsk.run.source_status,
};
const savedFinding = {
  finding_id: FINDING_ID,
  question: savedAsk.question,
  answer: findingAnswer,
  as_of: savedAsk.answer.as_of,
  status: 'current',
  claims: findingClaims,
  evidence: findingEvidence,
  saved_context: savedContext,
};

async function fulfill(route, entry, status, body){
  entry.status = status;
  entry.responseBody = body;
  await route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body),
  });
}

async function installFixtureBoundary(page, {findingStatus = 201, holdFindingResponse = false} = {}){
  const state = {
    api: [],
    external: [],
    unexpectedApi: [],
    blockedWrites: [],
    modelPosts: [],
    findingPosts: [],
    eventStreams: [],
    badCredentials: [],
    releaseFindingResponse: null,
  };
  const context = page.context();
  await page.addInitScript(({passcode}) => {
    localStorage.setItem('pulse_passcode', passcode);
    localStorage.setItem('pulse-region', 'ZA');
  }, {passcode: FIXTURE_PASSCODE});
  await context.routeWebSocket('**/*', (webSocket) => webSocket.close());
  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== ORIGIN){
      state.external.push({url: url.origin + url.pathname, action: 'aborted'});
      await route.abort('blockedbyclient');
      return;
    }
    if (!url.pathname.startsWith('/api/')){
      await route.continue();
      return;
    }

    const method = request.method().toUpperCase();
    const entry = {method, path: url.pathname, search: url.search, status: null, responseBody: null};
    state.api.push(entry);
    if (!['/api/health', '/api/auth/verify'].includes(url.pathname)
      && !request.headers()['x-passcode']) state.badCredentials.push(method + ' ' + url.pathname);

    if (url.pathname === '/api/health' && method === 'GET'){
      await fulfill(route, entry, 200, {ok: true, passcode: true, auth_mode: 'passcode', checks: {auth: 'ok'}});
      return;
    }
    if (url.pathname === '/api/auth/verify' && method === 'POST'){
      let body = null;
      try { body = request.postDataJSON(); } catch { body = null; }
      if (body?.passcode !== FIXTURE_PASSCODE) state.badCredentials.push('POST /api/auth/verify');
      await fulfill(route, entry, 200, {ok: true});
      return;
    }
    if (url.pathname === '/api/findings' && method === 'POST'){
      let body = null;
      try { body = request.postDataJSON(); } catch { body = null; }
      state.findingPosts.push({method, path: url.pathname, body, status: findingStatus});
      if (holdFindingResponse){
        await new Promise((resolve) => { state.releaseFindingResponse = resolve; });
      }
      await fulfill(
        route,
        entry,
        findingStatus,
        findingStatus < 300
          ? {finding_id: FINDING_ID, source_ask_id: savedAsk.ask_id}
          : {error: 'unavailable', message: 'The finding could not be saved.'},
      );
      return;
    }
    if (/^\/api\/ask\/[^/]+\/events$/.test(url.pathname)){
      state.eventStreams.push(method + ' ' + url.pathname);
      await fulfill(route, entry, 409, {error: 'fixture_stream_blocked', message: 'Ask event streams are blocked in this fixture.'});
      return;
    }
    if (url.pathname === '/api/ask' && method === 'POST'){
      state.modelPosts.push(method + ' ' + url.pathname);
      state.blockedWrites.push(method + ' ' + url.pathname);
      await fulfill(route, entry, 409, {error: 'fixture_write_blocked', message: 'Ask writes are blocked in this fixture.'});
      return;
    }
    if (method !== 'GET'){
      state.blockedWrites.push(method + ' ' + url.pathname);
      await fulfill(route, entry, 409, {error: 'fixture_write_blocked', message: 'Writes are blocked in this fixture.'});
      return;
    }
    if (url.pathname === '/api/ask/' + encodeURIComponent(savedAsk.ask_id)){
      await fulfill(route, entry, 200, savedAsk);
      return;
    }
    if (url.pathname === '/api/history/findings'){
      await fulfill(route, entry, 200, {findings: [savedFinding], note: null});
      return;
    }

    const readFixtures = {
      '/api/desk': {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}},
      '/api/research/recent': {artifacts: []},
      '/api/v2/investigations/list': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false},
      '/api/v2/investigations/scopes': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null},
      '/api/investigations': {investigations: []},
      '/api/schedules': {schedules: []},
      '/api/watches': {watches: []},
      '/api/chat/coverage': {contract_version: 'general_question_coverage_v1', state: 'covered', window: {start: '2026-08-25', end: '2026-09-07'}, cutoff_date: '2026-09-07'},
    };
    if (Object.hasOwn(readFixtures, url.pathname)){
      await fulfill(route, entry, 200, readFixtures[url.pathname]);
      return;
    }
    state.unexpectedApi.push(method + ' ' + url.pathname + url.search);
    await fulfill(route, entry, 404, {error: 'fixture_not_found', message: 'No local fixture matched.'});
  });
  return state;
}

async function expectFixtureBoundary(state){
  expect(state.unexpectedApi).toEqual([]);
  expect(state.badCredentials).toEqual([]);
  expect(state.blockedWrites).toEqual([]);
  expect(state.modelPosts).toEqual([]);
  expect(state.eventStreams).toEqual([]);
  expect(state.external.every((request) => request.action === 'aborted')).toBe(true);
  expect(state.api.filter((request) => request.method !== 'GET' && request.path !== '/api/auth/verify' && request.path !== '/api/findings'))
    .toEqual([]);
}

async function openBlockedSource(page, state, link, evidence){
  await expect(link).toHaveAttribute('href', evidence.url);
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', /noopener.*noreferrer/);
  const destination = new URL(evidence.url);
  const expectedRequest = destination.origin + destination.pathname;
  const popupPromise = page.waitForEvent('popup');
  await link.click();
  const popup = await popupPromise;
  await expect.poll(() => state.external.some((request) => request.url === expectedRequest && request.action === 'aborted'))
    .toBe(true);
  await popup.close();
}

async function openSavedAsk(page){
  await page.goto('/#/ask?follow=' + encodeURIComponent(savedAsk.ask_id));
  const main = page.locator('#main-content');
  const answer = main.getByRole('article').first();
  await expect(answer.getByRole('heading', {level: 2, name: savedAsk.question, exact: true})).toBeVisible();
  await expect(answer).toContainText(savedAsk.answer.short_answer);
  if (savedAsk.answer.gaps.length > 0) await expect(answer).toContainText(savedAsk.answer.gaps[0].what);
  return {main, answer};
}

test('a complete saved Ask can be saved to Findings and opened without another model request', async ({page}) => {
  const state = await installFixtureBoundary(page, {holdFindingResponse: true});
  const {main, answer} = await openSavedAsk(page);
  const save = await main.getByRole('button', {name: 'Save checked claims to Findings', exact: true}).elementHandle();
  expect(save).toBeTruthy();
  try {
    await save.click();
    await expect.poll(() => state.findingPosts.length).toBe(1);
    await expect.poll(() => save.getAttribute('aria-busy')).toBe('true');
    expect(await save.isDisabled()).toBe(true);
    await expect.poll(() => save.textContent()).toBe('Saving checked claims…');
    await expect(page).toHaveURL(/#\/ask\?follow=/);
    await expect(main.getByText('Checked claims saved to Findings', {exact: true})).toHaveCount(0);
    await expect(main.getByRole('link', {name: 'Open Findings', exact: true})).toHaveCount(0);
    state.releaseFindingResponse?.();

    const savedStatus = main.getByRole('status').filter({hasText: 'Checked claims saved to Findings'});
    await expect(savedStatus).toContainText('Checked claims saved to Findings');
    await expect(savedStatus).toContainText('Open Findings');
    const openFindings = main.getByRole('link', {name: 'Open Findings', exact: true});
    await expect(openFindings).toHaveAttribute('href', '#/history?tab=findings');
    expect(state.findingPosts).toEqual([{
      method: 'POST',
      path: '/api/findings',
      body: {from: {ask_id: savedAsk.ask_id}},
      status: 201,
    }]);
    expect(state.api.filter((request) => request.path === '/api/findings'))
      .toEqual([expect.objectContaining({
        method: 'POST',
        status: 201,
        responseBody: {finding_id: FINDING_ID, source_ask_id: savedAsk.ask_id},
      })]);
    await openFindings.click();
    await expect(page).toHaveURL(/#\/history\?tab=findings$/);

    const historyMain = page.locator('#main-content');
    await expect(historyMain.getByRole('heading', {level: 1, name: 'History', exact: true})).toBeVisible();
    const finding = historyMain.locator('[data-finding="' + FINDING_ID + '"]');
    await expect(finding.locator('.hi42-row-title')).toHaveText(savedAsk.question);
    const projectedAnswer = finding.locator('.hi42-answer');
    await expect(projectedAnswer).toHaveText(findingAnswer);
    await expect(projectedAnswer).not.toContainText(savedAsk.answer.short_answer);
    for (const implication of savedAsk.answer.so_what) {
      await expect(projectedAnswer).not.toContainText(implication.text);
    }
    expect(savedFinding.claims.length).toBeGreaterThanOrEqual(2);
    const locatedEvidence = savedFinding.evidence.filter((source) => source.market === scope.market && source.url && source.flags.length === 0);
    expect(locatedEvidence.length).toBeGreaterThanOrEqual(2);
    await expect(finding.locator('.hi42-claims > li')).toHaveCount(savedFinding.claims.length);
    await expect(finding.locator('.hi42-evidence > li')).toHaveCount(findingEvidence.length);
    for (const claim of findingClaims) await expect(finding).toContainText(claim.text);
    const contextProjection = finding.locator('[data-saved-context]');
    for (const gap of savedContext.gaps) await expect(contextProjection).toContainText(gap.what);
    for (const notice of savedContext.notices) await expect(contextProjection).toContainText(notice);
    await expect(contextProjection).toContainText('South Africa (ZA)');
    await expect(contextProjection).toContainText('21 September 2026');
    await expect(contextProjection).toContainText('27 September 2026');
    await expect(contextProjection).toContainText('Not recorded');
    for (const source of sourceCaveats){
      await expect(contextProjection).toContainText(source.platform);
      await expect(contextProjection).toContainText(source.status);
    }
    for (const source of findingEvidence) await expect(finding.locator('[data-evidence="' + source.id + '"]')).toContainText(source.text);
    const assumedEvidence = finding.locator('[data-evidence="' + sourceMarketCitation.id + '"]');
    await expect(assumedEvidence).toContainText('Recorded source market: South Africa');
    await expect(assumedEvidence).toContainText('Market assumed');
    await expect(historyMain.locator('[data-section="findings"]')).not.toContainText(historyFixture.findings.note);
    expect(Object.keys(savedFinding).sort()).toEqual(['answer', 'as_of', 'claims', 'evidence', 'finding_id', 'question', 'saved_context', 'status']);
    expect(Object.keys(savedContext).sort()).toEqual(['answer_status', 'context', 'gaps', 'market', 'notices', 'parent_id', 'schema_version', 'source_ask_id', 'source_run_id', 'source_status', 'window']);
    for (const claim of findingClaims){
      expect(Object.keys(claim).sort()).toEqual(['evidence_post_ids', 'item_ids', 'label', 'query_ids', 'result_hashes', 'run_ids', 'text']);
    }
    expect(savedFinding.status).toBe('current');
    expect(savedFinding.claims.length).toBeGreaterThanOrEqual(2);
    expect(savedFinding.evidence.filter((source) => source.market === scope.market && source.url && source.flags.length === 0).length)
      .toBeGreaterThanOrEqual(2);
    const evidence = savedAsk.answer.evidence[0];
    const evidenceRow = finding.locator('[data-evidence="' + evidence.id + '"]');
    await expect(evidenceRow).toContainText(evidence.text);
    await openBlockedSource(page, state, evidenceRow.getByRole('link', {name: 'Open the post', exact: true}), evidence);

    expect(state.api.filter((request) => request.path === '/api/ask/' + savedAsk.ask_id))
      .toEqual([expect.objectContaining({method: 'GET', status: 200, responseBody: savedAsk})]);
    expect(state.api.filter((request) => request.path === '/api/history/findings'))
      .toEqual([expect.objectContaining({method: 'GET', status: 200})]);
    const stored = state.api.find((request) => request.path === '/api/history/findings').responseBody.findings[0];
    expect(stored.answer).toBe(findingAnswer);
    expect(stored.saved_context).toEqual(savedContext);
    expect(stored.evidence).toEqual(findingEvidence);
    expect(stored.evidence.find((source) => source.id === sourceMarketCitation.id)).toEqual(sourceMarketCitation);
    await expectFixtureBoundary(state);
  } finally {
    state.releaseFindingResponse?.();
  }
});

test('a failed Finding save does not claim success or open Findings', async ({page}) => {
  const state = await installFixtureBoundary(page, {findingStatus: 503});
  const {main} = await openSavedAsk(page);
  await main.getByRole('button', {name: 'Save checked claims to Findings', exact: true}).click();
  await expect.poll(() => state.findingPosts.length).toBe(1);
  await page.waitForTimeout(1000);

  expect(state.findingPosts).toEqual([{
    method: 'POST',
    path: '/api/findings',
    body: {from: {ask_id: savedAsk.ask_id}},
    status: 503,
  }]);
  await expect(main.getByText('Checked claims saved to Findings', {exact: true})).toHaveCount(0);
  await expect(main.getByText('Saving checked claims…', {exact: true})).toHaveCount(0);
  await expect(main.getByRole('link', {name: 'Open Findings', exact: true})).toHaveCount(0);
  expect(state.api.filter((request) => request.path === '/api/history/findings')).toEqual([]);
  await expectFixtureBoundary(state);
});
