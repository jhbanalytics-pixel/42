/* Today on the 42 API (contract.md sections 4 and 5). The fixtures are
   core/api/today.build_today and build_trend over core/api/fixtures for
   30 September 2026, so the page is read against what f42-api serves. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';
import searchingNowFixture from './fixtures/searching-now.json';
import trendFixture from './fixtures/trend42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {getJson, fetchToday, fetchTrend, fetchAlerts, listInvestigations, listSchedules} = await import('../../api42.js');
const {TodayPage42} = await import('../../today42.jsx');
const {topicHref} = await import('../TrendCard.jsx');

const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});

function specificityFor(card, options = {}){
  const claimIds = new Set(Array.isArray(card.explanation_claim_ids) ? card.explanation_claim_ids : []);
  const claims = (Array.isArray(card.claims) ? card.claims : []).filter((claim) => claim && claimIds.has(claim.id));
  const citedIds = [...new Set(claims.flatMap((claim) => Array.isArray(claim.evidence_ids) ? claim.evidence_ids : []))];
  const quotePair = claims.flatMap((claim) => (Array.isArray(claim.quotes) ? claim.quotes : [])
    .filter((quote) => quote && Array.isArray(claim.evidence_ids) && claim.evidence_ids.includes(quote.evidence_id)))
    .find((quote) => citedIds.includes(quote.evidence_id)
      && (card.evidence || []).some((item) => item.id === quote.evidence_id));
  const quoteId = options.quoteId || (quotePair && quotePair.evidence_id);
  const localIds = options.localIds || [quoteId, ...citedIds.filter((id) => id !== quoteId
    && (card.evidence || []).some((item) => item.id === id))].slice(0, 2);
  return {
    status: 'pass',
    local_evidence_ids: localIds,
    quote: {evidence_id: quoteId, text: options.quoteText === undefined ? quotePair && quotePair.text : options.quoteText},
    why_now: options.whyNow === undefined ? card.explanation : options.whyNow,
    reason: options.reason === undefined ? null : options.reason,
  };
}

function setSpecificityQuote(card, text, evidenceId = card.specificity.quote.evidence_id, claimId = card.explanation_claim_ids[0]){
  card.specificity.quote = {evidence_id: evidenceId, text};
  const claim = card.claims.find((item) => item.id === claimId);
  claim.quotes = (Array.isArray(claim.quotes) ? claim.quotes : [])
    .filter((quote) => quote.evidence_id !== evidenceId)
    .concat({evidence_id: evidenceId, text});
}

function checkedCard(card, title = card.title, itemId = card.item_id){
  const copy = clone(card);
  copy.title = title;
  copy.item_id = itemId;
  copy.specificity = specificityFor(copy);
  return copy;
}

/* Every test answers fetch from a route table: path prefix to a reply. */
function serve(routes){
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), init});
    for (const [prefix, answer] of routes){
      if (String(url).startsWith(prefix)) return typeof answer === 'function' ? answer(url) : answer;
    }
    return reply(404, {error: 'not_found', message: 'No route'});
  };
}

beforeEach(() => {
  calls = [];
  localStorage.setItem('pulse_passcode', 'test-pass');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  globalThis.fetch = realFetch;
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };

function trackTodaySlowTimers(){
  const nativeSetTimeout = globalThis.setTimeout;
  const nativeClearTimeout = globalThis.clearTimeout;
  const scheduled = [];
  const cleared = [];
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (delay === 5000) {
      const timer = {callback, args, cleared: false, fired: false};
      scheduled.push(timer);
      return timer;
    }
    return nativeSetTimeout(callback, delay, ...args);
  };
  globalThis.clearTimeout = (timer) => {
    if (scheduled.includes(timer)) {
      if (!timer.cleared) cleared.push(timer);
      timer.cleared = true;
      return;
    }
    return nativeClearTimeout(timer);
  };
  return {
    scheduled,
    cleared,
    fire: (timer = scheduled[0]) => {
      if (timer && !timer.cleared && !timer.fired) {
        timer.fired = true;
        timer.callback(...timer.args);
      }
    },
    restore: () => {
      globalThis.setTimeout = nativeSetTimeout;
      globalThis.clearTimeout = nativeClearTimeout;
    },
  };
}

async function mount(props = {}, today = todayFixture){
  serve([['/api/today', reply(200, today)], ['/api/trends/', reply(200, trendFixture)]]);
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" {...props} />));
  await settle();
}

function resetRoot(){
  flushSync(() => root.unmount());
  root = createRoot(host);
}

const text = () => host.textContent.replace(/\s+/g, ' ');
const cards = () => [...host.querySelectorAll('[data-card]')];
const cardTitled = (title) => cards().find((card) => card.querySelector('h3').textContent === title);
const exactText = (scope, text) => [...scope.querySelectorAll('*')].filter((e) => e.children.length === 0 && e.textContent.trim() === text);
const button = (scope, label) => [...scope.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
const tab = (label) => [...host.querySelectorAll('[role="tab"]')].find((t) => t.textContent.trim() === label);
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));

test('getJson sends the passcode and returns the body', async () => {
  serve([['/api/today', reply(200, {ok: 1})]]);
  expect(await getJson('/api/today')).toEqual({ok: 1});
  expect(calls[0].init.headers['X-Passcode']).toBe('test-pass');
});

test('getJson throws the API code and status, and marks 401 as auth', async () => {
  serve([['/api/today', reply(409, {error: 'not_ready', message: 'Today is not published yet for 2026-09-30.'})], ['/api/x', reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})]]);
  const notReady = await getJson('/api/today').catch((error) => error);
  expect(notReady.status).toBe(409);
  expect(notReady.code).toBe('not_ready');
  expect(notReady.message).toBe('Today is not published yet for 2026-09-30.');
  expect(notReady.auth).toBeFalsy();
  const auth = await getJson('/api/x').catch((error) => error);
  expect(auth.status).toBe(401);
  expect(auth.code).toBe('unauthorized');
  expect(auth.auth).toBe(true);
});

test('fetchToday and fetchTrend build the contract paths', async () => {
  serve([['/api/', reply(200, {})]]);
  await fetchToday('2026-09-30');
  await fetchToday();
  await fetchTrend('abc 1', 'ZA', '2026-09-30');
  expect(calls.map((c) => c.url)).toEqual(['/api/today?date=2026-09-30', '/api/today', '/api/trends/abc%201?market=ZA&date=2026-09-30']);
});

test('heading, headline and the warm-up banner render once', async () => {
  await mount();
  const heading = host.querySelector('h1');
  expect(heading.textContent).toBe('Taking off, 30 September 2026');
  expect(heading.className).toContain('t42-heading');
  const headline = host.querySelector('.t42-headline');
  expect(headline.textContent).toBe(todayFixture.headline.text);
  expect(text().split('Warming up: day 2 of 14').length - 1).toBe(1);
  expect(calls[0].url).toBe('/api/today?date=2026-09-30');
});

/* Design review, 4 October 2026: a card whose Why now is the headline's
   own sentence does not say it twice in one view. */
test('a Why now that repeats the headline is marked so the card skips it', async () => {
  const today = clone(todayFixture);
  const za = today.markets.find((m) => m.market === today.headline.market);
  const card = [...za.cards, ...(za.more || [])].find((c) => c.item_id === today.headline.item_id);
  today.headline.text = card.explanation.trim();
  await mount({}, today);
  const marked = host.querySelector('.t42-card-headline-said');
  expect(marked).not.toBeNull();
  expect(marked.querySelector('.t42-card-title').textContent).toContain(card.title || '');
  expect(host.querySelectorAll('.t42-card-headline-said')).toHaveLength(1);
  const css = await Bun.file(new URL('../../styles/today42.css', import.meta.url)).text();
  expect(css).toMatch(/\.t42-card-headline-said \.t42-specificity-why-now \{ display: none; \}/);
});

test('a headline that differs from the Why now leaves the card whole', async () => {
  await mount();
  expect(host.querySelector('.t42-card-headline-said')).toBeNull();
});

test('an all-empty brief keeps the API heading and links to History once below the header', async () => {
  const today = clone(todayFixture);
  today.markets.forEach((market) => {
    market.cards = [];
    market.more = [];
  });
  delete today.markets.find((market) => market.market === 'KE').held_back.count;
  await mount({region: 'ALL'}, today);
  expect(host.querySelector('h1[data-today-loaded]').textContent).toBe(today.heading);
  const historyLinks = [...host.querySelectorAll('a[href="#/history"]')];
  expect(historyLinks).toHaveLength(1);
  const historyLine = historyLinks[0].closest('[data-today-history-link]');
  expect(historyLine).not.toBeNull();
  expect(historyLine.previousElementSibling).toBe(host.querySelector('header.t42-head'));
  expect(historyLinks[0].textContent).toBe('Choose a past brief in History');
  // Restated 4 October 2026: the empty-market summary is now the status block's bold lead,
  // with the held count in its own sentence beside the link to the held list.
  expect(host.querySelector('[data-market="KE"] [data-empty-market-reason]').textContent)
    .toBe('No trend cleared our checks in Kenya today.');
  expect(host.querySelector('[data-market="KE"]').querySelector('[data-held-count]').textContent).toBe('1 is held back.');
});

test('Today shows the late-run time carried by the selected market', async () => {
  const late = clone(todayFixture);
  const lateText = {ZA: 'Late run: checked at 18:47', NG: 'Late run: checked at 18:48', KE: 'Late run: checked at 18:49'};
  late.markets.forEach((market) => market.banners.push({kind: 'late_run', text: lateText[market.market]}));
  await mount({}, late);

  expect(host.querySelector('[data-market="ZA"] .t42-banner-late_run')?.textContent).toBe(lateText.ZA);
  expect(host.querySelector('[data-market="NG"]')).toBeNull();
  expect(text()).not.toContain(lateText.NG);

  click(host.querySelector('#t42-tab-NG'));
  expect(host.querySelector('[data-market="NG"] .t42-banner-late_run')?.textContent).toBe(lateText.NG);
  expect(host.querySelector('[data-market="ZA"]')).toBeNull();
  expect(text()).not.toContain(lateText.ZA);

  click(host.querySelector('#t42-tab-ALL'));
  expect(host.querySelector('[data-market="ZA"] .t42-banner-late_run')?.textContent).toBe(lateText.ZA);
  expect(host.querySelector('[data-market="NG"] .t42-banner-late_run')?.textContent).toBe(lateText.NG);
  expect(host.querySelector('[data-market="KE"] .t42-banner-late_run')?.textContent).toBe(lateText.KE);
  expect(host.querySelectorAll('[data-source-problem]')).toHaveLength(3);
  expect(host.querySelector('[data-market="NG"] [data-source-problem]').textContent).toBe('Some sources were incomplete today');
  const ngSourceDetails = host.querySelector('[data-market="NG"] details[data-section="source-details"]');
  expect(ngSourceDetails.open).toBe(false);
  expect(ngSourceDetails.textContent).toContain('Thin coverage: 3 of 5 collection series usable today');
  expect(ngSourceDetails.textContent).toContain('Reddit rising and hot: item count far from usual');
  expect(text()).toContain('Data issue: collection or detection failed for Kenya');
  expect(text().split('Warming up: day 2 of 14').length - 1).toBe(1);
});

test('Today escapes late-run text and leaves the banner absent when the payload omits it', async () => {
  const late = clone(todayFixture);
  const lateText = 'Late run: checked at 18:47 <script>alert(1)</script>';
  late.markets.find((market) => market.market === 'ZA').banners.push({kind: 'late_run', text: lateText});
  await mount({}, late);

  const banner = host.querySelector('[data-market="ZA"] .t42-banner-late_run');
  expect(banner.textContent).toBe(lateText);
  expect(banner.querySelector('script')).toBeNull();
  expect(host.querySelector('script')).toBeNull();
  expect(text()).toContain('Warming up: day 2 of 14');

  resetRoot();
  calls = [];
  await mount();
  click(host.querySelector('#t42-tab-ALL'));
  expect(host.querySelector('.t42-banner-late_run')).toBeNull();
  expect(text().split('Warming up: day 2 of 14').length - 1).toBe(1);
  const sourceDetails = host.querySelector('[data-market="NG"] details[data-section="source-details"]');
  expect(sourceDetails.open).toBe(false);
  expect(sourceDetails.textContent).toContain('Thin coverage: 3 of 5 collection series usable today');
  expect(text()).toContain('Data issue: collection or detection failed for Kenya');
});

test('default latest brief names an earlier date while an explicit date gives context', async () => {
  const nativeNow = Date.now;
  Date.now = () => Date.parse('2026-10-01T10:00:00+02:00');
  try {
    const earlier = clone(todayFixture);
    earlier.date = '2026-09-30';
    await mount({date: undefined}, earlier);
    expect(host.querySelector('[data-today-earlier]')?.textContent).toBe('Earlier brief: 30 September 2026. This is not today’s brief.');
    expect(calls.map((call) => call.url)).toEqual(['/api/today']);
    expect(calls.some((call) => String(call.init?.method || 'GET').toUpperCase() === 'POST')).toBe(false);

    resetRoot();
    calls = [];
    await mount({date: '2026-09-30'}, earlier);
    expect(host.querySelectorAll('[data-today-earlier]').length).toBe(0);
    expect(host.querySelector('[data-today-date-context]')?.textContent).toBe('Brief for 30 September 2026');
    expect(calls.map((call) => call.url)).toEqual(['/api/today?date=2026-09-30']);
    expect(calls.some((call) => String(call.init?.method || 'GET').toUpperCase() === 'POST')).toBe(false);
  } finally {
    Date.now = nativeNow;
  }
});

test('Today shows the API status caution and keeps the published counts', async () => {
  await mount();
  expect(host.querySelector('[data-today-status="partial"]')?.textContent).toBe('Some markets are incomplete: Kenya.');
  /* UX pass, 3 October 2026: the coverage strip left Today (Coverage is in
     the menu); the source line above the cards still names any gap. */
  expect(host.querySelector('[data-market="ZA"] [data-section="coverage"]')).toBeNull();
  expect(cards()).toHaveLength(1);
  expect(host.querySelector('[data-today-published-at]')?.textContent).toContain('SAST');
  expect(host.querySelector('[data-today-published-at]')?.textContent).not.toContain('2026-09-30T');
  expect(calls.map((call) => call.url)).toEqual(['/api/today?date=2026-09-30']);
  expect(calls.some((call) => String(call.init?.method || 'GET').toUpperCase() === 'POST')).toBe(false);

  resetRoot();
  calls = [];
  const issue = clone(todayFixture);
  issue.status = 'data_issue';
  await mount({}, issue);
  // Was 'Data issue: some data needed for this brief is incomplete.' The 3 October
  // review asked for calm, plain words that say what the reader will see below.
  expect(host.querySelector('[data-today-status="data_issue"]')?.textContent).toBe('Some data for this brief was incomplete, so the topics it affects are held back below with their reasons.');
  expect(host.querySelectorAll('[data-today-status="partial"]').length).toBe(0);
  expect(calls.map((call) => call.url)).toEqual(['/api/today?date=2026-09-30']);
  expect(calls.some((call) => String(call.init?.method || 'GET').toUpperCase() === 'POST')).toBe(false);

  resetRoot();
  calls = [];
  const published = clone(todayFixture);
  published.status = 'published';
  published.markets.forEach((market) => {
    market.status = 'published';
    market.banners = market.banners.filter((banner) => banner.kind !== 'data_issue');
  });
  await mount({}, published);
  expect(host.querySelectorAll('[data-today-status]').length).toBe(0);
  expect(host.querySelectorAll('.t42-banner-data_issue')).toHaveLength(0);
  expect(calls.map((call) => call.url)).toEqual(['/api/today?date=2026-09-30']);
  expect(calls.some((call) => String(call.init?.method || 'GET').toUpperCase() === 'POST')).toBe(false);
});

/* Restated 6 October 2026 (tester report, 5 October): a brief is partial
   when a held topic's explanation failed its checks, and "Some markets are
   incomplete" or "shown without an explanation yet" then read as a broken
   market, though Today never shows an unexplained card. Only a market with a
   real data problem is named; topics held by the checks are counted under
   each market with their reasons. */
test('a partial brief whose markets have no data problem names no market as incomplete', async () => {
  const today = clone(todayFixture);
  today.status = 'partial';
  today.markets.forEach((market) => {
    market.status = market.market === 'ZA' ? 'partial' : 'published';
    market.banners = market.banners.filter((banner) => banner.kind !== 'data_issue');
  });
  const za = today.markets.find((market) => market.market === 'ZA');
  za.held_back.items.push({item_id: 'held-explanation', title: 'Held explanation', rule: 'G10', reason: 'explanation_failed',
    reason_text: 'The explanation did not pass our checks', reason_raw: 'Explanation failed its checks', evidence_ids: [], evidence: []});
  za.held_back.count = za.held_back.items.length;
  await mount({}, today);
  expect(host.querySelectorAll('[data-today-status="partial"]')).toHaveLength(0);
  expect(text()).not.toContain('Some markets are incomplete');
  expect(text()).not.toContain('without an explanation yet');
  expect(text()).toContain('The explanation did not pass our checks');
  expect([...host.querySelectorAll('.t42-glance-note')]).toHaveLength(0);
});

test('a partial brief names only the market with a real data problem', async () => {
  const today = clone(todayFixture);
  today.status = 'partial';
  today.markets.forEach((market) => { market.status = market.market === 'KE' ? 'published' : 'partial'; });
  await mount({}, today);
  // KE keeps its "collection or detection failed" banner; ZA and NG are partial only through the checks.
  expect(host.querySelector('[data-today-status="partial"]')?.textContent).toBe('Some markets are incomplete: Kenya.');
  const notes = [...host.querySelectorAll('[data-glance-market]')]
    .filter((market) => market.querySelector('.t42-glance-note')).map((market) => market.getAttribute('data-glance-market'));
  expect(notes).toEqual(['KE']);
});

