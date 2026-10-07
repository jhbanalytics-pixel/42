/* The topic page on the 42 API (contract.md section 10.3): the story of one
   thing. The fixture is one ZA item with three platform series (one with a
   gap, one never usable), spread with tiers, one earlier wave, two share
   signals and a post whose link and image are not safe to use. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import React from 'react';
import topicFixture from './fixtures/topic42_za.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TopicPage42} = await import('../../topic42.jsx');

const ITEM = topicFixture.card.item_id;
const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});

function serve(answer){
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), init});
    return typeof answer === 'function' ? answer(url) : answer;
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

async function mount(body = topicFixture, props = {}){
  serve(reply(200, body));
  flushSync(() => root.render(<TopicPage42 itemId={ITEM} market="ZA" {...props} />));
  await settle();
}

const text = () => host.textContent.replace(/\s+/g, ' ');
const section = (name) => host.querySelector('[data-section="' + name + '"]');
const button = (label) => [...host.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));

test('reads the topic route with the passcode, market in the query', async () => {
  await mount();
  expect(calls).toHaveLength(1);
  expect(calls[0].url).toBe('/api/topics/' + encodeURIComponent(ITEM) + '?market=ZA');
  expect(calls[0].init.headers['X-Passcode']).toBe('test-pass');
});

test('shows a loading line while the topic is on its way', async () => {
  let release;
  serve(new Promise((resolve) => { release = resolve; }));
  flushSync(() => root.render(<TopicPage42 itemId={ITEM} market="ZA" />));
  expect(host.querySelector('[aria-busy="true"]')).not.toBeNull();
  expect(text()).toContain('Loading the topic');
  release(reply(200, topicFixture));
  await settle();
  expect(host.querySelector('[aria-busy="true"]')).toBeNull();
});

test('the card leads the page with its title, state word, flag word and explanation', async () => {
  await mount();
  const card = section('card');
  expect(card).not.toBeNull();
  expect(card.textContent).toContain('#fixture_za_step');
  expect(card.textContent).toContain('Emerging');
  expect(card.textContent).toContain('Check pattern');
  expect(card.textContent).toContain(topicFixture.card.explanation);
});

test('unmatched numeric thumbnail references leave no empty box', async () => {
  const topic = clone(topicFixture);
  topic.card.thumbnails = ['tt_za_006', 90210];
  await mount(topic);

  const knownThumbnail = host.querySelector('.t42-thumbs img');
  expect(knownThumbnail.getAttribute('alt')).toBe('Post by @fixture_za_6 on TikTok');
  /* UI polish, 2 October 2026: restated. An unmatched reference used to leave a
     labelled empty box; it now leaves nothing, so only the known still shows. */
  expect(host.querySelectorAll('.t42-thumbs [role="img"]')).toHaveLength(0);
  expect(host.querySelectorAll('.t42-thumbs img')).toHaveLength(1);
});

test('unmatched opaque thumbnail references leave no empty box', async () => {
  const topic = clone(topicFixture);
  topic.card.thumbnails = ['tt_za_006', 'sha256:opaque_thumbnail_fixture'];
  await mount(topic);

  const knownThumbnail = host.querySelector('.t42-thumbs img');
  expect(knownThumbnail.getAttribute('alt')).toBe('Post by @fixture_za_6 on TikTok');
  /* UI polish, 2 October 2026: restated. An unmatched reference used to leave a
     labelled empty box; it now leaves nothing, so only the known still shows. */
  expect(host.querySelectorAll('.t42-thumbs [role="img"]')).toHaveLength(0);
  expect(host.querySelectorAll('.t42-thumbs img')).toHaveLength(1);
});

test('the history of states is a quiet timeline in date order', async () => {
  await mount();
  const rows = [...section('history').querySelectorAll('li')].map((li) => li.textContent.replace(/\s+/g, ' ').trim());
  expect(rows).toEqual(['27 September 2026 · New to 42', '28 September 2026 · Spike', '29 September 2026 · Emerging']);
  // UI notes, 2 Oct 2026: plain section titles.
  expect(section('history').textContent).toContain('How it developed');
  expect(section('series').textContent).toContain('Daily posts by platform');
});

test('daily chart buttons name one post in the singular and keep unknown days unavailable', async () => {
  const topic = clone(topicFixture);
  topic.series[0].points = [0, 1, 2, null, undefined].map((value, index) => ({date: '2026-10-0' + (index + 1), value}));
  topic.series = [topic.series[0]];
  await mount(topic);
  const days = [...section('series').querySelectorAll('.tp42-day')];
  expect(days.map((day) => day.dataset.day)).toEqual(['2026-10-01', '2026-10-02', '2026-10-03']);
  expect(days.map((day) => day.getAttribute('aria-label'))).toEqual([
    '1 October 2026, 0 posts a day. Ask why this day jumped',
    '2 October 2026, 1 post a day. Ask why this day jumped',
    '3 October 2026, 2 posts a day. Ask why this day jumped',
  ]);
});

