/* Discover and Radar on the 42 API (contract.md sections 10.1 and 10.2),
   and the one trend card they share with Today. The cards are Today's
   fixture cards grown with the Stage 2 fields, so both screens are read
   against the same card shape. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';
import searchingNowFixture from './fixtures/searching-now.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {fetchDiscover, fetchRadar, fetchTopic, fetchCoverage} = await import('../../api42.js');
const {Discover42} = await import('../../discover42.jsx');
const {TrendCard} = await import('../TrendCard.jsx');

const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const figure = (value, unit) => ({value, unit, query_id: 'q_' + unit.replace(/\W+/g, '_'), run_id: 'r_d1', result_hash: 'sha256:d1'});

const za = todayFixture.markets[0];

/* A Today card grown with the Stage 2 fields of contract.md section 10.1. */
function grown(card, order, extra = {}){
  return {
    ...clone(card), tag: null, order, date: '2026-10-20',
    explanation_status: card.explained ? 'explained' : 'failed_checks',
    lifecycle: null, novelty: 'new', diffusion: 'bottom_up', origin: null,
    spread_line: null, reach: figure(10 + order, 'creators in 3 days'), growth: figure(1.5, 'times its usual level'),
    watch_id: null, ...extra,
  };
}

const first = grown(za.cards[0], 1, {
  lifecycle: {step: 2, word: 'Rising', rule: 'Significant on 2 days with ratio 2 or more'},
  spread_line: 'First seen on TikTok 12 September, X 18 September, news 24 September; 38 creators; Gauteng to Western Cape',
  novelty: 'recurrence',
  last_wave: {peak_date: '2026-08-02', peak_posts: figure(1200, 'posts on its peak day')},
  reach: figure(31, 'creators in 3 days'),
  growth: figure(2.8, 'times its usual level'),
});
const second = grown(za.cards[1], 2, {
  explained: false, explanation: null, claims: [], explanation_status: 'not_run',
  reach: figure(12, 'creators in 3 days'), growth: null, watch_id: 'w_1',
});
const pageOne = {
  date: '2026-10-20', market: 'ZA', run_id: 'r_d1',
  items: [first, second, ...za.cards.slice(2).map((card, i) => grown(card, i + 3))],
  next_cursor: 'c2',
  held_back: {count: 1, items: [{
    item_id: 'held_1', title: '#fixture_za_ring', reason: 'likely_coordinated',
    reason_text: 'Likely coordinated: many new accounts posting the same words',
    card: grown({...za.cards[0], item_id: 'held_1', title: '#fixture_za_ring', flag: 'likely_coordinated', flag_word: 'Likely coordinated'}, 9),
  }]},
  filters: {kinds: ['hashtag', 'sound', 'topic', 'format', 'creator'], states: ['emerging', 'new_to_42', 'spike'], platforms: ['tiktok', 'instagram', 'x']},
};
const pageTwo = {...pageOne, items: za.more.map((card, i) => grown(card, i + 6)), next_cursor: null};

const radar = {
  date: '2026-10-20', market: 'ZA',
  points: pageOne.items.map((card) => ({item_id: card.item_id, label: card.title, kind: card.kind, state: card.state, flag: card.flag, growth: card.growth || figure(1.1, 'times its usual level'), reach: card.reach})),
  held_back_count: 1, note: null,
};
const warmRadar = {
  ...radar,
  points: radar.points.map((p) => ({...p, growth: null})),
  note: 'Growth needs 14 days of data; showing reach only',
};

/* Every test answers fetch from a route table: the first matching prefix. */
function serve(routes){
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), init});
    for (const [prefix, answer] of routes){
      if (String(url).startsWith(prefix)) return typeof answer === 'function' ? answer(String(url)) : answer;
    }
    return reply(404, {error: 'not_found', message: 'No route'});
  };
}

const standard = (radarBody = radar) => [
  ['/api/discover/radar', reply(200, radarBody)],
  ['/api/discover', (url) => reply(200, url.includes('cursor=c2') ? pageTwo : pageOne)],
];

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

async function mount(props = {}, routes = standard()){
  serve(routes);
  flushSync(() => root.render(<Discover42 region="ZA" {...props} />));
  await settle();
}

const text = () => host.textContent.replace(/\s+/g, ' ');
const cards = () => [...host.querySelectorAll('.d42-feed [data-card]')];
const cardTitled = (title) => cards().find((card) => card.querySelector('h3').textContent === title);
const button = (scope, label) => [...scope.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
const tab = (list, label) => [...host.querySelectorAll('[role="tablist"][aria-label="' + list + '"] [role="tab"]')].find((t) => t.textContent.trim() === label);
const chip = (group, value) => host.querySelector('button[data-filter="' + group + '"][data-value="' + value + '"]');
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));
const choose = (select, value) => flushSync(() => {
  select.value = value;
  select.dispatchEvent(new Event('change', {bubbles: true}));
});
const discoverCalls = () => calls.map((c) => c.url).filter((u) => u.startsWith('/api/discover?'));
const radarCalls = () => calls.map((c) => c.url).filter((u) => u.startsWith('/api/discover/radar'));

test('fetchDiscover, fetchRadar, fetchTopic and fetchCoverage build the contract paths with the passcode', async () => {
  serve([['/api/', reply(200, {})]]);
  await fetchDiscover({market: 'ZA', kind: '', state: '', platform: '', sort: 'order', limit: 50, cursor: null});
  await fetchDiscover({market: 'all', kind: 'hashtag', state: 'rising', platform: 'tiktok', sort: 'reach', limit: 50, cursor: 'c 2'});
  await fetchRadar('ZA', '');
  await fetchRadar('NG', 'sound');
  await fetchTopic('abc 1', 'ZA');
  await fetchCoverage('2026-10-20');
  await fetchCoverage();
  expect(calls.map((c) => c.url)).toEqual([
    '/api/discover?market=ZA&sort=order&limit=50',
    '/api/discover?market=all&kind=hashtag&state=rising&platform=tiktok&sort=reach&limit=50&cursor=c+2',
    '/api/discover/radar?market=ZA',
    '/api/discover/radar?market=NG&kind=sound',
    '/api/topics/abc%201?market=ZA',
    '/api/coverage?date=2026-10-20',
    '/api/coverage',
  ]);
  expect(calls.every((c) => c.init.headers['X-Passcode'] === 'test-pass')).toBe(true);
});

test('Discover opens on the region market with its heading, and reads the feed and Radar', async () => {
  await mount();
  const heading = host.querySelector('h1');
  expect(heading.textContent).toBe('Discover');
  expect(tab('Markets', 'South Africa').getAttribute('aria-selected')).toBe('true');
  expect(['South Africa', 'Nigeria', 'Kenya', 'All'].every((label) => tab('Markets', label))).toBe(true);
  expect(discoverCalls()).toEqual(['/api/discover?market=ZA&sort=order&limit=50']);
  expect(radarCalls()).toEqual(['/api/discover/radar?market=ZA']);
  expect(cards()).toHaveLength(5);
  expect(text()).toContain('20 October 2026');
});

test('the ZA #couple result is labelled Global in South Africa searches', async () => {
  const couple = grown({...za.cards[0], item_id: 'couple_1', title: '#couple', market: 'ZA'}, 1, {market_scope: 'global'});
  const page = {...pageOne, items: [couple], next_cursor: null};
  await mount({}, [['/api/discover', reply(200, page)]]);
  expect(host.querySelector('.d42-global-scope').textContent).toBe('Also popular outside South Africa');
});

test('global cards in All use their own Nigeria and Kenya search context', async () => {
  const nigeria = grown({...za.cards[0], item_id: 'ng_1', title: '#nigeria_global', market: 'NG'}, 1, {market_scope: 'global'});
  const kenya = grown({...za.cards[0], item_id: 'ke_1', title: '#kenya_global', market: 'KE'}, 2, {market_scope: 'global'});
  const page = {...pageOne, market: 'all', items: [nigeria, kenya], next_cursor: null};
  await mount({region: 'ALL'}, [['/api/discover', reply(200, page)]]);
  expect([...host.querySelectorAll('.d42-global-scope')].map((label) => label.textContent)).toEqual([
    'Also popular outside Nigeria',
    'Also popular outside Kenya',
  ]);
});

/* UI polish, 2 October 2026: restated for the new card. The row used to be a
   three-column grid with the Global label alone on the left and Compare far
   right; now the card is the row, title first, then its line, then one facts
   row that carries the Global label, and Compare sits with the other actions. */
