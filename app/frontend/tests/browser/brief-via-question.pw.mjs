import {expect, test} from '@playwright/test';
import {lensRosterFixture, questionCoverageFixture, shellFixtures} from './support/capability-harness.mjs';
import {readFileSync} from 'node:fs';

const readFixture = (name) => JSON.parse(readFileSync(new URL('../../src/ui/__tests__/fixtures/' + name, import.meta.url), 'utf8'));

/* Page port, 3 October 2026: #/console?work=brief no longer opens the older
   cited brief workbench; it lands on Build (src/legacyRoutes.js). Build's own
   "Ask a question" form is now how a question reaches 42's question engine:
   it opens Ask with the question and market, Ask posts it once to /api/ask
   (contract section 6), follows the record, and shows the cited answer with
   its export. The two journeys below hold that path with the same strength:
   48px controls, one write with the typed question and market, the stored
   answer shown, no brief generation, no older chat send, and no read outside
   the fixture map. */
const BUILD_READS = {
  '/api/investigations': {investigations: []},
  '/api/dossiers': {dossiers: []},
  '/api/history/asks': {asks: []},
  '/api/history/briefs': {dates: []},
  '/api/schedules': {schedules: []},
};

async function smallBuildTargets(page){
  const small = [];
  for (const control of await page.locator('#main-content .b42-start').filter({has: page.locator('#b42-ask-question')}).locator('button, textarea, select').all()){
    const box = await control.boundingBox();
    if (box.height < 48 || box.width < 48) small.push(`${await control.getAttribute('id') || await control.textContent()} ${Math.round(box.width)}x${Math.round(box.height)}`);
  }
  return small;
}

/* Was: Build brief on staging asks the question engine and opens the stored request. */
test('Build asks the question engine through Ask and shows the stored cited answer', async ({page}) => {
  const complete = readFixture('ask42_complete.json');
  const question = 'What is behind mobile data prices in South Africa for a telecoms brand?';
  const record = {...complete, question};
  const requested = [], unexpected = [], errors = [], sends = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem('pulse_passcode', 'browser-fixture-only');
    localStorage.setItem('pulse-region', 'ZA');
  });
  const fixtures = {
    ...shellFixtures(),
    ...BUILD_READS,
    '/api/chat/coverage': questionCoverageFixture(),
    '/api/chat/lenses': lensRosterFixture(),
    ['/api/ask/' + record.ask_id]: record,
  };
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    requested.push(request.method() + ' ' + url.pathname);
    if (url.pathname === '/api/ask' && request.method() === 'POST'){
      sends.push(request.postDataJSON());
      return route.fulfill({status: 202, json: {ask_id: record.ask_id, status: 'running'}});
    }
    if (url.pathname === '/api/ask/' + record.ask_id + '/events') return route.fulfill({status: 404, json: {error: 'not_found', message: 'No stream in this fixture.'}});
    if (!(url.pathname in fixtures)){ unexpected.push(url.pathname); return route.fulfill({status: 404, json: {detail: 'Missing fixture'}}); }
    await route.fulfill({status: 200, json: fixtures[url.pathname]});
  });

  await page.goto('/#/console?work=brief');
  await expect(page).toHaveURL(/#\/console$/);
  await expect(page.getByRole('heading', {name: 'Ask a question', exact: true})).toBeVisible();
  const action = page.getByRole('button', {name: 'Ask', exact: true});
  await expect(action).toBeDisabled();
  expect(await smallBuildTargets(page), 'each Build question control is a 48px target').toEqual([]);
  await page.getByLabel('Your question', {exact: true}).fill(question);
  await page.locator('#b42-ask-market').selectOption('ZA');
  await expect(action).toBeEnabled();
  await action.click();

  await expect(page).toHaveURL(new RegExp('#/ask\\?follow=' + record.ask_id + '$'));
  await expect(page.locator('.ask42-result')).toContainText(complete.answer.short_answer);
  await expect(page.getByRole('button', {name: 'Export answer', exact: true})).toBeVisible();
  expect(sends).toEqual([{question, market: 'ZA', parent_id: null}]);
  expect(requested.filter(entry => / \/api\/research\/generate/.test(entry) || / \/api\/chat\/send$/.test(entry))).toEqual([]);
  expect(unexpected).toEqual([]);
  expect(errors).toEqual([]);
});