test('one sparkline per platform series, labelled in words, with gaps kept as breaks', async () => {
  await mount();
  const series = section('series');
  const svgs = [...series.querySelectorAll('svg')];
  expect(svgs).toHaveLength(2);
  expect(series.textContent).toContain('TikTok local feed');
  expect(series.textContent).toContain('X hub accounts');
  const tiktok = svgs[0];
  expect(tiktok.getAttribute('aria-label')).toContain('with days missing');
  expect(tiktok.querySelector('.tp42-line').getAttribute('d').match(/M/g)).toHaveLength(2);
  // UI notes, 2 Oct 2026: a series with no usable day is left out instead of saying so.
  expect(series.textContent).not.toContain('Instagram location posts');
  expect(series.textContent).not.toContain('No usable days yet');
  expect(series.textContent).not.toMatch(/\b0 posts/);
});

test('spread names platforms and markets with first seen dates and counts creators by size as Figures', async () => {
  await mount();
  const spread = section('spread');
  expect(spread.textContent).toContain(topicFixture.card.spread_line);
  expect(spread.textContent).toContain('TikTok, first seen 12 September 2026');
  expect(spread.textContent).toContain('X, first seen 18 September 2026');
  /* A market is where the posts were collected, never where people live. */
  expect(spread.textContent).toContain('Seen in South Africa feeds from 12 September 2026');
  expect(spread.textContent).toContain('Seen in Nigeria feeds from 20 September 2026');
  const tiers = [...spread.querySelectorAll('[data-query-id]')];
  expect(tiers.map((t) => t.getAttribute('data-query-id'))).toEqual(['q_tier_nano', 'q_tier_micro', 'q_tier_mid', 'q_tier_macro', 'q_tier_mega']);
  expect(tiers.map((t) => t.textContent)).toEqual(['20', '12', '4', '2', '0']);
  expect(spread.textContent).toContain('Nano');
  expect(spread.textContent).toContain('Mega');
});

test('spread tiers counted in posts are labelled as posts, never as creators', async () => {
  const topic = clone(topicFixture);
  for (const f of Object.values(topic.spread.tiers)) f.unit = 'posts first seen in 7 days';
  topic.spread.markets = null;
  await mount(topic);
  const spread = section('spread');
  expect(spread.textContent).toContain('Posts by creator size, first seen in 7 days:');
  expect(spread.textContent).not.toContain('Creators by size');
  expect(spread.textContent).toContain('TikTok, first seen 12 September 2026');
  expect([...spread.querySelectorAll('[data-query-id]')].map((t) => t.textContent)).toEqual(['20', '12', '4', '2', '0']);
});

test('spread markets show their posts as a Figure and a market with no first day says only where', async () => {
  const topic = clone(topicFixture);
  const posts = (value) => ({value, unit: 'posts first seen in 7 days', query_id: 'q_item_spread', run_id: 'r_detect_20260930_01', result_hash: 'sha256:x'});
  topic.spread.markets = [
    {market: 'ZA', first_seen: '2026-09-12', posts: posts(6)},
    {market: 'KE', first_seen: null, posts: null},
  ];
  await mount(topic);
  const rows = [...section('spread').querySelectorAll('[data-market]')].map((li) => li.textContent.replace(/\s+/g, ' ').trim());
  expect(rows).toEqual(['Seen in South Africa feeds from 12 September 2026, 6 posts first seen in 7 days', 'Seen in Kenya feeds']);
  expect(section('spread').querySelector('[data-market="ZA"] [data-query-id="q_item_spread"]').textContent).toBe('6');
  expect(section('spread').textContent).not.toMatch(/South African|Kenyan|Nigerian/);
});

const ORIGIN = {origin: null, market: 'ZA', first_measured: '2026-09-24', after_collection_began: true,
  first_state_day: '2026-09-27', lead_news_day: null, lag_days: null};
const NEWS = [
  {seed_date: '2026-09-26', news_day: '2026-09-25', news_day_from: 'gdelt', title: 'Heritage Day braai', matched_by: 'seed',
    collect_ran: true, reached_state: true, first_state_day: '2026-09-27', first_measured: '2026-09-28', lag_days: 3},
  {seed_date: '2026-09-22', news_day: null, news_day_from: null, title: 'load shedding', matched_by: 'hashtag_key',
    collect_ran: false, reached_state: false, first_state_day: null, first_measured: null, lag_days: null},
];