test('Discover global rows read title, line, then one facts row with the Global label, and Compare among the actions', async () => {
  const global = grown({...za.cards[0], item_id: 'compact_global', title: '#compact_global', market: 'ZA'}, 1, {
    market_scope: 'global', market_posts7: 3, total_posts7: 12, market_share7: 0.25,
  });
  const page = {...pageOne, items: [global], next_cursor: null};
  const style = document.createElement('style');
  style.textContent = (await Bun.file(new URL('../../styles/today42.css', import.meta.url)).text()) + '\n' +
    (await Bun.file(new URL('../../styles/discover42.css', import.meta.url)).text());
  document.head.appendChild(style);
  try {
    await mount({}, [['/api/discover', reply(200, page)]]);
    const card = cardTitled('#compact_global');
    const cell = card.closest('.d42-cell');
    expect(cell).toBe(card);
    expect(cell.classList.contains('d42-cell-global')).toBe(true);
    expect(card.parentElement.matches('.d42-feed > ol.t42-cards')).toBe(true);
    const order = [...card.children].map((child) => child.className.split(' ')[0]);
    expect(order.indexOf('t42-card-head')).toBeLessThan(order.indexOf('t42-explanation'));
    expect(order.indexOf('t42-explanation')).toBeLessThan(order.indexOf('t42-card-metrics'));
    const facts = card.querySelector('.t42-card-metrics');
    expect(window.getComputedStyle(facts).display).toBe('block');
    expect(facts.querySelector('.d42-global-scope').textContent).toBe('Also popular outside South Africa');
    expect(facts.querySelector('.d42-market-evidence').textContent).toContain('3 of 12 selected posts');
    const compare = card.querySelector('.t42-actions a[href^="#/compare"]');
    expect(compare.textContent).toBe('Compare with');
    const figureValue = card.querySelector('.tc-figure');
    expect(window.getComputedStyle(figureValue).overflowWrap).toBe('anywhere');
    /* Visual pass, 3 October 2026: reach is set large in the figure panel
       (still named Reach for a screen reader); growth stays in the facts row. */
    expect(card.querySelector('[data-figure="reach"]').textContent).toContain('Reach');
  } finally {
    style.remove();
  }
});

test('Held back names each reason in its row before it is opened', async () => {
  await mount();
  const held = host.querySelector('[data-section="held-back"]');
  const row = button(held, '#fixture_za_ring').closest('li');
  expect(row.querySelector('[data-held-row-reason]').textContent).toBe('Likely coordinated: many new accounts posting the same words');
  expect(held.querySelector('[data-card]')).toBeNull();
});

test('market scope does not get a Global label', async () => {
  const local = grown({...za.cards[0], item_id: 'local_1', title: '#local_scope', market: 'ZA'}, 1, {market_scope: 'market'});
  const page = {...pageOne, items: [local], next_cursor: null};
  await mount({}, [['/api/discover', reply(200, page)]]);
  expect(host.querySelector('.d42-global-scope')).toBeNull();
});

test('missing and invalid scope fail closed to a not-confirmed-local label', async () => {
  const missing = grown({...za.cards[0], item_id: 'missing_scope', title: '#missing_scope', market: 'ZA'}, 1);
  const invalid = grown({...za.cards[0], item_id: 'invalid_scope', title: '#invalid_scope', market: 'ZA'}, 2, {market_scope: 'local'});
  const page = {...pageOne, items: [missing, invalid], next_cursor: null};
  await mount({}, [['/api/discover', reply(200, page)]]);
  expect([...host.querySelectorAll('.d42-global-scope')].map((label) => label.textContent)).toEqual([
    'Not confirmed as local to South Africa',
    'Not confirmed as local to South Africa',
  ]);
});

test('global results with missing or unrecognized market show only Global', async () => {
  const missingMarket = grown({...za.cards[0], item_id: 'missing_market', title: '#missing_market'}, 1, {market: undefined, market_scope: 'global'});
  const invalidMarket = grown({...za.cards[0], item_id: 'invalid_market', title: '#invalid_market'}, 2, {market: 'ZZ', market_scope: 'global'});
  const page = {...pageOne, items: [missingMarket, invalidMarket], next_cursor: null};
  await mount({}, [['/api/discover', reply(200, page)]]);
  expect([...host.querySelectorAll('.d42-global-scope')].map((label) => label.textContent)).toEqual(['Also popular outside this market', 'Also popular outside this market']);
});

test('global Discover cards retain their search label and show the supplied seven-day market basis', async () => {
  const global = grown({...za.cards[0], item_id: 'global_basis', title: '#global_basis', market: 'ZA'}, 1, {
    market_scope: 'global', market_posts7: 3, total_posts7: 12, market_share7: 0.25,
  });
  const page = {...pageOne, items: [global], next_cursor: null};
  await mount({}, [['/api/discover', reply(200, page)]]);
  const card = cardTitled('#global_basis');
  const cell = card.closest('.d42-cell');
  expect(cards()).toHaveLength(1);
  expect(cell.querySelector('.d42-global-scope').textContent).toBe('Also popular outside South Africa');
  expect(cell.querySelector('.d42-market-evidence').textContent).toBe('Market evidence: 3 of 12 selected posts (25%) over 7 days');
});

test('global market basis distinguishes zero, no measured share, and unavailable or inconsistent inputs', async () => {
  const zeroOfTwelve = grown({...za.cards[0], item_id: 'zero_of_twelve', title: '#zero_of_twelve'}, 1, {
    market_scope: 'global', market_posts7: 0, total_posts7: 12, market_share7: 0,
  });
  const zeroOfZero = grown({...za.cards[0], item_id: 'zero_of_zero', title: '#zero_of_zero'}, 2, {
    market_scope: 'global', market_posts7: 0, total_posts7: 0, market_share7: null,
  });
  const unknown = grown({...za.cards[0], item_id: 'unknown_basis', title: '#unknown_basis'}, 3, {
    market_scope: 'global', market_posts7: null, total_posts7: null, market_share7: null,
  });
  const missingShare = grown({...za.cards[0], item_id: 'missing_share', title: '#missing_share'}, 4, {
    market_scope: 'global', market_posts7: 3, total_posts7: 12,
  });
  const inconsistent = grown({...za.cards[0], item_id: 'inconsistent_share', title: '#inconsistent_share'}, 5, {
    market_scope: 'global', market_posts7: 3, total_posts7: 12, market_share7: 0.5,
  });
  const page = {...pageOne, items: [zeroOfTwelve, zeroOfZero, unknown, missingShare, inconsistent], next_cursor: null};
  await mount({}, [['/api/discover', reply(200, page)]]);
  const evidenceLine = (title) => cardTitled(title).closest('.d42-cell').querySelector('.d42-market-evidence');
  const evidence = (title) => evidenceLine(title).textContent;
  expect(evidence('#zero_of_twelve')).toBe('Market evidence: 0 of 12 selected posts (0%) over 7 days');
  expect(evidence('#zero_of_zero')).toBe('Market evidence: 0 of 0 selected posts, no measured share over 7 days');
  /* Demo polish, 2 October 2026: a card with no market basis at all, or no
     share, shows no evidence line rather than "unavailable" on every card;
     its Global label still says where it was seen. Figures that are present
     but disagree still say the share is unavailable. */
  expect(evidenceLine('#unknown_basis')).toBeNull();
  expect(evidenceLine('#missing_share')).toBeNull();
  /* The Global label's own wording is pinned by the scope tests above; here
     it only has to still be there when the evidence line is not. */
  expect(cardTitled('#unknown_basis').closest('.d42-cell').querySelector('.d42-global-scope').textContent.trim()).not.toBe('');
  expect(evidence('#inconsistent_share')).toBe('Market evidence share unavailable');
});

test('a global card the API sends with no market basis fields shows no evidence line', async () => {
  const bare = grown({...za.cards[0], item_id: 'bare_global', title: '#bare_global', market: 'ZA'}, 1, {market_scope: 'global'});
  const page = {...pageOne, items: [bare], next_cursor: null};
  await mount({}, [['/api/discover', reply(200, page)]]);
  const cell = cardTitled('#bare_global').closest('.d42-cell');
  expect(cell.querySelector('.d42-global-scope').textContent.trim()).not.toBe('');
  expect(cell.querySelector('.d42-market-evidence')).toBeNull();
  expect(text()).not.toContain('Market evidence share unavailable');
});

test('an expanded held card shows its Global search context', async () => {
  const heldCard = grown({...za.cards[0], item_id: 'held_global', title: '#held_global', market: 'KE'}, 9, {market_scope: 'global'});
  const page = {...pageOne, items: [], held_back: {count: 1, items: [{
    item_id: 'held_global', title: '#held_global', reason: 'market_unconfirmed', reason_text: 'Market unconfirmed', card: heldCard,
  }]}};
  await mount({}, [['/api/discover', reply(200, page)]]);
  const held = host.querySelector('[data-section="held-back"]');
  click(button(held, '#held_global'));
  expect(held.querySelector('.d42-global-scope').textContent).toBe('Also popular outside Kenya');
});

test('an expanded held Global card shows its supplied market evidence basis', async () => {
  const heldCard = grown({...za.cards[0], item_id: 'held_basis', title: '#held_basis', market: 'KE'}, 9, {
    market_scope: 'global', market_posts7: 3, total_posts7: 12, market_share7: 0.25,
  });
  const page = {...pageOne, items: [], held_back: {count: 1, items: [{
    item_id: 'held_basis', title: '#held_basis', reason: 'market_unconfirmed', reason_text: 'Market unconfirmed', card: heldCard,
  }]}};
  await mount({}, [['/api/discover', reply(200, page)]]);
  const held = host.querySelector('[data-section="held-back"]');
  const opener = button(held, '#held_basis');
  expect(opener.getAttribute('aria-expanded')).toBe('false');
  click(opener);
  expect(held.querySelector('.d42-global-scope').textContent).toBe('Also popular outside Kenya');
  expect(held.querySelector('.d42-market-evidence').textContent).toBe('Market evidence: 3 of 12 selected posts (25%) over 7 days');
});

