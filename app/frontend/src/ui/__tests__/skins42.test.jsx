/* Client skins on the 42 API (contract.md section 15.1). The Skins screen
   lists each skin with its markets, how many terms, hashtags and accounts it
   holds, and its status, and New skin opens the form. Accounts come only
   from the approved list, which is empty today, so the form says so and
   offers no way to type one. A skin page shows Today narrowed to the skin,
   with the per-market kept and left-out counts and every held-back item with
   its reason, the skin's watches and alerts, Build weekly report as its one
   red action, and the weekly reports that became dossiers. Masked handles
   stay masks. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const api = await import('../../api42.js');
const {SkinsPage42, SkinPage42} = await import('../../skins42.jsx');
const {resolveHostRoute} = await import('../../App.jsx');

const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});

const SKIN = {
  skin_id: 'sk_000000000001', skin_key: 'bsa', created_at: '2026-09-28T08:00:00+02:00', status_at: '2026-09-28T08:00:00+02:00', who: 'passcode',
  name: 'Brand South Africa', markets: ['ZA', 'NG'], terms: ['proudly south african', 'ubuntu'], hashtags: ['#playyourpart'],
  accounts: [], watch_ids: ['w_000000000001'], template: 'weekly_report', status: 'active',
};
const ARCHIVED = {
  skin_id: 'sk_000000000002', skin_key: 'fixture_lens', created_at: '2026-09-20T08:00:00+02:00', status_at: '2026-09-27T08:00:00+02:00', who: 'passcode',
  name: 'Fixture lens', markets: ['KE'], terms: ['matatu art'], hashtags: [],
  accounts: [{platform: 'x', handle: '@fixture_org', org: 'Fixture Org', role: 'client'}], watch_ids: [], template: 'weekly_report', status: 'archived',
};
const SKINS = {skins: [SKIN, ARCHIVED]};

const CARD = {
  item_id: 'item_ubuntu', market: 'ZA', date: '2026-09-30', title: 'Ubuntu stories, first told by @***', state_word: 'Rising',
  count_line: '31 creators in 3 days', explanation_status: 'explained', explanation: 'People retell ubuntu stories from their street.', evidence: [],
};
const SKIN_TODAY = {
  date: '2026-09-30', heading: 'Today, 30 September 2026', headline: null,
  markets: [
    {
      market: 'ZA', label: 'South Africa', banners: [], cards: [CARD], more: [],
      held_back: {count: 1, text: '1 held back', items: [{
        item_id: 'item_held', title: '#playyourpart relay', reason_text: 'Seen on one platform only so far',
        evidence: [{id: 'e_held_1', platform: 'tiktok', text: 'Passing the baton to @*** next', posted_at: '2026-09-29T10:00:00+02:00'}],
      }]},
      dropped: {first_morning: false, items: []},
      moments: [], boards: [], coverage: null,
      skin_note: {kept: 2, left_out: 14, text: "2 of 16 items match this skin's terms and hashtags; 14 left out"},
    },
    {
      market: 'NG', label: 'Nigeria', banners: [], cards: [], more: [],
      held_back: {count: 0, text: 'Nothing held back', items: []},
      dropped: {first_morning: false, items: []},
      moments: [], boards: [], coverage: null,
      skin_note: {kept: 0, left_out: 9, text: "0 of 9 items match this skin's terms and hashtags; 9 left out"},
    },
  ],
  skin: {skin_id: SKIN.skin_id, name: SKIN.name, markets: ['ZA', 'NG'], left_out_markets: ['KE'], text: 'Not in this skin: Kenya'},
  alerts: {alerts: [{watch_id: 'w_000000000001', market: 'ZA', item_id: 'item_ubuntu', label: 'Ubuntu stories', fired_because: 'Started rising', card: null}], waiting: []},
};
const WATCHES = {watches: [
  {watch_id: 'w_000000000001', label: 'Ubuntu stories', target: {kind: 'item', item_id: 'item_ubuntu'}, market: 'ZA', rule: {state_in: ['rising']}, status: 'active'},
  {watch_id: 'w_000000000002', label: 'Amapiano in Lagos', target: {kind: 'query', value: 'amapiano lagos'}, market: 'NG', rule: {state_in: ['rising']}, status: 'active'},
]};
const INVESTIGATIONS = {investigations: [
  {investigation_id: 'i_report01', status: 'complete', question: 'Weekly report for Brand South Africa', market: null, ask_id: 'a_report01',
    plan: {skin_id: SKIN.skin_id, sub_questions: []}, created_at: '2026-09-29T08:00:00+02:00', updated_at: '2026-09-29T09:00:00+02:00'},
  {investigation_id: 'i_other001', status: 'complete', question: 'Something else', market: 'ZA', ask_id: 'a_other001',
    plan: {sub_questions: []}, created_at: '2026-09-28T08:00:00+02:00', updated_at: '2026-09-28T09:00:00+02:00'},
]};
const DOSSIERS = {dossiers: [
  {dossier_id: 'd_report01', version: 2, state: 'frozen', title: 'Brand South Africa, week 39', created_at: '2026-09-29T10:00:00+02:00', source_ask_id: 'a_report01', frozen_version: 2},
  {dossier_id: 'd_other001', version: 1, state: 'draft', title: 'Not a skin report', created_at: '2026-09-28T10:00:00+02:00', source_ask_id: 'a_other001', frozen_version: null},
], next_before: null};
const DRAFT = {investigation_id: 'i_new_report', status: 'draft', plan: {sub_questions: []}, estimate: {credits: 300, model_usd: 1, minutes: 10}, budget_left: {credits: 900, model_usd: 5}};

function serve(routes){
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), init: init || {}});
    for (const [prefix, answer] of routes){
      if (String(url).startsWith(prefix)) return typeof answer === 'function' ? answer(String(url), init || {}) : answer;
    }
    return reply(404, {error: 'not_found', message: 'No route'});
  };
}

beforeEach(() => {
  calls = [];
  localStorage.setItem('pulse_passcode', 'test-pass');
  window.location.hash = '';
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

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const text = (scope = host) => scope.textContent.replace(/\s+/g, ' ');
const button = (scope, label) => [...scope.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));
const submit = (form) => flushSync(() => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
function typeInto(element, value){
  const proto = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, value);
  flushSync(() => element.dispatchEvent(new window.Event('input', {bubbles: true})));
}
const tick = (input) => flushSync(() => input.click());
const section = (name) => host.querySelector('[data-section="' + name + '"]');
const row = (id) => host.querySelector('[data-skin="' + id + '"]');
const posted = (path) => calls.filter((c) => c.url === path && c.init.method === 'POST');
const bodyOf = (call) => JSON.parse(call.init.body);
const field = (scope, name) => scope.querySelector('[name="' + name + '"]');
const checkbox = (scope, name, value) => scope.querySelector('input[type="checkbox"][name="' + name + '"][value="' + value + '"]');

const listRoutes = (extra = []) => [
  ...extra,
  ['/api/watches', reply(200, WATCHES)],
  ['/api/skins', (url, init) => (init.method === 'POST'
    ? reply(201, {...JSON.parse(init.body), skin_id: 'sk_new000000001', created_at: '2026-09-30T09:00:00+02:00', status_at: '2026-09-30T09:00:00+02:00', who: 'passcode', accounts: [], status: 'active'})
    : reply(200, SKINS))],
];

const pageRoutes = (extra = []) => [
  ...extra,
  ['/api/skins/sk_000000000001/today', reply(200, SKIN_TODAY)],
  ['/api/skins/sk_000000000001/report', reply(201, DRAFT)],
  ['/api/skins/sk_000000000001/archive', reply(200, {...SKIN, status: 'archived'})],
  ['/api/skins/sk_000000000001', (url, init) => (init.method === 'PUT' ? reply(200, {...SKIN, ...JSON.parse(init.body)}) : reply(200, SKIN))],
  ['/api/watches', reply(200, WATCHES)],
  ['/api/investigations', reply(200, INVESTIGATIONS)],
  ['/api/dossiers', reply(200, DOSSIERS)],
];

async function mountList(routes = listRoutes()){
  serve(routes);
  flushSync(() => root.render(<SkinsPage42 />));
  await settle();
}

async function mountPage(routes = pageRoutes(), skinId = SKIN.skin_id){
  serve(routes);
  flushSync(() => root.render(<SkinPage42 skinId={skinId} />));
  await settle();
}

/* ---------------- the calls and the routes ---------------- */

