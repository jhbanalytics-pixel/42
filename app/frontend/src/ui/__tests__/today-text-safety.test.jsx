import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TrendCard} = await import('../TrendCard.jsx');
const {TodayPage42} = await import('../../today42.jsx');

const realFetch = globalThis.fetch;
const clone = (value) => JSON.parse(JSON.stringify(value));
const YOUTUBE_CHANNEL_ID = 'UC' + 'a'.repeat(22);
let host = null;
let root = null;

const reply = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

beforeEach(() => {
  localStorage.setItem('pulse_passcode', 'test-pass');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  window.__todayTextSafetyInjected = 0;
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  globalThis.fetch = realFetch;
  delete window.__todayTextSafetyInjected;
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };

function renderCard(card, todaySpecificity = null, onFeedback = undefined){
  flushSync(() => root.render(
    <ul><TrendCard card={card} market="ZA" date="2026-09-30" posts={false}
      todaySpecificity={todaySpecificity} onFeedback={onFeedback} /></ul>,
  ));
  return host.querySelector('[data-card]');
}

test('TrendCard hides exact YouTube channel IDs and keeps readable API labels', () => {
  const lowerChannelId = YOUTUBE_CHANNEL_ID.toLowerCase();
  const card = renderCard({item_id: 'youtube-item', title: '  ' + lowerChannelId + '  ', explanation_status: 'failed_checks',
    thumbnails: ['id-thumbnail'], evidence: [{id: 'id-thumbnail', platform: 'youtube', handle: '  @' + lowerChannelId + '  ',
      thumbnail_url: 'https://example.invalid/channel.png'}]}, {
    whyNow: 'A verified explanation',
    quote: 'A verified quote',
    examples: [
      {id: 'id-author', platform: 'youtube', handle: '@@' + lowerChannelId, text: 'Channel post'},
      {id: 'handle-author', platform: 'youtube', handle: '@fixture_creator', text: 'Handle post'},
      {id: 'named-author', platform: 'youtube', handle: 'Amara Ndlovu', text: 'Named post'},
    ],
  }, () => Promise.resolve());

  expect(card.querySelector('h3').textContent).toBe('YouTube channel');
  expect(card.querySelectorAll('.t42-post-meta').item(0).textContent).toBe('YouTube channel');
  expect(card.querySelectorAll('.t42-post-meta').item(1).textContent).toBe('YouTube · @fixture_creator');
  expect(card.querySelectorAll('.t42-post-meta').item(2).textContent).toBe('YouTube · Amara Ndlovu');
  expect(card.querySelector('img.t42-thumb').getAttribute('alt')).toBe('Post on YouTube, channel name unavailable');
  expect(card.querySelector('[role="group"]').getAttribute('aria-label')).toBe('Feedback on YouTube channel');
  expect(card.textContent).not.toMatch(/uc[A-Za-z0-9_-]{22}/i);

  const nonYoutube = renderCard({item_id: 'tiktok-item', title: 'Creator name', explanation_status: 'failed_checks'}, {
    whyNow: 'A verified explanation',
    quote: 'A verified quote',
    examples: [{id: 'tiktok-author', platform: 'tiktok', handle: YOUTUBE_CHANNEL_ID, text: 'TikTok post'}],
  });
  expect(nonYoutube.querySelector('.t42-post-meta').textContent).toBe('TikTok · ' + YOUTUBE_CHANNEL_ID);

  const readableTitle = '@' + lowerChannelId;
  const readableCard = renderCard({item_id: 'youtube-item-readable', title: readableTitle, explanation_status: 'failed_checks'});
  expect(readableCard.querySelector('h3').textContent).toBe(readableTitle);
});

test('TrendCard keeps hostile title, explanation, author, quote and excerpt values as plain text', async () => {
  const title = '<img src=x onerror="window.__todayTextSafetyInjected=1"> &lt;b&gt;';
  const whyNow = '<script>window.__todayTextSafetyInjected=2</script> why';
  const quote = '<img src=x onerror="window.__todayTextSafetyInjected=3"> quote';
  const handle = '<img src=x onerror="window.__todayTextSafetyInjected=4"> author';
  const excerpt = '<script>window.__todayTextSafetyInjected=5</script> excerpt';
  const card = renderCard({item_id: 'hostile-item', title, explanation_status: 'failed_checks'}, {
    whyNow,
    quote,
    examples: [{id: 'hostile-post', platform: 'youtube', handle, text: excerpt}],
  });

  await settle();
  expect(card.querySelector('h3').textContent).toBe(title);
  expect(card.querySelector('.t42-specificity-why-now').textContent).toBe('Why now' + whyNow);
  expect(card.querySelector('.t42-specificity-quote .t42-post-text').textContent).toBe(quote);
  expect(card.querySelector('.t42-post-meta').textContent).toBe('YouTube · ' + handle);
  expect(card.querySelector('[data-evidence-id="hostile-post"] .t42-post-text').textContent).toBe(excerpt);
  expect(card.querySelectorAll('img, script')).toHaveLength(0);
  expect(card.querySelector('[onerror]')).toBeNull();
  expect(window.__todayTextSafetyInjected).toBe(0);
  expect(card.textContent).toContain('&lt;b&gt;');
});

test('a clean Boomplay title renders its supplied rank once', async () => {
  const today = clone(todayFixture);
  today.markets[0].boards = [{
    platform: 'boomplay',
    list: 'Top tracks',
    left_out: 0,
    left_out_reason: null,
    entries: [{rank: 1, title: 'Boomplay Track', item_id: 'boomplay-track'}],
  }];
  globalThis.fetch = async (url) => String(url).startsWith('/api/today')
    ? reply(200, today)
    : reply(404, {error: 'not_found'});

  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
  await settle();

  const row = host.querySelector('[data-market="ZA"] [data-section="boards"] .tb-rows .tb-row');
  expect(row.querySelector('.tb-rank').textContent).toBe('1');
  expect(row.querySelector('.tb-title').textContent).toBe('Boomplay Track');
  expect(row.querySelectorAll('.tb-rank')).toHaveLength(1);
});