async function smallAskTargets(page){
  await page.evaluate(() => Promise.all(document.getAnimations()
    .filter((animation) => Number.isFinite(animation.effect?.getComputedTiming().endTime))
    .map((animation) => animation.finished.catch(() => null))));
  const small = [];
  for (const control of await page.locator('#main-content .ask42-result').locator('button, a').all()){
    if (!await control.isVisible()) continue;
    const box = await control.boundingBox();
    if (box.height < 48 || box.width < 48) small.push(`${await control.getAttribute('id') || await control.textContent()} ${Math.round(box.width)}x${Math.round(box.height)}`);
  }
  return small;
}

/* Was: Retry, Check again, Open the request and Start over are 48px targets on the way to a refusal.
   The Ask page's way to a refusal: a start the service could not confirm
   offers Try again, which posts the same question once more; the engine's
   refusal then names its reason and offers Try again. Each is a 48px target. */
test('Try again after a failed start and after a refusal are 48px targets on the way from Build to a refusal', async ({page}) => {
  const failed = readFixture('ask42_failed.json');
  const question = 'What is behind mobile data prices in South Africa?';
  const refused = {...failed, question};
  const unexpected = [], errors = [], sends = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem('pulse_passcode', 'browser-fixture-only');
    localStorage.setItem('pulse-region', 'ZA');
  });
  const fixtures = {
    ...shellFixtures(),
    ...BUILD_READS,
    '/api/chat/coverage': questionCoverageFixture(),
    '/api/chat/lenses': lensRosterFixture(),
    ['/api/ask/' + refused.ask_id]: refused,
  };
  const starts = [{__status: 503, body: {error: 'unavailable', message: 'Service unavailable right now.'}}, {ask_id: refused.ask_id, status: 'running'}];
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/ask' && request.method() === 'POST'){
      sends.push(request.postDataJSON());
      const value = starts.length > 1 ? starts.shift() : starts[0];
      if (value.__status) return route.fulfill({status: value.__status, json: value.body});
      return route.fulfill({status: 202, json: value});
    }
    if (url.pathname === '/api/ask/' + refused.ask_id + '/events') return route.fulfill({status: 404, json: {error: 'not_found', message: 'No stream in this fixture.'}});
    if (!(url.pathname in fixtures)){ unexpected.push(url.pathname); return route.fulfill({status: 404, json: {detail: 'Missing fixture'}}); }
    await route.fulfill({status: 200, json: fixtures[url.pathname]});
  });

  await page.goto('/#/console?work=brief');
  await expect(page).toHaveURL(/#\/console$/);
  await page.getByLabel('Your question', {exact: true}).fill(question);
  await page.getByRole('button', {name: 'Ask', exact: true}).click();
  const alert = page.locator('.ask42-result').getByRole('alert');
  await expect(alert).toContainText('Service unavailable right now.');
  await expect(alert.getByRole('button', {name: 'Try again', exact: true})).toBeVisible();
  expect(await smallAskTargets(page), 'Try again after a failed start').toEqual([]);
  await alert.getByRole('button', {name: 'Try again', exact: true}).click();
  await expect(alert).toContainText(failed.error.message);
  await expect(alert.getByRole('heading', {name: question, exact: true})).toBeVisible();
  await expect(alert.getByRole('button', {name: 'Try again', exact: true})).toBeEnabled();
  expect(await smallAskTargets(page), 'Try again after the refusal').toEqual([]);
  expect(sends).toHaveLength(2);
  expect(sends[1]).toEqual(sends[0]);
  expect(sends[0]).toEqual({question, market: 'ZA', parent_id: null});
  expect(unexpected).toEqual([]);
  expect(errors).toEqual([]);
});