test('the skin calls build the contract paths, methods and bodies, with the passcode', async () => {
  serve([['/api/', reply(200, {})]]);
  const body = {skin_key: 'bsa', name: 'Brand South Africa', markets: ['ZA'], terms: ['ubuntu'], hashtags: ['#playyourpart'], accounts: [], watch_ids: [], template: 'weekly_report'};
  await api.listSkins();
  await api.getSkin('sk 1');
  await api.createSkin({...body, extra: 'left here'});
  await api.updateSkin('sk_1', body);
  await api.archiveSkin('sk_1');
  await api.getSkinToday('sk_1');
  await api.getSkinToday('sk_1', '2026-09-30');
  await api.startSkinReport('sk_1');
  expect(calls.map((c) => [c.init.method || 'GET', c.url])).toEqual([
    ['GET', '/api/skins'],
    ['GET', '/api/skins/sk%201'],
    ['POST', '/api/skins'],
    ['PUT', '/api/skins/sk_1'],
    ['POST', '/api/skins/sk_1/archive'],
    ['GET', '/api/skins/sk_1/today'],
    ['GET', '/api/skins/sk_1/today?date=2026-09-30'],
    ['POST', '/api/skins/sk_1/report'],
  ]);
  expect(bodyOf(calls[2])).toEqual(body);
  expect(bodyOf(calls[3])).toEqual(body);
  for (const call of calls) expect(call.init.headers['X-Passcode']).toBe('test-pass');
});

