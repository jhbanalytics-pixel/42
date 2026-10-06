/* Load and response certification over the populated fixtures.

   One case per route, theme and viewport measures largest contentful paint
   and cumulative layout shift through PerformanceObserver on a cold load in
   a fresh browser context and again on a warm load, a reload of the same
   page in the same context, then presses the route's primary action and reads the
   time to the first visible feedback: a change in the DOM or in the pressed
   control's painted style, both timed inside the page from the pointer press.
   The fixture answers every read at once and no throttling is applied, and
   both facts are written on every row. On the ask route the send is held by
   the fixture, so the answer latency is recorded apart as a fixture figure
   that never counts as a fast useful answer. Titles carry a colon so the
   capability state reporter never reads them as state rows. */
import {expect, test} from '@playwright/test';
import {DESK_STATES} from './fixtures/42-capability-routes/_desk.js';
import {installFixtures, lensRosterFixture, shellFixtures} from './support/capability-harness.mjs';
import {
  FIXTURE_ANSWER, PRESSURE_ROUTES, THEMES, THRESHOLDS, VIEWPORTS, record, recordModelAnswer, retainFailureScreenshot, settle, themeInit, viewportName,
} from './support/certification.mjs';

const ASK_ROUTE = '#/console?work=ask';
const QUESTION = 'Which source carried the repair routine first?';
const ANSWER_WAIT_MS = 1500;

const ROUTES = {
  '#/pulse': {
    capability_id: 'today_briefing',
    fixtures: () => ({...shellFixtures(), '/api/desk': DESK_STATES.populated()}),
    async ready(page){ await expect(page.locator('.instrument-briefing[data-briefing-state="ready"]')).toBeVisible(); },
    primary: (page) => page.getByRole('button', {name: /^Compare /}).first(),
  },
  '#/explore': {
    capability_id: 'discovery_exploration',
    fixtures: () => ({...shellFixtures(), '/api/desk': DESK_STATES.populated()}),
    /* Quiet register, 23 Sept 2026: the count is in plain words, without the
       engine's "admitted". */
    async ready(page){ await expect(page.getByText('3 of 3 signals shown')).toBeVisible(); },
    primary: (page) => page.getByRole('button', {name: 'Open evidence', exact: true}).first(),
  },
  '#/compare': {
    capability_id: 'compare',
    fixtures: () => ({...shellFixtures(), '/api/desk': DESK_STATES.populated()}),
    async ready(page){ await expect(page.locator('article.comparison-instrument__signal')).toHaveCount(3); },
    primary: (page) => page.getByRole('button', {name: 'Select Balcony mix', exact: true}),
  },
  /* Ask redesign, 23 Sept 2026: the question field's accessible name is its visible
     label, "Your question" on an empty page. */
  [ASK_ROUTE]: {
    capability_id: 'questions_followups',
    fixtures: () => ({...shellFixtures(), '/api/chat/lenses': lensRosterFixture(), '/api/chat/send': {__hold: true}}),
    async ready(page){ await expect(page.getByRole('textbox', {name: 'Your question'})).toBeVisible(); },
    async prepare(page){ await page.getByRole('textbox', {name: 'Your question'}).fill(QUESTION); },
    primary: (page) => page.getByRole('button', {name: 'Send', exact: true}),
  },
};

/* Registered before any script of the page runs, on every navigation, so
   the buffered entries cover the first paint. */
function installObservers(){
  window.__certification = {lcp: [], shifts: []};
  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) window.__certification.lcp.push({startTime: entry.startTime, size: entry.size, tag: entry.element ? entry.element.tagName : null});
    }).observe({type: 'largest-contentful-paint', buffered: true});
    const describe = (node) => {
      if (!node || !node.tagName) return 'text';
      const classes = String(node.className || '').trim().split(/\s+/).filter(Boolean).slice(0, 2).join('.');
      return `${node.tagName.toLowerCase()}${classes ? '.' + classes : ''}`;
    };
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()){
        const sources = (entry.sources || []).slice(0, 3).map((source) => `${describe(source.node)} ${Math.round(source.previousRect.y)}>${Math.round(source.currentRect.y)}`);
        window.__certification.shifts.push({startTime: entry.startTime, value: entry.value, hadRecentInput: entry.hadRecentInput, sources});
      }
    }).observe({type: 'layout-shift', buffered: true});
  } catch (error){
    window.__certification.error = error.message;
  }
}