test('checked holds are named without calling the run incomplete', async () => {
  const today = clone(todayFixture);
  const ng = today.markets.find((market) => market.market === 'NG');
  ng.status = 'partial';
  ng.cards = [];
  ng.more = [];
  ng.held_back = {count: 1, items: [{item_id: 'checked-hold', title: 'Checked topic', rule: 'G10', reason: 'explanation_failed',
    reason_text: 'The explanation did not pass our checks', failed_reason: 'A simpler explanation was not ruled out', evidence: []}]};
  await mount({region: 'NG'}, today);
  expect(host.querySelector('[data-today-held-status]')?.textContent).toBe('All the topics checked in Nigeria were held back. See their reasons below.');
  expect(host.querySelector('[data-today-status="partial"]')?.textContent).toBe('Some markets are incomplete: Kenya.');
  expect(host.querySelector('[data-glance-market="NG"] .t42-glance-note')).toBeNull();
});

test('checked-held summary stays limited to checked topics when the omission audit is nonempty, empty, or unknown', async () => {
  const audits = [
    {detect_run_id: 'detect-fixture', pool_limit: 90, judged_limit: 10, count: 1, items: [
      {item_id: 'not-checked', title: 'Unassessed topic', status: 'not_assessed', reason: 'judged_limit_reached',
        reason_text: 'The morning limit was reached; checks did not run.', sql_rank: 18, pool_rank: 18, market_scope: 'market'}]},
    {detect_run_id: 'detect-fixture', pool_limit: 90, judged_limit: 10, count: 0, items: []},
    null,
    undefined,
  ];
  for (const audit of audits){
    const today = clone(todayFixture);
    const ng = today.markets.find((market) => market.market === 'NG');
    ng.status = 'partial';
    ng.cards = [];
    ng.more = [];
    ng.held_back = {count: 1, items: [{item_id: 'checked-hold', title: 'Checked topic', rule: 'G10', reason: 'explanation_failed',
      reason_text: 'The explanation did not pass our checks', failed_reason: 'A simpler explanation was not ruled out', evidence: []}]};
    if (audit === undefined) delete ng.not_assessed;
    else ng.not_assessed = audit;
    await mount({region: 'NG'}, today);
    expect(host.querySelector('[data-today-held-status]')?.textContent).toBe('All the topics checked in Nigeria were held back. See their reasons below.');
    expect(text()).not.toContain('All topics were held after checks');
    expect(cards().length).toBe(0);
    const omissions = host.querySelector('[data-market="NG"] [data-not-assessed]');
    expect(Boolean(omissions)).toBe(true);
    if (audit?.count === 1){
      expect(omissions.textContent).toContain('Unassessed topic');
      expect(omissions.textContent).toContain('checks did not run');
      expect(omissions.querySelectorAll('[data-held-row], [data-card]').length).toBe(0);
    } else if (audit?.count === 0){
      expect(omissions.textContent).toContain('No topics were left unassessed.');
    } else {
      expect(omissions.textContent).toContain('Selection audit unavailable for this brief.');
      expect(omissions.textContent).not.toContain('No topics were left unassessed.');
    }
    expect(host.querySelector('[data-glance-market="NG"]').textContent).toContain('0 trends cleared');
    expect(host.querySelector('[data-glance-market="NG"]').textContent).toContain('1 held back');
    resetRoot();
  }
});

test('an unreadable card cannot turn a partly accounted market into a fully checked held market', async () => {
  const today = clone(todayFixture);
  const ng = today.markets.find((market) => market.market === 'NG');
  ng.cards[0].explained = false;
  ng.more = [];
  ng.held_back = {count: 1, items: [{item_id: 'checked-hold', title: 'Checked topic', rule: 'G10', reason: 'explanation_failed',
    reason_text: 'The explanation did not pass our checks', failed_reason: 'A simpler explanation was not ruled out', evidence: []}]};
  await mount({region: 'NG'}, today);
  expect(host.querySelectorAll('[data-today-held-status]').length).toBe(0);
});

test('an explicit model interruption stays incomplete and an unknown check state stays neutral', async () => {
  for (const failed_reason of ['Model busy: not explained before the deadline', null]){
    const today = clone(todayFixture);
    const ng = today.markets.find((market) => market.market === 'NG');
    ng.status = 'partial';
    ng.cards = [];
    ng.more = [];
    ng.held_back = {count: 1, items: [{item_id: 'unfinished-hold', title: 'Unfinished topic', rule: 'G10', reason: 'explanation_failed',
      reason_text: 'The explanation did not pass our checks', failed_reason, evidence: []}]};
    await mount({region: 'NG'}, today);
    expect(host.querySelector('[data-today-status="partial"]')?.textContent).toBe(failed_reason
      ? 'Some markets are incomplete: Nigeria, Kenya.' : 'Some markets are incomplete: Kenya.');
    expect(host.querySelector('[data-today-held-status]')).toBeNull();
    expect(host.querySelector('[data-glance-market="NG"] .t42-glance-note')?.textContent || null).toBe(failed_reason ? ' · incomplete' : null);
    resetRoot();
  }
});

test('publication time is omitted when its timestamp is invalid, timezone-less, or null', async () => {
  const invalidValues = ['not a timestamp', '2026-09-30T06:14:40', null];
  for (const published_at of invalidValues){
    const response = clone(todayFixture);
    response.published_at = published_at;
    await mount({}, response);
    expect(host.querySelectorAll('[data-today-published-at]').length).toBe(0);
    expect(calls.filter((call) => call.url.startsWith('/api/today'))).toHaveLength(1);
    expect(calls.some((call) => String(call.init?.method || 'GET').toUpperCase() === 'POST')).toBe(false);
    resetRoot();
    calls = [];
  }
});

test('the default tab follows the region and tabs switch markets', async () => {
  await mount({region: 'NG'});
  expect(tab('Nigeria').getAttribute('aria-selected')).toBe('true');
  expect(cards()[0].querySelector('h3').textContent).toBe('#fixture_ng_owambe');
  expect(text()).toContain('Thin coverage: 3 of 5 collection series usable today');
  click(tab('South Africa'));
  expect(tab('South Africa').getAttribute('aria-selected')).toBe('true');
  expect(cards()).toHaveLength(1);
  expect(cards()[0].querySelector('h3').textContent).toBe('#fixture_za_step');
});

/* Visual pass, 3 October 2026: the glance band leads with items collected, draws
   the located share as a labelled bar, counts only cards Today will show,
   names each column for a screen reader, and opens its market. */
test('the market glance leads with items collected, labels its bar and opens its market', async () => {
  await mount({region: 'NG'});
  const columns = [...host.querySelectorAll('[data-today-glance] button.t42-glance-market')];
  expect(columns).toHaveLength(3);
  expect(host.querySelector('[data-today-glance] [data-glance-window]').textContent).toBe('Items collected for 30 September 2026. Each market counts every item its searches, lists and boards returned, so a post found twice counts twice.');
  const za = columns[0];
  expect(za.querySelector('.t42-glance-value').textContent).toBe('1 700');
  expect(za.querySelector('.t42-glance-bar-label').textContent).toBe('45% with a known location');
  expect(za.querySelector('.t42-glance-cleared').textContent).toBe('1 trend cleared');
  expect(za.getAttribute('aria-label')).toBe('Show South Africa: 1 700 items collected, 45% with a known location, 1 trend cleared, 2 held back');
  expect(columns[2].getAttribute('aria-label')).toMatch(/^Show Kenya: 240 items collected, (?:\d+% with a known location, )?0 trends cleared/);
  expect(columns[1].getAttribute('aria-pressed')).toBe('true');
  click(za);
  expect(za.getAttribute('aria-pressed')).toBe('true');
  expect(tab('South Africa').getAttribute('aria-selected')).toBe('true');
  expect(cards()[0].querySelector('h3').textContent).toBe('#fixture_za_step');
});

test('market tabs report selection to the shared region', async () => {
  const selected = [];
  await mount({onRegionChange: (region) => selected.push(region)});
  expect(host.querySelector('.t42-headline').textContent).toBe(todayFixture.headline.text);
  const requestCount = calls.length;
  expect(calls.filter((call) => call.url.startsWith('/api/today'))).toHaveLength(1);
  click(tab('Nigeria'));
  expect(selected).toEqual(['NG']);
  expect(tab('Nigeria').getAttribute('aria-selected')).toBe('true');
  expect(cards()[0].querySelector('h3').textContent).toBe('#fixture_ng_owambe');
  const ngCard = todayFixture.markets.find((market) => market.market === 'NG').cards[0];
  expect(host.querySelector('.t42-headline').textContent).toBe(ngCard.explanation);
  expect(cards()[0].querySelector('.tc-title-link').getAttribute('href')).toContain(ngCard.item_id);
  expect(host.querySelector('.t42-lead-figure').getAttribute('data-query-id')).toBe(ngCard.numbers[0].query_id);
  click(tab('Kenya'));
  expect(selected).toEqual(['NG', 'KE']);
  expect(tab('Kenya').getAttribute('aria-selected')).toBe('true');
  expect(host.querySelectorAll('.t42-headline').length).toBe(0);
  click(tab('All'));
  expect(selected).toEqual(['NG', 'KE', 'ALL']);
  expect(tab('All').getAttribute('aria-selected')).toBe('true');
  expect(host.querySelector('.t42-headline').textContent).toBe(todayFixture.headline.text);
  expect([...host.querySelectorAll('[data-market]')].map((section) => section.getAttribute('data-market'))).toEqual(['ZA', 'NG', 'KE']);
  click(tab('South Africa'));
  expect(selected).toEqual(['NG', 'KE', 'ALL', 'ZA']);
  expect(host.querySelector('.t42-headline').textContent).toBe(todayFixture.headline.text);
  expect(calls).toHaveLength(requestCount);
  expect(calls.some((call) => String(call.init?.method || 'GET').toUpperCase() === 'POST')).toBe(false);
});

test('an external region change applies the same headline tab scope', async () => {
  await mount();
  expect(host.querySelector('.t42-headline').textContent).toBe(todayFixture.headline.text);
  const renderRegion = async (region) => {
    flushSync(() => root.render(<TodayPage42 region={region} date="2026-09-30" />));
    await settle();
  };
  await renderRegion('NG');
  expect(tab('Nigeria').getAttribute('aria-selected')).toBe('true');
  const ngCard = todayFixture.markets.find((market) => market.market === 'NG').cards[0];
  expect(host.querySelector('.t42-headline').textContent).toBe(ngCard.explanation);
  expect(cards()[0].querySelector('.tc-title-link').getAttribute('href')).toContain(ngCard.item_id);
  expect(host.querySelector('.t42-lead-figure').getAttribute('data-query-id')).toBe(ngCard.numbers[0].query_id);
  await renderRegion('KE');
  expect(tab('Kenya').getAttribute('aria-selected')).toBe('true');
  expect(host.querySelectorAll('.t42-headline').length).toBe(0);
  await renderRegion('ALL');
  expect(tab('All').getAttribute('aria-selected')).toBe('true');
  expect(host.querySelector('.t42-headline').textContent).toBe(todayFixture.headline.text);
  await renderRegion('ZA');
  expect(tab('South Africa').getAttribute('aria-selected')).toBe('true');
  expect(host.querySelector('.t42-headline').textContent).toBe(todayFixture.headline.text);
  expect(calls.filter((call) => call.url.startsWith('/api/today'))).toHaveLength(1);
  expect(calls.some((call) => String(call.init?.method || 'GET').toUpperCase() === 'POST')).toBe(false);
});

test('All shows the top three cards of each market', async () => {
  await mount({region: 'ALL'});
  expect(tab('All').getAttribute('aria-selected')).toBe('true');
  const sections = [...host.querySelectorAll('[data-market]')];
  expect(sections.map((s) => s.getAttribute('data-market'))).toEqual(['ZA', 'NG', 'KE']);
  expect(sections.map((s) => s.querySelectorAll('[data-card]').length)).toEqual([1, 1, 0]);
  expect(button(host, 'Show all')).toBeUndefined();
});

test('the admitted card keeps its tag, state, count line and explanation', async () => {
  await mount();
  const first = cardTitled('#fixture_za_step');
  expect(first.textContent).toContain('Moved up');
  expect(first.textContent).toContain('Emerging');
  /* Visual pass, 3 October 2026: the count is set large in the card's figure
     panel ("31 creators in 3 days"), so the small count line that says the
     same fact gives way to it, as it already did to reach. */
  expect(first.querySelector('.tc-big-lead').textContent).toBe('31 creators in 3 days');
  expect(first.querySelector('.tc-big-lead').getAttribute('data-query-id')).toBe('q_creators3_za_a');
  expect(first.textContent).toContain('#fixture_za_step is spreading on Tiktok in South Africa, possibly tied to the weekend.');
  expect(first.querySelector('.t42-flag')).toBeNull();
  expect(cards()).toHaveLength(1);
});

test('Today labels the creator and post count window while keeping the supplied figures', async () => {
  await mount();
  const market = host.querySelector('[data-market="ZA"]');
  expect(market.querySelector('[data-today-count-window]')?.textContent).toBe('Creator and post counts are in the last 3 days.');
  expect(cardTitled('#fixture_za_step').querySelector('.tc-big-lead').textContent).toBe('31 creators in 3 days');
  expect(cardTitled('#fixture_za_step').querySelector('.tc-big-lead').getAttribute('data-query-id')).toBe('q_creators3_za_a');
  click(tab('Kenya'));
  expect(host.querySelector('[data-today-count-window]')).toBeNull();
});

test('no tag word shows on the first published morning', async () => {
  const today = clone(todayFixture);
  for (const card of today.markets[0].cards) card.tag = null;
  await mount({}, today);
  const first = cardTitled('#fixture_za_step');
  expect(first.querySelector('.t42-tag')).toBeNull();
  expect(first.textContent).not.toContain('Moved up');
});

test('Today hides a card whose explanation failed', async () => {
  await mount();
  expect(cardTitled('#fixture_za_spring')).toBeUndefined();
});

test('Today hides cards whose explained value is false, even when their explanation status has a known value', async () => {
  const today = clone(todayFixture);
  const [first, second] = today.markets[0].cards;
  Object.assign(first, {explained: false, explanation: null, claims: [], explanation_status: 'not_run'});
  Object.assign(second, {explained: false, explanation: null, claims: [], explanation_status: 'failed_checks'});
  await mount({}, today);
  expect(cardTitled('#fixture_za_step')).toBeUndefined();
  expect(cardTitled('fixture za sound one')).toBeUndefined();
});

test('Today renders its cards with the shared trend card, and shows no Watch button', async () => {
  const source = await Bun.file(new URL('../../today42.jsx', import.meta.url)).text();
  expect(source).toMatch(/from '\.\/ui\/TrendCard\.jsx'/);
  expect(source).not.toMatch(/function Card\(/);
  await mount();
  expect(cards().every((card) => !button(card, 'Watch'))).toBe(true);
});

test('the Ask link carries the question, market, item and date', async () => {
  await mount();
  const first = cardTitled('#fixture_za_step');
  const link = [...first.querySelectorAll('a')].find((a) => a.textContent === 'Ask about this');
  const card = todayFixture.markets[0].cards[0];
  expect(link.getAttribute('href')).toBe('#/ask?q=' + encodeURIComponent(card.ask) + '&market=ZA&item=' + card.item_id + '&date=2026-09-30');
});

test('the sparkline leaves gaps as breaks and draws the band only when expected values exist', async () => {
  await mount();
  const svg = cardTitled('#fixture_za_step').querySelector('svg');
  const line = svg.querySelector('.t42-line').getAttribute('d');
  expect(line.match(/M/g)).toHaveLength(2);
  expect(svg.querySelector('.t42-band')).toBeNull();
  flushSync(() => root.unmount());
  root = createRoot(host);
  const today = clone(todayFixture);
  for (const point of today.markets[0].cards[0].sparkline.points){
    point.expected_low = 1;
    point.expected_high = 8;
  }
  await mount({}, today);
  expect(cardTitled('#fixture_za_step').querySelector('svg .t42-band')).not.toBeNull();
});

/* Design pass, 3 October 2026: the trend line names its first and last day
   under the baseline, so it carries a time scale. */
test('the sparkline names its first and last day', async () => {
  await mount();
  const card = cardTitled('#fixture_za_step');
  const points = todayFixture.markets[0].cards[0].sparkline.points;
  const day = (date) => Number(date.slice(8, 10)) + ' ' + ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][Number(date.slice(5, 7)) - 1];
  expect([...card.querySelectorAll('svg .t42-tick-day')].map((t) => t.textContent))
    .toEqual([day(points[0].date), day(points[points.length - 1].date)]);
});

/* UI polish, 2 October 2026: restated. A missing image used to leave an empty
   bordered box; it now leaves nothing, so only the real still shows. */
test('two thumbnails with alt text, and nothing where the image is missing', async () => {
  const today = clone(todayFixture);
  today.markets[0].cards[0].evidence.find((e) => e.id === 'ig_za_007').thumbnail_url = null;
  await mount({}, today);
  const first = cardTitled('#fixture_za_step');
  const images = first.querySelectorAll('img');
  expect(images).toHaveLength(1);
  expect(images[0].getAttribute('alt')).toBe('Post by @fixture_za_6 on TikTok');
  expect(first.querySelectorAll('.t42-thumb-empty')).toHaveLength(0);
});