test('the routes: #/skins opens the Skins screen and #/skins/<id> one skin', () => {
  expect(resolveHostRoute('skins', '')).toBe('skins');
  expect(resolveHostRoute('skins', 'sk_000000000001')).toBe('skin');
});

/* ---------------- the Skins screen ---------------- */

test('each skin shows its name, markets, how many terms, hashtags and accounts, and its status', async () => {
  await mountList();
  expect(calls[0].url).toBe('/api/skins');
  expect(host.querySelector('h1').textContent).toBe('Skins');
  const first = row(SKIN.skin_id);
  expect(first.querySelector('a').getAttribute('href')).toBe('#/skins/sk_000000000001');
  expect(first.querySelector('a').textContent).toBe('Brand South Africa');
  expect(text(first)).toContain('South Africa and Nigeria');
  /* Demo polish, 2 October 2026: a zero count read as something broken
     ("0 accounts"), so a count is written only when the skin holds one.
     The whole line is now matched exactly, which is stricter than the
     earlier fragment match, and a zero is checked to be absent. */
  expect(text(first.querySelector('.a42-meta'))).toBe('South Africa and Nigeria · 2 terms · 1 hashtag');
  expect(text(first)).not.toMatch(/\b0 /);
  expect(text(first)).toContain('Active');
  const second = row(ARCHIVED.skin_id);
  expect(text(second)).toContain('Kenya');
  expect(text(second.querySelector('.a42-meta'))).toBe('Kenya · 1 term · 1 account');
  expect(text(second)).not.toMatch(/\b0 /);
  expect(text(second)).toContain('Archived');
});

test('a skin that holds no terms, hashtags or accounts shows its markets and no row of zeros', async () => {
  await mountList(listRoutes([['/api/skins', reply(200, {skins: [{...SKIN, terms: [], hashtags: [], accounts: []}]})]]));
  expect(text(row(SKIN.skin_id).querySelector('.a42-meta'))).toBe('South Africa and Nigeria · Nothing added yet');
});

test('the Skins screen says what a skin is in plain words and puts New skin beside the heading', async () => {
  await mountList();
  const head = host.querySelector('.t42-head');
  expect(text(head.querySelector('.t42-status'))).toBe("A skin is a client's own view of 42: their markets, terms, hashtags, approved accounts and watches, with a weekly report.");
  expect(text(head)).not.toMatch(/lens|engine/i);
  expect(button(head, 'New skin')).not.toBeUndefined();
  /* Restated for the polish pass: with the form closed, New skin is the one
     primary action on the screen, styled like Watch and Schedule. */
  expect([...host.querySelectorAll('.w42-primary')].map((b) => b.textContent.trim())).toEqual(['New skin']);
});

test('no skins says so, a failed read offers Try again, and a 401 hands over to the passcode screen', async () => {
  await mountList([['/api/skins', reply(200, {skins: []})]]);
  expect(text()).toContain('No skins yet.');
  expect(text()).toContain('No skins yet. Choose New skin to set up a client\'s markets, terms and accounts.');
  flushSync(() => root.unmount());
  root = createRoot(host);
  let n = 0;
  await mountList([['/api/watches', reply(200, WATCHES)], ['/api/skins', () => (++n === 1 ? reply(500, {error: 'internal', message: 'Broken'}) : reply(200, SKINS))]]);
  expect(section('skins').querySelector('[role="alert"]').textContent).toBe('The skins could not load.');
  click(button(host, 'Try again'));
  await settle();
  expect(row(SKIN.skin_id)).not.toBeNull();
  flushSync(() => root.unmount());
  root = createRoot(host);
  let asked = 0;
  serve([['/api/skins', reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})]]);
  flushSync(() => root.render(<SkinsPage42 onAuth={() => { asked += 1; }} />));
  await settle();
  expect(asked).toBe(1);
});