test('where it started gives the first measured day in the market feeds and when 42 flagged it', async () => {
  const topic = clone(topicFixture);
  topic.origin = ORIGIN;
  await mount(topic);
  const origin = section('origin');
  expect(origin.querySelector('h2').textContent).toBe('Where it started');
  expect(origin.textContent).toContain('First seen in South Africa feeds on 24 September 2026');
  expect(origin.textContent).toContain('42 was already collecting there before then');
  expect(origin.textContent).toContain('First flagged by 42 on 27 September 2026');
  expect(origin.textContent).toContain('Whether it started in the news or on social is not known yet');
});

test('an origin 42 may have missed says it may be older, and a news-led origin gives the news day and lag', async () => {
  const topic = clone(topicFixture);
  topic.origin = {...ORIGIN, after_collection_began: false, origin: 'news_led', lead_news_day: '2026-09-23', lag_days: 1};
  await mount(topic);
  const origin = section('origin');
  expect(origin.textContent).toContain('It may be older');
  expect(origin.textContent).toContain('Started in the news on 23 September 2026, reaching social 1 day later');
});

test('a native origin says it started on social', async () => {
  const topic = clone(topicFixture);
  topic.origin = {...ORIGIN, origin: 'native'};
  await mount(topic);
  expect(section('origin').textContent).toContain('Started on social');
});

test('origin that is not measured says so instead of hiding', async () => {
  await mount();
  expect(section('origin').textContent).toContain('Where it started is not measured yet');
});

test('news stories list each title with its dates and the lag only when 42 has it', async () => {
  const topic = clone(topicFixture);
  topic.news = NEWS;
  await mount(topic);
  const news = section('news');
  expect(news.querySelector('h2').textContent).toBe('From the news');
  const rows = [...news.querySelectorAll('li')].map((li) => li.textContent.replace(/\s+/g, ' ').trim());
  expect(rows).toHaveLength(2);
  expect(rows[0]).toContain('Heritage Day braai');
  expect(rows[0]).toContain('In the news 25 September 2026');
  expect(rows[0]).toContain('On social from 28 September 2026');
  expect(rows[0]).toContain('reached social 3 days after the news');
  expect(rows[0]).toContain('flagged by 42 on 27 September 2026');
  expect(rows[1]).toContain('load shedding');
  expect(rows[1]).toContain('42 searched for it from 22 September 2026');
  expect(rows[1]).not.toMatch(/days after|In the news|On social/);
  expect(news.textContent).not.toMatch(/hashtag_key|seed|gdelt/);
});

test('news with no linked story says so instead of hiding', async () => {
  await mount();
  expect(section('news').textContent).toContain('No news story is linked to this topic yet');
});

test('origin, news and markets name no person', async () => {
  const topic = clone(topicFixture);
  topic.origin = ORIGIN;
  topic.news = NEWS;
  await mount(topic);
  for (const name of ['origin', 'news', 'spread']){
    expect(section(name).textContent).not.toContain('@');
  }
});

test('spread that is not measured yet says so instead of hiding', async () => {
  const topic = clone(topicFixture);
  topic.spread = null;
  topic.card.spread_line = null;
  await mount(topic);
  expect(section('spread').textContent).toContain('Spread is not measured yet');
});

test('earlier waves show their peak date and peak posts as a Figure', async () => {
  await mount();
  const waves = section('waves');
  expect(waves.textContent).toContain('14 March 2026');
  const figure = waves.querySelector('[data-query-id="q_wave_za_a"]');
  expect(figure.textContent).toBe('412');
  expect(waves.textContent).toContain('posts on the peak day');
});

test('no earlier waves is said plainly', async () => {
  const topic = clone(topicFixture);
  topic.waves = [];
  await mount(topic);
  expect(section('waves').textContent).toContain('No earlier waves');
});

test('parts whose engine views are not there yet say so, and a signal with no share shows its words alone', async () => {
  const topic = clone(topicFixture);
  topic.waves = null;
  topic.series = null;
  topic.evidence = null;
  /* Demo polish, 2 October 2026 (QA item 13): the page now falls back to the
     card's own stored posts, so this case clears those too to keep testing
     the message for a topic with no stored posts at all. */
  topic.card.evidence = [];
  topic.authenticity.signals = [{signal: 'burst', words: 'Many posts in the busiest 10 minutes', share: null}];
  await mount(topic);
  expect(section('waves').textContent).toContain('Earlier waves are not measured yet');
  expect(section('series').textContent).toContain('Platform series are not measured yet');
  expect(section('evidence').textContent).toContain('Posts for this topic are not stored yet');
  expect([...section('authenticity').querySelectorAll('li')].map((li) => li.textContent)).toEqual(['Many posts in the busiest 10 minutes']);
});