/* UI polish, 2 October 2026: restated. A failed still now leaves no box. */
test('a failed thumbnail leaves no empty box behind', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  const post = card.evidence.find((e) => e.id === card.thumbnails[0]);
  post.handle = '@theonlywinnie5';
  post.platform = 'tiktok';
  await mount({}, today);
  const shown = cardTitled('#fixture_za_step');
  const image = [...shown.querySelectorAll('img')].find((img) => img.getAttribute('alt') === 'Post by @theonlywinnie5 on TikTok');
  expect(image).not.toBeNull();
  flushSync(() => image.dispatchEvent(new window.Event('error')));
  expect([...shown.querySelectorAll('img')].some((img) => img.getAttribute('alt') === 'Post by @theonlywinnie5 on TikTok')).toBe(false);
  expect(shown.querySelectorAll('.t42-thumb-empty')).toHaveLength(0);
  expect(shown.querySelectorAll('.t42-thumbs img')).toHaveLength(1);
});

test('a thumbnail can recover when its source URL changes', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  const post = card.evidence.find((e) => e.id === card.thumbnails[0]);
  today.markets[0].cards = [card, ...['Top two', 'Top three', 'Top four', 'Top five']
    .map((title, index) => checkedCard(card, title, 'top-thumbnail-' + index))];
  today.markets[0].more = [checkedCard(card, 'More qualified card', 'more-thumbnail-card')];
  await mount({}, today);
  const shown = cardTitled('#fixture_za_step');
  const image = [...shown.querySelectorAll('img')].find((img) => img.getAttribute('alt') === 'Post by @fixture_za_6 on TikTok');
  expect(image).not.toBeNull();
  flushSync(() => image.dispatchEvent(new window.Event('error')));
  post.thumbnail_url = 'https://example.invalid/thumb/refreshed.jpg';
  click(button(host, 'Show all'));
  const refreshed = [...cardTitled('#fixture_za_step').querySelectorAll('img')].find((img) => img.getAttribute('alt') === 'Post by @fixture_za_6 on TikTok');
  expect(refreshed.getAttribute('src')).toBe('https://example.invalid/thumb/refreshed.jpg');
});

/* UI polish, 2 October 2026: restated. An unsafe address now leaves no box. */
test('a thumbnail that is not a plain http or https address is never shown as an image', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  const [firstId, secondId] = card.thumbnails;
  card.evidence.find((e) => e.id === firstId).thumbnail_url = 'javascript:alert(1)';
  card.evidence.find((e) => e.id === secondId).thumbnail_url = 'https://user:secret@example.invalid/thumb.jpg';
  await mount({}, today);
  const first = cardTitled('#fixture_za_step');
  expect(first.querySelectorAll('img')).toHaveLength(0);
  expect(first.querySelector('.t42-thumbs')).toBeNull();
  expect([...host.querySelectorAll('img')].some((img) => /javascript|secret/i.test(img.getAttribute('src') || ''))).toBe(false);
});

test('a post with no handle is named by its platform alone, never "null"', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  const [firstId, secondId] = card.thumbnails;
  const first = card.evidence.find((e) => e.id === firstId);
  first.handle = null;
  first.platform = 'youtube';
  first.thumbnail_url = 'https://i.ytimg.com/vi/6LJ00I6yIlo/hqdefault.jpg';
  const second = card.evidence.find((e) => e.id === secondId);
  second.handle = null;
  /* UI polish, 2 October 2026: a missing image leaves no box, so the label is read from a still. */
  second.thumbnail_url = 'https://example.invalid/thumb/second.jpg';
  await mount({}, today);
  const shown = cardTitled('#fixture_za_step');
  const image = shown.querySelector('img');
  expect(image.getAttribute('src')).toBe('https://i.ytimg.com/vi/6LJ00I6yIlo/hqdefault.jpg');
  expect(image.getAttribute('alt')).toBe('Post on YouTube');
  expect(shown.querySelectorAll('img')[1].getAttribute('alt')).toBe('Post on Instagram');
  expect(shown.innerHTML).not.toContain('null');
});

test('a post or board collected as "twitter" is named X, never Twitter', async () => {
  const {platformWord} = await import('../TrendCard.jsx');
  expect(platformWord('twitter')).toBe('X');
  expect(platformWord('x')).toBe('X');
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  const [firstId, secondId] = card.thumbnails;
  const first = card.evidence.find((e) => e.id === firstId);
  first.handle = null;
  first.platform = 'twitter';
  /* UI polish, 2 October 2026: a missing image leaves no box, so the label is read from a still. */
  first.thumbnail_url = 'https://example.invalid/thumb/x.jpg';
  card.evidence.find((e) => e.id === secondId).platform = 'twitter';
  today.markets[0].boards[0].platform = 'twitter';
  await mount({}, today);
  const shown = cardTitled('#fixture_za_step');
  expect(shown.querySelector('.t42-thumbs img').getAttribute('alt')).toBe('Post on X');
  expect(host.querySelector('[data-section="boards"]').textContent).toContain("X's own list: Hashtag board, 7 days");
  expect(host.innerHTML).not.toMatch(/twitter/i);
});

test('only the loaded heading carries data-today-loaded', async () => {
  serve([['/api/today', reply(409, {error: 'not_ready', message: 'Today is not published yet for 2026-09-30.'})]]);
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
  await settle();
  expect(host.querySelector('h1.t42-heading')).not.toBeNull();
  expect(host.querySelector('[data-today-loaded]')).toBeNull();
  flushSync(() => root.unmount());
  root = createRoot(host);
  serve([['/api/today', reply(500, {error: 'internal', message: 'Something broke.'})]]);
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
  await settle();
  expect(text()).toContain('Today could not load');
  expect(host.querySelector('[data-today-loaded]')).toBeNull();
  flushSync(() => root.unmount());
  root = createRoot(host);
  await mount();
  const loaded = host.querySelector('[data-today-loaded]');
  expect(loaded).not.toBeNull();
  expect(loaded.tagName).toBe('H1');
  expect(loaded.textContent).toBe('Taking off, 30 September 2026');
});

test('Show all expands the more cards in place', async () => {
  const today = clone(todayFixture);
  const market = today.markets[0];
  const source = market.cards[0];
  market.cards = ['One', 'Two', 'Three', 'Four', 'Five'].map((title, index) => checkedCard(source, title, 'top-' + index));
  market.more = ['Six', 'Seven'].map((title, index) => checkedCard(source, title, 'more-' + index));
  await mount({}, today);
  expect(cards()).toHaveLength(5);
  const more = button(host, 'Show all');
  expect(more.getAttribute('aria-expanded')).toBe('false');
  click(more);
  expect(cards()).toHaveLength(7);
  expect(cardTitled('Seven')).toBeDefined();
  expect(button(host, 'Show fewer').getAttribute('aria-expanded')).toBe('true');
});

test('Kenya shows its data issue banner and no cards', async () => {
  await mount({region: 'KE'});
  const market = host.querySelector('[data-market="KE"]');
  const banners = [...host.querySelectorAll('.t42-banner')].map((b) => b.textContent);
  expect(banners).toContain('Data issue: collection or detection failed for Kenya');
  expect(banners).not.toContain('Thin coverage: 2 of 4 collection series usable today');
  expect(host.querySelector('.t42-banner-data_issue')).not.toBeNull();
  expect(market.querySelectorAll('[data-source-problem]')).toHaveLength(1);
  expect(market.querySelector('[data-source-problem]').textContent).toBe('Some sources were incomplete today');
  const details = market.querySelector('details[data-section="source-details"]');
  expect(details.open).toBe(false);
  expect(details.textContent).toContain('Thin coverage: 2 of 4 collection series usable today');
  expect(details.textContent).toContain('TikTok local feed: calls failed');
  expect(cards()).toHaveLength(0);
  expect(text()).toContain('First morning: nothing to compare yet');
});

test('source failure data_issue banners go into closed source details while other data issues stay visible', async () => {
  const today = clone(todayFixture);
  const market = today.markets.find((entry) => entry.market === 'KE');
  market.coverage.issues = [];
  market.banners = market.banners.filter((banner) => banner.kind !== 'thin_coverage');
  market.banners.push({kind: 'data_issue', text: '2 sources failed today: TikTok local feed, Reddit rising and hot'});
  await mount({region: 'KE'}, today);
  const shownMarket = host.querySelector('[data-market="KE"]');
  expect(shownMarket.querySelector('[data-source-problem]')?.textContent).toBe('Some sources were incomplete today');
  const details = shownMarket.querySelector('details[data-section="source-details"]');
  expect(details.open).toBe(false);
  expect(details.textContent).toContain('2 sources failed today: TikTok local feed, Reddit rising and hot');
  const visibleIssues = [...shownMarket.querySelectorAll('.t42-banner-data_issue')].map((banner) => banner.textContent);
  expect(visibleIssues).toContain('Data issue: collection or detection failed for Kenya');
  expect(visibleIssues).not.toContain('2 sources failed today: TikTok local feed, Reddit rising and hot');
});

test('a data issue without source evidence does not claim that sources were incomplete', async () => {
  const today = clone(todayFixture);
  const market = today.markets.find((entry) => entry.market === 'KE');
  market.coverage.issues = [];
  market.banners = market.banners.filter((banner) => banner.kind !== 'thin_coverage');
  await mount({region: 'KE'}, today);
  const shownMarket = host.querySelector('[data-market="KE"]');
  expect(shownMarket.querySelectorAll('[data-source-problem]')).toHaveLength(0);
  expect(shownMarket.querySelectorAll('details[data-section="source-details"]')).toHaveLength(0);
  expect(shownMarket.querySelector('.t42-banner-data_issue')?.textContent).toBe('Data issue: collection or detection failed for Kenya');
});

test('dropped since yesterday lists each item with its reason', async () => {
  await mount();
  const dropped = host.querySelector('[data-section="dropped"]');
  expect(dropped.querySelectorAll('li')).toHaveLength(3);
  expect(dropped.textContent).toContain('#fixture_za_picnic');
  expect(dropped.textContent).toContain('Reclassified as Seasonal');
  expect(dropped.textContent).toContain('Not confirmed today');
});

/* Review, 3 October 2026: the Left out line folds what 42 dropped or
   held. It counts both, starts open on a data issue, and is absent when there
   is nothing to fold, so an empty market never reads "Left out today:
   nothing" under its held list. */
test('the Left out line counts what it folds, opens on a data issue and is absent when empty', async () => {
  await mount();
  const za = host.querySelector('[data-section="left-out"]');
  expect(za.querySelector('summary').textContent).toBe('Left out today: 3 dropped since yesterday, 2 held back by our checks');
  expect(za.open).toBe(false);
  expect(za.querySelector('[data-section="dropped"]')).not.toBeNull();
  expect(za.querySelector('[data-section="held-back"]')).not.toBeNull();

  resetRoot();
  const issue = clone(todayFixture);
  issue.status = 'data_issue';
  await mount({}, issue);
  expect(host.querySelector('[data-section="left-out"]').open).toBe(true);

  resetRoot();
  const empty = clone(todayFixture);
  const market = empty.markets.find((entry) => entry.market === 'ZA');
  market.dropped = {first_morning: false, items: []};
  market.cards = [];
  market.more = [];
  await mount({}, empty);
  const shown = host.querySelector('[data-market="ZA"]');
  expect(shown.querySelectorAll('[data-section="left-out"]')).toHaveLength(0);
  expect(shown.textContent).not.toContain('Left out today');
});

/* Charts, 3 October 2026: the warm-up shows as a meter of its days, and a
   held list of two or more topics opens on a bar of why they were held. */
test('the warm-up meter marks the day reached, and held reasons chart only from two topics', async () => {
  await mount();
  const meter = host.querySelector('[data-today-warmup-meter]');
  expect(meter.querySelectorAll('.ch42-step')).toHaveLength(14);
  expect(meter.querySelectorAll('.ch42-step.is-now')).toHaveLength(1);
  const za = host.querySelector('[data-market="ZA"] [data-section="held-back"] [data-held-reasons]');
  expect(za.querySelectorAll('.ch42-part')).toHaveLength(2);
  resetRoot();
  await mount({region: 'NG'});
  expect(host.querySelector('[data-market="NG"] [data-held-reasons]')).toBeNull();
});

test('held back items open to their reason and evidence', async () => {
  await mount();
  const held = host.querySelector('[data-section="held-back"]');
  expect(held.querySelector('h3').textContent).toBe('Held back');
  expect(held.textContent).toContain('2 held back: too few creators, data issue');
  expect(held.textContent).not.toContain('@fixture_za_15');
  const opener = button(held, '#fixture_za_small');
  expect(opener.getAttribute('aria-expanded')).toBe('false');
  click(opener);
  expect(opener.getAttribute('aria-expanded')).toBe('true');
  expect(held.textContent).toContain('Too few creators');
  expect(held.textContent).toContain('@fixture_za_15');
  expect(held.textContent).toContain('Post 15: #fixture_za_small from our street stall');
});

test('each held item shows its reason and held detail beside its title before it is opened', async () => {
  const today = clone(todayFixture);
  const items = today.markets.find((market) => market.market === 'ZA').held_back.items;
  items[1].held_detail = 'A paid campaign could explain this, and the posts do not rule it out.';
  await mount({}, today);
  const held = host.querySelector('[data-section="held-back"]');
  const rows = [...held.querySelectorAll('.t42-rows > li')];
  expect(rows).toHaveLength(items.length);
  rows.forEach((row, index) => {
    expect(button(row, items[index].title).getAttribute('aria-expanded')).toBe('false');
    expect(row.querySelector('[data-held-row-reason]')?.textContent).toBe(items[index].reason_text);
  });
  expect(rows[1].querySelector('[data-held-row-detail]')?.textContent).toBe(items[1].held_detail);
  expect(rows[0].querySelectorAll('[data-held-row-detail]')).toHaveLength(0);
  expect(held.querySelectorAll('.t42-held-detail')).toHaveLength(0);
});

test('an opened held row names the trend once', async () => {
  const today = clone(todayFixture);
  const item = today.markets.find((market) => market.market === 'ZA').held_back.items[0];
  await mount({}, today);
  const held = host.querySelector('[data-section="held-back"]');
  click(button(held, item.title));
  const row = held.querySelector('.t42-held-detail').closest('li');
  expect(exactText(row, item.title).map((e) => e.tagName)).toEqual(['BUTTON']);
  expect(row.querySelectorAll('strong')).toHaveLength(0);
});

test('the empty-market held view still names each trend once', async () => {
  await mount({region: 'KE'});
  const item = host.querySelector('[data-market="KE"] [data-held-item-id]');
  const title = todayFixture.markets.find((market) => market.market === 'KE').held_back.items[0].title;
  expect(exactText(item, title).map((e) => e.tagName)).toEqual(['STRONG']);
});

test('a held row repeats neither a detail equal to its reason nor a missing reason', async () => {
  const today = clone(todayFixture);
  const items = today.markets.find((market) => market.market === 'ZA').held_back.items;
  items[0].held_detail = items[0].reason_text;
  items[1].reason_text = '  ';
  await mount({}, today);
  const rows = [...host.querySelectorAll('[data-section="held-back"] .t42-rows > li')];
  expect(rows[0].querySelectorAll('[data-held-row-detail]')).toHaveLength(0);
  expect(rows[1].querySelector('[data-held-row-reason]')?.textContent).toBe('No specific held reason was provided.');
});

test('populated held items show stored figures and the check detail beside the gate reason', async () => {
  const today = clone(todayFixture);
  const item = today.markets.find((market) => market.market === 'ZA').held_back.items[1];
  item.count_line = '0 posts, 3 days';
  item.numbers = [
    {value: 0, unit: 'posts in 3 days', query_id: 'q_held_zero', run_id: 'r_held_zero', result_hash: 'sha256:zero'},
    {value: null, unit: 'creators in 3 days', query_id: 'q_held_null', run_id: 'r_held_null', result_hash: 'sha256:null'},
  ];
  item.failed_reason = 'Critic: a simpler explanation was not ruled out: a paid campaign';
  item.held_detail = 'The stored evidence does not establish a recent local change.';
  await mount({}, today);
  const held = host.querySelector('[data-section="held-back"]');
  const opener = button(held, '#fixture_za_outage');
  click(opener);
  const detail = held.querySelector('.t42-held-detail');
  const row = detail.closest('li');
  expect(exactText(row, '#fixture_za_outage')).toHaveLength(1);
  expect(row.querySelector('[data-held-row-reason]')?.textContent).toBe('Data issue on TikTok in the last 3 days');
  expect(exactText(row, 'Data issue on TikTok in the last 3 days')).toHaveLength(1);
  expect(detail.textContent).toContain('Check detail:');
  expect(detail.textContent).toContain(item.failed_reason);
  expect(row.querySelector('[data-held-row-detail]')?.textContent).toBe(item.held_detail);
  expect(exactText(row, item.held_detail)).toHaveLength(1);
  expect(detail.textContent).toContain('Recorded figures');
  expect(detail.textContent).toContain(item.count_line);
  const figures = [...detail.querySelectorAll('[data-held-number]')];
  expect(figures).toHaveLength(2);
  expect(figures[0].textContent).toContain('0 posts in 3 days');
  expect(figures[0].getAttribute('data-query-id')).toBe('q_held_zero');
  expect(figures[1].textContent).toContain('Not recorded');
  expect(figures[1].textContent).toContain('creators in 3 days');
  expect(figures[1].textContent).not.toContain('0 creators');
  expect(figures[1].getAttribute('data-query-id')).toBe('q_held_null');
});