test('kind tabs come from filters.kinds and a kind reads the feed and Radar again', async () => {
  await mount();
  const labels = [...host.querySelectorAll('[role="tablist"][aria-label="Kinds"] [role="tab"]')].map((t) => t.textContent.trim());
  expect(labels).toEqual(['All kinds', 'Hashtags', 'Sounds', 'Topics', 'Formats', 'Creators']);
  expect(tab('Kinds', 'All kinds').getAttribute('aria-selected')).toBe('true');
  click(tab('Kinds', 'Hashtags'));
  await settle();
  expect(tab('Kinds', 'Hashtags').getAttribute('aria-selected')).toBe('true');
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&kind=hashtag&sort=order&limit=50');
  expect(radarCalls().at(-1)).toBe('/api/discover/radar?market=ZA&kind=hashtag');
});

test('market tabs switch the shared region and feed, and All reads every market without a Radar call', async () => {
  const selected = [];
  await mount({onRegionChange: (region) => selected.push(region)});
  click(tab('Markets', 'Nigeria'));
  await settle();
  expect(selected).toEqual(['NG']);
  expect(tab('Markets', 'Nigeria').getAttribute('aria-selected')).toBe('true');
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=NG&sort=order&limit=50');
  expect(radarCalls().at(-1)).toBe('/api/discover/radar?market=NG');
  const radarsBefore = radarCalls().length;
  click(tab('Markets', 'All'));
  await settle();
  expect(selected).toEqual(['NG', 'ALL']);
  expect(tab('Markets', 'All').getAttribute('aria-selected')).toBe('true');
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=all&sort=order&limit=50');
  expect(radarCalls()).toHaveLength(radarsBefore);
  expect(host.querySelector('[data-section="radar"]').textContent).toContain('Pick one market to see Radar');
});

test('state, platform and sort filters read the feed with their values', async () => {
  await mount();
  const sort = host.querySelector('select[name="sort"]');
  const group = (name) => [...host.querySelectorAll('button[data-filter="' + name + '"]')].map((b) => (b.querySelector('.d42-chip-name') || b).textContent.trim());
  expect(group('state')).toEqual(['All states', 'Emerging', 'First spotted', 'Spike or high on the charts']);
  expect(group('platform')).toEqual(['All platforms', 'TikTok', 'Instagram', 'X']);
  expect([...sort.options].map((o) => o.value)).toEqual(['order', 'velocity', 'reach', 'new']);
  expect(host.querySelector('label[for="' + sort.id + '"]')).not.toBeNull();
  click(chip('state', 'emerging'));
  await settle();
  click(chip('platform', 'tiktok'));
  await settle();
  choose(sort, 'reach');
  await settle();
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&state=emerging&platform=tiktok&sort=reach&limit=50');
});

test('each card links to its topic page for its market', async () => {
  await mount();
  const card = cardTitled('#fixture_za_step');
  const link = card.querySelector('h3 a');
  expect(link.getAttribute('href')).toBe('#/t/' + encodeURIComponent(first.item_id) + '?market=ZA');
  const ask = [...card.querySelectorAll('a')].find((a) => a.textContent === 'Ask about this');
  expect(ask.getAttribute('href')).toContain('&market=ZA&item=' + first.item_id + '&date=2026-10-20');
});