test('authenticity shows the flag word and each signal in words with its share', async () => {
  await mount();
  const auth = section('authenticity');
  // UI notes, 2 Oct 2026: the server's "Check pattern" reads as an unusual posting pattern here.
  expect(auth.textContent).toContain('Unusual posting pattern');
  expect(auth.textContent).not.toContain('Check pattern');
  expect(auth.textContent).toContain('Accounts under 30 days old: 42%');
  expect(auth.textContent).toContain('Near-duplicate posts: 31%');
  expect(auth.querySelector('[data-query-id="q_young_za_a"]')).not.toBeNull();
});

test('the account-age signal is always worded "Accounts under 30 days old"', async () => {
  expect(topicFixture.authenticity.signals[0]).toMatchObject({signal: 'young_accounts', words: 'Accounts under 30 days old'});
  for (const words of ['Young accounts', null]){
    const topic = clone(topicFixture);
    topic.authenticity.signals[0].words = words;
    flushSync(() => root.unmount());
    root = createRoot(host);
    await mount(topic);
    const first = section('authenticity').querySelector('li');
    expect(first.textContent).toBe('Accounts under 30 days old: 42%');
    expect(text()).not.toMatch(/young/i);
  }
});

test('no signals reads as nothing unusual rather than an empty box', async () => {
  const topic = clone(topicFixture);
  topic.authenticity = {flag: null, flag_word: null, signals: []};
  await mount(topic);
  expect(section('authenticity').textContent).toContain('No unusual patterns in the posts 42 read');
});

test('an item that was not assessed says it was not checked, never that nothing was unusual', async () => {
  const topic = clone(topicFixture);
  topic.authenticity = {flag: 'not_assessed', flag_word: 'Not assessed (thin sample)', signals: []};
  await mount(topic);
  const auth = section('authenticity');
  expect(auth.textContent).toContain('Not checked: too few posts to judge.');
  expect(auth.textContent).not.toContain('No unusual patterns');
});

test('a not-assessed item says it was not checked once, in plain words', async () => {
  const topic = clone(topicFixture);
  topic.authenticity = {flag: 'not_assessed', flag_word: 'Not assessed (thin sample)', signals: []};
  await mount(topic);
  const auth = section('authenticity');
  expect(auth.textContent).toBe('What the posts look likeNot checked: too few posts to judge.');
  expect(auth.textContent).not.toContain('Not assessed');
  expect(auth.querySelectorAll('.tp42-flag')).toHaveLength(0);
});

test('a not-assessed item with signals still says it was not checked once', async () => {
  const topic = clone(topicFixture);
  topic.authenticity = {flag: 'not_assessed', flag_word: 'Not assessed (thin sample)',
    signals: [{signal: 'burst', words: 'Many posts in the busiest 10 minutes', share: null}]};
  await mount(topic);
  const auth = section('authenticity');
  expect(auth.textContent.match(/Not checked/g)).toHaveLength(1);
  expect(auth.textContent).not.toContain('Not assessed');
  expect([...auth.querySelectorAll('li')].map((li) => li.textContent)).toEqual(['Many posts in the busiest 10 minutes']);
});

test('an item with no stored assessment never reads as nothing unusual', async () => {
  const topic = clone(topicFixture);
  topic.authenticity = {flag: 'not_stored', flag_word: null, signals: []};
  await mount(topic);
  const auth = section('authenticity');
  expect(auth.textContent).toContain('Not checked: no check of these posts is stored.');
  expect(auth.textContent).not.toContain('No unusual patterns');
});

test('a topic without an authenticity block never reads as nothing unusual', async () => {
  const topic = clone(topicFixture);
  delete topic.authenticity;
  await mount(topic);
  expect(section('authenticity').textContent).not.toContain('No unusual patterns');
});

test('a flagged item with no listed signals never reads as nothing unusual', async () => {
  const topic = clone(topicFixture);
  topic.authenticity = {flag: 'likely_coordinated', flag_word: 'Check pattern', signals: []};
  await mount(topic);
  const auth = section('authenticity');
  expect(auth.textContent).toContain('Unusual posting pattern');
  expect(auth.textContent).not.toContain('No unusual patterns');
  expect(auth.textContent).not.toContain('Not checked');
});