test('empty-market held items escape supplied text and keep only safe source links', async () => {
  const today = clone(todayFixture);
  const item = today.markets.find((market) => market.market === 'KE').held_back.items[0];
  item.title = '<img src=x onerror="alert(1)">';
  item.reason_text = '<svg onload="alert(1)">gate reason</svg>';
  item.failed_reason = '<script>alert(1)</script>';
  item.held_detail = '<script>window.heldDetail=1</script>';
  item.numbers = [{value: 12, unit: 'posts in 3 days', query_id: 'q_ke_held', run_id: 'r_ke_held', result_hash: 'sha256:ke'}];
  item.evidence[0].url = 'javascript:alert(1)';
  item.evidence[1].url = 'https://example.invalid/held/source-post';
  item.evidence[0].text = '<img src=x onerror="alert(1)">';
  await mount({region: 'KE'}, today);
  const details = host.querySelector('[data-market="KE"] details[data-section="held-for-evidence"]');
  details.open = true;
  const detail = details.querySelector('[data-held-item-id]');
  // Was: the reason inside each item. Items are now grouped by reason and the
  // reason is said once above its group, so it is read from the group.
  const group = detail.closest('[data-held-group]');
  expect(detail.textContent).toContain(item.title);
  expect(group.querySelector('[data-held-group-reason]').textContent).toContain(item.reason_text);
  expect(group.querySelectorAll('img, svg, script')).toHaveLength(0);
  expect(detail.textContent).toContain(item.failed_reason);
  expect(detail.querySelector('[data-held-detail]')?.textContent).toBe(item.held_detail);
  expect(detail.querySelectorAll('img, svg, script')).toHaveLength(0);
  expect(detail.querySelector('[data-query-id="q_ke_held"]')?.textContent).toContain('12 posts in 3 days');
  expect([...detail.querySelectorAll('a[href]')].map((link) => link.getAttribute('href'))).toEqual([
    'https://example.invalid/held/source-post',
  ]);
});

test('missing held metrics, source posts, and failed-check detail are stated in both held views', async () => {
  const today = clone(todayFixture);
  const populatedMarket = today.markets.find((market) => market.market === 'ZA');
  const populated = populatedMarket.held_back.items[0];
  const malformed = populatedMarket.held_back.items[1];
  const empty = today.markets.find((market) => market.market === 'KE').held_back.items[0];
  populated.reason = 'explanation_failed';
  populated.explanation_status = 'failed_checks';
  populated.held_detail = '   ';
  malformed.held_detail = {text: 'Not a plain detail string'};
  for (const item of [populated, empty]){
    delete item.numbers;
    delete item.count_line;
    item.evidence = [];
  }
  await mount({region: 'ZA'}, today);
  const heldBack = host.querySelector('[data-market="ZA"] [data-section="held-back"]');
  click(button(heldBack, populated.title));
  const populatedDetail = heldBack.querySelector('.t42-held-detail');
  expect(populatedDetail.closest('li').querySelector('[data-held-row-reason]')?.textContent).toBe('Too few creators');
  expect(populatedDetail.textContent).toContain('The specific check reason was not stored for this held item.');
  expect(populatedDetail.textContent).not.toContain('Trend numbers were not stored');
  expect(populatedDetail.querySelectorAll('[data-held-numbers-label], [data-held-numbers]')).toHaveLength(0);
  expect(populatedDetail.textContent).toContain('No source posts were stored for this held item.');
  expect(populatedDetail.querySelectorAll('[data-held-detail]')).toHaveLength(0);
  click(button(heldBack, malformed.title));
  expect(heldBack.querySelectorAll('.t42-held-detail [data-held-detail]')).toHaveLength(0);
  click(tab('Kenya'));
  const details = host.querySelector('[data-market="KE"] details[data-section="held-for-evidence"]');
  details.open = true;
  const item = details.querySelector('[data-held-item-id]');
  expect(item.textContent).not.toContain('Trend numbers were not stored');
  expect(item.querySelectorAll('[data-held-numbers-label], [data-held-numbers]')).toHaveLength(0);
  expect(item.textContent).toContain('No source posts were stored for this held item.');
  expect(item.querySelectorAll('[data-held-detail]')).toHaveLength(0);
});

test('a held item whose check detail could not be read says so instead of saying it was not stored', async () => {
  const today = clone(todayFixture);
  const populatedMarket = today.markets.find((market) => market.market === 'ZA');
  const held = populatedMarket.held_back.items[0];
  held.reason = 'explanation_failed';
  held.explanation_status = 'failed_checks';
  delete held.failed_reason;
  delete held.held_detail;
  held.check_detail = 'unavailable';
  await mount({region: 'ZA'}, today);
  const heldBack = host.querySelector('[data-market="ZA"] [data-section="held-back"]');
  click(button(heldBack, held.title));
  const detail = heldBack.querySelector('.t42-held-detail');
  expect(detail.querySelector('[data-held-check-detail-unavailable]')?.textContent)
    .toBe('The check detail could not be read just now.');
  expect(detail.textContent).not.toContain('The specific check reason was not stored for this held item.');
  expect(detail.querySelectorAll('[data-held-check-reason-missing]')).toHaveLength(0);
});

test('held_detail supplies failed-check context as escaped plain text', async () => {
  const today = clone(todayFixture);
  const held = today.markets.find((market) => market.market === 'KE').held_back.items[0];
  held.reason = 'explanation_failed';
  held.explanation_status = 'failed_checks';
  delete held.failed_reason;
  held.held_detail = '<script>Stored local context is incomplete.</script>';
  await mount({region: 'KE'}, today);
  const detail = host.querySelector('[data-market="KE"] [data-held-item-id]');
  expect(detail.querySelector('[data-held-detail]')?.textContent).toBe(held.held_detail);
  expect(detail.querySelectorAll('script')).toHaveLength(0);
  expect(detail.querySelectorAll('[data-held-check-reason-missing]')).toHaveLength(0);
  // Was: the reason inside the item; it is now said once on the item's group.
  expect(detail.closest('[data-held-group]').querySelector('[data-held-group-reason]').textContent)
    .toContain('Data issue on TikTok in the last 3 days');
});

test('an empty Today market shows one held summary and opens its Held back disclosure', async () => {
  await mount({region: 'KE'});
  const market = host.querySelector('[data-market="KE"]');
  const why = market.querySelector('[data-empty-market-reason]');
  // Restated 4 October 2026: the empty-market summary is now the status block's bold lead,
  // with the held count in its own sentence beside the link to the held list.
  expect(why.textContent).toBe('No trend cleared our checks in Kenya today.');
  expect(market.querySelector('[data-held-count]').textContent).toBe('1 is held back.');
  const status = market.querySelector('[data-market-status]');
  expect(status.getAttribute('data-status-tone')).toBe('alert');
  expect(market.querySelectorAll('[data-market-status]')).toHaveLength(1);
  const jump = [...status.querySelectorAll('a')].find((link) => link.textContent === 'Why each was held');
  expect(jump.getAttribute('href')).toBe('#t42-held-KE');
  expect(market.querySelector('#t42-held-KE')?.getAttribute('data-section')).toBe('held-for-evidence');
  expect(status.querySelector('[data-source-problem]')).not.toBeNull();
  expect(status.querySelector('.t42-banner-data_issue')?.textContent).toBe('Data issue: collection or detection failed for Kenya');
  const history = market.querySelector('a[href="#/history"]');
  expect(history.textContent).toBe('Choose a past brief in History');
  history.focus();
  expect(document.activeElement).toBe(history);
  const details = market.querySelector('details[data-section="held-for-evidence"]');
  expect(details).not.toBeNull();
  expect(details.open).toBe(true);
  expect(market.querySelector('[data-section="held-back"]')).toBeNull();
  const summary = details.querySelector('summary');
  expect(summary.localName).toBe('summary');
  expect(summary.textContent).toBe('Held back');
  summary.focus();
  expect(document.activeElement).toBe(summary);
  details.open = false;
  expect(details.open).toBe(false);
  details.open = true;
  expect(details.open).toBe(true);
  const item = details.querySelector('ul > li[data-held-item-id="4affc90c6e7ec9dae041f8d4a601461b789925a66ecd8cc30cd961d92113f186"]');
  expect(item.textContent).toContain('#fixture_ke_match');
  // Was: the reason inside the item; it is now said once on the item's group.
  expect(item.closest('[data-held-group]').querySelector('[data-held-group-reason]').textContent)
    .toContain('Data issue on TikTok in the last 3 days');
  const links = [...item.querySelectorAll('a')];
  expect(links.map((link) => link.getAttribute('href'))).toEqual([
    'https://example.invalid/x/@fixture_ke_1/x_ke_001',
    'https://example.invalid/tiktok/@fixture_ke_2/tt_ke_002',
  ]);
  expect(links.every((link) => link.getAttribute('target') === '_blank' && link.rel.includes('noopener'))).toBe(true);
});

test('a published empty market keeps the held summary concise and shows its recorded item reason', async () => {
  const today = clone(todayFixture);
  const marketData = today.markets.find((entry) => entry.market === 'KE');
  marketData.status = 'published';
  marketData.held_back.items[0].reason_text = 'Only two checked creators were available';
  marketData.held_back.text = '1 held back: too few creators';
  await mount({region: 'KE'}, today);
  const market = host.querySelector('[data-market="KE"]');
  const why = market.querySelector('[data-empty-market-reason]').textContent;
  // Restated 4 October 2026: the empty-market summary is now the status block's bold lead,
  // with the held count in its own sentence beside the link to the held list.
  expect(why).toBe('No trend cleared our checks in Kenya today.');
  expect(market.querySelector('[data-held-count]').textContent).toBe('1 is held back.');
  expect(market.querySelector('[data-section="held-for-evidence"]').textContent).toContain('Only two checked creators were available');
});

test('a data issue with missing or generic held reasons stays honestly unknown', async () => {
  const today = clone(todayFixture);
  const marketData = today.markets.find((entry) => entry.market === 'KE');
  marketData.held_back.items[0].reason_text = 'Reason not provided';
  const missing = {...marketData.held_back.items[0], item_id: 'missing-reason'};
  delete missing.reason_text;
  marketData.held_back.items.push(missing);
  marketData.held_back.count = 2;
  marketData.held_back.text = '2 held back';
  await mount({region: 'KE'}, today);
  const why = host.querySelector('[data-market="KE"] [data-empty-market-reason]').textContent;
  // Restated 4 October 2026: the empty-market summary is now the status block's bold lead,
  // with the held count in its own sentence beside the link to the held list.
  expect(why).toBe('No trend cleared our checks in Kenya today.');
  expect(host.querySelector('[data-market="KE"]').querySelector('[data-held-count]').textContent).toBe('2 are held back.');
  expect(why).not.toContain('Reason not provided');
  expect(why).not.toContain('collection or detection failed');
});

test('an empty published market with no held items says its specific reason is unknown', async () => {
  const today = clone(todayFixture);
  const marketData = today.markets.find((entry) => entry.market === 'KE');
  marketData.status = 'published';
  marketData.held_back.items = [];
  marketData.held_back.count = 0;
  marketData.held_back.text = 'Nothing held back today.';
  await mount({region: 'KE'}, today);
  const why = host.querySelector('[data-market="KE"] [data-empty-market-reason]').textContent;
  // Restated 4 October 2026: the empty-market summary is now the status block's bold lead,
  // with the held count in its own sentence beside the link to the held list.
  expect(why).toBe('No trend cleared our checks in Kenya today.');
  expect(host.querySelector('[data-market="KE"]').querySelector('[data-held-count]').textContent).toBe('0 are held back.');
  const details = host.querySelector('[data-market="KE"] details[data-section="held-for-evidence"]');
  expect(details.open).toBe(true);
  expect(details.textContent).toContain('Nothing held back today.');
});

test('held evidence details omit unsafe source links', async () => {
  const today = clone(todayFixture);
  const held = today.markets.find((market) => market.market === 'KE').held_back.items[0];
  held.evidence[0].url = 'javascript:alert(1)';
  held.evidence[1].url = 'https://user:secret@example.invalid/post';
  await mount({region: 'KE'}, today);
  const details = host.querySelector('[data-market="KE"] details[data-section="held-for-evidence"]');
  details.open = true;
  expect(details.querySelectorAll('a')).toHaveLength(0);
});

test('a positive held count with no item records says details are unavailable', async () => {
  const today = clone(todayFixture);
  const market = today.markets.find((entry) => entry.market === 'KE');
  market.held_back.items = [];
  market.held_back.count = 1;
  await mount({region: 'KE'}, today);
  const details = host.querySelector('[data-market="KE"] details[data-section="held-for-evidence"]');
  expect(details.open).toBe(true);
  expect(details.textContent).toContain('Held item details are unavailable.');
  expect(details.textContent).not.toContain('Nothing held back today.');
  expect(details.querySelectorAll('li[data-held-item-id]')).toHaveLength(0);
});

test('All shows the empty market fallback while keeping other markets\' admitted cards', async () => {
  await mount({region: 'ALL'});
  const southAfrica = host.querySelector('[data-market="ZA"]');
  const nigeria = host.querySelector('[data-market="NG"]');
  const kenya = host.querySelector('[data-market="KE"]');
  expect(southAfrica.querySelectorAll('[data-card]').length).toBeGreaterThan(0);
  expect(nigeria.querySelectorAll('[data-card]').length).toBeGreaterThan(0);
  expect(kenya.querySelectorAll('[data-card]')).toHaveLength(0);
  // Restated 4 October 2026: the empty-market summary is now the status block's bold lead,
  // with the held count in its own sentence beside the link to the held list.
  expect(kenya.querySelector('[data-empty-market-reason]').textContent).toBe('No trend cleared our checks in Kenya today.');
  expect(kenya.querySelector('[data-held-count]').textContent).toBe('1 is held back.');
  expect(kenya.querySelector('a[href="#/history"]').getAttribute('href')).toBe('#/history');
  expect(kenya.querySelectorAll('details[data-section="held-for-evidence"]')).toHaveLength(1);
  expect(kenya.querySelector('details[data-section="held-for-evidence"]').open).toBe(true);
  expect(kenya.querySelector('details[data-section="held-for-evidence"] summary').textContent).toBe('Held back');
  expect(kenya.querySelector('[data-section="held-back"]')).toBeNull();
  expect(southAfrica.querySelector('details[data-section="held-for-evidence"]')).toBeNull();
  expect(nigeria.querySelector('details[data-section="held-for-evidence"]')).toBeNull();
  expect(southAfrica.querySelector('[data-empty-market-reason]')).toBeNull();
  expect(nigeria.querySelector('[data-empty-market-reason]')).toBeNull();
});

test('cards filtered by the reader do not become held evidence items', async () => {
  const today = clone(todayFixture);
  const market = today.markets.find((entry) => entry.market === 'ZA');
  const filtered = clone(market.cards[0]);
  filtered.item_id = 'reader-filtered-only';
  filtered.title = 'Reader-filtered only';
  filtered.explained = false;
  filtered.explanation = null;
  filtered.claims = [];
  delete filtered.specificity;
  market.cards = [filtered];
  market.more = [];
  await mount({}, today);
  const details = host.querySelector('[data-market="ZA"] details[data-section="held-for-evidence"]');
  const shownIds = [...details.querySelectorAll('li[data-held-item-id]')].map((item) => item.getAttribute('data-held-item-id'));
  expect(shownIds).toEqual(market.held_back.items.map((item) => item.item_id));
  expect(details.querySelector('[data-held-item-id="reader-filtered-only"]')).toBeNull();
});

/* UX pass, 3 October 2026: Today ends with moments and boards; the coverage
   strip left the page and its figures live on Coverage. */
test('moments and boards render below the cards, with no coverage strip', async () => {
  await mount();
  const moments = host.querySelector('[data-section="moments"]');
  expect(moments.textContent).toContain('Fixture spring festival');
  expect(moments.textContent).toContain('3 October 2026');
  const boards = host.querySelector('[data-section="boards"]');
  expect(boards.textContent).toContain("TikTok's own list: Hashtag board, 7 days");
  expect(boards.textContent).toContain('#fixture_za_board');
  expect(host.querySelector('[data-section="coverage"]')).toBeNull();
  const details = host.querySelector('[data-market="ZA"] details[data-section="source-details"]');
  expect(details.open).toBe(false);
  expect(details.textContent).toContain('TikTok hashtag board: calls failed');
});

test('with no source issues there is no source line and no coverage strip', async () => {
  const today = clone(todayFixture);
  today.markets.find((market) => market.market === 'ZA').coverage.issues = [];
  await mount({}, today);
  expect(host.querySelector('[data-market="ZA"] [data-section="coverage"]')).toBeNull();
  expect(host.querySelectorAll('[data-market="ZA"] [data-source-problem]')).toHaveLength(0);
  expect(host.querySelectorAll('[data-market="ZA"] details[data-section="source-details"]')).toHaveLength(0);
});

test('a board lists each entry with its own best rank, no automatic numbering, and marks a tie with =', async () => {
  const today = clone(todayFixture);
  today.markets[0].boards[0].entries = [
    {rank: 1, title: '#shorts', item_id: 'a'},
    {rank: 1, title: '#ishowspeed', item_id: 'b'},
    {rank: 2, title: '#fixture_za_step', item_id: 'c'},
  ];
  await mount({}, today);
  const boards = host.querySelector('[data-section="boards"]');
  expect(boards.querySelector('ol')).toBeNull();
  const list = boards.querySelector('ul.t42-board-rows');
  expect(list).not.toBeNull();
  expect([...list.querySelectorAll('li')].map((li) => li.textContent)).toEqual(['=1 #shorts', '=1 #ishowspeed', '2 #fixture_za_step']);
  expect(boards.querySelector('.t42-board').textContent).toContain("TikTok's own list: Hashtag board, 7 days, best rank today");
  const css = await Bun.file(new URL('../../styles/today42.css', import.meta.url)).text();
  expect(css).toMatch(/\.t42-board-rows\s*\{[^}]*list-style:\s*none/);
});

