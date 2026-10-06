import {expect, test} from '@playwright/test';
import {intelligenceFixture} from '../../src/ui/__tests__/fixtures/general-intelligence.js';
import {lensRosterFixture} from './support/capability-harness.mjs';

const COVERAGE = {contract_version: 'general_question_coverage_v1', state: 'covered', window: {start: '2026-08-25', end: '2026-09-07'}, cutoff_date: '2026-09-07'};

async function setup(page, {lostAdmission = false, failOnce = null, hold = false, terminal, coverage = COVERAGE, fromPlan = true, health = 'ready'} = {}){
  const posts = [], polls = [], unknown = [], reads = [];
  let pending = hold;
  let coverageHeld = coverage === 'hold';
  let answerCoverage = null;
  let healthHeld = health === 'hold';
  let answerHealth = null;
  await page.addInitScript(() => {
    localStorage.setItem('pulse_passcode', 'browser-fixture-only');
    localStorage.setItem('pulse-region', 'ZA');
  });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let json;
    if (path === '/api/health' && healthHeld){ answerHealth = () => route.fulfill({status: 200, json: {passcode: true}}); return; }
    if (path === '/api/health') json = {passcode: true};
    else if (path === '/api/auth/verify') json = {ok: true};
    else if (path === '/api/desk') json = {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}};
    else if (path === '/api/research/recent') json = {artifacts: []};
    else if (path === '/api/v2/investigations/list') json = {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false};
    else if (path === '/api/v2/investigations/scopes') json = {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null};
    else if (path === '/api/chat/lenses') json = lensRosterFixture();
    else if (path === '/api/chat/coverage' && coverageHeld){ answerCoverage = () => route.fulfill({status: 200, json: COVERAGE}); return; }
    else if (path === '/api/chat/coverage') json = coverage;
    else if (path === '/api/internal/v2/fieldwork/question' && terminal?.intelligence) {
      reads.push(url.searchParams.get('request_id'));
      json = {contract_version: 'general_question_detail_v1', request_id: terminal.intelligence.request_id, observed_state: terminal.intelligence.status, question: QUESTION, history: [], selected_market: 'za', requested_window: null, response: {answer: terminal.answer, sources: [], intelligence: terminal.intelligence}, window_from_plan: fromPlan, reserved_microusd: 100000, missing_work: []};
    }
    else if (path === '/api/chat/send') {
      posts.push(route.request().postDataJSON());
      if (lostAdmission) return route.abort('failed');
      if (failOnce === 'lost'){ failOnce = null; return route.abort('failed'); }
      if (failOnce === 503){ failOnce = null; return route.fulfill({status: 503, json: {detail: 'General question admission is unavailable'}}); }
      json = {job_id: 'job_browser_request'};
    } else if (path === '/api/chat/status') {
      polls.push(url.searchParams.get('job_id'));
      json = pending ? {pending: true} : terminal || {answer: 'The existing request returned its reply.', sources: []};
    } else { unknown.push(path); return route.fulfill({status: 404, json: {detail: 'Missing fixture'}}); }
    await route.fulfill({status: 200, json});
  });
  return {posts, polls, unknown, reads, release(){ pending = false; }, hold(){ pending = true; }, async releaseCoverage(){ coverageHeld = false; await expect.poll(() => answerCoverage !== null).toBe(true); await answerCoverage(); }, async releaseHealth(){ healthHeld = false; await expect.poll(() => answerHealth !== null).toBe(true); await answerHealth(); }};
}

const QUESTION = 'How is repair culture changing across categories?';
/* Ask redesign, 23 Sept 2026: the question field carries a visible label,
   "Your question" on an empty page and "Ask a follow-up" under an answer, and
   its accessible name is that label. The coverage line is stated once in the
   page head, the stored request's bookkeeping sits in the Request and usage
   details disclosure, the stored question heads its answer, and dates read as
   a reader says them. */
const FOLLOW_UP = 'Ask a follow-up';
const SHARED = /#\/console\?work=ask&request=00000000-0000-4000-8000-000000000001$/;

async function ask(page){
  await page.goto('/#/console?work=ask');
  await page.locator('textarea').fill(QUESTION);
  await page.locator('textarea').press('Enter');
}

test('a lost admission reply never silently resubmits', async ({page}) => {
  const state = await setup(page, {lostAdmission: true});
  await ask(page);
  await expect(page.getByText('The connection ended before the service confirmed this question, so it may already have started.', {exact: false})).toBeVisible();
  expect(state.posts).toHaveLength(1);
  expect(state.polls).toEqual([]);
});

test('reload preserves an admitted job and checks it without another submission', async ({page}) => {
  const state = await setup(page, {hold: true});
  await ask(page);
  await expect.poll(() => state.polls.length).toBeGreaterThan(0);
  await page.reload();
  state.release();
  await page.getByRole('button', {name: 'Check existing request'}).click();
  await expect(page.getByText('The existing request returned its reply.', {exact: true})).toBeVisible();
  expect(state.posts).toHaveLength(1);
  expect(new Set(state.polls)).toEqual(new Set(['job_browser_request']));
  expect(state.unknown).toEqual([]);
});

test('stopping the wait preserves the job for a later status check', async ({page}) => {
  const state = await setup(page, {hold: true});
  await ask(page);
  await expect.poll(() => state.polls.length).toBeGreaterThan(0);
  await page.getByRole('button', {name: 'Stop waiting', exact: true}).click();
  await expect(page.getByRole('button', {name: 'Check existing request'})).toBeVisible();
  state.release();
  await page.getByRole('button', {name: 'Check existing request'}).click();
  await expect(page.getByText('The existing request returned its reply.', {exact: true})).toBeVisible();
  expect(state.posts).toHaveLength(1);
});