test('New skin opens the form: key, name, markets, terms and hashtags one per line, watches and the weekly report', async () => {
  await mountList();
  expect(host.querySelector('[data-section="skin-form"]')).toBeNull();
  click(button(host, 'New skin'));
  await settle();
  const form = host.querySelector('[data-section="skin-form"] form');
  expect(form).not.toBeNull();
  expect(field(form, 'skin_key')).not.toBeNull();
  expect(field(form, 'name')).not.toBeNull();
  expect(field(form, 'terms').tagName).toBe('TEXTAREA');
  expect(field(form, 'hashtags').tagName).toBe('TEXTAREA');
  expect([...form.querySelectorAll('input[name="markets"]')].map((i) => i.value)).toEqual(['ZA', 'NG', 'KE']);
  expect([...form.querySelectorAll('input[name="watch_ids"]')].map((i) => i.value)).toEqual(['w_000000000001', 'w_000000000002']);
  expect([...form.querySelectorAll('select[name="template"] option')].map((o) => [o.value, o.textContent])).toEqual([['weekly_report', 'Weekly report']]);
});

test('the approved list is empty today, so the form says so and offers no way to type an account', async () => {
  await mountList();
  click(button(host, 'New skin'));
  await settle();
  const accounts = host.querySelector('[data-section="skin-form"] [data-part="accounts"]');
  expect(text(accounts)).toContain('No approved accounts yet. Accounts come from the approved list only.');
  expect(accounts.querySelector('input, textarea, select')).toBeNull();
  const form = host.querySelector('[data-section="skin-form"] form');
  expect(form.querySelector('[name="accounts"], [name="handle"], [name="account"]')).toBeNull();
});

test('Save posts the skin in the contract shape, one term and hashtag per line, and opens its page', async () => {
  await mountList();
  click(button(host, 'New skin'));
  await settle();
  const form = host.querySelector('[data-section="skin-form"] form');
  typeInto(field(form, 'skin_key'), 'bsa');
  typeInto(field(form, 'name'), 'Brand South Africa');
  tick(checkbox(form, 'markets', 'NG'));
  typeInto(field(form, 'terms'), 'proudly south african\n\n  ubuntu  \n');
  typeInto(field(form, 'hashtags'), '#playyourpart');
  tick(checkbox(form, 'watch_ids', 'w_000000000001'));
  submit(form);
  await settle();
  const saved = posted('/api/skins');
  expect(saved).toHaveLength(1);
  expect(bodyOf(saved[0])).toEqual({
    skin_key: 'bsa', name: 'Brand South Africa', markets: ['ZA', 'NG'], terms: ['proudly south african', 'ubuntu'],
    hashtags: ['#playyourpart'], accounts: [], watch_ids: ['w_000000000001'], template: 'weekly_report',
  });
  expect(window.location.hash).toBe('#/skins/sk_new000000001');
});

test('a refused save shows the server words as they come', async () => {
  for (const [status, message] of [
    [400, 'Not accepted (rule 1, rule 2 or mixed script)'],
    [400, 'Only approved organisation accounts'],
    [503, 'The word check is not available yet'],
  ]){
    flushSync(() => root.unmount());
    root = createRoot(host);
    calls = [];
    await mountList(listRoutes([['/api/skins', (url, init) => (init.method === 'POST' ? reply(status, {error: 'bad_request', message}) : reply(200, SKINS))]]));
    click(button(host, 'New skin'));
    await settle();
    const form = host.querySelector('[data-section="skin-form"] form');
    typeInto(field(form, 'skin_key'), 'bsa');
    typeInto(field(form, 'name'), 'Brand South Africa');
    submit(form);
    await settle();
    expect(form.querySelector('[role="alert"]').textContent).toBe(message);
    expect(window.location.hash).toBe('');
  }
});