test('Today repeated rows stay compact and wrap their full labels', async () => {
  const css = await Bun.file(new URL('../../styles/today42.css', import.meta.url)).text();
  const appCss = await Bun.file(new URL('../../app.css', import.meta.url)).text();
  expect(css).toMatch(/\.t42-today-market\s*>\s*\.t42-cards\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  expect(css).toMatch(/\.t42-today-market\s+\.t42-card\s*\{[^}]*min-block-size:\s*64px/);
  expect(css).toMatch(/\.t42-today-market\s+\.t42-board-rows\s*>\s*li\s*\{[^}]*overflow-wrap:\s*anywhere/);
  /* UI polish, 2 October 2026: restated. Rows below the cards keep a full tap
     height but no longer pad to 64px, so held-back rows stay smaller than shown cards. */
  expect(css).toMatch(/\.t42-today-market\s+\.t42-rows\s*>\s*li\s*\{[^}]*min-block-size:\s*var\(--target-min\)/);
  expect(css).toMatch(/\.t42-today-market\s+\.t42-disclose,[\s\S]*?white-space:\s*normal/);
  expect(css).not.toMatch(/\.t42-market\s+\.t42-card\s*\{/);
  expect(appCss).toMatch(/\.t42-card-metrics\s*\{\s*display:\s*contents;\s*\}/);
  await mount();
  expect(host.querySelector('[data-market="ZA"]').classList.contains('t42-today-market')).toBe(true);
  expect(host.querySelectorAll('[data-section="boards"] .t42-board-rows > li').length).toBeGreaterThan(0);
  expect(host.querySelectorAll('[data-section="held-back"] .t42-rows > li').length).toBeGreaterThan(0);
});

test('a board leaves null and missing ranks off unranked titles without changing valid ties', async () => {
  const today = clone(todayFixture);
  today.markets[0].boards[0].entries = [
    {rank: 1, title: '#shorts', item_id: 'a'},
    {rank: 1, title: '#ishowspeed', item_id: 'b'},
    {rank: 2, title: '#fixture_za_step', item_id: 'c'},
    {rank: null, title: '#unranked_null', item_id: 'd'},
    {title: '#unranked_missing', item_id: 'e'},
    {rank: null, title: '#also_unranked', item_id: 'f'},
  ];
  await mount({}, today);
  const rows = host.querySelectorAll('[data-section="boards"] .t42-board-rows li');
  expect([...rows].map((li) => li.textContent)).toEqual([
    '=1 #shorts',
    '=1 #ishowspeed',
    '2 #fixture_za_step',
    '#unranked_null',
    '#unranked_missing',
    '#also_unranked',
  ]);
});

/* Shaped like f42-api's boards for the staging briefs of 29 September: each
   board its own group, each entry its own best rank today (ties and gaps
   kept), and entries titled with an id left out and counted. The channel id
   here is made up. */
const FAKE_CHANNEL = 'uc' + 'a1b2c3d4e5f6g7h8i9j0k_';

test('a board never shows an id as a title, says how many were left out, and keeps each best rank', async () => {
  const today = clone(todayFixture);
  today.markets[0].boards = [
    {platform: 'tiktok', list: 'TikTok hashtag board', left_out: 0, left_out_reason: null, entries: [
      {rank: 1, title: '#fixture_tt_one', item_id: 'a'},
      {rank: 3, title: '#fixture_tt_two', item_id: 'b'},
    ]},
    {platform: 'youtube', list: 'YouTube trending board', left_out: 4, left_out_reason: 'No readable name', entries: [
      {rank: 1, title: '#fixture_yt_one', item_id: 'c'},
      {rank: 1, title: FAKE_CHANNEL, item_id: 'd'},
      {rank: 2, title: 'fixture yt name', item_id: 'e'},
      {rank: 2, title: '#fixture_yt_two', item_id: 'f'},
    ]},
  ];
  await mount({}, today);
  const section = host.querySelector('[data-section="boards"]');
  expect(section.textContent).not.toMatch(/uc[a-z0-9_-]{22}/i);
  const groups = [...section.querySelectorAll('.t42-board')];
  expect(groups).toHaveLength(2);
  for (const group of groups) expect(group.querySelector('.t42-line-text').textContent).toContain('best rank today');
  const rows = groups.map((g) => [...g.querySelectorAll('ul.t42-board-rows li')].map((li) => li.textContent));
  expect(rows).toEqual([['1 #fixture_tt_one', '3 #fixture_tt_two'], ['1 #fixture_yt_one', '=2 fixture yt name', '=2 #fixture_yt_two']]);
  const shown = rows.flat().map((row) => Number(row.replace(/^=/, '').split(' ')[0]));
  expect(shown).toEqual([1, 3, 1, 2, 2]);
  expect(groups[0].textContent).not.toContain('left out');
  expect(groups[1].querySelector('.t42-board-left-out').textContent).toBe('5 left out: No readable name');
});

test('a board whose every entry is an id shows only its count', async () => {
  const today = clone(todayFixture);
  today.markets[0].boards = [{platform: 'youtube', list: 'YouTube trending board', left_out: 10, left_out_reason: 'No readable name', entries: []}];
  await mount({}, today);
  const board = host.querySelector('[data-section="boards"] .t42-board');
  expect(board.querySelector('ul')).toBeNull();
  expect(board.querySelector('.t42-board-left-out').textContent).toBe('10 left out: No readable name');
});

test('a card whose series has too few measured days says so instead of a lone dash, and no series shows nothing', async () => {
  const today = clone(todayFixture);
  const source = today.markets[0].cards[0];
  const first = checkedCard(source, 'No series', 'no-series');
  const second = checkedCard(source, 'Thin series', 'thin-series');
  const third = checkedCard(source, 'Measured series', 'measured-series');
  first.sparkline = null;
  second.sparkline = {unit: 'list appearances a day', points: source.sparkline.points.map((p, i, all) => ({
    ...p, value: i >= all.length - 2 ? 3 : null, expected_low: null, expected_high: null}))};
  today.markets[0].cards = [first, second, third];
  today.markets[0].more = [];
  await mount({}, today);
  const none = cardTitled(first.title);
  expect(none.querySelector('svg.t42-spark')).toBeNull();
  expect(none.querySelector('.t42-spark-empty')).toBeNull();
  expect(none.textContent).not.toContain('Not enough measured days yet');
  const thin = cardTitled(second.title);
  expect(thin.querySelector('svg.t42-spark')).toBeNull();
  expect(thin.querySelector('.t42-spark-empty').textContent).toBe('Not enough measured days yet');
  expect(cardTitled('Measured series').querySelector('svg.t42-spark .t42-line')).not.toBeNull();
});

test('a Discover-style card with a null sparkline shows no chart and no words about days', async () => {
  const {TrendCard} = await import('../TrendCard.jsx');
  const card = {...clone(todayFixture.markets[0].cards[0]), sparkline: null, reach: null, growth: null, spread_line: null,
    lifecycle: {step: 2, word: 'Rising', rule: 'Growing for three days'}};
  flushSync(() => root.render(<ul><TrendCard card={card} market="ZA" date="2026-09-30" linkTopic posts={false} /></ul>));
  await settle();
  const el = host.querySelector('[data-card]');
  expect(el).not.toBeNull();
  expect(el.querySelector('svg.t42-spark')).toBeNull();
  expect(el.querySelector('.t42-spark-empty')).toBeNull();
  expect(el.textContent).not.toMatch(/Not enough (measured )?days yet/);
});

test('Posts loads the trend and lists its evidence in place', async () => {
  await mount();
  const first = cardTitled('#fixture_za_step');
  const posts = button(first, 'Posts');
  expect(posts.getAttribute('aria-expanded')).toBe('false');
  click(posts);
  const panel = first.querySelector('.t42-posts');
  expect(panel.getAttribute('aria-live')).toBe('polite');
  await settle();
  const card = todayFixture.markets[0].cards[0];
  expect(calls.some((c) => c.url === '/api/trends/' + card.item_id + '?market=ZA&date=2026-09-30')).toBe(true);
  const items = panel.querySelectorAll('li');
  expect(items).toHaveLength(3);
  expect(items[2].textContent).toContain('YouTube');
  expect(items[2].textContent).toContain('@fixture_za_8');
  expect(items[2].textContent).toContain('28 September 2026');
  expect(items[2].textContent).toContain('9 600 views');
  expect(items[2].textContent).toContain('Post 8: the #fixture_za_step routine at the taxi rank');
  const link = items[2].querySelector('a');
  expect(link.getAttribute('href')).toBe('https://example.invalid/youtube/@fixture_za_8/yt_za_008');
  expect(link.getAttribute('target')).toBe('_blank');
  expect(link.getAttribute('rel')).toContain('noopener');
});

test('a 401 hands the reader to the passcode flow', async () => {
  let asked = 0;
  serve([['/api/today', reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})]]);
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" onAuth={() => { asked += 1; }} />));
  await settle();
  expect(asked).toBe(1);
  expect(host.querySelector('[data-card]')).toBeNull();
});

test('a 409 says Today is not published yet, with the date', async () => {
  serve([['/api/today', reply(409, {error: 'not_ready', message: 'Today is not published yet for 2026-09-30.'})]]);
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
  await settle();
  expect(text()).toContain('Today is not published yet');
  expect(text()).toContain('30 September 2026');
  expect(button(host, 'Try again')).toBeUndefined();
});

test('loading shows first, and an error offers Try again that reads again', async () => {
  const timers = trackTodaySlowTimers();
  try {
    let answer = reply(500, {error: 'internal', message: 'Something broke.'});
    serve([['/api/today', () => answer]]);
    flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
    expect(text()).toContain('Loading Today');
    expect(host.querySelector('h1').textContent).toBe('Today');
    expect(host.querySelector('[role="tablist"][aria-label="Markets"] button')).not.toBeNull();
    expect(host.querySelector('#t42-panel[aria-busy="true"]')).not.toBeNull();
    expect(host.querySelector('.t42-loading-structure[aria-hidden="true"]')).not.toBeNull();
    expect(host.querySelector('[role="status"]').closest('[aria-busy="true"]')).toBeNull();
    await settle();
    expect(text()).toContain('Today could not load');
    expect(timers.cleared).toContain(timers.scheduled[0]);
    answer = reply(200, todayFixture);
    click(button(host, 'Try again'));
    await settle();
    expect(host.querySelector('h1').textContent).toBe('Taking off, 30 September 2026');
    expect(calls).toHaveLength(2);
    expect(timers.scheduled).toHaveLength(2);
    expect(timers.cleared).toContain(timers.scheduled[1]);
  } finally {
    timers.restore();
  }
});

test('late reads for earlier dates cannot replace the selected date', async () => {
  let resolveOld;
  let resolveSelected;
  let rejectStale;
  let resolveLatest;
  let reads = 0;
  const selectedDate = {...clone(todayFixture), date: '2026-10-01', heading: 'Brief for 1 October 2026', published_at: '2026-10-01T06:10:00+02:00'};
  const latestDate = {...clone(todayFixture), date: '2026-10-03', heading: 'Brief for 3 October 2026', published_at: '2026-10-03T06:10:00+02:00'};
  serve([['/api/today', () => {
    reads += 1;
    if (reads === 1) return new Promise((resolve) => { resolveOld = resolve; });
    if (reads === 2) return new Promise((resolve) => { resolveSelected = resolve; });
    if (reads === 3) return new Promise((_resolve, reject) => { rejectStale = reject; });
    return new Promise((resolve) => { resolveLatest = resolve; });
  }], ['/api/trends/', reply(200, trendFixture)]]);

  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
  await settle();
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-10-01" />));
  await settle();
  expect(calls[0].init.signal.aborted).toBe(true);
  resolveSelected(reply(200, selectedDate));
  await settle();
  expect(host.querySelector('h1').textContent).toBe(selectedDate.heading);

  resolveOld(reply(200, todayFixture));
  await settle();
  expect(host.querySelector('h1').textContent).toBe(selectedDate.heading);
  expect(host.querySelector('[data-today-date-context]').textContent).toBe('Brief for 1 October 2026');

  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-10-02" />));
  await settle();
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-10-03" />));
  await settle();
  expect(calls[2].init.signal.aborted).toBe(true);
  resolveLatest(reply(200, latestDate));
  await settle();
  expect(host.querySelector('h1').textContent).toBe(latestDate.heading);

  rejectStale(new Error('Older date read failed.'));
  await settle();
  expect(host.querySelector('h1').textContent).toBe(latestDate.heading);
  expect(text()).not.toContain('Today could not load');
  expect(text()).not.toContain('Older date read failed.');
  expect(button(host, 'Try again')).toBeUndefined();
});

test('a date change hides the previous brief until the requested date loads', async () => {
  let resolveRead;
  let reads = 0;
  const nextDate = {...clone(todayFixture), date: '2026-10-01', heading: 'Brief for 1 October 2026', published_at: '2026-10-01T06:10:00+02:00'};
  serve([['/api/today', () => {
    reads += 1;
    if (reads === 1) return reply(200, todayFixture);
    if (reads === 2) return new Promise((resolve) => { resolveRead = resolve; });
    return reply(200, nextDate);
  }], ['/api/trends/', reply(200, trendFixture)]]);
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
  await settle();
  expect(cards().length).toBeGreaterThan(0);

  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-10-01" />));
  await settle();
  expect(text()).toContain('Loading Today');
  expect(host.querySelector('h1').textContent).toBe('Today');
  expect(host.querySelector('[data-today-loaded]')).toBeNull();
  expect(host.querySelector('[data-today-published-at]')).toBeNull();
  expect(cards()).toHaveLength(0);
  expect(host.querySelector('#t42-panel[aria-busy="true"]')).not.toBeNull();
  expect(host.querySelector('[data-today-load-status]').closest('[aria-busy="true"]')).toBeNull();

  resolveRead(reply(503, {error: 'unavailable', message: 'Service timed out.'}));
  await settle();
  expect(text()).toContain('Today could not load');
  expect(text()).toContain('Service timed out.');
  expect(cards()).toHaveLength(0);
  expect(host.querySelector('#t42-panel[aria-busy="true"]')).toBeNull();
  expect(button(host, 'Try again')).not.toBeUndefined();

  click(button(host, 'Try again'));
  await settle();
  expect(reads).toBe(3);
  expect(host.querySelector('h1').textContent).toBe(nextDate.heading);
  expect(host.querySelector('[data-today-date-context]').textContent).toBe('Brief for 1 October 2026');
});

test('loading tabs follow the current region without rereading Today', async () => {
  let resolveToday;
  serve([['/api/today', () => new Promise((resolve) => { resolveToday = resolve; })]]);
  const renderRegion = (region) => flushSync(() => root.render(<TodayPage42 region={region} date="2026-09-30" />));
  renderRegion('NG');
  await settle();
  expect(host.querySelectorAll('[role="tab"][aria-selected="true"]')).toHaveLength(1);
  expect(tab('Nigeria').getAttribute('aria-selected')).toBe('true');
  renderRegion('KE');
  await settle();
  expect(host.querySelectorAll('[role="tab"][aria-selected="true"]')).toHaveLength(1);
  expect(tab('Kenya').getAttribute('aria-selected')).toBe('true');
  renderRegion('ALL');
  await settle();
  expect(host.querySelectorAll('[role="tab"][aria-selected="true"]')).toHaveLength(1);
  expect(tab('All').getAttribute('aria-selected')).toBe('true');
  expect(calls).toHaveLength(1);
  resolveToday(reply(200, todayFixture));
  await settle();
});

test('a pending Today read announces that it is still in progress without another fetch', async () => {
  const timers = trackTodaySlowTimers();
  let resolveToday;
  try {
    serve([['/api/today', () => new Promise((resolve) => { resolveToday = resolve; })]]);
    flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
    expect(text()).toContain('Loading Today');
    expect(calls).toHaveLength(1);
    expect(timers.scheduled).toHaveLength(1);
    flushSync(() => timers.fire());
    await settle();
    expect(text()).toContain('The Today read is still in progress.');
    expect(calls).toHaveLength(1);
    resolveToday(reply(200, todayFixture));
    await settle();
    expect(text()).not.toContain('The Today read is still in progress.');
    expect(timers.cleared).toContain(timers.scheduled[0]);
  } finally {
    if (root) {
      flushSync(() => root.unmount());
      root = null;
    }
    timers.restore();
  }
});

test('an immediate Today response removes the loading structure', async () => {
  const timers = trackTodaySlowTimers();
  let resolveToday;
  try {
    serve([['/api/today', () => new Promise((resolve) => { resolveToday = resolve; })]]);
    flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
    expect(host.querySelector('.t42-loading-structure')).not.toBeNull();
    resolveToday(reply(200, todayFixture));
    await settle();
    expect(host.querySelector('.t42-loading-structure')).toBeNull();
    expect(text()).not.toContain('The Today read is still in progress.');
    expect(calls).toHaveLength(1);
    expect(timers.scheduled).toHaveLength(1);
    expect(timers.cleared).toContain(timers.scheduled[0]);
  } finally {
    timers.restore();
  }
});

test('unmounting a pending Today read aborts it and clears the slow notice timer', async () => {
  const timers = trackTodaySlowTimers();
  let resolveToday;
  try {
    serve([['/api/today', () => new Promise((resolve) => { resolveToday = resolve; })]]);
    flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
    const signal = calls[0].init.signal;
    expect(timers.scheduled).toHaveLength(1);
    flushSync(() => root.unmount());
    root = null;
    expect(signal.aborted).toBe(true);
    expect(timers.cleared).toContain(timers.scheduled[0]);
    resolveToday(reply(200, todayFixture));
    await settle();
  } finally {
    timers.restore();
  }
});

test('copy carries no generation words and no retired sources', async () => {
  const source = await Bun.file(new URL('../../today42.jsx', import.meta.url)).text();
  expect(source).not.toMatch(/gen ?z|millennial|youth|google trends|prompt pulse|nano banana/i);
});

/* Stage 2 on Today (contract.md sections 10.5 and 10.6). The page is given
   the alerts read, the watch write and the feedback write by the app; the
   tests above mount it without them, as a Stage 1 page. */
const TODAY_ALERTS = {date: '2026-09-30', alerts: [
  {watch_id: 'w_1', label: '#amapiano', item_id: 'item_ama', market: 'ZA', fired_because: 'Entered Rising', card: {item_id: 'item_ama'}, since: '2026-09-30'},
  {watch_id: 'w_2', label: 'Castle Lager', item_id: 'item_castle', market: 'NG', fired_because: 'Reach passed 50 creators in 3 days', card: {item_id: 'item_castle'}, since: '2026-09-29'},
  {watch_id: 'w_3', waiting: 'Waiting for breakout detection'},
]};