test('a structured reply renders its admitted claims and opens sources without routing away', async ({page}) => {
  const intelligence = intelligenceFixture();
  const state = await setup(page, {terminal: {answer: 'UNTRUSTED_SECOND_ANSWER', sources: [], intelligence}});
  await ask(page);
  await expect(page.getByText(intelligence.claims[0].text, {exact: true})).toBeVisible();
  await expect(page.getByText('UNTRUSTED_SECOND_ANSWER', {exact: true})).toHaveCount(0);
  await page.getByRole('button', {name: /^Open R1:/}).click();
  await expect(page.getByRole('link', {name: 'View original source'})).toHaveAttribute('href', 'https://example.test/post_1');
  await expect(page).toHaveURL(SHARED);
  await expect(page.getByRole('textbox', {name: FOLLOW_UP})).toBeVisible();
  const saved = await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-chat'))[0].messages.at(-1));
  expect(saved.intelligence).toEqual(intelligence);
  await page.reload();
  await expect(page.getByText(intelligence.claims[0].text, {exact: true})).toBeVisible();
  expect(state.posts).toHaveLength(1);
});

test('a withheld structured reply never enters follow-up history', async ({page}) => {
  const intelligence = intelligenceFixture(); intelligence.receipts[0].market = 'ng';
  const state = await setup(page, {terminal: {answer: 'UNTRUSTED_SECOND_ANSWER', sources: [], intelligence}});
  await ask(page);
  await expect(page.getByRole('heading', {name: 'Reply withheld'})).toBeVisible();
  await page.getByRole('textbox', {name: FOLLOW_UP}).fill('What about the available source window?');
  await page.getByRole('textbox', {name: FOLLOW_UP}).press('Enter');
  await expect.poll(() => state.posts.length).toBe(2);
  expect(JSON.stringify(state.posts[1].history)).not.toContain('UNTRUSTED_SECOND_ANSWER');
  expect(state.posts[1].history.every(turn => turn.role === 'user')).toBe(true);
});

test('unresolved usage is visible on a typed unavailable result', async ({page}) => {
  const intelligence = intelligenceFixture();
  Object.assign(intelligence, {status: 'unavailable', window: null, snapshot_id: null, sections: [], claims: [], receipts: []});
  Object.assign(intelligence.usage, {status: 'unresolved', model_calls: null, input_tokens: null, output_tokens: null, reason: 'metering_persistence_failed'});
  await setup(page, {terminal: {error: true, reason: 'metering_persistence_failed', answer: 'UNTRUSTED_SECOND_ANSWER', sources: [], intelligence}});
  await ask(page);
  await expect(page.getByText('Reply unavailable', {exact: true})).toBeVisible();
  await page.getByText('Request and usage details', {exact: true}).click();
  await expect(page.getByText('Usage is unresolved.', {exact: false})).toBeVisible();
  await expect(page.getByText('Model calls: Unmeasured', {exact: true})).toBeVisible();
  await expect(page.getByText('UNTRUSTED_SECOND_ANSWER')).toHaveCount(0);
});

test('proposals remain review-required and readings preserve unknown values', async ({page}) => {
  const intelligence = intelligenceFixture();
  intelligence.review_required = true;
  intelligence.claims.push({...intelligence.claims[0], claim_id: 'proposal_1', kind: 'proposal', support_state: 'proposed', text: 'Consider a small repair workshop pilot.', parent_claim_ids: ['claim_1'], falsifier: 'Stop if further evidence does not support the need.'});
  intelligence.sections.push({kind: 'actions', claim_ids: ['proposal_1']});
  intelligence.readings.push({reading_id: 'reading_1', value: null, unit: 'distinct people', window: intelligence.window, method: 'No population measurement was supplied.', denominator: null, source_receipt_ids: ['receipt_1'], limitations: ['A post count cannot establish audience size.']});
  intelligence.claims[0].reading_ids = ['reading_1'];
  await setup(page, {terminal: {answer: 'UNTRUSTED_SECOND_ANSWER', sources: [], intelligence}});
  await ask(page);
  await expect(page.getByText('Review required before downstream use.', {exact: true})).toBeVisible();
  await expect(page.getByText('Unmeasured distinct people', {exact: true})).toBeVisible();
  await page.getByText('Measurement details', {exact: true}).click();
  await expect(page.getByText('Denominator: Unmeasured', {exact: true})).toBeVisible();
  await expect(page.getByText('Proposal, with no source record', {exact: true})).toBeVisible();
  await expect(page.getByText('What would change this: Stop if further evidence does not support the need.', {exact: true})).toBeVisible();
});