test('the Skins screen has one red action, the save, and names no age group and no Google Trends', async () => {
  await mountList();
  /* Restated for the polish pass: New skin is the red action until the form
     opens; then it steps aside and the save is the only red action. */
  expect([...host.querySelectorAll('.w42-primary')].map((b) => b.textContent.trim())).toEqual(['New skin']);
  click(button(host, 'New skin'));
  await settle();
  expect([...host.querySelectorAll('.w42-primary')].map((b) => b.textContent.trim())).toEqual(['Save skin']);
  expect(text()).not.toMatch(/gen ?z|millennial|youth|generation|google trends/i);
});

/* ---------------- one skin ---------------- */

test('the skin page names the skin, its markets and what it leaves out', async () => {
  await mountPage();
  expect(host.querySelector('h1').textContent).toBe('Brand South Africa');
  expect(text()).toContain('South Africa and Nigeria');
  expect(text()).toContain('Not in this skin: Kenya');
  expect(host.querySelector('a[href="#/skins"]')).not.toBeNull();
});

test('the narrowed Today shows the cards through the trend card, and each market says what was kept and left out', async () => {
  await mountPage();
  expect(calls.some((c) => c.url === '/api/skins/sk_000000000001/today')).toBe(true);
  const today = section('skin-today');
  const za = today.querySelector('[data-market="ZA"]');
  expect(za.querySelectorAll('[data-card]')).toHaveLength(1);
  expect(text(za)).toContain('Ubuntu stories, first told by @***');
  expect(text(za)).toContain('31 creators in 3 days');
  expect(text(za)).toContain("2 of 16 items match this skin's terms and hashtags; 14 left out");
  const ng = today.querySelector('[data-market="NG"]');
  expect(text(ng)).toContain("0 of 9 items match this skin's terms and hashtags; 9 left out");
  expect(text(ng)).toContain('No trends in this skin passed the checks for Nigeria today.');
});

test('a skin Today from an earlier day names its date and says it is not today, and the current day does not', async () => {
  const nativeNow = Date.now;
  Date.now = () => Date.parse('2026-10-05T10:00:00+02:00');
  try {
    await mountPage(pageRoutes([['/api/skins/sk_000000000001/today', reply(200, {...SKIN_TODAY, date: '2026-10-04', heading: 'Today, 4 October 2026'})]]));
    const earlier = section('skin-today').querySelector('[data-skin-today-earlier]');
    expect(earlier).not.toBeNull();
    expect(text(earlier)).toBe('Earlier brief: 4 October 2026. This is not today’s brief.');
    expect(earlier.getAttribute('role')).toBe('status');
    flushSync(() => root.unmount());
    root = createRoot(host);
    await mountPage(pageRoutes([['/api/skins/sk_000000000001/today', reply(200, {...SKIN_TODAY, date: '2026-10-05', heading: 'Today, 5 October 2026'})]]));
    expect(section('skin-today').querySelector('[data-skin-today-earlier]')).toBeNull();
    expect(text(section('skin-today'))).not.toContain('Earlier brief');
  } finally {
    Date.now = nativeNow;
  }
});

test('held-back items are shown with their reasons, never dropped', async () => {
  await mountPage();
  const held = section('skin-today').querySelector('[data-market="ZA"] [data-part="held-back"]');
  expect(text(held)).toContain('1 held back');
  expect(text(held)).toContain('#playyourpart relay');
  expect(text(held)).toContain('Seen on one platform only so far');
});

test('masked handles stay masks in plain text, never a link', async () => {
  await mountPage();
  expect(text()).toContain('first told by @***');
  expect(text()).toContain('Passing the baton to @*** next');
  // A mask may ride inside 42's own links (Ask about this carries the card title as it is), never out to a post or profile.
  for (const a of host.querySelectorAll('a')){
    if (String(a.getAttribute('href')).includes('***')) expect(a.getAttribute('href').startsWith('#/')).toBe(true);
  }
  expect([...host.querySelectorAll('a')].some((a) => a.textContent.includes('@***'))).toBe(false);
});

test("the skin's watches and today's alerts show, with no other watch", async () => {
  await mountPage();
  const watches = section('skin-watches');
  expect(text(watches)).toContain('Ubuntu stories');
  expect(text(watches)).toContain('When it starts rising');
  expect(text(watches)).not.toContain('Amapiano in Lagos');
  expect(text(watches)).toContain('1 alert today');
  expect(text(watches)).toContain('Started rising');
  const link = [...watches.querySelectorAll('a')].find((a) => a.getAttribute('href') === '#/t/item_ubuntu?market=ZA');
  expect(link).toBeDefined();
});