async function mountStage2(props = {}, alerts = reply(200, TODAY_ALERTS)){
  serve([['/api/today', reply(200, todayFixture)], ['/api/trends/', reply(200, trendFixture)], ['/api/alerts', alerts]]);
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" loadAlerts={fetchAlerts} {...props} />));
  await settle();
}

test('the alerts strip sits above the cards with the count and a link to each topic', async () => {
  await mountStage2();
  expect(calls.map((c) => c.url)).toContain('/api/alerts?date=2026-09-30');
  const strip = host.querySelector('[data-section="alerts"]');
  expect(strip).not.toBeNull();
  expect(strip.compareDocumentPosition(cards()[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(strip.textContent).toContain('2 alerts today');
  const links = [...strip.querySelectorAll('a')];
  expect(links.find((a) => a.textContent === '#amapiano').getAttribute('href')).toBe('#/t/item_ama?market=ZA');
  expect(links.find((a) => a.textContent === 'Castle Lager').getAttribute('href')).toBe('#/t/item_castle?market=NG');
  expect(strip.textContent).toContain('Entered Rising');
  expect(strip.textContent).not.toContain('Waiting for breakout detection');
  expect(links.some((a) => a.getAttribute('href') === '#/alerts')).toBe(true);
});

/* UX pass, 3 October 2026: a side read that is loading or failed no longer
   adds a section to Today; Alerts is in the menu and says so itself. */
test('an alerts read with no fired alerts, or one that failed, adds nothing to Today', async () => {
  await mountStage2({}, reply(200, {date: '2026-09-30', alerts: []}));
  expect(host.querySelectorAll('[data-section="alerts"]')).toHaveLength(0);
  flushSync(() => root.unmount());
  root = createRoot(host);
  calls = [];
  await mountStage2({}, reply(500, {error: 'internal', message: 'Something broke.'}));
  expect(host.querySelectorAll('[data-section="alerts"]')).toHaveLength(0);
  expect(cards()).toHaveLength(1);
});

test('alerts that are still loading add nothing above or below the cards', async () => {
  await mountStage2({loadAlerts: () => new Promise(() => {})});
  expect(host.querySelectorAll('[data-section="alerts"]')).toHaveLength(0);
  expect(cards()).toHaveLength(1);
});

test('a 401 on the alerts read hands the reader to the passcode flow', async () => {
  let asked = 0;
  await mountStage2({onAuth: () => { asked += 1; }}, reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'}));
  expect(asked).toBe(1);
});

test('each card takes one-tap feedback, says a quiet Thanks and never blocks', async () => {
  const sent = [];
  let release;
  await mountStage2({onFeedback: (body) => { sent.push(body); return new Promise((resolve) => { release = resolve; }); }});
  const first = cardTitled('#fixture_za_step');
  expect(['Real', 'Not real', 'Useful'].every((label) => button(first, label))).toBe(true);
  click(button(first, 'Real'));
  expect(sent).toEqual([{target: {kind: 'card', item_id: todayFixture.markets[0].cards[0].item_id, market: 'ZA', date: '2026-09-30'}, value: 'real'}]);
  expect(['Real', 'Not real', 'Useful'].every((label) => !button(first, label).disabled)).toBe(true);
  release({ok: true});
  await settle();
  expect(first.textContent).toContain('Thanks');
  expect(button(first, 'Real').getAttribute('aria-pressed')).toBe('true');
  click(button(first, 'Not real'));
  expect(sent[1].value).toBe('not_real');
});

test('feedback that fails says so in words, and a 401 hands over to the passcode flow', async () => {
  let asked = 0;
  let failure = Object.assign(new Error('The feedback could not be saved; try again.'), {status: 500});
  await mountStage2({onAuth: () => { asked += 1; }, onFeedback: () => Promise.reject(failure)});
  const first = cardTitled('#fixture_za_step');
  click(button(first, 'Useful'));
  await settle();
  expect(first.textContent).toContain('did not save');
  expect(first.textContent).not.toContain('Thanks');
  failure = Object.assign(new Error('Missing or wrong passcode.'), {status: 401, auth: true});
  click(button(first, 'Useful'));
  await settle();
  expect(asked).toBe(1);
});

test('with the watch write, Watch on a Today card opens the chooser and the card turns to Watching', async () => {
  const made = [];
  await mountStage2({onCreateWatch: (body) => { made.push(body); return Promise.resolve({watch_id: 'w_9', ...body}); }});
  const first = cardTitled('#fixture_za_step');
  click(button(first, 'Watch'));
  const dialog = host.querySelector('[role="dialog"]');
  expect(dialog).not.toBeNull();
  click(button(dialog, 'Watch'));
  await settle();
  expect(made).toEqual([{target: {kind: 'item', item_id: todayFixture.markets[0].cards[0].item_id}, market: 'ZA', rule: {state_in: ['rising']}, label: '#fixture_za_step'}]);
  expect(button(cardTitled('#fixture_za_step'), 'Watching')).toBeDefined();
  expect(cardTitled('fixture za sound one')).toBeUndefined();
});

test('an alert on a held-back item shows Held back with its reason in the strip', async () => {
  const held = {date: '2026-09-30', alerts: [
    {watch_id: 'w_1', label: '#amapiano', item_id: 'item_ama', market: 'ZA', fired_because: 'Entered Rising', since: '2026-09-30',
      card: {item_id: 'item_ama', held_back: {rule: 'G6', reason: 'not_local', reason_text: 'Not local: most posts come from outside South Africa'}}},
    {watch_id: 'w_4', waiting: 'Waiting for a second detect run to compare against'},
  ]};
  await mountStage2({}, reply(200, held));
  const strip = host.querySelector('[data-section="alerts"]');
  expect(strip.textContent).toContain('1 alert today');
  const item = strip.querySelector('[data-held]');
  expect(item).not.toBeNull();
  expect(item.textContent).toContain('Held back: Not local: most posts come from outside South Africa');
  expect(strip.textContent).not.toContain('Waiting for a second detect run');
});

/* Contract section 13.1: a quiet notice for investigations finished in the
   last 7 days, each linked, read from the finished list. It never holds
   Today up: a read that fails or has nothing to say shows no line. */
const daysAgo = (days) => new Date(Date.now() - days * 86400000).toISOString();
const FINISHED = {investigations: [
  {investigation_id: 'i_recent000001', status: 'complete', question: 'How is amapiano travelling in Nigeria?', market: 'NG', created_at: daysAgo(2), updated_at: daysAgo(1)},
  {investigation_id: 'i_recent000002', status: 'complete', question: 'What are Kenyan food creators posting?', market: 'KE', created_at: daysAgo(7), updated_at: daysAgo(6)},
  {investigation_id: 'i_old000000003', status: 'complete', question: 'Where did the kitchen-table format start?', market: 'ZA', created_at: daysAgo(12), updated_at: daysAgo(9)},
]};

async function mountWithInvestigations(props = {}, finished = reply(200, FINISHED)){
  serve([['/api/today', reply(200, todayFixture)], ['/api/trends/', reply(200, trendFixture)], ['/api/investigations', finished]]);
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" loadInvestigations={listInvestigations} {...props} />));
  await settle();
}

test('investigations finished in the last 7 days show as one quiet notice, each linked to its page', async () => {
  await mountWithInvestigations();
  expect(calls.map((c) => c.url)).toContain('/api/investigations?status=complete');
  const notice = host.querySelector('[data-section="investigations-done"]');
  expect(notice).not.toBeNull();
  expect(notice.compareDocumentPosition(cards()[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const links = [...notice.querySelectorAll('a')];
  expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
    ['How is amapiano travelling in Nigeria?', '#/investigations/i_recent000001'],
    ['What are Kenyan food creators posting?', '#/investigations/i_recent000002'],
  ]);
  expect(notice.textContent).toContain('2 investigations finished');
  expect(notice.textContent).not.toContain('kitchen-table');
});

test('the investigations notice never blocks Today: nothing finished, or a failed read, shows no line and the cards still show', async () => {
  await mountWithInvestigations({}, reply(200, {investigations: []}));
  expect(host.querySelector('[data-section="investigations-done"]')).toBeNull();
  expect(cards()).toHaveLength(1);
  flushSync(() => root.unmount());
  root = createRoot(host);
  calls = [];
  await mountWithInvestigations({}, reply(503, {error: 'agent_unavailable', message: 'The agent service is not reachable.'}));
  expect(host.querySelector('[data-section="investigations-done"]')).toBeNull();
  expect(cards()).toHaveLength(1);
  expect(host.querySelector('[role="alert"]')).toBeNull();
});

test('a finished investigation with one result reads in the singular', async () => {
  await mountWithInvestigations({}, reply(200, {investigations: [FINISHED.investigations[0]]}));
  expect(host.querySelector('[data-section="investigations-done"]').textContent).toContain('1 investigation finished');
});

/* Contract section 14.2: scheduled answers of the last 7 days join the same
   quiet notice pattern: the question, how the answer ended, and a link to
   it. No answer text reaches Today, a skipped run is not an answer, and a
   failed read shows nothing and holds nothing up. */
const dayOf = (days) => new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
const SCHEDULED = {schedules: [
  {schedule_id: 's_a', question: 'What are Lagos food creators posting?', market: 'NG', status: 'active',
    last_run: {run_id: 'sched-a', date: dayOf(1), status: 'complete', ask_id: 'a_sched_000001', outcome: 'complete'}, skip_reason: null},
  {schedule_id: 's_b', question: 'Which sounds are new in Nairobi?', market: 'KE', status: 'active',
    last_run: {run_id: 'sched-b', date: dayOf(2), status: 'complete', ask_id: 'a_sched_000002', outcome: 'insufficient_evidence'}, skip_reason: null},
  {schedule_id: 's_c', question: 'Who is naming the new Durban dance?', market: 'ZA', status: 'paused',
    last_run: {run_id: 'sched-c', date: dayOf(3), status: 'complete', ask_id: 'a_sched_000003', outcome: 'refused'}, skip_reason: null},
  {schedule_id: 's_d', question: 'What moved in Soweto football talk?', market: 'ZA', status: 'active',
    last_run: {run_id: 'sched-d', date: dayOf(1), status: 'skipped', ask_id: null, outcome: 'Scheduled share spent'}, skip_reason: 'Scheduled share spent'},
  {schedule_id: 's_e', question: 'What was big in Accra last month?', market: 'NG', status: 'active',
    last_run: {run_id: 'sched-e', date: dayOf(12), status: 'complete', ask_id: 'a_sched_000005', outcome: 'complete'}, skip_reason: null},
  {schedule_id: 's_f', question: 'Never asked yet?', market: 'KE', status: 'active', last_run: null, skip_reason: null},
]};

async function mountWithSchedules(props = {}, scheduled = reply(200, SCHEDULED)){
  serve([['/api/today', reply(200, todayFixture)], ['/api/trends/', reply(200, trendFixture)], ['/api/schedules', scheduled]]);
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" loadSchedules={listSchedules} {...props} />));
  await settle();
}

test('scheduled answers of the last 7 days show as one quiet notice with their status and a link', async () => {
  await mountWithSchedules();
  expect(calls.map((c) => c.url)).toContain('/api/schedules');
  const notice = host.querySelector('[data-section="scheduled-done"]');
  expect(notice).not.toBeNull();
  expect(notice.className).toBe('t42-line-text');
  expect(notice.compareDocumentPosition(cards()[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const links = [...notice.querySelectorAll('a')];
  expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
    ['What are Lagos food creators posting?', '#/ask?follow=a_sched_000001'],
    ['Which sounds are new in Nairobi?', '#/ask?follow=a_sched_000002'],
    ['Who is naming the new Durban dance?', '#/ask?follow=a_sched_000003'],
  ]);
  const words = notice.textContent.replace(/\s+/g, ' ');
  expect(words).toContain('3 scheduled answers in the last 7 days');
  expect(words).toContain('What are Lagos food creators posting? (Answered)');
  expect(words).toContain('Which sounds are new in Nairobi? (Not enough evidence)');
  expect(words).toContain('Who is naming the new Durban dance? (Refused)');
  expect(words).not.toContain('Soweto');
  expect(words).not.toContain('Accra');
});

test('the scheduled notice never blocks Today: nothing recent, or a failed read, shows no line and the cards still show', async () => {
  await mountWithSchedules({}, reply(200, {schedules: [SCHEDULED.schedules[3], SCHEDULED.schedules[5]]}));
  expect(host.querySelector('[data-section="scheduled-done"]')).toBeNull();
  expect(cards()).toHaveLength(1);
  flushSync(() => root.unmount());
  root = createRoot(host);
  calls = [];
  await mountWithSchedules({}, reply(503, {error: 'agent_unavailable', message: 'The agent service is not reachable.'}));
  expect(host.querySelector('[data-section="scheduled-done"]')).toBeNull();
  expect(cards()).toHaveLength(1);
  expect(host.querySelector('[role="alert"]')).toBeNull();
});

test('one scheduled answer reads in the singular', async () => {
  await mountWithSchedules({}, reply(200, {schedules: [SCHEDULED.schedules[0]]}));
  expect(host.querySelector('[data-section="scheduled-done"]').textContent).toContain('1 scheduled answer in the last 7 days');
});

test('Today card titles open the market topic without changing Ask or Posts controls', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  card.specificity = specificityFor(card);
  await mount({}, today);
  const shown = cardTitled(card.title);
  const titleLink = shown.querySelector('h3 a.tc-title-link');
  expect(titleLink?.getAttribute('href')).toBe(topicHref(card.item_id, 'ZA'));
  const askLink = [...shown.querySelectorAll('a')].find((link) => link.textContent.trim() === 'Ask about this');
  expect(askLink?.getAttribute('href')).toBe('#/ask?q=' + encodeURIComponent(card.ask) + '&market=ZA&item=' + card.item_id + '&date=2026-09-30');
  expect(button(shown, 'Posts')?.getAttribute('aria-expanded')).toBe('false');
  expect(shown.querySelector('[data-today-specificity] blockquote')?.textContent).toContain(card.specificity.quote.text);
  expect(shown.querySelector('[data-local-examples]')).not.toBeNull();
  expect(calls.map((call) => call.url)).toEqual(['/api/today?date=2026-09-30']);
});

test('Today shows the checked why-now, verbatim quote, and two local examples before Posts is opened', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  card.specificity = specificityFor(card);
  await mount({}, today);
  const shown = cardTitled(card.title);
  expect(shown).toBeDefined();
  const checked = shown.querySelector('[data-today-specificity]');
  expect(checked).not.toBeNull();
  expect(checked.textContent).toContain('Why now');
  expect(checked.querySelector('blockquote').textContent).toContain(card.specificity.quote.text);
  const examples = checked.querySelector('[data-local-examples]');
  const ids = [...examples.querySelectorAll('[data-evidence-id]')].map((item) => item.getAttribute('data-evidence-id'));
  expect(ids).toEqual([card.specificity.quote.evidence_id, 'ig_za_007']);
  expect(examples.textContent).toContain('TikTok');
  expect(examples.textContent).toContain('@fixture_za_6');
  expect(examples.textContent).toContain('Instagram');
  expect(examples.textContent).toContain('@fixture_za_7');
  expect(button(shown, 'Posts').getAttribute('aria-expanded')).toBe('false');
  expect(calls.map((call) => call.url)).toEqual(['/api/today?date=2026-09-30']);
});

test('Today resolves reordered local records by ID and keeps two available examples when another ID is missing', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  const original = specificityFor(card);
  const quoteId = original.quote.evidence_id;
  card.evidence.reverse();
  card.specificity = {
    ...original,
    local_evidence_ids: ['missing-local-id', 'ig_za_007', quoteId],
    quote: {...original.quote, evidence_id: quoteId},
  };
  card.claims.find((claim) => claim.id === 'c1').evidence_ids.push('missing-local-id');
  await mount({}, today);
  const examples = cardTitled(card.title).querySelector('[data-local-examples]');
  expect([...examples.querySelectorAll('[data-evidence-id]')].map((item) => item.getAttribute('data-evidence-id'))).toEqual([quoteId, 'ig_za_007']);
});

test('Today uses nonempty quote_text and displays the exact named claim quote', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  const specificity = specificityFor(card);
  const source = card.evidence.find((item) => item.id === specificity.quote.evidence_id);
  source.text = 'A different full post text';
  source.quote_text = 'The “Café night” in Durban';
  card.specificity = specificity;
  setSpecificityQuote(card, '“Café night”');
  await mount({}, today);
  const quote = cardTitled(card.title).querySelector('blockquote');
  expect(quote.textContent).toContain('“Café night”');
  expect(quote.textContent).not.toContain('different full post text');
});

test('Today compares why-now to trimmed explanation while keeping the original explanation text', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  card.explanation = '  ' + card.explanation + '  ';
  card.specificity = {...specificityFor(card), why_now: card.explanation.trim()};
  card.explanation_status = null;
  await mount({}, today);
  const whyNow = cardTitled(card.title).querySelector('.t42-specificity-why-now');
  expect(whyNow.textContent).toContain(card.explanation);
});

test('Today hides malformed specificity, stripped claims, false explanations, and missing examples and their headline', async () => {
  const mutations = [
    (card) => { delete card.specificity; },
    (card) => { card.explained = 'true'; },
    (card) => { card.explained = 'false'; },
    (card) => { delete card.claims; },
    (card) => { card.explanation = ''; card.specificity.why_now = ''; },
    (card) => { card.specificity.why_now = 'A different sentence'; },
    (card) => { delete card.specificity.reason; },
    (card) => { card.specificity.reason = 'missing_explanation'; },
    (card) => { card.specificity.status = 'PASS'; },
    (card) => { card.explanation_claim_ids = ['missing-claim']; },
    (card) => { card.specificity.local_evidence_ids = [card.specificity.local_evidence_ids[0]]; },
    (card) => { card.evidence = card.evidence.filter((item) => item.id !== card.specificity.local_evidence_ids[1]); },
    (card) => {
      const item = card.evidence.find((entry) => entry.id === card.specificity.local_evidence_ids[1]);
      item.text = null;
      item.quote_text = null;
      item.url = 'javascript:alert(1)';
    },
    (card) => { card.evidence = card.evidence.filter((item) => item.id !== card.specificity.quote.evidence_id); },
    (card) => { card.specificity.quote.evidence_id = 'uncited-source'; },
    (card) => { setSpecificityQuote(card, 'This quote is absent from the post'); },
    (card) => { setSpecificityQuote(card, 'Post'); },
    (card) => {
      const source = card.evidence.find((item) => item.id === card.specificity.quote.evidence_id);
      source.quote_text = 'The “Café   night” in Durban';
      setSpecificityQuote(card, '“Café night”');
    },
    (card) => {
      const quote = new Array(27).fill('word').join(' ');
      card.evidence.find((item) => item.id === card.specificity.quote.evidence_id).text = quote;
      setSpecificityQuote(card, quote);
    },
    (card) => {
      const quote = 'word ' + 'x'.repeat(156);
      card.evidence.find((item) => item.id === card.specificity.quote.evidence_id).text = quote;
      setSpecificityQuote(card, quote);
    },
    (card) => { card.explanation_status = 'failed_checks'; },
    (card) => { card.claims.push(clone(card.claims[0])); },
    (card) => { card.evidence.push(clone(card.evidence.find((item) => item.id === card.specificity.quote.evidence_id))); },
  ];
  for (const mutate of mutations){
    const today = clone(todayFixture);
    const card = today.markets[0].cards[0];
    card.specificity = specificityFor(card);
    mutate(card);
    today.headline = {text: 'A headline for the first card', market: 'ZA', item_id: card.item_id};
    flushSync(() => root.unmount());
    root = createRoot(host);
    await mount({}, today);
    expect(cardTitled(card.title)).toBeUndefined();
    expect(host.querySelector('.t42-headline')).toBeNull();
  }
});

test('Today rejects a quote that starts inside a Unicode word', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  card.specificity = specificityFor(card);
  const source = card.evidence.find((item) => item.id === card.specificity.quote.evidence_id);
  source.text = 'pré' + card.specificity.quote.text;
  today.headline = {text: 'A headline for the first card', market: 'ZA', item_id: card.item_id};
  await mount({}, today);
  expect(cardTitled(card.title)).toBeUndefined();
  expect(host.querySelector('.t42-headline')).toBeNull();
});

