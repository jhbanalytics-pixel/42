import {readFileSync} from 'node:fs';

export const MANIFEST_URL = new URL('../../../../docs/capability-journeys.json', import.meta.url);

export function readManifest(){
  return JSON.parse(readFileSync(MANIFEST_URL, 'utf8'));
}

/* The shell reads these on every route. A path outside the map is recorded
   and fails the test, so a 404 fixture reply can never pass as a render. */
export function shellFixtures(){
  return {
    '/api/health': {passcode: true},
    '/api/auth/verify': {ok: true},
    '/api/desk': {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}},
    '/api/research/recent': {artifacts: []},
    '/api/v2/investigations/list': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false},
    '/api/v2/investigations/scopes': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null},
    '/api/chat/coverage': questionCoverageFixture(),
  };
}

/* The Console rail and the Ask page read the dates a fresh question can be
   answered for, as the server does at /api/chat/coverage from the registry
   the question worker selects its source profile from. */
export function questionCoverageFixture(){
  return {contract_version: 'general_question_coverage_v1', state: 'covered', window: {start: '2026-08-25', end: '2026-09-07'}, cutoff_date: '2026-09-07'};
}

/* The Console reads the client lens roster when the live ask opens, as the
   server does at /api/chat/lenses. This is the roster a scope with no named
   lens receives: general 42 as the default and nothing else to choose. */
export function lensRosterFixture(){
  return {contract_version: 'client_lens_roster_v1', default_client_lens_id: null, client_scope_id: 'ogilvy_default', lenses: []};
}

/* A fixture value is the JSON body of a 200 reply, {__status, body} for a
   failure whose body is JSON, {__status, text, contentType} for a failure
   whose body is not JSON, {__abort: true} for a read that gets no answer at
   all, as when the network drops, or {__hold: true} for a read that never
   answers, which is how a route is held in its loading state. The passcode and region
   are the ones every browser journey stores before the first paint. */
export async function installFixtures(page, fixtures, {region = 'ZA'} = {}){
  const requested = [], unexpected = [], errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.addInitScript((storedRegion) => {
    localStorage.setItem('pulse_passcode', 'browser-fixture-only');
    localStorage.setItem('pulse-region', storedRegion);
  }, region);
  await page.route('**/api/**', async (route) => {
    /* A fixture keyed with a query string is matched on the full path and
       query, so a region-scoped read (`/api/desk?region=ng`) can be served its
       own market's payload rather than the pathname's default. A key with no
       query still matches on the pathname alone, as every route relies on. */
    const url = new URL(route.request().url());
    const full = url.pathname + url.search;
    const key = full in fixtures ? full : url.pathname in fixtures ? url.pathname : null;
    requested.push(key || url.pathname);
    if (!key){ unexpected.push(url.pathname); return route.fulfill({status: 404, json: {detail: 'Missing fixture'}}); }
    const reply = fixtures[key];
    if (reply && reply.__hold) return undefined;
    if (reply && reply.__abort) return route.abort('failed');
    if (reply && reply.__status && typeof reply.text === 'string'){
      return route.fulfill({status: reply.__status, contentType: reply.contentType, body: reply.text});
    }
    if (reply && reply.__status) return route.fulfill({status: reply.__status, json: reply.body});
    return route.fulfill({status: 200, json: reply});
  });
  return {requested, unexpected, errors};
}