test('the card shows the lifecycle marker with its rule on focus, the spread line, novelty and the figures with their queries', async () => {
  await mount();
  const card = cardTitled('#fixture_za_step');
  const marker = card.querySelector('.tc-lifecycle');
  expect(marker.getAttribute('tabindex')).toBe('0');
  expect(marker.querySelectorAll('.tc-step')).toHaveLength(5);
  expect(marker.querySelectorAll('.tc-step-on')).toHaveLength(2);
  expect(marker.textContent).toContain('Rising');
  expect(marker.getAttribute('aria-label')).toBe('Lifecycle: Rising, step 2 of 5');
  const rule = host.querySelector('#' + marker.getAttribute('aria-describedby'));
  /* Demo polish, 2 October 2026: the API's rule reads in plain words. */
  expect(rule.textContent).toBe('At least twice its usual posting on 2 days');
  expect(card.querySelector('.tc-spread').textContent).toBe(first.spread_line);
  expect(card.querySelector('.tc-novelty').textContent).toBe('Recurrence: the last wave peaked on 2 August 2026 at 1 200 posts on its peak day');
  const reach = card.querySelector('[data-figure="reach"]');
  expect(reach.textContent).toBe('Reach: 31 creators in 3 days');
  expect(reach.getAttribute('data-query-id')).toBe(first.reach.query_id);
  const growth = card.querySelector('[data-figure="growth"]');
  expect(growth.textContent).toBe('Growth: 2.8 times its usual level');
  expect(growth.getAttribute('data-query-id')).toBe(first.growth.query_id);
  const css = await Bun.file(new URL('../../styles/discover42.css', import.meta.url)).text();
  expect(css).toMatch(/\.tc-lifecycle:focus[^{]*\.tc-rule[^{]*\{/);
});

test('reach uses creator for one and keeps creators plural for other counts', () => {
  const oneCreator = {...first, item_id: 'one-creator', reach: figure(1, 'creators in 3 days')};
  const twoCreators = {...second, item_id: 'two-creators', reach: figure(2, 'creators in 3 days')};
  const zeroCreators = {...first, item_id: 'zero-creators', reach: figure(0, 'creators in 3 days')};
  const oneCreatorTotal = {...second, item_id: 'one-creator-total', reach: figure(1, 'creators')};
  flushSync(() => root.render(<ol>
    <TrendCard card={oneCreator} market="ZA" date="2026-10-20" />
    <TrendCard card={twoCreators} market="ZA" date="2026-10-20" />
    <TrendCard card={zeroCreators} market="ZA" date="2026-10-20" />
    <TrendCard card={oneCreatorTotal} market="ZA" date="2026-10-20" />
  </ol>));
  expect([...host.querySelectorAll('[data-figure="reach"]')].map((reach) => reach.textContent)).toEqual([
    'Reach: 1 creator in 3 days',
    'Reach: 2 creators in 3 days',
    'Reach: 0 creators in 3 days',
    'Reach: 1 creator',
  ]);
});

test('a news-driven card says so beside its state and other cards do not', () => {
  const news = {...first, item_id: 'news-card', news_driven: true};
  const plain = {...second, item_id: 'plain-card', news_driven: false};
  flushSync(() => root.render(<ol>
    <TrendCard card={news} market="ZA" date="2026-10-20" />
    <TrendCard card={plain} market="ZA" date="2026-10-20" />
  </ol>));
  expect([...host.querySelectorAll('[data-card]')].map((c) => c.querySelector('.t42-news')?.textContent ?? null))
    .toEqual(['News-driven', null]);
});

test('a card with no data yet says so instead of hiding the field, and not_run reads No explanation yet', async () => {
  await mount();
  const card = cardTitled('fixture za sound one');
  expect(card.textContent).toContain('No explanation yet');
  expect(card.textContent).not.toContain('Explanation held back');
  expect(card.querySelector('.tc-lifecycle')).toBeNull();
  expect(card.querySelector('.tc-spread').textContent).toBe('Spread is not measured yet');
  expect(card.querySelector('[data-figure="growth"]').textContent).toBe('Growth: needs 14 days of data');
  expect(card.querySelector('.tc-novelty').textContent).toBe('New');
  expect(card.innerHTML).not.toContain('null');
});

test('the shared card: Watch and Watching call onWatch with the card', () => {
  const watched = [];
  const onWatch = (card) => watched.push(card.item_id);
  flushSync(() => root.render(<ol><TrendCard card={first} market="ZA" date="2026-10-20" onWatch={onWatch} /><TrendCard card={second} market="ZA" date="2026-10-20" onWatch={onWatch} /></ol>));
  const [one, two] = [...host.querySelectorAll('[data-card]')];
  const watch = button(one, 'Watch');
  expect(watch.getAttribute('aria-pressed')).toBe('false');
  const watching = button(two, 'Watching');
  expect(watching.getAttribute('aria-pressed')).toBe('true');
  click(watch);
  click(watching);
  expect(watched).toEqual([first.item_id, second.item_id]);
});

test('with no watch route to call, Discover shows Watching as words and no dead Watch button', async () => {
  await mount();
  expect(cards().every((card) => !button(card, 'Watch') && !button(card, 'Watching'))).toBe(true);
  expect(cardTitled('fixture za sound one').querySelector('.tc-watching').textContent).toBe('Watching');
  expect(cardTitled('#fixture_za_step').querySelector('.tc-watching')).toBeNull();
});

test('Load more retains current scope results and announces the run date while appending', async () => {
  let resolveMore;
  await mount({}, [
    ['/api/discover/radar', reply(200, radar)],
    ['/api/discover', (url) => url.includes('cursor=c2')
      ? new Promise((resolve) => { resolveMore = resolve; })
      : reply(200, pageOne)],
  ]);
  const more = button(host, 'Load more');
  click(more);
  await settle();
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&sort=order&limit=50&cursor=c2');
  expect(cards()).toHaveLength(5);
  expect(host.querySelector('.d42-feed[aria-busy="true"]')).not.toBeNull();
  expect(host.querySelector('[data-feed-status]').textContent).toContain('20 October 2026');
  expect(host.querySelector('[data-feed-status]').closest('[aria-busy="true"]')).toBeNull();
  expect(button(host, 'Loading more').disabled).toBe(true);

  resolveMore(reply(200, pageTwo));
  await settle();
  expect(cards()).toHaveLength(7);
  expect(host.querySelector('.d42-feed[aria-busy="true"]')).toBeNull();
  expect(host.querySelector('[data-feed-status]')).toBeNull();
  expect(button(host, 'Load more')).toBeUndefined();
});

test('Discover repeated cards and held rows stay compact and wrap full text', async () => {
  const css = await Bun.file(new URL('../../styles/discover42.css', import.meta.url)).text();
  expect(css).toMatch(/\.d42-feed\s*>\s*\.t42-cards\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  expect(css).toMatch(/\.d42-feed\s*>\s*\.t42-cards\s*>\s*\.d42-cell\s*\{[^}]*min-block-size:\s*64px/);
  expect(css).toMatch(/\.d42-feed\s+\.t42-card\s*\{[^}]*min-block-size:\s*64px/);
  expect(css).toMatch(/\.d42-held\s+\.t42-rows\s*>\s*li\s*\{[^}]*min-block-size:\s*64px/);
  expect(css).toContain('overflow-wrap: anywhere;');
  await mount();
  expect(host.querySelectorAll('.d42-feed > .t42-cards > .d42-cell')).toHaveLength(pageOne.items.length);
  expect(host.querySelector('[data-section="held-back"]').textContent).toContain('1 held back');
});

test('Held back shows its count and opens each item to its reason and card', async () => {
  await mount();
  const held = host.querySelector('[data-section="held-back"]');
  expect(held.textContent).toContain('1 held back');
  const opener = button(held, '#fixture_za_ring');
  expect(opener.getAttribute('aria-expanded')).toBe('false');
  expect(held.querySelector('[data-card]')).toBeNull();
  click(opener);
  expect(opener.getAttribute('aria-expanded')).toBe('true');
  expect(held.textContent).toContain('Likely coordinated: many new accounts posting the same words');
  const card = held.querySelector('[data-card]');
  expect(card.querySelector('h3').textContent).toBe('#fixture_za_ring');
  expect(card.querySelector('.t42-flag').textContent).toBe('Likely coordinated');
});

test('Radar plots growth against reach, each point focusable and labelled, flags shown and the held-back count beside it', async () => {
  await mount();
  const section = host.querySelector('[data-section="radar"]');
  const svg = section.querySelector('svg');
  expect(svg.getAttribute('aria-label')).toContain('Growth against reach');
  const points = [...svg.querySelectorAll('a.d42-point')];
  expect(points).toHaveLength(radar.points.length);
  const top = points.find((p) => p.getAttribute('aria-label').startsWith('#fixture_za_step'));
  expect(top.getAttribute('href')).toBe('#/t/' + encodeURIComponent(first.item_id) + '?market=ZA');
  expect(top.getAttribute('aria-label')).toBe('#fixture_za_step: reach 31 creators in 3 days, growth 2.8 times its usual level');
  expect(top.querySelector('text').textContent).toBe('#fixture_za_step');
  const flagged = points.find((p) => p.getAttribute('aria-label').startsWith('fixture za sound one'));
  expect(flagged.getAttribute('aria-label')).toContain('Not assessed (thin sample)');
  expect(flagged.querySelector('.d42-mark-flagged')).not.toBeNull();
  expect(top.querySelector('.d42-mark-flagged')).toBeNull();
  const cx = (p) => Number(p.querySelector('.d42-mark').getAttribute('data-x'));
  const cy = (p) => Number(p.querySelector('.d42-mark').getAttribute('data-y'));
  const low = points.find((p) => p.getAttribute('aria-label').startsWith('#fixture_za_spring'));
  expect(cx(top)).toBeGreaterThan(cx(low));
  expect(cy(top)).toBeLessThan(cy(low));
  expect(section.textContent).toContain('1 held back, not plotted');
});

test('Radar disclosure preserves scatter points, flags, links and visible caveats', async () => {
  const note = 'Collection notes remain visible while the chart is closed';
  const radarWithCaveats = {
    ...radar,
    note,
    points: [...radar.points, {...radar.points[0], item_id: 'without_growth', label: 'fixture without growth', growth: null}],
  };
  await mount({}, standard(radarWithCaveats));
  const section = host.querySelector('[data-section="radar"]');
  const disclosure = section.querySelector('details');
  expect(disclosure).not.toBeNull();
  expect(disclosure.open).toBe(false);
  expect(disclosure.querySelector('summary').textContent).toBe('Open growth against reach chart');
  expect(section.querySelector('.d42-note').textContent).toBe(note);
  expect(section.querySelector('.d42-note').closest('details')).toBeNull();
  expect(section.querySelector('.d42-radar-held').textContent).toBe('1 held back, not plotted');
  expect(section.querySelector('.d42-radar-held').closest('details')).toBeNull();
  expect(section.textContent).toContain('1 without a growth figure yet, not plotted');
  disclosure.open = true;
  expect(disclosure.open).toBe(true);
  const points = [...disclosure.querySelectorAll('a.d42-point')];
  expect(points).toHaveLength(radar.points.length);
  expect(points.map((point) => point.getAttribute('href'))).toEqual(radar.points.map((point) => '#/t/' + encodeURIComponent(point.item_id) + '?market=ZA'));
  const plotted = points.find((point) => point.getAttribute('aria-label').startsWith('#fixture_za_step'));
  expect(plotted.getAttribute('aria-label')).toBe('#fixture_za_step: reach 31 creators in 3 days, growth 2.8 times its usual level');
  const flagged = points.find((point) => point.getAttribute('aria-label').startsWith('fixture za sound one'));
  expect(flagged.getAttribute('aria-label')).toContain('Not assessed (thin sample)');
  expect(flagged.querySelector('.d42-mark-flagged')).not.toBeNull();
  expect(disclosure.querySelector('[aria-label*="without growth"]')).toBeNull();
});

test('Radar keeps a single label inside the existing plot when room is available', async () => {
  const onePoint = {...radar, points: [radar.points[0]]};
  await mount({}, standard(onePoint));
  const svg = host.querySelector('[data-section="radar"] svg');
  expect(svg.getAttribute('viewBox')).toBe('0 0 640 360');
  expect(svg.querySelector('.d42-point-label').textContent).toBe(onePoint.points[0].label);
});

test('Radar keeps every full label and original point link when long labels wrap', async () => {
  const longLabel = 'A long title that must wrap instead of being cut off';
  const radarWithLongLabel = {...radar, points: radar.points.map((point, index) => index === 0 ? {...point, label: longLabel} : point)};
  await mount({}, standard(radarWithLongLabel));
  const svg = host.querySelector('[data-section="radar"] svg');
  const points = [...svg.querySelectorAll('a.d42-point')];
  const labels = [...svg.querySelectorAll('text.d42-point-label')];
  expect(points).toHaveLength(radarWithLongLabel.points.length);
  expect(points.map((point) => point.getAttribute('href'))).toEqual(radarWithLongLabel.points.map((point) => '#/t/' + encodeURIComponent(point.item_id) + '?market=ZA'));
  expect(labels.map((label) => label.textContent)).toEqual(radarWithLongLabel.points.map((point) => point.label));
  const wrappedLines = [...labels[0].querySelectorAll('tspan')].map((line) => line.textContent);
  expect(wrappedLines.length).toBeGreaterThan(1);
  for (const word of longLabel.split(/\s+/)) expect(wrappedLines.some((line) => line.includes(word))).toBe(true);
  expect(labels[0].textContent).toBe(longLabel);
  expect(points[0].getAttribute('aria-label')).toContain(longLabel);
});

test('in warm-up Radar shows the note and ranks reach in a strip', async () => {
  await mount({}, standard(warmRadar));
  const section = host.querySelector('[data-section="radar"]');
  expect(section.textContent).toContain('Growth needs 14 days of data; showing reach only');
  expect(section.querySelector('a.d42-point')).toBeNull();
  const rows = [...section.querySelectorAll('ol.d42-strip li')];
  expect(rows).toHaveLength(warmRadar.points.length);
  expect(rows[0].textContent).toContain('#fixture_za_step');
  /* UI polish, 2 October 2026: restated. The unit is said once in a caption
     over one shared scale, and each row carries its number alone. */
  expect(section.querySelector('[data-strip-caption]').textContent).toBe('Creators in 3 days; bars run from 0 to 31');
  expect(rows[0].querySelector('.d42-bar-value').textContent).toBe('31');
  const reaches = rows.map((row) => Number(row.getAttribute('data-reach')));
  expect(reaches).toEqual([...reaches].sort((a, b) => b - a));
  expect(rows[0].querySelector('a').getAttribute('href')).toBe('#/t/' + encodeURIComponent(first.item_id) + '?market=ZA');
});

const measures = (creators, posts, more = {}) => ({
  creators7: figure(creators, 'creators in 7 days'), posts7: figure(posts, 'posts in 7 days'),
  measured_creators7: figure(Math.min(creators, 2), 'creators in 7 days on boards and followed accounts'),
  views7: null, views_posts7: figure(0, 'posts with a view count in 7 days'),
  located7: figure(0, 'posts with a known place in 7 days'), local_share7: null, platforms: ['tiktok'], ...more,
});

test('in warm-up Radar ranks creators in 7 days, words the rest, and keeps thin rows unbarred', async () => {
  const [a, b, c, ...rest] = warmRadar.points;
  const measured = {
    ...warmRadar,
    window: {from: '2026-10-14', to: '2026-10-20', days: 7, platforms: [{platform: 'youtube', days: 7, days_ok: 6}, {platform: 'tiktok', days: 7, days_ok: 7}]},
    points: [
      {...a, measures: measures(4, 9)},
      {...b, flag: 'market_unconfirmed', measures: measures(12, 40, {views7: figure(120000, 'views in 7 days'), views_posts7: figure(32, 'posts with a view count in 7 days'), located7: figure(48, 'posts with a known place in 7 days'), local_share7: figure(0.625, 'share of located posts local to the market'), platforms: ['tiktok', 'x']})},
      {...c, measures: measures(3, 3)},
      ...rest.map((p) => ({...p, measures: null})),
    ],
  };
  await mount({}, standard(measured));
  const section = host.querySelector('[data-section="radar"]');
  expect(section.querySelector('[data-strip-caption]').textContent)
    .toBe('Each row is a trend, sorted by accounts posting in the last 7 days (14 October to 20 October 2026). Bars compare with the top row; rows under 8 posts come last, without a bar.');
  expect(section.querySelector('[data-window-short]').textContent).toBe('Short collection days: YouTube 6 of 7 days');
  /* Design review, 4 October 2026: restated. The facts were one run-on
     sentence per row; they are now labelled table columns, one per measure.
     Polish pass, 4 October 2026: restated again. Long headers wrapped and
     pushed the row onto three lines, so each header is one short word and
     its full meaning moves to the header's title. */
  /* Wave 8: one creators count per row. The "on boards" count moved into
     the lead figure's tooltip, so no two columns count the same people. */
  const heads = [...section.querySelectorAll('table.d42-table thead th')].map((th) => th.textContent.replace(/\s+/g, ' ').trim());
  expect(heads).toEqual(['Trend', 'Accounts posting last 7 days Different accounts that posted about this in the last 7 days. One account counts once, however many times it posted.',
    'Posts last 7 days', 'Views last 7 days', 'Platforms', 'Local']);
  expect(section.querySelector('[data-col="boards"]')).toBeNull();
  const rows = [...section.querySelectorAll('table.d42-table tbody tr')];
  expect(rows).toHaveLength(measured.points.length);
  expect(rows[0].textContent).toContain(b.label);
  expect(rows[0].querySelector('.d42-bar-value').textContent).toBe('12');
  const cell = (row, key) => row.querySelector('[data-col="' + key + '"]').textContent;
  /* Polish pass, 4 October 2026: restated. Each figure is one unbroken
     token with its qualifier as a muted word beside it, views of 100 000
     and more are compact with the full count in the title, and platforms
     are a short list rather than a sentence. */
  expect([cell(rows[0], 'posts'), cell(rows[0], 'views'), cell(rows[0], 'place')])
    .toEqual(['40', '120k', '63% of 48']);
  expect(rows[0].querySelector('.d42-creators-in').getAttribute('title')).toBe('12 accounts posting in the last 7 days; 2 of them on the boards and followed accounts');
  /* Wave 8: platforms are their marks, each named for a screen reader. */
  expect([...rows[0].querySelectorAll('[data-col="platforms"] [role="img"]')].map((m) => m.getAttribute('aria-label'))).toEqual(['TikTok', 'X']);
  /* Wave 8: the coverage of the view count lives in the title, not beside the figure. */
  expect(rows[0].querySelector('[data-col="views"] .d42-figure').getAttribute('title')).toBe('120\u00a0000 views on 32 of 40 posts');
  expect(rows[0].textContent).not.toContain('Market unconfirmed');
  expect(rows[1].textContent).toContain(a.label);
  /* A measure not recorded is a muted word, never a dash or a gap. */
  expect(cell(rows[1], 'views')).toBe('not recorded');
  expect(cell(rows[1], 'place')).toBe('none known');
  /* Under the floor: a count, no bar, never a drawn level. */
  expect(rows[2].hasAttribute('data-thin')).toBe(true);
  expect(rows[2].querySelector('.d42-bar span')).toBeNull();
  expect(cell(rows[2], 'posts')).toBe('3');
  /* Unmeasured points say so; they are never drawn as zero. */
  expect(rows.at(-1).querySelector('.d42-unmeasured').textContent).toBe('Not measured');
});

test('Radar keeps views under 100 000 in full and names every platform past the first four', async () => {
  const [a, ...rest] = warmRadar.points;
  const measured = {
    ...warmRadar,
    points: [
      {...a, measures: measures(12, 40, {views7: figure(99999, 'views in 7 days'), views_posts7: figure(40, 'posts with a view count in 7 days'),
        platforms: ['tiktok', 'instagram', 'youtube', 'x', 'twitter', 'threads', 'reddit']})},
      ...rest.map((p) => ({...p, measures: null})),
    ],
  };
  await mount({}, standard(measured));
  const row = host.querySelector('[data-section="radar"] table.d42-table tbody tr');
  expect(row.querySelector('[data-col="views"]').textContent).toBe('99\u00a0999');
  const platforms = row.querySelector('[data-col="platforms"]');
  /* x and twitter are one platform; the two past the first four marks are said
     to a screen reader in words, not only as a count on hover. */
  expect([...platforms.querySelectorAll('[role="img"]')].map((m) => m.getAttribute('aria-label'))).toEqual(['TikTok', 'Instagram', 'YouTube', 'X']);
  expect(platforms.querySelector('.d42-platform-more [aria-hidden]').textContent).toBe('+2');
  expect(platforms.querySelector('.d42-platform-more .sr-only').textContent).toBe('and Threads and Reddit');
});

test('a 401 hands the reader to the passcode flow', async () => {
  let asked = 0;
  await mount({onAuth: () => { asked += 1; }}, [['/api/', reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})]]);
  expect(asked).toBeGreaterThan(0);
  expect(host.querySelector('[data-card]')).toBeNull();
});

test('an error offers Try again that reads again', async () => {
  let answer = reply(500, {error: 'internal', message: 'Something broke.'});
  await mount({}, [['/api/discover/radar', reply(200, radar)], ['/api/discover', () => answer]]);
  expect(text()).toContain('Discover could not load');
  expect(text()).toContain('Something broke.');
  answer = reply(200, pageOne);
  click(button(host, 'Try again'));
  await settle();
  expect(cards()).toHaveLength(5);
  expect(discoverCalls()).toHaveLength(2);
  expect(radarCalls()).toHaveLength(1);
});

test('a failed filter change clears prior scope results and offers a manual retry', async () => {
  let retry = false;
  const freshPage = {...pageOne, items: [grown({...first, title: '#after_retry'}, 1)]};
  await mount({}, [
    ['/api/discover/radar', reply(200, radar)],
    ['/api/discover', (url) => {
      if (url.includes('state=emerging')) return retry
        ? reply(200, freshPage)
        : reply(503, {error: 'unavailable', message: 'Service timed out.'});
      return reply(200, pageOne);
    }],
  ]);
  expect(cards()).toHaveLength(5);

  click(chip('state', 'emerging'));
  await settle();
  expect(text()).toContain('Discover could not load');
  expect(text()).toContain('Service timed out.');
  expect(cards()).toHaveLength(0);
  expect(host.querySelector('.d42-feed[aria-busy="true"]')).toBeNull();
  expect(button(host, 'Try again')).not.toBeUndefined();

  retry = true;
  click(button(host, 'Try again'));
  await settle();
  expect(cardTitled('#after_retry')).toBeDefined();
  expect(discoverCalls()).toHaveLength(3);
});

test('slow feed and Radar reads keep the page shell, make no extra requests and show ready data as soon as it arrives', async () => {
  let resolveFeed;
  let resolveRadar;
  await mount({}, [
    ['/api/discover/radar', () => new Promise((resolve) => { resolveRadar = resolve; })],
    ['/api/discover', () => new Promise((resolve) => { resolveFeed = resolve; })],
  ]);

  expect(host.querySelector('h1').textContent).toBe('Discover');
  expect(tab('Markets', 'South Africa').getAttribute('aria-selected')).toBe('true');
  expect(chip('state', '')).not.toBeNull();
  expect(text()).toContain('Loading Discover');
  expect(text()).toContain('Loading Radar');
  expect(host.querySelector('.d42-feed[aria-busy="true"] .d42-loading-grid[aria-hidden="true"]')).not.toBeNull();
  expect(host.querySelectorAll('.d42-loading-grid .ui-skeleton')).toHaveLength(1);
  expect(host.querySelector('[data-feed-status][role="status"]').closest('[aria-busy="true"]')).toBeNull();
  expect(discoverCalls()).toHaveLength(1);
  expect(radarCalls()).toHaveLength(1);

  await new Promise((resolve) => setTimeout(resolve, 5100));
  expect(text()).toContain('Discover is taking longer than usual.');
  expect(text()).toContain('Radar is taking longer than usual.');
  expect(discoverCalls()).toHaveLength(1);
  expect(radarCalls()).toHaveLength(1);

  resolveRadar(reply(200, radar));
  await settle();
  expect(host.querySelector('[data-section="radar"] svg')).not.toBeNull();
  expect(text()).not.toContain('Radar is taking longer than usual.');
  expect(text()).toContain('Discover is taking longer than usual.');

  resolveFeed(reply(200, pageOne));
  await settle();
  expect(cards()).toHaveLength(pageOne.items.length);
  expect(text()).not.toContain('Discover is taking longer than usual.');
  expect(host.querySelector('.d42-feed[aria-busy="true"]')).toBeNull();
}, 10000);

test('a filter change aborts the older Feed read and its late response cannot replace the newer results', async () => {
  let resolveStale;
  const stalePage = {...pageOne, items: [grown({...first, title: '#stale_filter'}, 1)]};
  const freshPage = {...pageOne, items: [grown({...first, title: '#fresh_filter'}, 1)]};
  await mount({}, [
    ['/api/discover/radar', reply(200, radar)],
    ['/api/discover', (url) => {
      if (url.includes('state=emerging') && !url.includes('platform=tiktok')){
        return new Promise((resolve) => { resolveStale = resolve; });
      }
      if (url.includes('platform=tiktok')) return reply(200, freshPage);
      return reply(200, pageOne);
    }],
  ]);

  click(chip('state', 'emerging'));
  await settle();
  const staleCall = calls.find((call) => call.url.includes('state=emerging') && !call.url.includes('platform=tiktok'));
  expect(staleCall).toBeDefined();
  expect(staleCall.init.signal.aborted).toBe(false);
  /* Wave 8: the earlier rows stay on screen, dimmed, while the new ones load. */
  expect(host.querySelector('.d42-feed[aria-busy="true"]')).not.toBeNull();
  expect(host.querySelector('.d42-feed').classList.contains('d42-dim')).toBe(true);
  expect(cards()).toHaveLength(pageOne.items.length);
  expect(host.querySelector('[data-feed-status]').textContent).toContain('Updating results');
  expect(button(host, 'Try again')).toBeUndefined();

  click(chip('platform', 'tiktok'));
  await settle();
  expect(staleCall.init.signal.aborted).toBe(true);
  expect(cardTitled('#fresh_filter')).toBeDefined();
  expect(discoverCalls()).toHaveLength(3);
  /* Radar reads again with each choice, so it follows the list. */
  expect(radarCalls()).toHaveLength(3);

  resolveStale(reply(200, stalePage));
  await settle();
  expect(cardTitled('#fresh_filter')).toBeDefined();
  expect(cardTitled('#stale_filter')).toBeUndefined();
  expect(radarCalls()).toHaveLength(3);
});

test('an empty answer still renders the heading and says nothing matched', async () => {
  await mount({}, [['/api/', reply(200, {})]]);
  expect(host.querySelector('h1').textContent).toBe('Discover');
  expect(text()).toContain('No trends have cleared the checks in South Africa yet.');
  expect(text()).toContain('Nothing held back.');
  expect(host.innerHTML).not.toContain('undefined');
});

test('the shared card renders a Stage 1 card with no Stage 2 lines and no Watch without onWatch', () => {
  flushSync(() => root.render(<ol><TrendCard card={za.cards[0]} market="ZA" date="2026-09-30" /></ol>));
  const card = host.querySelector('[data-card]');
  expect(card.querySelector('h3').textContent).toBe('#fixture_za_step');
  expect(card.querySelector('.tc-spread')).toBeNull();
  expect(card.querySelector('[data-figure]')).toBeNull();
  expect(button(card, 'Watch')).toBeUndefined();
  expect(button(card, 'Posts')).toBeDefined();
});

test('copy stays clear and the page and section use their type roles', async () => {
  for (const file of ['../../discover42.jsx', '../TrendCard.jsx']){
    const source = await Bun.file(new URL(file, import.meta.url)).text();
    expect(source).not.toMatch(/gen ?z|millennial|youth|google trends|prompt pulse|nano banana/i);
  }
  const css = await Bun.file(new URL('../../styles/discover42.css', import.meta.url)).text();
  const shared = await Bun.file(new URL('../../styles/today42.css', import.meta.url)).text();
  const page = shared.split('.t42-heading {')[1]?.split('}')[0] || '';
  const section = css.split('.d42-title {')[1]?.split('}')[0] || '';
  expect(page).toContain('font-size: var(--type-page-title)');
  expect(page).toContain('line-height: var(--leading-page-title)');
  expect(section).toContain('font-size: var(--type-section-title)');
  expect(section).toContain('line-height: var(--leading-section-title)');
});

/* With the watch write (contract.md section 10.5), which the app passes in,
   Watch opens the rule chooser and the new watch shows as Watching. */
test('with the watch write, Watch opens the chooser and the card turns to Watching', async () => {
  const made = [];
  await mount({onCreateWatch: (body) => { made.push(body); return Promise.resolve({watch_id: 'w_9', ...body}); }});
  click(button(cardTitled('#fixture_za_step'), 'Watch'));
  const dialog = host.querySelector('[role="dialog"]');
  expect(dialog).not.toBeNull();
  const growth = [...dialog.querySelectorAll('label')].find((l) => l.textContent.includes('When growth passes')).querySelector('input');
  click(growth);
  const ratio = dialog.querySelector('input[name="ratio"]');
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(ratio, '3');
  flushSync(() => ratio.dispatchEvent(new window.Event('input', {bubbles: true})));
  click(button(dialog, 'Watch'));
  await settle();
  expect(made).toEqual([{target: {kind: 'item', item_id: first.item_id}, market: 'ZA', rule: {ratio_over: 3}, label: '#fixture_za_step'}]);
  expect(button(cardTitled('#fixture_za_step'), 'Watching')).toBeDefined();
});

test('with the watch write, Watching on a watched card points to Alerts and writes nothing', async () => {
  const made = [];
  await mount({onCreateWatch: (body) => { made.push(body); return Promise.resolve(body); }});
  click(button(cardTitled('fixture za sound one'), 'Watching'));
  const dialog = host.querySelector('[role="dialog"]');
  expect(dialog.textContent).toContain('You are watching this');
  expect([...dialog.querySelectorAll('a')].some((a) => a.getAttribute('href') === '#/alerts')).toBe(true);
  expect(button(dialog, 'Watch')).toBeUndefined();
  click(button(dialog, 'Close'));
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(made).toHaveLength(0);
});

test('on All, a watch is made in the card market', async () => {
  const made = [];
  const allPage = {...pageOne, market: 'all', items: pageOne.items.map((card) => ({...card, market: 'NG'})), next_cursor: null};
  await mount({region: 'ALL', onCreateWatch: (body) => { made.push(body); return Promise.resolve({watch_id: 'w_9', ...body}); }}, [
    ['/api/discover', reply(200, allPage)],
  ]);
  click(button(cardTitled('#fixture_za_step'), 'Watch'));
  click(button(host.querySelector('[role="dialog"]'), 'Watch'));
  await settle();
  expect(made[0].market).toBe('NG');
});

const compareWith = (scope) => [...scope.querySelectorAll('a')].find((a) => a.textContent === 'Compare with');

test('each card, held back or not, opens Compare with its item in its market', async () => {
  await mount();
  for (const card of pageOne.items){
    const cell = cardTitled(card.title).closest('.d42-cell');
    expect(compareWith(cell).getAttribute('href')).toBe('#/compare?mode=items&items=' + encodeURIComponent(card.item_id) + '&market=ZA');
  }
  const held = host.querySelector('[data-section="held-back"]');
  click(button(held, '#fixture_za_ring'));
  expect(compareWith(held).getAttribute('href')).toBe('#/compare?mode=items&items=held_1&market=ZA');
});

test('on All, Compare with opens in the card market', async () => {
  const allPage = {...pageOne, market: 'all', items: pageOne.items.map((card) => ({...card, market: 'NG'})), next_cursor: null};
  await mount({region: 'ALL'}, [['/api/discover', reply(200, allPage)]]);
  const cell = cardTitled('#fixture_za_step').closest('.d42-cell');
  expect(compareWith(cell).getAttribute('href')).toBe('#/compare?mode=items&items=' + encodeURIComponent(first.item_id) + '&market=NG');
});

test('Searching now: Discover selected and All groups stay outside cards and survive Load more', async () => {
  await mount();
  expect(Boolean(host.querySelector('[data-section="searching-now"]'))).toBe(false);
  const baseline = {
    cards: cards().map((card) => card.textContent),
    marketEvidence: [...host.querySelectorAll('.d42-market-evidence')].map((node) => node.textContent),
    filters: [...host.querySelectorAll('button[data-filter="state"]')].map((option) => [option.getAttribute('data-value'), option.textContent]),
  };

  flushSync(() => root.unmount());
  root = createRoot(host);
  calls = [];
  const selectedPage = {...pageOne, searching_now: clone(searchingNowFixture)};
  const emptyAllPage = {...selectedPage, market: 'all', items: [], next_cursor: null};
  const routes = standard();
  routes[1] = ['/api/discover', (url) => {
    if (url.includes('cursor=c2')) return reply(200, pageTwo);
    return reply(200, url.includes('market=all') ? emptyAllPage : selectedPage);
  }];
  await mount({}, routes);

  const group = (market) => host.querySelector('[data-section="searching-now"] [data-search-market="' + market + '"]');
  expect(group('ZA')?.textContent || '').toContain('fixture query ZA');
  expect(Boolean(group('NG'))).toBe(false);
  expect(cards().map((card) => card.textContent)).toEqual(baseline.cards);
  expect([...host.querySelectorAll('.d42-market-evidence')].map((node) => node.textContent)).toEqual(baseline.marketEvidence);
  expect([...host.querySelectorAll('button[data-filter="state"]')].map((option) => [option.getAttribute('data-value'), option.textContent])).toEqual(baseline.filters);
  expect(discoverCalls()).toEqual(['/api/discover?market=ZA&sort=order&limit=50']);
  expect(radarCalls()).toEqual(['/api/discover/radar?market=ZA']);

  click(button(host, 'Load more'));
  await settle();
  expect(host.querySelectorAll('[data-section="searching-now"]').length).toBe(1);
  expect(cards().slice(0, baseline.cards.length).map((card) => card.textContent)).toEqual(baseline.cards);
  expect(cards().length).toBe(baseline.cards.length + pageTwo.items.length);
  expect(discoverCalls()).toHaveLength(2);
  expect(radarCalls()).toHaveLength(1);

  click(tab('Markets', 'All'));
  await settle();
  expect(cards().length).toBe(0);
  expect(text()).toContain('No trends have cleared the checks in any market yet.');
  const groups = [...host.querySelectorAll('[data-section="searching-now"] [data-search-market]')];
  expect(groups.map((node) => node.getAttribute('data-search-market'))).toEqual(['ZA', 'NG', 'KE']);
  expect(Boolean(group('ZA')?.closest('ol'))).toBe(false);
  expect(discoverCalls()).toHaveLength(3);
  expect(radarCalls()).toEqual(['/api/discover/radar?market=ZA']);
});

test('Searching now: Discover hides an empty array without changing cards or reads', async () => {
  const routes = standard();
  routes[1] = ['/api/discover', reply(200, {...pageOne, searching_now: []})];
  await mount({}, routes);
  expect(Boolean(host.querySelector('[data-section="searching-now"]'))).toBe(false);
  expect(cards().length).toBe(pageOne.items.length);
  expect(discoverCalls()).toEqual(['/api/discover?market=ZA&sort=order&limit=50']);
  expect(radarCalls()).toEqual(['/api/discover/radar?market=ZA']);
});

/* Design critique, 2 October 2026: each Discover row said reach twice ("31
   creators, 3 days" then "Reach: 31 creators in 3 days") and repeated the
   same not-yet notes on every card. A fact is said once: reach keeps its
   figure and query, and a not-yet state true of every card is one sentence
   for the page. A state that differs between cards stays on each card. */
const warmItems = pageOne.items.map((card, i) => ({
  ...card, spread_line: null, growth: null,
  count_line: (10 + i + 1) + ' creators, 3 days', reach: figure(10 + i + 1, 'creators in 3 days'),
}));
const warmPage = {...pageOne, items: warmItems, next_cursor: null};

test('a not-yet state true of every card is said once for the page, not on each card', async () => {
  await mount({}, [['/api/discover/radar', reply(200, radar)], ['/api/discover', reply(200, warmPage)]]);
  const note = host.querySelector('.d42-feed [data-feed-not-yet]');
  expect(note?.textContent).toBe('For every trend here, spread is not measured yet and growth needs 14 days of data.');
  expect(host.querySelectorAll('.d42-feed .tc-spread').length).toBe(0);
  expect(host.querySelectorAll('.d42-feed [data-figure="growth"]').length).toBe(0);
  expect(text().split('needs 14 days of data').length - 1).toBe(1);
});

test('when Radar already says growth needs 14 days, the page note does not say it again', async () => {
  await mount({}, [['/api/discover/radar', reply(200, warmRadar)], ['/api/discover', reply(200, warmPage)]]);
  expect(host.querySelector('[data-section="radar"]').textContent).toContain('Growth needs 14 days of data');
  expect(host.querySelector('.d42-feed [data-feed-not-yet]')?.textContent).toBe('For every trend here, spread is not measured yet.');
  expect(text().split('needs 14 days of data').length - 1).toBe(1);
});

test('a not-yet state that differs between cards stays on each card and the page says nothing', async () => {
  await mount();
  expect(Boolean(host.querySelector('[data-feed-not-yet]'))).toBe(false);
  expect(cardTitled('fixture za sound one').querySelector('.tc-spread').textContent).toBe('Spread is not measured yet');
});

test('reach is said once: a count line that says the same as reach gives way to the figure', async () => {
  const same = {...first, item_id: 'same', count_line: '31 creators, 3 days', reach: figure(31, 'creators in 3 days')};
  const other = {...second, item_id: 'other', count_line: '12 creators, 3 days', reach: figure(4, 'creators in 3 days')};
  flushSync(() => root.render(<ol>
    <TrendCard card={same} market="ZA" date="2026-10-20" />
    <TrendCard card={other} market="ZA" date="2026-10-20" />
  </ol>));
  const [one, two] = [...host.querySelectorAll('[data-card]')];
  expect(Boolean(one.querySelector('.t42-count'))).toBe(false);
  expect(one.querySelector('[data-figure="reach"]').textContent).toBe('Reach: 31 creators in 3 days');
  expect(one.querySelector('[data-figure="reach"]').getAttribute('data-query-id')).toBe(same.reach.query_id);
  expect(two.querySelector('.t42-count').textContent).toBe('12 creators, 3 days');
  expect(two.querySelector('[data-figure="reach"]').textContent).toBe('Reach: 4 creators in 3 days');
});

/* Audit, 4 October 2026 (F07): the kind, state, platform and sort filters
   live in the #/explore query, so Back from a topic page lands on the same
   filtered list. The hash is replaced, never pushed. */
test('Discover reads its filters from the #/explore query and writes them back without a new history entry', async () => {
  const withBrand = {...pageOne, filters: {...pageOne.filters, kinds: ['brand', ...pageOne.filters.kinds]}};
  const routes = [
    ['/api/discover/radar', reply(200, radar)],
    ['/api/discover', reply(200, withBrand)],
  ];
  window.history.replaceState(null, '', '#/explore?kind=brand&state=emerging&platform=tiktok&sort=reach');
  const entries = window.history.length;
  try {
    await mount({}, routes);
    expect(tab('Kinds', 'Brand').getAttribute('aria-selected')).toBe('true');
    expect(chip('state', 'emerging').getAttribute('aria-pressed')).toBe('true');
    expect(chip('platform', 'tiktok').getAttribute('aria-pressed')).toBe('true');
    expect(host.querySelector('select[name="sort"]').value).toBe('reach');
    expect(discoverCalls()[0]).toBe('/api/discover?market=ZA&kind=brand&state=emerging&platform=tiktok&sort=reach&limit=50');
    expect(radarCalls()[0]).toBe('/api/discover/radar?market=ZA&kind=brand&state=emerging&platform=tiktok');

    click(tab('Kinds', 'Hashtags'));
    await settle();
    expect(window.location.hash).toBe('#/explore?kind=hashtag&state=emerging&platform=tiktok&sort=reach');
    click(chip('state', 'emerging'));
    await settle();
    click(chip('platform', 'tiktok'));
    await settle();
    choose(host.querySelector('select[name="sort"]'), 'order');
    await settle();
    expect(window.location.hash).toBe('#/explore?kind=hashtag');
    click(tab('Kinds', 'All kinds'));
    await settle();
    expect(window.location.hash).toBe('#/explore');
    expect(window.history.length).toBe(entries);

    /* Back remounts the page on the stored hash: it reads the filters again. */
    window.history.replaceState(null, '', '#/explore?kind=brand');
    flushSync(() => root.unmount());
    root = createRoot(host);
    calls = [];
    await mount({}, routes);
    expect(tab('Kinds', 'Brand').getAttribute('aria-selected')).toBe('true');
    expect(discoverCalls()[0]).toBe('/api/discover?market=ZA&kind=brand&sort=order&limit=50');
  } finally {
    window.history.replaceState(null, '', '#');
  }
});

test('Discover ignores an unknown sort in the query and leaves other pages’ hashes alone', async () => {
  window.history.replaceState(null, '', '#/explore?sort=loudest&kind=sound');
  try {
    await mount();
    expect(host.querySelector('select[name="sort"]').value).toBe('order');
    expect(discoverCalls()[0]).toBe('/api/discover?market=ZA&kind=sound&sort=order&limit=50');
    expect(window.location.hash).toBe('#/explore?kind=sound');
    window.history.replaceState(null, '', '#/topic/abc');
    click(tab('Kinds', 'Hashtags'));
    await settle();
    expect(window.location.hash).toBe('#/topic/abc');
  } finally {
    window.history.replaceState(null, '', '#');
  }
});

/* Audit, 4 October 2026 (F01, DIS-02, DIS-03): the Radar table shows its
   first 20 rows, with a button for the rest; its scroll box is a named,
   focusable region; a zero draws no bar and an unknown says not recorded. */
const manyRadar = (count, extra = () => ({})) => ({
  ...warmRadar,
  points: Array.from({length: count}, (_, i) => ({
    item_id: 'pt_' + i, label: 'Trend number ' + i, kind: 'topic', state: 'emerging', flag: null,
    growth: null, reach: figure(5, 'creators in 3 days'), measures: measures(100 - i, 40), ...extra(i),
  })),
});

test('Radar shows the first 20 rows and a button to show all, then fewer', async () => {
  await mount({}, standard(manyRadar(30)));
  const section = host.querySelector('[data-section="radar"]');
  const bodyRows = () => [...section.querySelectorAll('table.d42-table tbody tr')];
  expect(bodyRows()).toHaveLength(20);
  expect(bodyRows()[0].textContent).toContain('Trend number 0');
  const all = button(section, 'Show all 30');
  expect(all).toBeTruthy();
  expect(all.getAttribute('aria-expanded')).toBe('false');
  click(all);
  expect(bodyRows()).toHaveLength(30);
  const fewer = button(section, 'Show fewer');
  expect(fewer.getAttribute('aria-expanded')).toBe('true');
  click(fewer);
  expect(bodyRows()).toHaveLength(20);
});

test('Radar with 20 rows or fewer has no show-all button', async () => {
  await mount({}, standard(manyRadar(20)));
  const section = host.querySelector('[data-section="radar"]');
  expect(section.querySelectorAll('table.d42-table tbody tr')).toHaveLength(20);
  expect([...section.querySelectorAll('button')].some((b) => /^Show (all|fewer)/.test(b.textContent.trim()))).toBe(false);
});

test('the Radar scroll box is a named, focusable region and each name keeps its full text in a title', async () => {
  const long = 'A very long trend name that the table cuts short with an ellipsis at laptop width';
  await mount({}, standard(manyRadar(3, (i) => (i === 0 ? {label: long} : {}))));
  const wrap = host.querySelector('[data-section="radar"] .d42-table-wrap');
  expect(wrap.getAttribute('role')).toBe('region');
  expect(wrap.getAttribute('aria-label')).toBeTruthy();
  expect(wrap.getAttribute('tabindex')).toBe('0');
  const link = host.querySelector('[data-section="radar"] tbody th a');
  expect(link.getAttribute('title')).toBe(long);
});

test('Radar says no flag only for a row with no flag, never for a market it could not confirm', async () => {
  const data = manyRadar(3, (i) => (
    i === 0 ? {flag: 'likely_coordinated', measures: measures(100, 40)}
    : i === 1 ? {flag: 'market_unconfirmed', measures: measures(50, 20)}
    : {measures: measures(20, 10)}
  ));
  await mount({}, standard(data));
  const rows = [...host.querySelectorAll('[data-section="radar"] table.d42-table tbody tr')];
  const flagOf = (label) => rows.find((row) => row.textContent.includes(label)).querySelector('[data-col="flag"]').textContent;
  expect(flagOf('Trend number 0')).toBe('Likely coordinated');
  /* Market unconfirmed was recorded: fewer than 8 placed posts in 7 days. */
  expect(flagOf('Trend number 1')).toBe('few placed posts');
  expect(rows.find((row) => row.textContent.includes('Trend number 1')).querySelector('[data-col="flag"] span').getAttribute('title'))
    .toBe('Fewer than 8 posts in 7 days had a known place, so 42 has not confirmed the market');
  expect(flagOf('Trend number 2')).toBe('no flag');
});

test('Radar draws no bar for zero creators and says not recorded for unknown measures', async () => {
  const data = manyRadar(4, (i) => (
    i === 1 ? {measures: measures(0, 20)}
    : i === 2 ? {measures: {...measures(0, 20), creators7: null, located7: null, views7: null}}
    : i === 3 ? {measures: {...measures(0, 0), views7: null, views_posts7: null}}
    : {flag: 'likely_coordinated', measures: measures(100, 40, {views7: figure(500, 'views in 7 days')})}
  ));
  await mount({}, standard(data));
  const rows = [...host.querySelectorAll('[data-section="radar"] table.d42-table tbody tr')];
  const byLabel = (label) => rows.find((row) => row.textContent.includes(label));
  const cellOf = (row, key) => row.querySelector('[data-col="' + key + '"]').textContent;
  const zero = byLabel('Trend number 1');
  expect(zero.querySelector('.d42-bar-value').textContent).toBe('0');
  expect(Boolean(zero.querySelector('.d42-bar span'))).toBe(false);
  const unknown = byLabel('Trend number 2');
  expect(Boolean(unknown.querySelector('.d42-bar span'))).toBe(false);
  expect(unknown.querySelector('.d42-creators').textContent).toContain('not recorded');
  expect(unknown.hasAttribute('data-creators')).toBe(false);
  expect(cellOf(unknown, 'place')).toBe('not recorded');
  /* No flag is a checked row with nothing raised, not an unknown measure. */
  expect(cellOf(unknown, 'flag')).toBe('no flag');
  /* Unknown creators sort after a known zero, among the barred rows. */
  expect(rows.indexOf(unknown) - rows.indexOf(zero)).toBe(1);
  const noPosts = byLabel('Trend number 3');
  expect(cellOf(noPosts, 'views')).toBe('not recorded');
  expect(noPosts.textContent).not.toMatch(/\bnone\b/);
  expect(unknown.textContent).not.toMatch(/\bnone\b/);
  const bar = byLabel('Trend number 0').querySelector('.d42-bar span');
  expect(bar.style.width).toBe('100%');
});

/* Audit, 4 October 2026 (DIS-06): only the "No explanation yet" line is
   visually hidden; a failed-checks line stays visible. */
test('Discover hides only the not-run explanation line, not a failed-checks one', async () => {
  const failedCard = {...first, item_id: 'failed_1', title: '#failed_one', explained: false, explanation: null, claims: [], explanation_status: 'failed_checks'};
  const page = {...pageOne, items: [second, failedCard]};
  await mount({}, [['/api/discover/radar', reply(200, radar)], ['/api/discover', reply(200, page)]]);
  const notRun = cardTitled(second.title);
  const failed = cardTitled('#failed_one');
  expect(notRun.classList.contains('d42-cell-not-run')).toBe(true);
  expect(failed.classList.contains('d42-cell-not-run')).toBe(false);
  expect(failed.querySelector('.t42-held-line').textContent).toBe('Explanation held back: it did not pass the checks');
  const css = await Bun.file(new URL('../../styles/discover42.css', import.meta.url)).text();
  const hiders = [...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]*\.t42-held-line[^{}]*)\{([^}]*)\}/g)].filter((m) => /clip-path/.test(m[2])).map((m) => m[1].trim());
  expect(hiders).toEqual(['.t42 .d42-feed .t42-card.d42-cell-not-run > .t42-held-line']);
});