test('Today counts quote characters as Unicode codepoints', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  card.specificity = specificityFor(card);
  const quote = '😀'.repeat(80) + ' ok';
  card.evidence.find((item) => item.id === card.specificity.quote.evidence_id).text = quote;
  setSpecificityQuote(card, quote);
  await mount({}, today);
  expect(cardTitled(card.title).querySelector('blockquote').textContent).toContain(quote);
});

test('Today uses only evidence IDs from the named explanation claims', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  const specificity = specificityFor(card);
  const [quoteId, secondId] = specificity.local_evidence_ids;
  card.claims.find((claim) => claim.id === 'c1').evidence_ids = [quoteId];
  card.claims.find((claim) => claim.id === 'c2').evidence_ids = [secondId];
  card.claims.push({id: 'unreferenced', evidence_ids: ['yt_za_008'], quotes: []});
  card.explanation_claim_ids = ['c1', 'c2'];
  card.specificity = specificity;
  await mount({}, today);
  expect(cardTitled(card.title)).toBeDefined();
});

test('Today rejects local IDs outside named claim evidence_ids, including quote-only IDs', async () => {
  const mutations = [
    (card, quoteId, secondId, extraId) => {
      card.claims.find((claim) => claim.id === 'c1').evidence_ids = [quoteId, secondId];
      card.claims.find((claim) => claim.id === 'c2').evidence_ids = [extraId];
      card.explanation_claim_ids = ['c1'];
      card.specificity.local_evidence_ids = [quoteId, secondId, extraId];
    },
    (card, quoteId, secondId, extraId) => {
      card.claims.find((claim) => claim.id === 'c1').evidence_ids = [quoteId, secondId];
      card.claims.find((claim) => claim.id === 'c1').quotes.push({
        evidence_id: extraId,
        text: card.evidence.find((item) => item.id === extraId).text,
      });
      card.explanation_claim_ids = ['c1'];
      card.specificity.local_evidence_ids = [quoteId, secondId, extraId];
    },
  ];
  for (const mutate of mutations){
    const today = clone(todayFixture);
    const card = today.markets[0].cards[0];
    card.specificity = specificityFor(card);
    const [quoteId, secondId] = card.specificity.local_evidence_ids;
    mutate(card, quoteId, secondId, 'yt_za_008');
    flushSync(() => root.unmount());
    root = createRoot(host);
    calls = [];
    await mount({}, today);
    expect(cardTitled(card.title)).toBeUndefined();
  }
});

test('Today requires the named claim with the quote pair to cite the quote evidence ID', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  card.specificity = specificityFor(card);
  const [quoteId, secondId] = card.specificity.local_evidence_ids;
  card.claims.find((claim) => claim.id === 'c1').evidence_ids = [secondId];
  card.claims.find((claim) => claim.id === 'c2').evidence_ids = [quoteId, secondId];
  card.explanation_claim_ids = ['c1', 'c2'];
  await mount({}, today);
  expect(cardTitled(card.title)).toBeUndefined();
});

test('unsafe local post URLs do not become links in the Today examples', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  card.specificity = specificityFor(card);
  for (const id of card.specificity.local_evidence_ids){
    card.evidence.find((item) => item.id === id).url = 'javascript:alert(1)';
  }
  await mount({}, today);
  const examples = cardTitled(card.title).querySelector('[data-local-examples]');
  expect(examples.querySelectorAll('a[href^=\"javascript:\"]')).toHaveLength(0);
  expect(examples.querySelectorAll('a')).toHaveLength(0);
});

test('Today filters before the top three and five card split, and keeps a headline for an admitted card in more', async () => {
  const today = clone(todayFixture);
  const market = today.markets[0];
  const source = market.cards[0];
  const legacy = clone(source);
  legacy.title = 'Legacy card without specificity';
  legacy.item_id = 'legacy-card';
  delete legacy.specificity;
  const first = checkedCard(source, 'Qualified one', 'qualified-one');
  const second = checkedCard(source, 'Qualified two', 'qualified-two');
  const third = checkedCard(source, 'Qualified three', 'qualified-three');
  const fourth = checkedCard(source, 'Qualified four', 'qualified-four');
  const fifth = checkedCard(source, 'Qualified five', 'qualified-five');
  const sixth = checkedCard(source, 'Qualified six', 'qualified-six');
  market.cards = [legacy, first, second, third, fourth];
  market.more = [fifth, sixth];
  today.headline = {text: 'Headline for the sixth card', market: 'ZA', item_id: sixth.item_id};
  await mount({region: 'ALL'}, today);
  const section = host.querySelector('[data-market=\"ZA\"]');
  expect([...section.querySelectorAll('[data-card] h3')].map((title) => title.textContent)).toEqual([
    'Qualified one', 'Qualified two', 'Qualified three',
  ]);
  expect(host.querySelector('.t42-headline').textContent).toBe(today.headline.text);
  click(tab('South Africa'));
  expect([...host.querySelector('[data-market=\"ZA\"]').querySelectorAll('[data-card] h3')].map((title) => title.textContent)).toEqual([
    'Qualified one', 'Qualified two', 'Qualified three', 'Qualified four', 'Qualified five',
  ]);
  click(button(host, 'Show all'));
  expect(cardTitled('Qualified six')).toBeDefined();
});

test('the shared TrendCard keeps its normal display without the Today-only specificity prop', async () => {
  const {TrendCard} = await import('../TrendCard.jsx');
  const card = {...clone(todayFixture.markets[0].cards[2]), specificity: {status: 'fail'}, explained: false};
  flushSync(() => root.render(<ul><TrendCard card={card} market="ZA" date="2026-09-30" posts={false} /></ul>));
  const shown = host.querySelector('[data-card]');
  expect(shown).not.toBeNull();
  expect(shown.textContent).toContain('Explanation held back: it did not pass the checks');
  expect(shown.querySelector('[data-today-specificity]')).toBeNull();
});

test('Searching now: Today selected and All groups leave cards and reads unchanged', async () => {
  await mount({region: 'NG'});
  expect(Boolean(host.querySelector('[data-section="searching-now"]'))).toBe(false);
  const selectedCards = cards().map((card) => card.textContent);
  const selectedHeadline = host.querySelector('.t42-headline')?.textContent;
  click(tab('All'));
  const allCards = cards().map((card) => card.textContent);
  const allHeadline = host.querySelector('.t42-headline')?.textContent;

  resetRoot();
  calls = [];
  const today = clone(todayFixture);
  today.searching_now = clone(searchingNowFixture);
  await mount({region: 'NG'}, today);
  const group = (market) => host.querySelector('[data-section="searching-now"] [data-search-market="' + market + '"]');
  expect(group('NG')?.textContent || '').toContain('fixture query NG');
  expect(Boolean(group('ZA'))).toBe(false);
  expect(cards().map((card) => card.textContent)).toEqual(selectedCards);
  expect(host.querySelector('.t42-headline')?.textContent).toBe(selectedHeadline);
  expect(calls.map((call) => call.url)).toEqual(['/api/today?date=2026-09-30']);

  click(tab('All'));
  const groups = [...host.querySelectorAll('[data-section="searching-now"] [data-search-market]')];
  expect(groups.map((node) => node.getAttribute('data-search-market'))).toEqual(['ZA', 'NG', 'KE']);
  expect(group('ZA')?.textContent || '').toContain('fixture query ZA');
  expect(group('KE')?.textContent || '').toContain('fixture query KE');
  expect(group('KE')?.closest('[data-market="KE"]')?.querySelectorAll('[data-card]').length || 0).toBe(0);
  expect(text()).toContain('Google search interest');
  expect(cards().map((card) => card.textContent)).toEqual(allCards);
  expect(host.querySelector('.t42-headline')?.textContent).toBe(allHeadline);
  expect(calls.map((call) => call.url)).toEqual(['/api/today?date=2026-09-30']);

  resetRoot();
  calls = [];
  const emptySignals = clone(todayFixture);
  emptySignals.searching_now = [];
  await mount({region: 'ALL'}, emptySignals);
  expect(Boolean(host.querySelector('[data-section="searching-now"]'))).toBe(false);
  expect(cards().map((card) => card.textContent)).toEqual(allCards);
  expect(host.querySelector('.t42-headline')?.textContent).toBe(allHeadline);
  expect(calls.map((call) => call.url)).toEqual(['/api/today?date=2026-09-30']);
});

/* UI polish, 2 October 2026: the header is one calm status area. The
   earlier-brief warning leads it and keeps role=status; the receipt, the
   publication time, the partial note and warm-up read as one quiet line,
   none of them in a bordered box. */
test('the header status area leads with the earlier-brief warning and groups the quiet facts in one line', async () => {
  const nativeNow = Date.now;
  Date.now = () => Date.parse('2026-10-01T10:00:00+02:00');
  try {
    await mount({date: undefined});
    const area = host.querySelector('.t42-head [data-today-status-area]');
    expect(area).not.toBeNull();
    const warning = area.querySelector('[data-today-earlier]');
    expect(warning.getAttribute('role')).toBe('status');
    expect(area.firstElementChild).toBe(warning);
    const quiet = area.querySelector('[data-today-facts]');
    expect(quiet).not.toBeNull();
    expect(quiet.querySelector('[data-today-published-at]')).not.toBeNull();
    expect(quiet.querySelector('[data-today-status="partial"]')).not.toBeNull();
    expect(quiet.textContent).toContain('Warming up: day 2 of 14');
    expect(host.querySelectorAll('.t42-head .t42-banner')).toHaveLength(0);
  } finally {
    Date.now = nativeNow;
  }
});

test('a single market tab does not repeat the market name as a heading; All names each market', async () => {
  await mount();
  expect(host.querySelector('[data-market="ZA"] .t42-market-name')).toBeNull();
  expect(host.querySelector('[data-market="ZA"]').getAttribute('aria-label')).toBe('South Africa');
  click(host.querySelector('#t42-tab-ALL'));
  expect([...host.querySelectorAll('.t42-market-name')].map((h) => h.textContent)).toEqual(['South Africa', 'Nigeria', 'Kenya']);
});

test('the card puts its title and state in one head row and gives Ask the one primary action', async () => {
  await mount();
  const card = cardTitled('#fixture_za_step');
  const head = card.querySelector(':scope > .t42-card-head');
  expect(head.querySelector('h3').textContent).toBe('#fixture_za_step');
  expect(head.querySelector('.t42-tag').textContent).toBe('Moved up');
  expect(head.querySelector('.t42-state').textContent).toBe('Emerging');
  const actions = card.querySelector('.t42-card-foot .t42-actions');
  const primary = actions.querySelectorAll('.t42-action-primary');
  expect(primary).toHaveLength(1);
  expect(primary[0].textContent).toBe('Ask about this');
  expect([...actions.querySelectorAll('button')].every((b) => b.classList.contains('t42-action'))).toBe(true);
});

test('the trend line carries a visible caption, a zero baseline and a mark for each measured day', async () => {
  await mount();
  const trend = cardTitled('#fixture_za_step').querySelector('figure.t42-trend');
  expect(trend).not.toBeNull();
  expect(trend.querySelector('figcaption').textContent).toBe('Posts a day, 24 to 30 September · latest 19 · 1 day not collected');
  expect(trend.querySelector('svg .t42-axis')).not.toBeNull();
  expect(trend.querySelectorAll('svg .t42-dot')).toHaveLength(6);
});

test('a post with no usable image leaves no empty frame, and a card with none shows no strip', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  card.evidence.find((e) => e.id === card.thumbnails[1]).thumbnail_url = null;
  await mount({}, today);
  const shown = cardTitled('#fixture_za_step');
  expect(shown.querySelectorAll('.t42-thumbs img')).toHaveLength(1);
  expect(shown.querySelectorAll('.t42-thumbs [role="img"]')).toHaveLength(0);
  flushSync(() => shown.querySelector('.t42-thumbs img').dispatchEvent(new window.Event('error')));
  expect(shown.querySelector('.t42-thumbs')).toBeNull();
});

test('cards enter with a capped stagger index that only the stylesheet turns into time', async () => {
  const today = clone(todayFixture);
  const source = today.markets[0].cards[0];
  today.markets[0].cards = ['One', 'Two', 'Three'].map((title, index) => checkedCard(source, title, 'stagger-' + index));
  today.markets[0].more = [];
  await mount({}, today);
  expect(cards().map((card) => card.style.getPropertyValue('--i'))).toEqual(['0', '1', '2']);
  const css = await Bun.file(new URL('../../styles/today42.css', import.meta.url)).text();
  expect(css).toMatch(/animation-delay:\s*calc\(min\(var\(--i, 0\), \d\) \* 80ms\)/);
  expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*\.t42-card[\s\S]*animation:\s*none/);
});

/* Design critique, 2 October 2026: Today said things twice and stacked too
   many registers. These hold the fixes: the date in the heading stays whole,
   the quote is not repeated word for word in the example under it, the
   feedback taps carry a visible label, and a moment's facts keep their word
   spaces in one line. */
test('the heading keeps its date whole on one line and reads the same', async () => {
  await mount();
  const heading = host.querySelector('h1');
  expect(heading.textContent).toBe('Taking off, 30 September 2026');
  expect(heading.querySelector('.t42-nowrap')?.textContent).toBe('30 September 2026');
});

test('the earlier-brief notice keeps its date whole too', async () => {
  serve([['/api/today', reply(200, todayFixture)], ['/api/trends/', reply(200, trendFixture)]]);
  flushSync(() => root.render(<TodayPage42 region="ZA" />));
  await settle();
  const notice = host.querySelector('[data-today-earlier]');
  expect(notice.textContent).toBe('Earlier brief: 30 September 2026. This is not today’s brief.');
  expect(notice.querySelector('.t42-nowrap')?.textContent).toBe('30 September 2026');
});

test('a local example whose text is the quote is not printed twice', async () => {
  const today = clone(todayFixture);
  const card = today.markets[0].cards[0];
  card.specificity = specificityFor(card);
  await mount({}, today);
  const checked = cardTitled(card.title).querySelector('[data-today-specificity]');
  const quote = card.specificity.quote.text;
  expect(checked.querySelector('blockquote').textContent).toContain(quote);
  expect(checked.textContent.split(quote).length - 1).toBe(1);
  const examples = checked.querySelector('[data-local-examples]');
  const ids = [...examples.querySelectorAll('[data-evidence-id]')].map((item) => item.getAttribute('data-evidence-id'));
  expect(ids).toEqual([card.specificity.quote.evidence_id, 'ig_za_007']);
  const quoted = examples.querySelector('[data-evidence-id="' + card.specificity.quote.evidence_id + '"]');
  expect(quoted.querySelector('[data-quoted-above]')?.textContent).toBe('Quoted above');
  expect(Boolean(quoted.querySelector('a.t42-link'))).toBe(true);
});

test('the feedback taps sit under a visible run-in label after the actions', async () => {
  await mountStage2({onFeedback: () => Promise.resolve({ok: true})});
  const first = cardTitled('#fixture_za_step');
  const group = first.querySelector('.f42-feedback');
  expect(group.getAttribute('role')).toBe('group');
  expect(group.getAttribute('aria-label')).toBe('Feedback on #fixture_za_step');
  expect(group.firstElementChild.textContent).toBe('Was this right?');
  const foot = first.querySelector('.t42-card-foot');
  expect(foot.firstElementChild.classList.contains('t42-actions')).toBe(true);
});