test('past reports are the dossiers whose investigation carries this skin', async () => {
  await mountPage();
  const reports = section('skin-reports');
  const links = [...reports.querySelectorAll('a')];
  expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([['Brand South Africa, week 39', '#/dossiers/d_report01']]);
  expect(text(reports)).toContain('Frozen');
  expect(text(reports)).not.toContain('Not a skin report');
});

test('no report yet says so', async () => {
  await mountPage(pageRoutes([['/api/dossiers', reply(200, {dossiers: [], next_before: null})]]));
  expect(text(section('skin-reports'))).toContain('No weekly report has become a dossier yet.');
});

test('Build weekly report is the one red action; it posts the report route and opens the investigation plan', async () => {
  await mountPage();
  expect([...host.querySelectorAll('.w42-primary')].map((b) => b.textContent.trim())).toEqual(['Build weekly report']);
  click(button(host, 'Build weekly report'));
  await settle();
  expect(posted('/api/skins/sk_000000000001/report')).toHaveLength(1);
  expect(window.location.hash).toBe('#/investigations/i_new_report');
});

test('a refused report shows the server words and stays on the page', async () => {
  const message = "Today's model budget is spent; try tomorrow or ask Albert to raise MODEL_DAILY_USD";
  await mountPage(pageRoutes([['/api/skins/sk_000000000001/report', reply(429, {error: 'rate_limited', message})]]));
  click(button(host, 'Build weekly report'));
  await settle();
  expect(host.querySelector('[data-part="report-error"]').textContent).toBe(message);
  expect(window.location.hash).toBe('');
});

test('Archive is quiet and asks first; Cancel takes first focus and sends nothing; the confirm appends an archived row', async () => {
  await mountPage();
  const archive = button(host, 'Archive');
  expect(archive.className).not.toContain('w42-primary');
  click(archive);
  const confirm = host.querySelector('[data-part="archive-confirm"]');
  expect(confirm).not.toBeNull();
  expect(text(confirm)).toContain('Nothing is deleted');
  expect(document.activeElement && document.activeElement.textContent).toBe('Cancel');
  click(button(confirm, 'Cancel'));
  expect(host.querySelector('[data-part="archive-confirm"]')).toBeNull();
  expect(posted('/api/skins/sk_000000000001/archive')).toHaveLength(0);
  click(button(host, 'Archive'));
  click(button(host.querySelector('[data-part="archive-confirm"]'), 'Archive this skin'));
  await settle();
  expect(posted('/api/skins/sk_000000000001/archive')).toHaveLength(1);
  expect(text(host.querySelector('header'))).toContain('Archived');
  expect(host.querySelectorAll('.w42-primary')).toHaveLength(0);
});

test('Edit opens the same form filled in, keeps the approved accounts the skin holds, and saves with PUT', async () => {
  const held = {...SKIN, accounts: [{platform: 'x', handle: '@brandsouthafrica', org: 'Brand South Africa', role: 'client'}]};
  await mountPage(pageRoutes([['/api/skins/sk_000000000001/today', reply(200, SKIN_TODAY)], ['/api/skins/sk_000000000001', (url, init) => (init.method === 'PUT' ? reply(200, {...held, ...JSON.parse(init.body)}) : reply(200, held))]]));
  click(button(host, 'Edit'));
  await settle();
  const form = host.querySelector('[data-section="skin-form"] form');
  expect(field(form, 'name').value).toBe('Brand South Africa');
  expect(field(form, 'terms').value).toBe('proudly south african\nubuntu');
  expect(checkbox(form, 'markets', 'NG').checked).toBe(true);
  expect(checkbox(form, 'watch_ids', 'w_000000000001').checked).toBe(true);
  const account = form.querySelector('input[type="checkbox"][name="account"]');
  expect(account.checked).toBe(true);
  expect(text(form.querySelector('[data-part="accounts"]'))).toContain('@brandsouthafrica');
  typeInto(field(form, 'name'), 'Brand South Africa lens');
  submit(form);
  await settle();
  const put = calls.filter((c) => c.url === '/api/skins/sk_000000000001' && c.init.method === 'PUT');
  expect(put).toHaveLength(1);
  expect(bodyOf(put[0])).toEqual({
    skin_key: 'bsa', name: 'Brand South Africa lens', markets: ['ZA', 'NG'], terms: ['proudly south african', 'ubuntu'], hashtags: ['#playyourpart'],
    accounts: [{platform: 'x', handle: '@brandsouthafrica'}], watch_ids: ['w_000000000001'], template: 'weekly_report',
  });
  expect(host.querySelector('h1').textContent).toBe('Brand South Africa lens');
});