test('a citation beyond the first source page opens its actual record', async ({page}) => {
  const intelligence = intelligenceFixture();
  for (let number = 2; number <= 8; number++) {
    intelligence.receipts.push({...intelligence.receipts[0], receipt_id: 'receipt_' + number, citation_label: 'R' + number, source_label: 'Source ' + number, url: 'https://example.test/post_' + number, source_row_id: 'post_' + number, excerpt: 'Receipt ' + number + ' evidence excerpt.'});
    intelligence.claims.push({...intelligence.claims[0], claim_id: 'claim_' + number, text: 'An admitted observation ' + number, receipt_ids: ['receipt_' + number]});
  }
  intelligence.sections.push({kind: 'evidence', claim_ids: intelligence.claims.slice(1).map(claim => claim.claim_id)});
  expect(new Set(intelligence.receipts.map(receipt => receipt.url)).size).toBe(8);
  expect(new Set(intelligence.receipts.map(receipt => receipt.excerpt)).size).toBe(8);
  expect(new Set(intelligence.receipts.map(receipt => receipt.source_row_id)).size).toBe(8);
  await setup(page, {terminal: {answer: '', sources: [], intelligence}});
  await ask(page);
  await page.getByRole('button', {name: 'Open R8: Instagram', exact: true}).click();
  const summary = page.getByText('R8 · Instagram', {exact: true});
  await expect(summary).toBeFocused();
  const record = summary.locator('..');
  await expect(record).toHaveAttribute('open', '');
  await expect(record.getByRole('link', {name: 'View original source'})).toHaveAttribute('href', 'https://example.test/post_8');
  await expect(record.getByText('Receipt 8 evidence excerpt.', {exact: true})).toBeVisible();
  await record.getByText('Record reference', {exact: true}).click();
  await expect(record.getByText('post_8', {exact: true})).toBeVisible();
  await expect(page).toHaveURL(SHARED);
});

test('still needed work shows the planned question and keeps the requirement id behind a reference', async ({page}) => {
  const intelligence = intelligenceFixture();
  const question = 'Missing required evidence: What were South Africans talking about in the first week of September 2026?';
  intelligence.missing_work = ['req_za_trending_topics_sep_2026', question, 'Challenge search incomplete: no challenge requirement was planned.'];
  await setup(page, {terminal: {answer: '', sources: [], intelligence}});
  await ask(page);
  const section = page.locator('.general-section').filter({has: page.getByRole('heading', {name: 'What is still needed'})});
  await expect(section.getByText(question, {exact: true})).toBeVisible();
  await expect(section.getByText('Challenge search incomplete: no challenge requirement was planned.', {exact: true})).toBeVisible();
  const reference = section.getByText('req_za_trending_topics_sep_2026', {exact: true});
  await expect(reference).toBeHidden();
  await section.getByText('Requirement reference', {exact: true}).click();
  await expect(reference).toBeVisible();
});

test('a failed terminal envelope cannot display a completed factual reply', async ({page}) => {
  const intelligence = intelligenceFixture(); intelligence.status = 'complete'; intelligence.missing_work = [];
  await setup(page, {terminal: {error: true, reason: 'claim_support_failed', answer: 'Unsafe success.', sources: [], intelligence}});
  await ask(page);
  await expect(page.getByRole('heading', {name: 'Reply withheld'})).toBeVisible();
  await expect(page.getByText(intelligence.claims[0].text, {exact: true})).toHaveCount(0);
  await expect(page.locator('.general-status[data-status="complete"]')).toHaveCount(0);
});

for (const width of [390, 1024, 1440]) for (const theme of ['daylight', 'midnight']) {
  test(`structured reply layout at ${width} ${theme}`, async ({page}) => {
    const intelligence = intelligenceFixture();
    await setup(page, {terminal: {answer: '', sources: [], intelligence}});
    await page.addInitScript(value => localStorage.setItem('oi-theme', value), theme);
    await page.setViewportSize({width, height: 1000});
    await ask(page);
    await page.getByRole('button', {name: /^Open R1:/}).click();
    await page.evaluate(() => document.fonts.ready);
    await page.evaluate(() => Promise.all(document.getAnimations().filter(animation => Number.isFinite(animation.effect.getTiming().iterations)).map(animation => animation.finished.catch(() => {}))));
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (width === 390) expect(await page.locator('.general-reply').evaluate(node => node.getBoundingClientRect().width)).toBeGreaterThanOrEqual(250);
    const source = page.getByRole('link', {name: 'View original source'});
    await source.focus();
    await page.keyboard.press('Shift+Tab');
    await page.keyboard.press('Tab');
    await expect(source).toBeFocused();
    expect(await source.evaluate(node => getComputedStyle(node).outlineStyle)).toBe('solid');
    expect(await source.evaluate(node => {
      const box = node.getBoundingClientRect();
      return node.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
    })).toBe(true);
    await page.screenshot({path: test.info().outputPath('reply.png'), fullPage: true});
    await page.emulateMedia({reducedMotion: 'reduce'});
    expect(await source.evaluate(node => getComputedStyle(node).transitionDuration)).toBe('0s');
  });
}

test('the Ask page names its window and a refusal outside it offers the latest available week', async ({page}) => {
  const intelligence = intelligenceFixture();
  Object.assign(intelligence, {status: 'unavailable', as_of: '2026-09-23T10:00:00Z', window: {start: '2026-09-14', end: '2026-09-20', closed: true}, snapshot_id: null, sections: [], claims: [], receipts: [], readings: [], limitations: ['No completed answer is available.'], missing_work: ['retrieval_incomplete', 'coverage_incomplete']});
  const state = await setup(page, {terminal: {error: true, reason: 'retrieval_incomplete', answer: '', sources: [], intelligence}});
  await page.goto('/#/console?work=ask');
  /* Round three, 24 Sept 2026: the Ask copy fix named the dates twice, as
     "Ask covers ... Other dates cannot be answered yet." under the invitation
     and as "Ask window ..." in the rail. The redesign states them once, in
     the page head, and the rail repeats nothing, so the redesign's line and
     the absence of the rail line are kept. */
  await expect(page.getByText('Covers South Africa, 25 Aug to 7 Sept 2026.', {exact: true})).toBeVisible();
  await expect(page.getByText('Ask window', {exact: false})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'What should we brief on from the latest available week?'})).toBeVisible();
  await page.locator('textarea').fill('What happened in Kenya between 14 and 20 September 2026?');
  await page.locator('textarea').press('Enter');
  await expect(page.getByText('This question falls outside the dates 42 can answer. No answer was generated.', {exact: true})).toBeVisible();
  await expect(page.getByText('Ask covers 25 Aug to 7 Sept 2026.', {exact: true})).toBeVisible();
  await expect(page.getByText('The evidence retrieval did not complete', {exact: false})).toHaveCount(0);
  await page.getByRole('button', {name: 'Ask about the latest available week'}).click();
  await expect(page.locator('textarea')).toHaveValue('What stood out in South Africa in the latest available week?');
  expect(state.posts).toHaveLength(1);
  expect(state.unknown).toEqual([]);
});