test('evidence posts use safe links and images only', async () => {
  await mount();
  const posts = section('evidence');
  const items = [...posts.querySelectorAll('li')];
  expect(items).toHaveLength(topicFixture.evidence.length);
  const links = [...posts.querySelectorAll('a')];
  expect(links.every((a) => /^https?:\/\//.test(a.getAttribute('href')))).toBe(true);
  expect(links.every((a) => a.getAttribute('rel') === 'noopener noreferrer')).toBe(true);
  expect(links).toHaveLength(topicFixture.evidence.length - 1);
  const images = [...posts.querySelectorAll('img')];
  expect(images.length).toBe(3);
  expect(images.every((img) => /^https?:\/\//.test(img.getAttribute('src')))).toBe(true);
  expect(images[0].getAttribute('alt')).toBe('Post by @fixture_za_6 on TikTok');
  expect(host.innerHTML).not.toMatch(/javascript:|data:image/);
  expect(posts.textContent).toContain('Post 9: the step at the rank again');
});

test('Ask about this goes to the Ask route with the question, market and item', async () => {
  await mount();
  const links = [...host.querySelectorAll('a')].filter((a) => a.textContent === 'Ask about this');
  expect(links.length).toBeGreaterThan(0);
  for (const link of links){
    const href = link.getAttribute('href');
    expect(href.startsWith('#/ask?q=' + encodeURIComponent(topicFixture.card.ask))).toBe(true);
    expect(href).toContain('market=ZA');
    expect(href).toContain('item=' + ITEM);
  }
});

/* Demo run, 2 Oct 2026: the Ask page starts a paid live ask as soon as it
   opens with a question, so Ask about this confirms the cost first. */
test('Ask about this names the cost and starts nothing until Ask is pressed', async () => {
  await mount();
  window.location.hash = '#/t/' + ITEM;
  /* A real click on a link can be cancelled; a bare synthetic one cannot. */
  const press = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true})));
  const links = [...host.querySelectorAll('a')].filter((a) => a.textContent === 'Ask about this');
  expect(links.length).toBe(2);
  for (const link of links){
    press(link);
    await settle();
    const dialog = host.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(dialog.textContent).toContain('Ask 42 about this now?');
    expect(dialog.textContent).toContain('This starts a new live ask, up to 60 credits.');
    expect(window.location.hash).toBe('#/t/' + ITEM);
    expect(document.activeElement.textContent).toBe('Cancel');
    click(button('Cancel'));
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(window.location.hash).toBe('#/t/' + ITEM);
  }
  press(links[1]);
  await settle();
  click([...host.querySelectorAll('[role="dialog"] button')].find((b) => b.textContent.trim() === 'Ask'));
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(window.location.hash).toBe(links[1].getAttribute('href'));
});

test('a held-back topic says why at the top', async () => {
  const topic = clone(topicFixture);
  topic.card.held_back = {rule: 'G6', reason: 'not_local', reason_text: 'Most posts are from outside South Africa'};
  await mount(topic);
  expect(text()).toContain('Held back: Most posts are from outside South Africa');
  // UI notes, 2 Oct 2026: a held card is labelled as what we saw, not what we published.
  expect(section('card').hasAttribute('data-held')).toBe(true);
  expect(section('card').querySelector('.tp42-card-label').textContent).toBe('What we saw (not published)');
});

test('a published topic card carries no held label', async () => {
  await mount();
  expect(section('card').hasAttribute('data-held')).toBe(false);
  expect(section('card').querySelector('.tp42-card-label')).toBeNull();
});

test('a series list where no platform has a usable day says so once', async () => {
  const topic = clone(topicFixture);
  topic.series = topic.series.map((s) => ({...s, points: s.points.map((p) => ({...p, value: null}))}));
  await mount(topic);
  expect(section('series').querySelectorAll('svg')).toHaveLength(0);
  expect(section('series').textContent).toContain('No platform has a usable day for this topic yet.');
});

test('a failed read shows the message and Try again reads again', async () => {
  let n = 0;
  serve(() => (++n === 1 ? reply(500, {error: 'internal', message: 'The topic could not be read.'}) : reply(200, topicFixture)));
  flushSync(() => root.render(<TopicPage42 itemId={ITEM} market="ZA" />));
  await settle();
  expect(host.querySelector('[role="alert"]').textContent).toBe('The topic could not be read.');
  click(button('Try again'));
  await settle();
  expect(calls).toHaveLength(2);
  expect(section('card').textContent).toContain('#fixture_za_step');
});

test('a topic 42 does not hold says so', async () => {
  serve(reply(404, {error: 'not_found', message: 'No such item.'}));
  flushSync(() => root.render(<TopicPage42 itemId={ITEM} market="ZA" />));
  await settle();
  expect(text()).toContain('42 has no topic with this id in South Africa');
});

