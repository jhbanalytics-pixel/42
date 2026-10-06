import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';
const plan = JSON.parse(readFileSync(new URL('../../../tests/fixtures/workspaces/investigation_plan_v1.json', import.meta.url), 'utf8'));

async function setup(page, {artifactStatus = 404, coreFails = false, holdDesk = false, artifactVersion = 'a'.repeat(64)} = {}){
  const artifactReads = [], reads = [];
  let releaseDesk;
  const deskRelease = new Promise(resolve => {releaseDesk = resolve;});
  await page.addInitScript(() => {localStorage.setItem('pulse_passcode', 'browser-fixture-only'); localStorage.setItem('pulse-region', 'ZA');});
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const base = '/api/v2/investigations/' + plan.investigationId;
    reads.push(path);
    let body;
    if (path === '/api/health') body = {passcode: true};
    /* Page port, 3 October 2026: the older console's investigation link now
       opens the 42 investigation page, which reads /api/investigations/<id>.
       Its answer follows the case: a 401 where the artifact read was refused
       for authentication, a 503 where the plan read failed, and a plain
       failure otherwise, since its record is not part of this fixture. */
    else if (path === '/api/investigations/' + plan.investigationId) {
      if (artifactStatus === 401) return route.fulfill({status: 401, json: {detail: {code: 'unauthorized'}}});
      return route.fulfill({status: coreFails ? 503 : 501, json: {error: coreFails ? 'workspace_unavailable' : 'fixture_landing', message: 'The investigation could not be read.'}});
    }
    else if (path === '/api/desk') {
      if (holdDesk) await deskRelease;
      body = {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}};
    }
    else if (path === '/api/research/recent') body = {artifacts: []};
    else if (path === '/api/v2/investigations/list') body = {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false};
    else if (path === '/api/v2/investigations/scopes') body = {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null};
    else if (path === base + '/status') {
      if (coreFails) return route.fulfill({status: 503, json: {detail: {code: 'workspace_unavailable'}}});
      body = plan.status;
    } else if (path === base + '/claims/read') body = plan.claims;
    else if (path === base + '/decision/read') body = plan.decision;
    else if (path === base + '/artifacts/ra_missing/read') {
      artifactReads.push(path);
      return route.fulfill({status: artifactStatus, json: {detail: {code: artifactStatus === 401 ? 'unauthorized' : 'scope_invalid'}}});
    } else return route.fulfill({status: 501, json: {detail: 'Missing fixture'}});
    await route.fulfill({status: 200, json: body});
  });
  await page.goto('/#/console?work=brief&investigation=' + plan.investigationId + '&artifact=ra_missing' + (artifactVersion ? '&artifact_version=' + artifactVersion : ''));
  return {artifactReads, reads, releaseDesk};
}

test('a later desk response does not restart a settled investigation read', async ({page}) => {
  const state = await setup(page, {holdDesk: true});
  await expect(page.locator('.evidence-room')).toContainText('Requested artifact is unavailable.');
  expect(state.artifactReads).toHaveLength(1);
  const deskResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/desk');
  state.releaseDesk();
  await deskResponse;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator('.evidence-room')).toContainText('Requested artifact is unavailable.');
  expect(state.artifactReads).toHaveLength(1);
  await page.getByRole('button', {name: 'Retry read', exact: true}).click();
  await expect(page.locator('.evidence-room')).toContainText('Requested artifact is unavailable.');
  expect(state.artifactReads).toHaveLength(2);
});

/* Page port, 3 October 2026: the evidence room was the older console's
   investigation view, and its link (#/console?work=brief&investigation=<id>
   &artifact=...) now opens the 42 investigation page #/investigations/<id>
   (src/legacyRoutes.js). The evidence room is unreachable, so these hold the
   landing with the same failing replies installed: the page reads its own
   record for the same investigation, its retry reads that same identity
   again, an authentication failure still reaches the gate, a failed read
   shows no partial investigation, and no artifact or older investigation
   read is ever made. The old title is kept above each test. */
const INVESTIGATION_HASH = new RegExp('#/investigations/' + plan.investigationId + '$');
const OWN_READ = '/api/investigations/' + plan.investigationId;
const olderReads = (state) => state.reads.filter((path) => path.startsWith('/api/v2/investigations/' + plan.investigationId));

/* Was: unavailable artifact retains the valid investigation plan and retries the same identity. */
test('an evidence room link with an unavailable artifact opens the investigation, whose retry reads the same identity', async ({page}) => {
  const state = await setup(page);
  await expect(page).toHaveURL(INVESTIGATION_HASH);
  await expect(page.locator('#main-content h1')).toHaveText('Investigation');
  await expect(page.locator('.evidence-room')).toHaveCount(0);
  await expect(page.getByText('No artifact exists for this investigation.')).toHaveCount(0);
  await expect.poll(() => state.reads.filter((path) => path === OWN_READ).length).toBe(1);
  await page.locator('#main-content').getByRole('button', {name: 'Try again', exact: true}).click();
  await expect.poll(() => state.reads.filter((path) => path === OWN_READ).length).toBe(2);
  expect(new Set(state.reads.filter((path) => path.startsWith('/api/investigations/'))).size).toBe(1);
  expect(state.artifactReads).toEqual([]);
  expect(olderReads(state)).toEqual([]);
});
/* Was: artifact authentication failure still reaches the authentication gate. */
test('an evidence room link whose investigation read fails authentication still reaches the authentication gate', async ({page}) => {
  const state = await setup(page, {artifactStatus: 401});
  await expect(page.locator('input[type="password"]')).toBeVisible();
  await expect(page.locator('.evidence-room')).toHaveCount(0);
  expect(state.artifactReads).toEqual([]);
});
/* Was: required plan failure does not display a partial unverified investigation. */
test('an evidence room link whose investigation read fails displays no partial unverified investigation', async ({page}) => {
  const state = await setup(page, {coreFails: true});
  await expect(page).toHaveURL(INVESTIGATION_HASH);
  await expect(page.locator('#main-content').getByRole('button', {name: 'Try again', exact: true})).toBeVisible();
  await expect(page.locator('#main-content')).not.toContainText(plan.question);
  await expect(page.locator('.evidence-room')).toHaveCount(0);
  expect(olderReads(state)).toEqual([]);
});

/* The server refuses an artifact read that does not name its exact version,
   so a link without one sends no read at all.
   Was: a link without an artifact version sends no read and shows the artifact as unavailable. */
test('a link without an artifact version opens the investigation and sends no artifact read', async ({page}) => {
  const artifactRequests = [];
  page.on('request', request => { if (new URL(request.url()).pathname.includes('/artifacts/')) artifactRequests.push(request.url()); });
  const state = await setup(page, {artifactVersion: null});
  await expect(page).toHaveURL(INVESTIGATION_HASH);
  await expect(page.locator('#main-content h1')).toHaveText('Investigation');
  await expect.poll(() => state.reads.filter((path) => path === OWN_READ).length).toBe(1);
  await expect(page.locator('.dossier-client-read')).toHaveCount(0);
  expect(state.artifactReads).toEqual([]);
  expect(artifactRequests).toEqual([]);
  expect(olderReads(state)).toEqual([]);
});