/* LCP is the last candidate the observer saw. CLS is the largest session
   window of shifts without recent input, one second gap and five second cap,
   with the plain sum recorded beside it. Cache state is read from the
   resource timing of the served assets rather than assumed: an asset came
   from cache when the browser says so, when nothing was transferred, or when
   a revalidation transferred less than the body it already held. */
function readVitals(){
  const store = window.__certification || {lcp: [], shifts: []};
  const last = store.lcp[store.lcp.length - 1] || null;
  let cls = 0, session = 0, sessionStart = 0, previous = 0;
  for (const shift of store.shifts){
    if (shift.hadRecentInput) continue;
    if (session > 0 && shift.startTime - previous < 1000 && shift.startTime - sessionStart < 5000) session += shift.value;
    else { session = shift.value; sessionStart = shift.startTime; }
    previous = shift.startTime;
    if (session > cls) cls = session;
  }
  const sum = store.shifts.filter((shift) => !shift.hadRecentInput).reduce((total, shift) => total + shift.value, 0);
  const largest = store.shifts.filter((shift) => !shift.hadRecentInput).sort((a, b) => b.value - a.value).slice(0, 2)
    .map((shift) => `${Math.round(shift.value * 10000) / 10000} at ${Math.round(shift.startTime)}ms from ${shift.sources.join(', ') || 'no source'}`);
  const assets = performance.getEntriesByType('resource').filter((entry) => /\/assets\//.test(entry.name));
  const cached = (entry) => entry.deliveryType === 'cache' || entry.transferSize === 0 || (entry.encodedBodySize > 0 && entry.transferSize < entry.encodedBodySize);
  return {
    lcp_ms: last ? Math.round(last.startTime * 10) / 10 : null,
    lcp_element: last ? last.tag : null,
    cls: Math.round(cls * 10000) / 10000,
    cls_sum: Math.round(sum * 10000) / 10000,
    shifts: store.shifts.length,
    largest,
    assets: {total: assets.length, from_cache: assets.filter(cached).length},
    observer_error: store.error || null,
  };
}

/* Armed on the control before the press. The pointer press stamps t0; the
   first DOM mutation anywhere in the body, the first frame on which the
   control's painted style differs from the snapshot, or a hash navigation
   stamps t1. Both stamps come from the page's own clock. */
function armFeedback(node){
  const snapshot = () => {
    const style = getComputedStyle(node);
    return [style.boxShadow, style.backgroundColor, style.color, style.transform, style.outlineStyle, style.outlineWidth, style.borderColor, style.opacity, style.textDecorationLine].join('|');
  };
  const state = {t0: null, t1: null, kind: null, before: snapshot()};
  window.__press = state;
  const mark = (kind) => {
    if (state.t0 === null || state.t1 !== null) return;
    state.t1 = performance.now();
    state.kind = kind;
  };
  const observer = new MutationObserver(() => mark('dom'));
  node.addEventListener('pointerdown', () => {
    state.t0 = performance.now();
    observer.observe(document.body, {subtree: true, childList: true, attributes: true, characterData: true});
    const poll = () => {
      if (state.t1 !== null) return;
      if (snapshot() !== state.before) return mark('style');
      if (performance.now() - state.t0 < 2000) requestAnimationFrame(poll);
      return null;
    };
    requestAnimationFrame(poll);
  }, {capture: true, once: true});
  window.addEventListener('hashchange', () => mark('navigation'), {once: true});
}

async function pressAndMeasure(page, locator){
  await locator.scrollIntoViewIfNeeded();
  await locator.evaluate(armFeedback);
  const box = await locator.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(60);
  await page.mouse.up();
  await page.waitForTimeout(250);
  return page.evaluate(() => {
    const state = window.__press;
    if (!state || state.t0 === null) return {ms: null, kind: 'no press observed'};
    return {ms: state.t1 === null ? null : Math.round((state.t1 - state.t0) * 10) / 10, kind: state.kind || 'no feedback within 2000ms'};
  });
}

test.afterEach(async ({page}, testInfo) => { await retainFailureScreenshot(page, testInfo); });

for (const viewport of VIEWPORTS){
  test.describe(`viewport ${viewportName(viewport)}`, () => {
    test.use({viewport: {...viewport}});
    for (const theme of THEMES){
      for (const route of PRESSURE_ROUTES){
        test(`pressure: ${route} ${theme} ${viewportName(viewport)}`, async ({page}, testInfo) => {
          const surface = ROUTES[route];
          await themeInit(page, theme);
          await page.addInitScript(installObservers);
          const state = await installFixtures(page, surface.fixtures());
          const base = {suite: '42-request-pressure.pw.mjs', capability_id: surface.capability_id, route, theme, viewport: viewportName(viewport), throttling: 'none'};

          const loads = {};
          for (const cache of ['cold', 'warm']){
            if (cache === 'warm') await page.reload();
            else await page.goto('/' + route);
            await surface.ready(page);
            await settle(page);
            const vitals = await page.evaluate(readVitals);
            loads[cache] = vitals;
            record(testInfo, {...base, check: 'lcp', cache_state: cache, value: vitals.lcp_ms, unit: 'ms', threshold: THRESHOLDS.lcp_ms, pass: vitals.lcp_ms !== null && vitals.lcp_ms <= THRESHOLDS.lcp_ms, detail: `element ${vitals.lcp_element}; assets ${vitals.assets.from_cache} of ${vitals.assets.total} from cache${vitals.observer_error ? '; observer ' + vitals.observer_error : ''}`});
            record(testInfo, {...base, check: 'cls', cache_state: cache, value: vitals.cls, unit: 'score', threshold: THRESHOLDS.cls, pass: vitals.cls <= THRESHOLDS.cls, detail: `${vitals.shifts} shift entries, plain sum ${vitals.cls_sum}${vitals.largest.length ? '; largest ' + vitals.largest.join('; ') : ''}`});
            expect.soft(vitals.lcp_ms, `${cache} load LCP observed`).not.toBeNull();
            expect.soft(vitals.lcp_ms ?? Infinity, `${cache} load LCP`).toBeLessThanOrEqual(THRESHOLDS.lcp_ms);
            expect.soft(vitals.cls, `${cache} load CLS`).toBeLessThanOrEqual(THRESHOLDS.cls);
          }
          expect.soft(loads.warm.assets.total, 'the warm load reported its served assets').toBeGreaterThan(0);

          if (surface.prepare) await surface.prepare(page);
          const primary = surface.primary(page);
          await expect(primary).toBeEnabled();
          const feedback = await pressAndMeasure(page, primary);
          record(testInfo, {...base, check: 'feedback', cache_state: 'warm', value: feedback.ms, unit: 'ms', threshold: THRESHOLDS.feedback_ms, pass: feedback.ms !== null && feedback.ms <= THRESHOLDS.feedback_ms, detail: `first feedback: ${feedback.kind}`});
          expect.soft(feedback.ms, 'visible feedback observed').not.toBeNull();
          expect.soft(feedback.ms ?? Infinity, 'visible feedback within the threshold').toBeLessThanOrEqual(THRESHOLDS.feedback_ms);

          if (route === ASK_ROUTE){
            await expect(page.getByText('Submitting the question', {exact: true})).toBeVisible();
            await page.waitForTimeout(ANSWER_WAIT_MS);
            const answered = await page.locator('.general-reply, .chat-structured-bubble').count();
            recordModelAnswer(testInfo, {
              route, theme, viewport: viewportName(viewport), kind: FIXTURE_ANSWER, answer_state: 'held',
              latency_ms: null, waited_ms: ANSWER_WAIT_MS, answer_rendered: answered > 0,
              deadline_ms: null, deadline_note: 'the approved absolute deadline including queue time is bound at the native run',
              counts_as_fast_useful_answer: false,
              note: 'the fixture holds /api/chat/send, so no answer arrives; feedback above is the submitting state, not an answer',
            });
            expect.soft(answered, 'a held send renders no answer').toBe(0);
          }

          expect(state.unexpected, 'every read was answered by a fixture').toEqual([]);
          expect(state.errors, 'no page error').toEqual([]);
        });
      }
    }
  });
}

test('bootstrap: rejected critical font requests still render Compare', async ({page}) => {
  await page.addInitScript(() => {
    Object.defineProperty(document.fonts, 'load', {value: () => Promise.reject(new Error('font fixture refusal'))});
  });
  const state = await installFixtures(page, ROUTES['#/compare'].fixtures());
  await page.goto('/#/compare');
  await ROUTES['#/compare'].ready(page);
  expect(state.unexpected, 'every read was answered by a fixture').toEqual([]);
  expect(state.errors, 'font refusal leaves no page error').toEqual([]);
});