test("Today not published yet says so on the skin page and the rest still shows", async () => {
  await mountPage(pageRoutes([['/api/skins/sk_000000000001/today', reply(409, {error: 'not_ready', message: 'Today is not published yet for the latest date.'})]]));
  expect(text(section('skin-today'))).toContain('Today is not published yet for the latest date.');
  expect(host.querySelector('h1').textContent).toBe('Brand South Africa');
  expect(button(host, 'Build weekly report')).toBeDefined();
});

/* ---------------- masked evidence in a skin report ---------------- */

test('report evidence with no handle and no url opens in 42 own source panel by its id, with no name made up', async () => {
  const {EvidenceChip} = await import('../EvidenceChip.jsx');
  const {SourcePanel} = await import('../SourcePanel.jsx');
  const {PostStrip} = await import('../PostStrip.jsx');
  const masked = {id: 'e_masked_1', platform: 'tiktok', text: 'Big up @*** for the relay', posted_at: '2026-09-29T10:00:00+02:00'};
  let pinned = null;
  flushSync(() => root.render(
    <div>
      <EvidenceChip evidence={masked} quotes={[]} pinned={false} onPin={(id) => { pinned = id; }} />
      <SourcePanel evidence={masked} quotes={[]} onClose={() => {}} />
      <PostStrip evidence={[masked]} />
    </div>,
  ));
  const chip = host.querySelector('.ask42-chip');
  expect(chip.getAttribute('aria-label')).toBe('Source: TikTok post, 29 September 2026');
  click(chip);
  expect(pinned).toBe('e_masked_1');
  expect(host.querySelector('.ask42-source-title').textContent).toBe('TikTok post');
  expect(text()).toContain('Big up @*** for the relay');
  expect(text()).toContain('No link to this post');
  expect(text()).not.toMatch(/undefined|null/);
  expect(host.querySelectorAll('a')).toHaveLength(0);
});

/* UI polish, 2 October 2026: restated. A failed still now turns the post into
   a text row with its link, duration and line, not a blank labelled frame. */
test('PostStrip turns a failed thumbnail into a text row and loads a changed url', async () => {
  const {PostStrip} = await import('../PostStrip.jsx');
  const post = {
    id: 'e_thumbnail_1', platform: 'tiktok', handle: '@fixture_creator',
    thumbnail_url: 'https://example.invalid/thumb/failed.jpg', url: 'https://tiktok.com/@fixture_creator/video/1',
    duration_s: 68, transcript_span: {start_s: 4, text: 'The relay keeps moving'},
  };
  const render = () => flushSync(() => root.render(<PostStrip evidence={[post]} />));
  render();
  expect(host.querySelectorAll('.ask42-post-frame img')).toHaveLength(1);
  const image = host.querySelector('.ask42-post-frame img');
  expect(image.getAttribute('src')).toBe('https://example.invalid/thumb/failed.jpg');
  flushSync(() => image.dispatchEvent(new window.Event('error')));
  expect(host.querySelectorAll('.ask42-post-frame')).toHaveLength(0);
  const link = host.querySelector('[data-post-row] a.ask42-post-open');
  expect(link.getAttribute('aria-label')).toBe('Open the TikTok post by @fixture_creator');
  expect(link.getAttribute('href')).toBe('https://tiktok.com/@fixture_creator/video/1');
  expect(text()).not.toContain('Still unavailable');
  expect(text()).toContain('1:08');
  expect(text()).toContain('The relay keeps moving');
  render();
  expect(host.querySelectorAll('.ask42-post-frame img')).toHaveLength(0);
  post.thumbnail_url = 'https://example.invalid/thumb/recovered.jpg';
  render();
  expect(host.querySelector('.ask42-post-frame img').getAttribute('src')).toBe('https://example.invalid/thumb/recovered.jpg');
  expect(host.querySelectorAll('[data-post-row]')).toHaveLength(0);
  expect(host.querySelectorAll('.ask42-post-blank')).toHaveLength(0);
});

/* UI polish, 2 October 2026: a post with no usable still used to keep a tall
   9:16 frame with two centred words. It is now a text row with the same
   facts, its creator the link, and never an empty frame. */