/* A live reply has no stored record to say where its window came from, so it
   keeps naming the window in its header, answered or not.
   Round three, 24 Sept 2026: the redesign names the answer's window in its
   meta line as the dates its evidence comes from, in readable dates, and an
   unresolved one as "Window unresolved", so the words are restated to that
   line; the behaviour is the Ask copy fix's.
   Round three, 24 Sept 2026: "Window" and "unresolved" are system words
   beside a page head that says "Covers" and a meta line that says "Evidence
   from", so an unsettled window reads "Dates not settled". */
for (const answered of [true, false]) {
  test('a live ' + (answered ? 'answer' : 'failure') + ' names its observation window', async ({page}) => {
    const intelligence = intelligenceFixture();
    if (!answered) Object.assign(intelligence, {status: 'unavailable', snapshot_id: null, sections: [], claims: [], receipts: [], readings: [], limitations: ['No completed answer is available.'], missing_work: ['claim_support_failed'], review_required: false, ready_for_downstream: false});
    const state = await setup(page, {terminal: answered ? {answer: '', sources: [], intelligence} : {error: true, reason: 'claim_support_failed', answer: '', sources: [], intelligence}});
    await ask(page);
    await expect(page.getByText(answered ? intelligence.claims[0].text : 'Reply unavailable', {exact: true})).toBeVisible();
    await expect(page.locator('.general-meta')).toContainText('Evidence from 23 Aug to 5 Sept 2026');
    await expect(page.locator('.general-meta', {hasText: 'Dates not settled'})).toHaveCount(0);
    expect(state.unknown).toEqual([]);
  });
}

/* A reply written before any plan carries the engine's default window, the
   fourteen days before as_of, so the stored page names no resolved window for
   it. The same dates from a plan are still named.
   Round three, 24 Sept 2026: the redesign heads a stored answer with its own
   question and keeps the stored bookkeeping in the Request and usage details
   disclosure, and names windows in readable dates in the answer's meta line,
   so the page is read there; the behaviour is the Ask copy fix's. */
/* Page port, 3 October 2026: a stored question link
   (#/console?work=ask&request=<id>) names an older record, and it now opens
   History, where 42's saved asks are listed (src/legacyRoutes.js), so the
   stored answer page and its window lines are unreachable from it. Each case
   keeps its stored failure installed and holds the landing: History renders
   and makes its own read, the stored question is never read, and neither
   window line nor stored state is shown.
   Was: a stored failure (before planning names no|after planning names its) resolved window. */