test('a 401 hands over to the passcode screen', async () => {
  let asked = 0;
  serve(reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'}));
  flushSync(() => root.render(<TopicPage42 itemId={ITEM} market="ZA" onAuth={() => { asked += 1; }} />));
  await settle();
  expect(asked).toBe(1);
  expect(host.querySelector('[role="alert"]')).toBeNull();
});

test('the page copy names no age group and no Google Trends', async () => {
  await mount();
  expect(text()).not.toMatch(/gen ?z|millennial|youth|generation|google trends/i);
});

test('the stylesheet keeps type roles neutral and source actions use the readable accent', () => {
  const css = readFileSync(new URL('../../styles/topic42.css', import.meta.url), 'utf8');
  const page = css.split('.tp42-heading {')[1]?.split('}')[0] || '';
  const section = css.split('.tp42-part-title {')[1]?.split('}')[0] || '';
  expect(page).toContain('font-size: var(--type-page-title)');
  expect(page).toContain('line-height: var(--leading-page-title)');
  expect(section).toContain('font-size: var(--type-section-title)');
  expect(section).toContain('line-height: var(--leading-section-title)');
  const sourceAction = css.split('.tp42-actions .tp42-link {')[1]?.split('}')[0] || '';
  expect(sourceAction).toContain('color: var(--accent-text)');
  expect(sourceAction).toContain('min-height: 32px');
  // UI notes, 2 Oct 2026: the page heading names the topic, so the card's title is not drawn twice.
  expect(css).toContain('.tp42-card .t42-card-title { display: none; }');
  const painted = css.split('\n').filter((line) => !/outline|^\.tp42-actions \.tp42-link/.test(line)).join('\n');
  expect(painted).not.toMatch(/--accent|#e4002b|\bred\b/i);
});

test('Compare with opens Compare with this topic in its market', async () => {
  await mount();
  const links = [...host.querySelectorAll('a')].filter((a) => a.textContent === 'Compare with');
  expect(links).toHaveLength(1);
  expect(links[0].getAttribute('href')).toBe('#/compare?mode=items&items=' + ITEM + '&market=ZA');
});

/* Contract section 14.1: a measured day on a platform series asks why it
   jumped. A tap opens a small confirm naming the cost; Ask posts the spike
   and follows the ask it starts on the Ask page. A gap cannot be asked
   about, so it has no control. */
const days = (series) => [...section('series').querySelectorAll('.tp42-series-row')]
  .find((li) => li.textContent.includes(series))
  .querySelectorAll('button[data-day]');
const dialog = () => host.querySelector('[role="dialog"]');

async function mountWithSpikes(spike){
  serve((url) => (String(url) === '/api/spikes' ? spike : reply(200, topicFixture)));
  flushSync(() => root.render(<TopicPage42 itemId={ITEM} market="ZA" />));
  await settle();
}

test('each measured day on a series is a button, reachable by keyboard, and gaps have none', async () => {
  await mount();
  const tiktok = [...days('TikTok local feed')];
  expect(tiktok.map((b) => b.getAttribute('data-day'))).toEqual(['2026-09-24', '2026-09-25', '2026-09-27', '2026-09-28', '2026-09-29', '2026-09-30']);
  expect([...days('X hub accounts')].map((b) => b.getAttribute('data-day'))).toEqual(['2026-09-27', '2026-09-28', '2026-09-29', '2026-09-30']);
  for (const b of tiktok){
    expect(b.tagName).toBe('BUTTON');
    expect(b.disabled).toBe(false);
    expect(b.getAttribute('tabindex')).not.toBe('-1');
  }
  expect(tiktok[1].getAttribute('aria-label')).toBe('25 September 2026, 5 posts a day. Ask why this day jumped');
  expect(section('series').querySelectorAll('svg')).toHaveLength(2);
});

test('a day opens the confirm; Cancel closes it and nothing is posted', async () => {
  await mountWithSpikes(reply(202, {ask_id: 'a_20260930_0000spk1', status: 'running'}));
  click(days('TikTok local feed')[3]);
  expect(dialog()).not.toBeNull();
  expect(dialog().textContent).toContain('Ask why this day jumped?');
  expect(dialog().textContent).toContain('A quick scan, up to 60 credits.');
  expect(dialog().textContent).toContain('TikTok local feed, 28 September 2026');
  expect([...dialog().querySelectorAll('.w42-primary')].map((b) => b.textContent)).toEqual(['Ask']);
  // Cancel takes first focus, so Enter never spends by accident.
  expect(document.activeElement && document.activeElement.textContent).toBe('Cancel');
  click(button('Cancel'));
  expect(dialog()).toBeNull();
  expect(calls.filter((c) => c.url === '/api/spikes')).toHaveLength(0);
});

test('Ask posts the spike for that day and series, then follows the ask on the Ask page', async () => {
  window.location.hash = '#/t/' + ITEM + '?market=ZA';
  await mountWithSpikes(reply(202, {ask_id: 'a_20260930_0000spk1', status: 'running', events_url: '/api/ask/a_20260930_0000spk1/events', url: '/api/ask/a_20260930_0000spk1'}));
  click(days('TikTok local feed')[3]);
  click(button('Ask'));
  await settle();
  const posts = calls.filter((c) => c.url === '/api/spikes');
  expect(posts).toHaveLength(1);
  expect(posts[0].init.method).toBe('POST');
  expect(posts[0].init.headers['X-Passcode']).toBe('test-pass');
  expect(JSON.parse(posts[0].init.body)).toEqual({item_id: ITEM, market: 'ZA', date: '2026-09-28', series: 'feed_tiktok'});
  expect(window.location.hash).toBe('#/ask?follow=a_20260930_0000spk1');
});

test('a refused spike shows the server message plainly in the confirm', async () => {
  for (const [status, message] of [
    [400, 'No measurement that day'],
    [429, 'Too many questions from this address today; try tomorrow.'],
    [503, 'The Ask agent is not installed on this service.'],
  ]){
    flushSync(() => root.unmount());
    root = createRoot(host);
    calls = [];
    await mountWithSpikes(reply(status, {error: 'refused', message}));
    click(days('X hub accounts')[0]);
    click(button('Ask'));
    await settle();
    expect(dialog().querySelector('[role="alert"]').textContent).toBe(message);
    expect(button('Ask').disabled).toBe(false);
  }
});

/* Try again on a failed spike ask comes back here with the day in the URL,
   so the same confirm opens on that day, Cancel first. A day that is a gap,
   or a hash for another topic, opens nothing. */
async function mountAt(hash){
  window.history.replaceState(null, '', hash);
  try { await mount(); } finally { window.history.replaceState(null, '', '#/'); }
}

test('a day in the topic URL opens the spike confirm on that day and series', async () => {
  await mountAt('#/t/' + encodeURIComponent(ITEM) + '?market=ZA&day=2026-09-28&series=panel_x_hub');
  expect(dialog()).not.toBeNull();
  expect(dialog().textContent).toContain('X hub accounts, 28 September 2026');
  expect(dialog().textContent).toContain('up to 60 credits');
  expect(document.activeElement && document.activeElement.textContent).toBe('Cancel');
  expect(calls.filter((c) => c.url === '/api/spikes')).toHaveLength(0);
});

test('a day in the URL with no series opens the first series measured that day', async () => {
  await mountAt('#/t/' + encodeURIComponent(ITEM) + '?market=ZA&day=2026-09-28');
  expect(dialog()).not.toBeNull();
  expect(dialog().textContent).toContain('TikTok local feed, 28 September 2026');
});

test('the day in the URL opens the confirm once: day and series leave the hash, the rest stays', async () => {
  const path = '#/t/' + encodeURIComponent(ITEM);
  window.history.replaceState(null, '', path + '?market=ZA&day=2026-09-28&series=panel_x_hub');
  const entries = window.history.length;
  try {
    await mount();
    expect(dialog()).not.toBeNull();
    expect(window.location.hash).toBe(path + '?market=ZA');
    expect(window.history.length).toBe(entries);
    flushSync(() => root.unmount());
    root = createRoot(host);
    await mount();
    expect(dialog()).toBeNull();
  } finally { window.history.replaceState(null, '', '#/'); }
});

test('a gap day, or a day for another topic, opens no confirm', async () => {
  await mountAt('#/t/' + encodeURIComponent(ITEM) + '?market=ZA&day=2026-09-26&series=feed_tiktok');
  expect(dialog()).toBeNull();
  flushSync(() => root.unmount());
  root = createRoot(host);
  await mountAt('#/t/i_other?market=ZA&day=2026-09-28');
  expect(dialog()).toBeNull();
});

/* Demo polish, 2 October 2026 (QA item 13): the topic read can come back with
   no posts while the same trend's card, as Today shows it, carries its stored
   posts. The page shows those rather than saying none are stored. */
test('a topic read with no posts shows the posts its card carries', async () => {
  for (const missing of [[], null]){
    const topic = clone(topicFixture);
    topic.evidence = missing;
    await mount(topic);
    const posts = section('evidence');
    expect([...posts.querySelectorAll('li')].map((li) => li.querySelector('.tp42-post-meta').textContent.split(' · ')[1]))
      .toEqual(topicFixture.card.evidence.map((e) => e.handle));
    expect(posts.textContent).not.toContain('No posts are stored for this topic');
    expect(posts.textContent).not.toContain('Posts for this topic are not stored yet');
    flushSync(() => root.unmount());
    root = createRoot(host);
  }
});

test('a topic with no posts anywhere still says none are stored', async () => {
  const topic = clone(topicFixture);
  topic.evidence = [];
  topic.card.evidence = [];
  await mount(topic);
  expect(section('evidence').textContent).toContain('No posts are stored for this topic.');
});

/* Demo polish, 2 October 2026 (QA item 14): when the lifecycle marker already
   names the state, the card does not say it again in a chip above it. */
test('the state word shows once when the lifecycle marker names the same state', async () => {
  await mount();
  const card = section('card');
  expect(card.querySelector('.tc-word').textContent).toBe('Emerging');
  expect(card.querySelector('.t42-state')).toBeNull();
  expect(card.textContent.match(/Emerging/g)).toHaveLength(1);
});

test('a state word the lifecycle marker does not repeat keeps its chip', async () => {
  const topic = clone(topicFixture);
  topic.card.state_word = 'Seasonal';
  await mount(topic);
  expect(section('card').querySelector('.t42-state').textContent).toBe('Seasonal');
});

/* UI polish, 2 October 2026: a post with no still, or a still that fails to
   load, leaves no empty frame in the Posts list. */
test('topic posts show no empty frame for a missing or failed still', async () => {
  const topic = clone(topicFixture);
  topic.evidence[0].thumbnail_url = null;
  await mount(topic);
  const posts = [...section('evidence').querySelectorAll('li')];
  expect(posts[0].querySelector('.tp42-thumb')).toBeNull();
  const image = posts[1].querySelector('img.tp42-thumb');
  expect(image).not.toBeNull();
  flushSync(() => image.dispatchEvent(new window.Event('error')));
  expect(posts[1].querySelector('.tp42-thumb')).toBeNull();
});

/* Audit TOP-01, 4 October 2026: the topic read stores the first 280
   characters of a post with no mark that it was cut. A post of 280
   characters or more reads as an excerpt, keeps its words as stored, and
   says the full text is on the post beside the link. A shorter post shows no
   such note. */
test('a 280-character post excerpt says the full text is on the post and keeps the link', async () => {
  const topic = clone(topicFixture);
  const excerpt = ('Taxi rank routine with the whole crew dancing in step while the queue waits and claps along. ').repeat(4).slice(0, 280);
  expect([...excerpt].length).toBe(280);
  topic.evidence[0].text = excerpt;
  await mount(topic);
  const posts = [...section('evidence').querySelectorAll('li')];
  const cut = posts[0];
  const note = cut.querySelector('[data-excerpt-note]');
  expect(note).not.toBeNull();
  expect(note.textContent).toBe('Excerpt; the full text is on the post');
  expect(cut.querySelector('.tp42-post-text').textContent.startsWith(excerpt)).toBe(true);
  expect(cut.querySelector('.tp42-post-text').textContent.endsWith('…')).toBe(true);
  expect([...cut.querySelectorAll('a')].some((a) => a.textContent === 'Open the post')).toBe(true);
  expect(posts[1].querySelector('[data-excerpt-note]')).toBeNull();
  expect(posts[1].textContent).not.toContain('Excerpt');
});

/* Visual QA, 5 October 2026 (DT01, DT02): one Instagram post showed as two
   identical cards, and a count of one read "1 posts". */
test('the same post read twice shows as one card', async () => {
  const body = clone(topicFixture);
  const twice = {...body.evidence[0], id: body.evidence[0].id + '_again'};
  body.evidence.splice(1, 0, twice);
  await mount(body);
  const cards = [...section('evidence').querySelectorAll('.tp42-post')];
  expect(cards).toHaveLength(topicFixture.evidence.length);
  const links = cards.map((card) => card.querySelector('a.tp42-link')?.getAttribute('href')).filter(Boolean);
  expect(new Set(links).size).toBe(links.length);
});

test('a market count of one takes the singular, and one young reading of one says post', async () => {
  const body = clone(topicFixture);
  body.spread.markets = [
    {market: 'ZA', first_seen: '2026-10-05', posts: {value: 1, unit: 'posts first seen in 7 days', query_id: 'q_spread', run_id: 'r1', result_hash: 'sha256:x'}},
    {market: 'NG', first_seen: '2026-10-04', posts: {value: 0, unit: 'posts first seen in 7 days', query_id: 'q_spread', run_id: 'r1', result_hash: 'sha256:x'}},
  ];
  body.series = [{platform: 'x', series: 'feed_x', series_words: 'X local feed', unit: 'posts a day', points: [{date: '2026-10-05', value: 1, expected_low: null, expected_high: null}]}];
  await mount(body);
  expect(section('spread').textContent).toContain('1 post first seen in 7 days');
  expect(section('spread').textContent).toContain('0 posts first seen in 7 days');
  expect(section('spread').textContent).not.toContain('1 posts');
  expect(section('series').textContent).toContain('1 day of readings so far: 1 post a day');
});