test('a moment keeps its facts in one inline line with their word spaces', async () => {
  await mount();
  const chip = host.querySelector('[data-section="moments"] .t42-chip');
  const unit = chip.querySelector('.fact-unit');
  expect(unit.parentElement === chip).toBe(false);
  expect(chip.textContent.replace(/\s+/g, ' ').trim()).toBe('3 October 2026 · Fixture spring festival · Festival');
});

test('a board caption breaks only between whose list it is and the list itself', async () => {
  await mount();
  const caption = host.querySelector('[data-section="boards"] .t42-board > .t42-line-text');
  expect(caption.textContent).toBe("TikTok's own list: Hashtag board, 7 days, best rank today");
  expect([...caption.querySelectorAll('.fact-unit')].map((unit) => unit.textContent))
    .toEqual(["TikTok's own list:", 'Hashtag board, 7 days, best rank today']);
});

test('an empty market groups its held items by reason, says each reason once and keeps posts behind a disclosure', async () => {
  const today = clone(todayFixture);
  const kenya = today.markets.find((market) => market.market === 'KE');
  const base = kenya.held_back.items[0];
  const outage = [0, 1, 2].map((n) => ({...clone(base), item_id: 'outage-' + n, title: 'Outage topic ' + n,
    rule: 'G1', reason: 'data_issue', reason_text: 'Not enough clean data on the main platform'}));
  // Same reason code, but held because its evidence could not be read: no invalid day, so no G1 help.
  const unread = {...clone(base), item_id: 'unread-1', title: 'Unread topic', rule: undefined, reason: 'data_issue',
    reason_text: 'Evidence could not be read'};
  const critic = {...clone(base), item_id: 'critic-1', title: 'Critic topic', rule: undefined, reason: 'explanation_failed',
    reason_text: 'The explanation did not pass its checks', held_detail: 'A simpler explanation could not be ruled out from these posts.'};
  kenya.held_back.items = [critic, ...outage, unread];
  kenya.held_back.count = 5;
  await mount({region: 'KE'}, today);
  const details = host.querySelector('[data-market="KE"] details[data-section="held-for-evidence"]');
  const groups = [...details.querySelectorAll('[data-held-group]')];
  expect(groups.map((group) => group.querySelector('[data-held-group-reason]').textContent)).toEqual([
    'Not enough clean data on the main platform3 topics',
    'The explanation did not pass its checks1 topic',
    'Evidence could not be read1 topic',
  ]);
  expect(groups.every((group) => group.querySelector('h3[data-held-group-reason]').id === group.getAttribute('aria-labelledby'))).toBe(true);
  /* Charts, 3 October 2026: restated. The held view now opens on a chart of
     the reasons, which names each reason once in its key (and once in its
     screen-reader table); the grouped list below still says it once. */
  const chart = details.querySelector('[data-held-reasons]');
  expect([...chart.querySelectorAll('.ch42-key-label')].map((l) => l.textContent)).toEqual([
    'Not enough clean data on the main platform',
    'The explanation did not pass its checks',
    'Evidence could not be read',
  ]);
  expect(groups.map((g) => g.textContent).join('').split('Not enough clean data on the main platform')).toHaveLength(2);
  expect(groups[0].querySelectorAll('li[data-held-item-id]')).toHaveLength(3);
  expect(groups[0].textContent).toContain('At least one of the last three days had invalid data on the main platform');
  expect(groups[1].textContent).not.toContain('invalid data');
  expect(groups[2].textContent).not.toContain('invalid data');
  expect(groups[1].querySelector('[data-held-detail]').textContent).toBe(critic.held_detail);
  const more = [...details.querySelectorAll('li[data-held-item-id] > details')];
  expect(more).toHaveLength(5);
  expect(more.every((d) => !d.open)).toBe(true);
  expect(more[0].querySelector('summary').textContent).toBe('Posts and figures for Outage topic 0');
  expect(more[0].querySelectorAll('a[href]').length).toBeGreaterThan(0);
});

/* Design review, 4 October 2026: the poster's trend gets its lead figure and
   its line beside the headline, so the first screen shows the number and
   its shape. The card below keeps the same facts for screen readers, so the
   side panel is drawn for the eye only and still names its query. */
test('the poster carries its lead figure and line beside it, and only while it shows', async () => {
  await mount();
  const lead = host.querySelector('.t42-lead');
  expect(lead).not.toBeNull();
  expect(lead.querySelector('.t42-headline').textContent).toBe(todayFixture.headline.text);
  const side = lead.querySelector('.t42-lead-side');
  expect(side.getAttribute('aria-hidden')).toBe('true');
  const figure = side.querySelector('.t42-lead-figure');
  expect(figure.textContent).toBe('31 creators in 3 days');
  expect(figure.getAttribute('data-query-id')).toBe('q_creators3_za_a');
  expect(side.querySelector('svg.t42-spark .t42-line')).not.toBeNull();
  click(tab('Nigeria'));
  const ngCard = todayFixture.markets.find((market) => market.market === 'NG').cards[0];
  expect(host.querySelector('.t42-headline').textContent).toBe(ngCard.explanation);
  expect(cards()[0].querySelector('.tc-title-link').getAttribute('href')).toContain(ngCard.item_id);
  const ngFigure = host.querySelector('.t42-lead-figure');
  expect(ngFigure.textContent).toBe('24 creators in 3 days');
  expect(ngFigure.getAttribute('data-query-id')).toBe(ngCard.numbers[0].query_id);
  click(tab('Kenya'));
  expect(host.querySelectorAll('.t42-lead-side')).toHaveLength(0);
});

test('the poster side panel drops what it cannot show and never says "not enough days"', async () => {
  const leadCard = (today) => {
    const za = today.markets.find((m) => m.market === today.headline.market);
    return [...za.cards, ...(za.more || [])].find((c) => c.item_id === today.headline.item_id);
  };
  const bare = clone(todayFixture);
  Object.assign(leadCard(bare), {numbers: [], reach: null, sparkline: null});
  await mount({}, bare);
  expect(host.querySelector('.t42-headline')).not.toBeNull();
  expect(host.querySelector('.t42-lead-side')).toBeNull();

  const thin = clone(todayFixture);
  const card = leadCard(thin);
  card.sparkline = {...card.sparkline, points: card.sparkline.points.map((p, i) => (i < 2 ? p : {...p, value: null}))};
  resetRoot();
  await mount({}, thin);
  const side = host.querySelector('.t42-lead-side');
  expect(side.querySelector('.t42-lead-figure').textContent).toBe('31 creators in 3 days');
  expect(side.querySelector('svg.t42-spark')).toBeNull();
  expect(side.textContent).not.toContain('Not enough measured days');
});

/* Visual QA, 5 October 2026 (T01, T02): a TurnTable title carries the
   source page's <br> tags, and at 1024 px a market name and its
   "incomplete" note broke mid-word. */
test('a board title with source <br> tags reads as one line, with no tag text', async () => {
  const today = clone(todayFixture);
  today.markets[0].boards[0].entries = [
    {rank: 19, title: '<br>Gratitude<br>Asake', item_id: 'a'},
    {rank: 22, title: 'IMALI<br/>Fireboy DML, JAZZWRLD & Thukuthela', item_id: 'b'},
    {rank: 24, title: '<br><br>', item_id: 'c'},
  ];
  await mount({}, today);
  const boards = host.querySelector('[data-section="boards"]');
  expect([...boards.querySelectorAll('ul.t42-board-rows li')].map((li) => li.textContent)).toEqual(['19 Gratitude · Asake', '22 IMALI · Fireboy DML, JAZZWRLD & Thukuthela']);
  expect(boards.textContent).not.toContain('<br');
  expect(boards.textContent).toContain('1 left out');
});

test('a glance market name and its incomplete note each stay whole', async () => {
  await mount();
  const name = host.querySelector('[data-glance-market] .t42-glance-name');
  expect(name.querySelector('.t42-glance-label')).not.toBeNull();
  const css = await Bun.file(new URL('../../styles/nightdesk.css', import.meta.url)).text();
  expect(css).toMatch(/\.t42-glance-name\s*\{[^}]*flex-wrap:\s*wrap/);
  expect(css).toMatch(/\.t42-glance-label,\s*html \.oi-product \.t42 \.t42-glance-note\s*\{[^}]*white-space:\s*nowrap/);
});

/* Tester report, 5 October 2026 (Thabang, items 2 to 9), and the demo
   readout's "After the demo" list: Today and its cards read as a client
   reads them. */
const zaCard = (today) => today.markets.find((m) => m.market === 'ZA').cards[0];

test('a count of one reads "1 creator" and "1 post", on the card and in a stored line', async () => {
  const {figureWords, countLineWords} = await import('../TrendCard.jsx');
  expect(figureWords({value: 1, unit: 'posts in 3 days'})).toBe('1 post in 3 days');
  expect(figureWords({value: 1, unit: 'creators in 3 days'})).toBe('1 creator in 3 days');
  expect(figureWords({value: 21, unit: 'posts in 3 days'})).toBe('21 posts in 3 days');
  expect(countLineWords('1 creators and 1 posts in 3 days')).toBe('1 creator and 1 post in 3 days');
  expect(countLineWords('1 creators and 21 posts in 3 days')).toBe('1 creator and 21 posts in 3 days');
  expect(countLineWords('11 creators and 101 posts in 3 days')).toBe('11 creators and 101 posts in 3 days');

  const today = clone(todayFixture);
  const card = zaCard(today);
  card.count_line = '1 creators and 1 posts in 3 days';
  card.numbers[0].value = 1;
  card.numbers[1].value = 1;
  today.headline = null;
  await mount({}, today);
  const shown = cards()[0];
  expect(shown.querySelector('.t42-count').textContent).toBe('1 creator and 1 post in 3 days');
  expect([...shown.querySelectorAll('.tc-big-item')].map((item) => item.textContent)).toEqual(['1 creator in 3 days', '1 post in 3 days']);
  expect(shown.textContent).not.toMatch(/\b1 (creators|posts)\b/);
});

test('an ISO date in the model prose reads as a day and month; the stored text is unchanged', async () => {
  const {proseDates} = await import('../TrendCard.jsx');
  const now = Date.parse('2026-10-06T06:00:00Z');
  expect(proseDates('Posts on 2026-10-01 mark Independence Day.', now)).toBe('Posts on 1 Oct mark Independence Day.');
  expect(proseDates('Since 2025-12-31 and 2026-13-01', now)).toBe('Since 31 Dec 2025 and 2026-13-01');
  expect(proseDates('At 2026-10-01T09:00:00Z', now)).toBe('At 2026-10-01T09:00:00Z');

  const today = clone(todayFixture);
  const card = zaCard(today);
  const sentence = 'Local creators posted their own takes on 2026-09-27, ahead of the weekend.';
  card.explanation = sentence;
  card.specificity.why_now = sentence;
  today.headline.text = 'The biggest mover since 2026-09-27.';
  const stored = clone(today);
  await mount({}, today);
  const whyNow = host.querySelector('.t42-specificity-why-now');
  expect(whyNow.textContent).toBe('Why now' + sentence.replace('2026-09-27', '27 Sep'));
  expect(host.querySelector('.t42-headline').textContent).toBe('The biggest mover since 27 Sep.');
  expect(text()).not.toContain('2026-09-27');
  expect(today).toEqual(stored);
});

test('local examples say their window, and a post from before the counted days says so', async () => {
  await mount();
  const examples = host.querySelector('[data-local-examples]');
  expect(examples.querySelector('[data-examples-window]').textContent).toBe('From the last 7 days of posts. The counts above cover 3 days.');
  // The brief is for 30 September, so the counted days are 28 to 30 September.
  const metas = [...examples.querySelectorAll('.t42-post-meta')].map((meta) => meta.textContent.replace(/\u00a0/g, ' '));
  expect(metas).toEqual([
    'TikTok · @fixture_za_6 · 26 September 2026 · posted before the counted days · 7 200 views',
    'Instagram · @fixture_za_7 · 27 September 2026 · posted before the counted days · 8 400 views',
  ]);

  resetRoot();
  const today = clone(todayFixture);
  const card = zaCard(today);
  card.evidence.find((e) => e.id === 'ig_za_007').posted_at = '2026-09-28T08:00:00+02:00';
  await mount({}, today);
  const after = [...host.querySelectorAll('[data-local-examples] .t42-post-meta')].map((meta) => meta.textContent.replace(/\u00a0/g, ' '));
  expect(after[0]).toContain('posted before the counted days');
  expect(after[1]).toBe('Instagram · @fixture_za_7 · 28 September 2026 · 8 400 views');
});

test('a capped post list says it is not every counted post', async () => {
  const {postsShownWords} = await import('../TrendCard.jsx');
  const posts = (value) => [{value, unit: 'posts in 3 days', query_id: 'q'}];
  expect(postsShownWords(12, posts(21))).toBe('Showing 12 example posts, not all 21 posts counted in 3 days.');
  expect(postsShownWords(12, posts(12))).toBeNull();
  expect(postsShownWords(3, [{value: 9, unit: 'creators in 3 days', query_id: 'q'}])).toBeNull();

  const today = clone(todayFixture);
  const held = today.markets.find((m) => m.market === 'KE').held_back.items[0];
  held.numbers = [{value: 21, unit: 'posts in 3 days', query_id: 'q_posts3_ke', run_id: 'r', result_hash: 'sha256:1'}];
  await mount({region: 'KE'}, today);
  const detail = host.querySelector('[data-held-item-id] .t42-held-detail');
  expect(detail.querySelector('[data-posts-shown]').textContent).toBe('Showing 2 example posts, not all 21 posts counted in 3 days.');
  expect(detail.querySelectorAll('.t42-post')).toHaveLength(2);
});

test('a topic a busy model left unexplained says so, not as a failed check', async () => {
  const today = clone(todayFixture);
  const held = today.markets.find((m) => m.market === 'KE').held_back.items[0];
  Object.assign(held, {rule: 'G10', reason: 'explanation_failed', reason_text: 'Not explained in time: the model was busy',
    failed_reason: 'Model busy: not explained before the deadline'});
  await mount({region: 'KE'}, today);
  expect(host.querySelector('[data-held-group-reason]').textContent).toContain('Not explained in time: the model was busy');
  expect(host.querySelector('[data-held-failed-reason]').textContent).toBe('The model was busy, so this was not explained before the deadline. It is not a failed check.');
  expect(text()).not.toContain('Check detail: Model busy');
  expect(text()).not.toMatch(/[–—]/);
});

test('a tap anywhere on a Today card opens it, and its controls keep their own job', async () => {
  await mount();
  const card = cards()[0];
  const link = card.querySelector('.tc-title-link');
  expect(card.className).toContain('t42-card-tap');
  let opened = 0;
  link.addEventListener('click', (event) => { opened += 1; event.preventDefault(); });
  click(card.querySelector('.t42-specificity-why-now'));
  expect(opened).toBe(1);
  click(card.querySelector('.t42-card-metrics') || card);
  expect(opened).toBe(2);
  click(button(card, 'Posts'));
  expect(opened).toBe(2);
  click(card.querySelector('[data-local-examples] a.t42-link'));
  expect(opened).toBe(2);
  // The title link stays the card's one keyboard stop.
  expect(card.getAttribute('tabindex')).toBeNull();
  expect(link.getAttribute('href')).toBe(topicHref(todayFixture.markets[0].cards[0].item_id, 'ZA'));
});

test('a long Today page offers a way back to the top', async () => {
  const realScrollTo = window.scrollTo;
  const scrolled = [];
  window.scrollTo = (options) => { scrolled.push(options); };
  try {
    await mount();
    expect(host.querySelector('[data-today-top]')).toBeNull();
    Object.defineProperty(window, 'scrollY', {value: window.innerHeight * 2, configurable: true});
    flushSync(() => window.dispatchEvent(new Event('scroll')));
    const top = host.querySelector('[data-today-top]');
    expect(top.textContent).toBe('Back to top');
    expect(top.tagName).toBe('BUTTON');
    click(top);
    expect(scrolled).toHaveLength(1);
    expect(scrolled[0].top).toBe(0);
    expect(document.activeElement).toBe(host.querySelector('h1.t42-heading'));
    Object.defineProperty(window, 'scrollY', {value: 0, configurable: true});
    flushSync(() => window.dispatchEvent(new Event('scroll')));
    expect(host.querySelector('[data-today-top]')).toBeNull();
  } finally {
    window.scrollTo = realScrollTo;
    Object.defineProperty(window, 'scrollY', {value: 0, configurable: true});
  }
});

/* Wave 8, W8-DEC-04: the Searching now strip sits below the cards in every
   Today market, never above them. */
test('Searching now sits below the cards on a market tab, on All, and under the held list of an empty market', async () => {
  const today = clone(todayFixture);
  today.searching_now = clone(searchingNowFixture);
  const follows = (first, second) => Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);
  await mount({region: 'NG'}, today);
  let strip = host.querySelector('[data-section="searching-now"]');
  expect(strip).not.toBeNull();
  const lastCard = cards().filter((card) => strip.closest('[data-market]').contains(card)).pop();
  expect(follows(lastCard, strip)).toBe(true);

  click(tab('All'));
  const groups = [...host.querySelectorAll('[data-market]')].filter((node) => node.querySelector('[data-section="searching-now"]'));
  expect(groups.length).toBeGreaterThan(1);
  for (const group of groups){
    const own = group.querySelector('[data-section="searching-now"]');
    for (const card of group.querySelectorAll('[data-card]')) expect(follows(card, own)).toBe(true);
    const heldList = group.querySelector('[id^="t42-held-"]');
    if (heldList) expect(follows(heldList, own)).toBe(true);
  }
});