for (const fromPlan of [false, true]) {
  test('a stored failure link ' + (fromPlan ? 'after planning' : 'before planning') + ' opens History and names no stored window', async ({page}) => {
    const intelligence = intelligenceFixture();
    Object.assign(intelligence, {status: 'unavailable', as_of: '2026-09-23T10:00:00Z', window: {start: '2026-09-09', end: '2026-09-22', closed: true}, snapshot_id: null, sections: [], claims: [], receipts: [], readings: [], limitations: ['No completed answer is available.'], missing_work: ['model_timeout'], review_required: false, ready_for_downstream: false});
    const state = await setup(page, {terminal: {answer: 'I could not produce a supported answer for this request.', sources: [], intelligence}, fromPlan});
    const historyReads = [];
    await page.route('**/api/history/asks*', (route) => { historyReads.push(new URL(route.request().url()).search); return route.fulfill({status: 503, json: {error: 'fixture_landing', message: 'The landing page read is not served in this fixture.'}}); });
    await page.goto('/#/console?work=ask&request=' + intelligence.request_id);
    await expect(page).toHaveURL(/#\/history$/);
    await expect(page.locator('#main-content h1')).toHaveText('History');
    await expect.poll(() => historyReads.length).toBeGreaterThan(0);
    await expect(page.getByRole('heading', {name: QUESTION, exact: true})).toHaveCount(0);
    await expect(page.getByText('Request and usage details', {exact: true})).toHaveCount(0);
    await expect(page.getByText('Stored response: unavailable.', {exact: true})).toHaveCount(0);
    await expect(page.getByText('Requested window', {exact: false})).toHaveCount(0);
    await expect(page.getByText('Resolved window', {exact: false})).toHaveCount(0);
    await expect(page.getByText('22 Sept 2026', {exact: false})).toHaveCount(0);
    await expect(page.locator('.general-meta')).toHaveCount(0);
    expect(state.reads, 'the stored question is never read').toEqual([]);
    expect(state.posts).toEqual([]);
    expect(state.unknown).toEqual([]);
  });
}

/* The covered window arrives after the page paints. Its line holds its place
   from the first paint, in small text, so filling the dates in moves nothing
   on the page.
   Round three, 24 Sept 2026: the redesign states the dates once, in the page
   head under the Ask title, where the line carries data-ask-window, and has
   no invitation or rail line. So the line is read in the page head and
   measured against the question field under it; it is set in the page head's
   muted body size, smaller than the title, rather than the smallest token,
   and the dates are named once. */
/* The type steps the Ask page is set in, read inside the page: each named
   token, the page title, the coverage line, the offered questions, the
   question heading its answer, the section heads, and the largest run of
   visible text in the workspace other than the title. */
function typeScale(){
  const main = document.querySelector('#main-content');
  const size = (node) => parseFloat(getComputedStyle(node).fontSize);
  const token = {};
  for (const step of [3, 4, 5, 6, 7]){
    const probe = document.createElement('span');
    probe.style.fontSize = `var(--type-${step})`;
    main.appendChild(probe);
    token[step] = size(probe);
    probe.remove();
  }
  const title = main.querySelector('.workbench-title');
  let largestElse = 0;
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  let text;
  while ((text = walker.nextNode())){
    const element = text.parentElement;
    if (!text.textContent.trim() || !element || title.contains(element)) continue;
    const range = document.createRange();
    range.selectNodeContents(text);
    const box = range.getBoundingClientRect();
    if (!box.width || !box.height || getComputedStyle(element).visibility === 'hidden') continue;
    largestElse = Math.max(largestElse, size(element));
  }
  const question = main.querySelector('.chat-question');
  return {
    token,
    title: size(title),
    line: size(main.querySelector('.workbench-coverage')),
    offers: [...main.querySelectorAll('.workbench-ask-suggestion')].map(size),
    question: question ? size(question) : null,
    sections: [...main.querySelectorAll('.general-reply h3')].map(size),
    largestElse,
    head: main.querySelector('.workbench-topbar').getBoundingClientRect().width,
  };
}

test('the coverage lines keep their place and stay small when the dates land', async ({page}) => {
  await page.addInitScript(() => {
    window.__shifts = [];
    new PerformanceObserver((list) => { for (const entry of list.getEntries()) window.__shifts.push({at: entry.startTime, value: entry.value}); }).observe({type: 'layout-shift', buffered: true});
  });
  const intelligence = intelligenceFixture();
  const state = await setup(page, {coverage: 'hold', terminal: {answer: '', sources: [], intelligence}});
  /* The page settles its entrance motion over the first frames, so geometry
     is read with motion reduced. */
  await page.emulateMedia({reducedMotion: 'reduce'});
  await page.goto('/#/console?work=ask');
  const field = page.getByRole('textbox', {name: 'Your question'});
  await expect(field).toBeVisible();
  const line = page.locator('.workbench-coverage');
  await expect(line).toHaveCount(1);
  await expect(page.locator('[data-ask-window]')).toHaveCount(1);
  await expect(line).toHaveText('');
  /* Measured once the fonts have landed, so a font swap is not read as the
     dates moving the line. */
  await page.evaluate(() => document.fonts.ready.then(() => true));
  const before = await line.boundingBox();
  const fieldBefore = await field.boundingBox();
  expect(before.height).toBeGreaterThan(0);
  const released = await page.evaluate(() => performance.now());
  await state.releaseCoverage();
  await expect(line).toHaveText('Covers South Africa, 25 Aug to 7 Sept 2026.');
  await expect(line).toHaveAttribute('data-ask-coverage', '2026-08-25/2026-09-07');
  await page.waitForTimeout(500);
  const after = await line.boundingBox();
  const fieldAfter = await field.boundingBox();
  expect(after.y).toBeCloseTo(before.y, 1);
  expect(after.height).toBeCloseTo(before.height, 1);
  expect(fieldAfter.y).toBeCloseTo(fieldBefore.y, 1);
  /* Nothing on the page shifts when the dates land. */
  expect((await page.evaluate(() => window.__shifts)).filter((shift) => shift.at >= released)).toEqual([]);
  /* The line is set in the page head's body size and no wider than the page
     head; the dates are named once.
     Round three, 24 Sept 2026: set against a type-7 title, a type-3 line is
     smaller whatever the page does, so that comparison pinned nothing. The
     redesign's hierarchy is pinned instead: the page title, the serif "Ask"
     at type-7, is the largest type in the workspace, and every other run
     there is smaller; the offered questions read at type-4, a named step
     above the coverage line at type-3. Once a question is answered, below. */
  const sizes = await page.evaluate(typeScale);
  expect(sizes.title).toBe(sizes.token[7]);
  expect(sizes.largestElse, 'the page title is the largest type in the workspace').toBeLessThan(sizes.title);
  expect(sizes.line).toBe(sizes.token[3]);
  expect(sizes.offers.length).toBeGreaterThan(0);
  for (const offer of sizes.offers) expect(offer).toBe(sizes.token[4]);
  expect(sizes.token[3]).toBeLessThan(sizes.token[4]);
  expect(after.width).toBeLessThanOrEqual(sizes.head);
  await expect(page.getByText('Covers South Africa', {exact: false})).toHaveCount(1);
  await expect(page.getByText('Ask window', {exact: false})).toHaveCount(0);
  /* The text itself, the area a largest paint is measured by, stays under
     that of the largest offered question at this width. */
  const [lineArea, offerArea] = await page.evaluate(() => ['.workbench-coverage', '.workbench-ask-suggestion'].map((selector) => Math.max(...[...document.querySelectorAll(selector)].map((node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return [...range.getClientRects()].reduce((total, rect) => total + rect.width * rect.height, 0);
  }))));
  expect(lineArea).toBeLessThan(offerArea);
  /* Round three, 24 Sept 2026: under an answer the title keeps the top step,
     the question that heads the answer sits one named step below it at
     type-6, each section head one step below that at type-5, and the
     coverage line stays at type-3. An answer title that grew to the page
     title's size, or a page title that shrank under it, fails here. */
  await page.locator('textarea').fill(QUESTION);
  await page.locator('textarea').press('Enter');
  await expect(page.getByText(intelligence.claims[0].text, {exact: true})).toBeVisible();
  const answered = await page.evaluate(typeScale);
  expect(answered.title).toBe(answered.token[7]);
  expect(answered.largestElse, 'the page title is still the largest type in the workspace').toBeLessThan(answered.title);
  expect(answered.question).toBe(answered.token[6]);
  expect(answered.question, 'the answer title sits one step below the page title').toBeLessThan(answered.title);
  expect(answered.sections.length).toBeGreaterThan(0);
  for (const section of answered.sections){
    expect(section).toBe(answered.token[5]);
    expect(section, 'each section head sits below the answer title').toBeLessThan(answered.question);
  }
  expect(answered.line).toBe(answered.token[3]);
  expect(state.unknown).toEqual([]);
});

/* Read with the page's own entrance motion. The questions offered under the
   field are largest paint candidates from their first paint, so a covered
   window landing late can never become the page's largest paint.
   Round three, 24 Sept 2026: the redesign has no Ask card or invitation, and
   the page head line and the offered questions stand in for them. */
test('under default motion a late coverage read is never the largest paint', async ({page}) => {
  await page.addInitScript(() => {
    window.__lcp = [];
    new PerformanceObserver((list) => { for (const entry of list.getEntries()) window.__lcp.push({name: entry.element ? String(entry.element.className) : '', text: entry.element ? entry.element.textContent.slice(0, 40) : ''}); }).observe({type: 'largest-contentful-paint', buffered: true});
  });
  const state = await setup(page, {coverage: 'hold'});
  await page.emulateMedia({reducedMotion: 'no-preference'});
  await page.goto('/#/console?work=ask');
  await expect(page.getByRole('textbox', {name: 'Your question'})).toBeVisible();
  await page.waitForTimeout(1200);
  await state.releaseCoverage();
  await expect(page.locator('.workbench-coverage')).toHaveText('Covers South Africa, 25 Aug to 7 Sept 2026.');
  await page.waitForTimeout(500);
  const entries = await page.evaluate(() => window.__lcp);
  expect(entries.filter((entry) => entry.name.includes('workbench-coverage'))).toEqual([]);
  expect(entries.some((entry) => entry.name.includes('workbench-ask-suggestion'))).toBe(true);
  expect(state.unknown).toEqual([]);
});

/* Round three, 24 Sept 2026: Chrome never counts text as the largest paint
   when its first frame is painted at opacity 0, even once it fades in. The
   route layer's entrance starts at opacity 0, so when the Ask page landed on
   that first frame its head and suggestions never counted, and the coverage
   line, arriving late and fully shown, became the largest paint. The Ask page
   takes its entrance from the rail and the question field alone, so its head
   and suggestions are painted from their first frame. */
test('under default motion the Ask page head and suggestions take no entrance fade', async ({page}) => {
  const state = await setup(page);
  await page.emulateMedia({reducedMotion: 'no-preference'});
  await page.goto('/#/console?work=ask');
  await expect(page.getByRole('textbox', {name: 'Your question'})).toBeVisible();
  await expect(page.locator('.workbench-coverage')).toHaveText('Covers South Africa, 25 Aug to 7 Sept 2026.');
  /* Round three, 24 Sept 2026: a selector that matched nothing walked no
     ancestors and read as no fade, so each measured element must be shown
     before its fades are read, and a missing one reads as missing. */
  const measured = ['.workbench-back', '.workbench-title', '.workbench-coverage', '.chat-starters-title', '.workbench-ask-suggestion'];
  for (const selector of measured) await expect(page.locator(selector).first(), selector).toBeVisible();
  const fades = await page.evaluate((selectors) => selectors.map((selector) => {
    if (!document.querySelector(selector)) return [selector, 'missing'];
    const names = [];
    for (let at = document.querySelector(selector); at; at = at.parentElement){
      const name = getComputedStyle(at).animationName;
      if (name !== 'none') names.push(name);
    }
    return [selector, names];
  }), measured);
  expect(fades).toEqual([['.workbench-back', []], ['.workbench-title', []], ['.workbench-coverage', []], ['.chat-starters-title', []], ['.workbench-ask-suggestion', []]]);
  expect(state.unknown).toEqual([]);
});

/* Round three, 24 Sept 2026: the Ask page skips the route layer's entrance,
   but whether it does is settled when the route layer mounts. Moving from Ask
   to Build brief stays on the console route, so the layer is not remounted
   and nothing on it may start the entrance over: the page stays fully shown
   on every frame while focus moves to the stage heading. */
test('under default motion moving from Ask to Build brief does not replay the route entrance', async ({page}) => {
  await setup(page);
  await page.emulateMedia({reducedMotion: 'no-preference'});
  await page.goto('/#/console?work=ask');
  await expect(page.getByRole('textbox', {name: 'Your question'})).toBeVisible();
  const modes = page.getByRole('group', {name: 'Workbench modes'});
  await expect(modes.getByRole('button', {name: 'Build brief', exact: true})).toBeVisible();
  await expect(page.locator('#main-content')).toHaveCount(1);
  await expect.poll(() => page.evaluate(() => document.getAnimations().filter((move) => move.animationName === 'routeIn').length)).toBe(0);
  const frames = await page.evaluate(() => new Promise((resolve) => {
    const layer = document.getElementById('main-content');
    const control = [...document.querySelectorAll('[aria-label="Workbench modes"] button')].find((button) => button.textContent.trim() === 'Build brief');
    const seen = [];
    const read = () => {
      const now = document.getElementById('main-content');
      seen.push({
        same: now === layer,
        opacity: getComputedStyle(now).opacity,
        transform: getComputedStyle(now).transform,
        entrances: document.getAnimations().filter((move) => move.animationName === 'routeIn').length,
      });
      if (seen.length < 12) requestAnimationFrame(read); else resolve(seen);
    };
    control.click();
    read();
  }));
  await expect(modes.getByRole('button', {name: 'Build brief', exact: true})).toHaveAttribute('aria-current', 'page');
  expect(frames).toHaveLength(12);
  expect(frames.filter((frame) => !frame.same || frame.opacity !== '1' || frame.transform !== 'none' || frame.entrances !== 0)).toEqual([]);
});

/* Round three, 24 Sept 2026: the Ask page's opt out belongs to its own mount.
   A real route change from Ask mounts a new route layer, and that layer still
   takes the entrance. */
test('under default motion a real route change from Ask still takes the route entrance', async ({page}) => {
  await setup(page);
  await page.emulateMedia({reducedMotion: 'no-preference'});
  await page.goto('/#/console?work=ask');
  await expect(page.getByRole('textbox', {name: 'Your question'})).toBeVisible();
  await expect(page.locator('#main-content')).toHaveCount(1);
  const entrance = await page.evaluate(() => new Promise((resolve) => {
    const layer = document.getElementById('main-content');
    const seen = [];
    const read = () => {
      const now = document.getElementById('main-content');
      if (now && now !== layer) seen.push({opacity: Number(getComputedStyle(now).opacity), entrances: now.getAnimations().filter((move) => move.animationName === 'routeIn').length});
      if (seen.length < 3) requestAnimationFrame(read); else resolve(seen);
    };
    window.location.hash = '#/method';
    requestAnimationFrame(read);
  }));
  expect(entrance.some((frame) => frame.entrances === 1 && frame.opacity < 1)).toBe(true);
});

/* Round three, 24 Sept 2026: while the sign-in check is out the shell shows
   the route wait, whose sentence is larger than any line on the Ask page.
   It stood outside the route layer's entrance, so on a slow check Chrome
   counted it as the largest paint and nothing the Ask page painted after it
   could count. It now enters as the route layer does, so the Ask page's own
   suggestion is the largest paint however long the check takes. */
test('under default motion a slow sign-in check never makes its wait the largest paint on Ask', async ({page}) => {
  await page.addInitScript(() => {
    window.__lcp = [];
    new PerformanceObserver((list) => { for (const entry of list.getEntries()) window.__lcp.push({name: entry.element ? String(entry.element.className) : '', text: entry.element ? entry.element.textContent.slice(0, 40) : ''}); }).observe({type: 'largest-contentful-paint', buffered: true});
  });
  const state = await setup(page, {health: 'hold'});
  await page.emulateMedia({reducedMotion: 'no-preference'});
  await page.goto('/#/console?work=ask');
  await expect(page.getByText('The workspace opens once its code and its reading have arrived.', {exact: true})).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(600);
  await state.releaseHealth();
  await expect(page.getByRole('textbox', {name: 'Your question'})).toBeVisible();
  await expect(page.locator('.workbench-coverage')).toHaveText('Covers South Africa, 25 Aug to 7 Sept 2026.');
  await page.waitForTimeout(500);
  const entries = await page.evaluate(() => window.__lcp);
  expect(entries.filter((entry) => entry.name.includes('state-view') || entry.name.includes('workbench-coverage'))).toEqual([]);
  expect(entries.at(-1).name).toContain('workbench-ask-suggestion');
  expect(state.unknown).toEqual([]);
});

/* Under default motion the dock around the question box takes the entrance
   reveal and settles fully shown, while the Ask card above it takes none.
   Round three, 24 Sept 2026: in the redesign the question field and its
   label, hint and Send are the dock, and the rest of the desk stands in for
   the Ask card. */
test('under default motion the dock reveals and the Ask card does not', async ({page}) => {
  const state = await setup(page);
  await page.emulateMedia({reducedMotion: 'no-preference'});
  await page.goto('/#/console?work=ask');
  const box = page.getByRole('textbox', {name: 'Your question'});
  await expect(box).toBeVisible();
  const motion = () => box.evaluate((node) => {
    const stack = node.closest('.m-stack');
    const around = [];
    for (let at = node.parentElement; at && at !== stack; at = at.parentElement) around.push(getComputedStyle(at).animationName);
    return {around, stack: getComputedStyle(stack).animationName, offers: getComputedStyle(document.querySelector('.chat-starters')).animationName};
  });
  const read = await motion();
  expect(read.around.filter((name) => name === 'reveal')).toHaveLength(1);
  expect(read.stack).toBe('none');
  expect(read.offers).toBe('none');
  await expect.poll(() => box.evaluate((node) => {
    let opacity = 1;
    for (let at = node; at; at = at.parentElement) opacity *= Number(getComputedStyle(at).opacity);
    return opacity;
  })).toBe(1);
  expect(state.unknown).toEqual([]);
});

test('an unreadable coverage names no dates on the Ask page', async ({page}) => {
  await setup(page, {coverage: {contract_version: 'general_question_coverage_v1', state: 'unavailable', window: null, cutoff_date: null}});
  await page.goto('/#/console?work=ask');
  await expect(page.getByRole('textbox', {name: 'Your question'})).toBeVisible();
  await expect(page.locator('.workbench-coverage')).toHaveCount(0);
  await expect(page.getByText('Ask covers', {exact: false})).toHaveCount(0);
  await expect(page.getByText('Ask window', {exact: false})).toHaveCount(0);
});

test('an answer puts its request id in the address and reopens from it', async ({page}) => {
  const intelligence = intelligenceFixture();
  const state = await setup(page, {terminal: {answer: '', sources: [], intelligence}});
  await ask(page);
  await expect(page.getByText(intelligence.claims[0].text, {exact: true})).toBeVisible();
  await expect(page).toHaveURL(SHARED);
  await page.reload();
  /* Round three, 24 Sept 2026: the redesign heads a stored answer with its
     own question rather than a "Stored question" heading and keeps the
     bookkeeping one click down, so those are kept; the requested window line
     takes the Ask copy fix's words, which say only what the reply covers, in
     the redesign's readable dates. */
  await expect(page.getByRole('heading', {name: QUESTION, exact: true})).toBeVisible();
  await expect(page.getByRole('link', {name: 'Back to Fieldwork', exact: true})).toBeVisible();
  await page.getByText('Request and usage details', {exact: true}).click();
  await expect(page.getByText('Requested window: none was sent with the question. The reply covers 23 Aug to 5 Sept 2026.', {exact: true})).toBeVisible();
  await expect(page.getByText('USD allowance: US$0.10. This is a reserved allowance, not measured spend or vendor credits.', {exact: true})).toBeVisible();
  await expect(page.getByText(intelligence.claims[0].text, {exact: true})).toBeVisible();
  expect(state.reads).toEqual([intelligence.request_id]);
  expect(state.posts).toHaveLength(1);
  expect(state.unknown).toEqual([]);
});

test('a new question leaves the shared address of the previous answer', async ({page}) => {
  const intelligence = intelligenceFixture();
  const state = await setup(page, {terminal: {answer: '', sources: [], intelligence}});
  await ask(page);
  await expect(page).toHaveURL(SHARED);
  state.hold();
  await page.getByRole('textbox', {name: FOLLOW_UP}).fill('And in Nigeria?');
  await page.getByRole('textbox', {name: FOLLOW_UP}).press('Enter');
  await expect.poll(() => state.posts.length).toBe(2);
  await expect(page).toHaveURL(/#\/console\?work=ask$/);
  await expect(page.getByRole('link', {name: 'Back to Fieldwork', exact: true})).toHaveCount(0);
  await expect(page.getByText('And in Nigeria?', {exact: true})).toBeVisible();
  await expect(page.getByText(intelligence.claims[0].text, {exact: true})).toBeVisible();
});

for (const [failure, words] of [['lost', 'The connection ended before the service confirmed this question, so it may already have started. Retry sends it again as the same request, so it cannot start a second one.'], [503, 'The question service had a temporary problem, so this question may not have started. Retry sends it again as the same request, so it cannot start a second one.']]) {
  test(`an unconfirmed submission (${failure}) retries once as the same request`, async ({page}) => {
    const state = await setup(page, {failOnce: failure});
    await ask(page);
    await expect(page.getByText(words, {exact: true})).toBeVisible();
    expect(state.posts).toHaveLength(1);
    await page.getByRole('button', {name: 'Retry', exact: true}).click();
    await expect(page.getByText('The existing request returned its reply.', {exact: true})).toBeVisible();
    expect(state.posts).toHaveLength(2);
    expect(state.posts[1]).toEqual(state.posts[0]);
    expect(state.posts[0].idempotency_key).toMatch(/^ask-[0-9a-f-]{36}$/);
    await expect(page.getByText(words, {exact: true})).toHaveCount(0);
  });
}

test('each new question carries its own request key', async ({page}) => {
  const state = await setup(page);
  await ask(page);
  await expect(page.getByText('The existing request returned its reply.', {exact: true})).toBeVisible();
  await page.getByRole('textbox', {name: FOLLOW_UP}).fill('And in Nigeria?');
  await page.getByRole('textbox', {name: FOLLOW_UP}).press('Enter');
  await expect.poll(() => state.posts.length).toBe(2);
  expect(state.posts[1].idempotency_key).not.toBe(state.posts[0].idempotency_key);
});

test('a new question waits for its reply and only a recheck names the existing request', async ({page}) => {
  const state = await setup(page, {hold: true});
  await ask(page);
  await expect(page.getByText('Waiting for the reply', {exact: true})).toBeVisible();
  await expect(page.getByText('Waiting for the existing request', {exact: true})).toHaveCount(0);
  await page.reload();
  await page.getByRole('button', {name: 'Check existing request'}).click();
  await expect(page.getByText('Checking the existing request', {exact: true})).toBeVisible();
  state.release();
  await expect(page.getByText('The existing request returned its reply.', {exact: true})).toBeVisible();
  expect(state.posts).toHaveLength(1);
});