/* Owner, 5 October 2026: "1 creator in 3 days" on every NG row read as rookie
   numbers. The 3-day reach counts only trending boards and followed accounts
   (what the quality checks use), so each Discover card now also shows the
   7-day creators from every source, which the API sends as reach7. */
const withReach7 = (card, value) => ({...card, reach: figure(1, 'creators in 3 days'), reach7: {...figure(value, 'creators in 7 days'), query_id: 'q_item_reach'}});

test('a Discover card shows the 7-day reach from every source beside the 3-day reach, and says which is which', async () => {
  const page = clone(pageOne);
  page.items = [withReach7(page.items[0], 59), withReach7(page.items[1], 1), ...page.items.slice(2)];
  await mount({}, [['/api/discover/radar', reply(200, radar)], ['/api/discover', reply(200, page)]]);
  const [one, two, three] = cards();
  const big = one.querySelector('.tc-big');
  const lead = big.querySelector('[data-figure="reach"]');
  const seven = big.querySelector('[data-figure="reach7"]');
  // The 3-day figure is unchanged and still leads.
  expect(lead.classList.contains('tc-big-lead')).toBe(true);
  expect(lead.querySelector('.tc-big-value').textContent + lead.querySelector('.tc-big-unit').textContent).toBe('1 creator in 3 days');
  expect(lead.getAttribute('title')).toBe('From query q_creators_in_3_days. Counts only trending boards and followed accounts, which is what the quality checks use.');
  expect(seven.classList.contains('tc-big-lead')).toBe(false);
  expect(seven.querySelector('.tc-big-value').textContent + seven.querySelector('.tc-big-unit').textContent).toBe('59 creators in 7 days, every source');
  expect(seven.getAttribute('data-query-id')).toBe('q_item_reach');
  expect(seven.getAttribute('title')).toContain('Every collection lane 42 reads, over 7 days, with flagged accounts left out. The 3-day reach counts only trending boards and followed accounts');
  expect(seven.querySelector('.sr-only').textContent).toBe('Reach from every source: ');
  expect(two.querySelector('[data-figure="reach7"]').textContent).toContain('1 creator in 7 days, every source');
  // A card the API sent no reach7 for shows no second reach and keeps its plain query title.
  expect(three.querySelector('[data-figure="reach7"]')).toBeNull();
  expect(three.querySelector('[data-figure="reach"]').getAttribute('title')).toBe('From query ' + page.items[2].reach.query_id);
});

test('the 7-day reach changes neither the card order nor the cards shown', async () => {
  const page = clone(pageOne);
  page.items = page.items.map((card, i) => withReach7(card, 100 - i * 30));
  page.items.reverse();
  await mount({}, [['/api/discover/radar', reply(200, radar)], ['/api/discover', reply(200, page)]]);
  expect(cards().map((card) => card.querySelector('h3').textContent)).toEqual(page.items.map((card) => card.title));
});

test('the 7-day reach shows only on a card that carries it: Today cards and cards without the field are as before', () => {
  flushSync(() => root.render(<ol>
    <TrendCard card={{...first, reach7: null}} market="ZA" date="2026-10-20" />
    <TrendCard card={first} market="ZA" date="2026-10-20" />
  </ol>));
  expect(host.querySelectorAll('[data-figure="reach7"]').length).toBe(0);
  expect(host.textContent).not.toContain('every source');
});