test('PostStrip shows a post without a still as a text row, never an empty frame', async () => {
  const {PostStrip} = await import('../PostStrip.jsx');
  const bare = (id, extra = {}) => ({id, platform: 'tiktok', handle: '@' + id, thumbnail_url: null, url: 'https://tiktok.com/@' + id + '/video/1', ...extra});
  flushSync(() => root.render(<PostStrip evidence={[bare('a'), bare('b')]} />));
  expect(host.querySelectorAll('.ask42-post-frame')).toHaveLength(0);
  const rows = [...host.querySelectorAll('[data-post-row]')];
  expect(rows).toHaveLength(2);
  expect(rows[0].querySelector('a.ask42-post-open').getAttribute('href')).toBe('https://tiktok.com/@a/video/1');
  const withStill = bare('c', {thumbnail_url: 'https://example.invalid/thumb/c.jpg'});
  flushSync(() => root.render(<PostStrip evidence={[bare('a'), withStill]} />));
  expect(host.querySelectorAll('.ask42-post-frame')).toHaveLength(1);
  expect(host.querySelectorAll('[data-post-row]')).toHaveLength(1);
  flushSync(() => host.querySelector('.ask42-post-frame img').dispatchEvent(new window.Event('error')));
  expect(host.querySelectorAll('.ask42-post-frame')).toHaveLength(0);
  expect(host.querySelectorAll('[data-post-row]')).toHaveLength(2);
});

test('SourcePanel replaces a failed still with an accessible tile and recovers for a changed url', async () => {
  const {SourcePanel} = await import('../SourcePanel.jsx');
  const evidence = {
    id: 'e_source_thumbnail_1', platform: 'tiktok', handle: '@fixture_creator',
    thumbnail_url: 'https://example.invalid/thumb/failed-source.jpg', url: 'https://tiktok.com/@fixture_creator/video/1',
    text: 'The relay keeps moving together.', transcript_span: {start_s: 4, text: 'We all move together'},
  };
  let closeCount = 0;
  const render = () => flushSync(() => root.render(
    <SourcePanel evidence={evidence} quotes={['moving together']} onClose={() => { closeCount++; }} />,
  ));
  render();
  await settle();
  expect(document.activeElement.className).toBe('ask42-source-title');
  expect(host.querySelectorAll('.ask42-source-thumb[src]')).toHaveLength(1);
  const image = host.querySelector('.ask42-source-thumb');
  expect(image.getAttribute('src')).toBe('https://example.invalid/thumb/failed-source.jpg');
  flushSync(() => image.dispatchEvent(new window.Event('error')));
  expect(host.querySelectorAll('.ask42-source-thumb[src]')).toHaveLength(0);
  expect(host.querySelector('.ask42-source-thumb-empty').getAttribute('aria-label')).toBe('Still unavailable for TikTok post by @fixture_creator');
  expect(host.querySelector('.ask42-source-thumb-empty').className).toBe('ask42-source-thumb ask42-source-thumb-empty');
  expect(host.querySelector('mark').textContent).toBe('moving together');
  expect(text()).toContain('We all move together');
  expect(host.querySelector('.ask42-source-actions a').getAttribute('href')).toBe('https://tiktok.com/@fixture_creator/video/1');
  expect(document.activeElement.className).toBe('ask42-source-title');
  render();
  expect(host.querySelectorAll('.ask42-source-thumb[src]')).toHaveLength(0);
  expect(host.querySelectorAll('.ask42-source-thumb-empty')).toHaveLength(1);
  evidence.thumbnail_url = 'https://example.invalid/thumb/recovered-source.jpg';
  render();
  const recovered = host.querySelector('.ask42-source-thumb');
  expect(host.querySelectorAll('.ask42-source-thumb[src]')).toHaveLength(1);
  expect(recovered.tagName).toBe('IMG');
  expect(recovered.getAttribute('src')).toBe('https://example.invalid/thumb/recovered-source.jpg');
  click(host.querySelector('.ask42-source-actions button'));
  expect(closeCount).toBe(1);
});

test('an unknown skin says so in the server words', async () => {
  await mountPage([['/api/skins/', reply(404, {error: 'not_found', message: 'No skin sk_missing is held here.'})]], 'sk_missing');
  expect(host.querySelector('h1').textContent).toBe('Skin not found');
  expect(text()).toContain('No skin sk_missing is held here.');
});
