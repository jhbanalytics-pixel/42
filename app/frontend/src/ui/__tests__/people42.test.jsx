/* Creator pages and communities on the 42 API (task 3.2, screen half;
   contract.md sections 12.1 to 12.3). The fixture is what core/api/people.py
   builds over its fixture store in Nigeria: one macro creator on TikTok with
   two clean items, two formats and one community, the same creator before
   L2's sensitive set exists (no items, no community, no recent posts, the
   words instead), and one community of six creators of whom two are named.
   The screens show only what the API sent: no handle, follower count or
   profile link of their own making, and never coord_score. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import fixture from './fixtures/people42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const api = await import('../../api42.js');
const {CreatorPage42, CommunitiesPage42, CommunityPage42} = await import('../../people42.jsx');
const {resolveHostRoute} = await import('../../App.jsx');

const COMMUNITY = fixture.community.community.community_id;
const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const text = () => host.textContent.replace(/ /g, ' ').replace(/\s+/g, ' ');
const links = () => [...host.querySelectorAll('a')].map((a) => ({text: a.textContent.trim(), href: a.getAttribute('href'), target: a.getAttribute('target'), rel: a.getAttribute('rel')}));
const section = (name) => host.querySelector('[data-section="' + name + '"]');

function serve(answer){
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), init});
    return typeof answer === 'function' ? answer(String(url)) : answer;
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

function trackCommunitiesTimeout(){
  const nativeSetTimeout = globalThis.setTimeout;
  const nativeClearTimeout = globalThis.clearTimeout;
  const scheduled = [];
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (delay === 30000) {
      const timer = {callback, args, delay, cleared: false, fired: false};
      scheduled.push(timer);
      return timer;
    }
    return nativeSetTimeout(callback, delay, ...args);
  };
  globalThis.clearTimeout = (timer) => {
    if (scheduled.includes(timer)) {
      timer.cleared = true;
      return;
    }
    return nativeClearTimeout(timer);
  };
  return {
    scheduled,
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

async function mount(element, answer){
  serve(answer);
  flushSync(() => root.render(element));
  await settle();
}

const creator = (body = fixture.creator, props = {}) => mount(<CreatorPage42 creatorId="c_ng_macro" market="NG" {...props} />, reply(200, body));

/* The reads. */

test('the fetch functions ask the paths contract section 12 names, with the passcode', async () => {
  serve(reply(200, {}));
  await api.fetchCreator('c_ng_macro', 'NG');
  await api.fetchCommunities('NG');
  await api.fetchCommunity(COMMUNITY, 'NG');
  await api.fetchCommunity(COMMUNITY);
  expect(calls.map((c) => c.url)).toEqual([
    '/api/creators/c_ng_macro?market=NG',
    '/api/communities?market=NG',
    '/api/communities/' + COMMUNITY + '?market=NG',
    '/api/communities/' + COMMUNITY,
  ]);
  expect(calls.every((c) => c.init.headers['X-Passcode'] === 'test-pass')).toBe(true);
});

test('the routes: #/creators/<id>, #/communities and #/communities/<id>', () => {
  expect(resolveHostRoute('creators', 'c_ng_macro')).toBe('creator42');
  expect(resolveHostRoute('communities', '')).toBe('communities');
  expect(resolveHostRoute('communities', COMMUNITY)).toBe('community');
  expect(resolveHostRoute('creator', '@someone')).toBe('creator');
});

/* The creator page. */

test('the header carries the handle, platform, tier, followers as a Figure, home market and the profile link', async () => {
  await creator();
  expect(calls.map((c) => c.url)).toEqual(['/api/creators/c_ng_macro?market=NG']);
  expect(host.querySelector('h1').textContent).toBe('@fixture_ng_macro');
  const head = host.querySelector('[data-section="creator"]');
  const words = head.textContent.replace(/ /g, ' ');
  for (const part of ['TikTok', 'Macro', '750 000 followers', 'Home market Nigeria']) expect(words).toContain(part);
  expect(head.querySelector('[data-query-id="q_creator"]').textContent.replace(/ /g, ' ')).toBe('750 000');
  const profile = links().find((l) => l.text === 'Open the profile');
  expect(profile).toEqual({text: 'Open the profile', href: 'https://tiktok.com/@fixture_ng_macro', target: '_blank', rel: 'noopener noreferrer'});
});

test('recent posts are the creator’s own, newest first, as the API sent them', async () => {
  await creator();
  const posts = [...section('recent').querySelectorAll('li')];
  expect(posts.length).toBe(fixture.creator.recent_posts.length);
  expect(posts[0].textContent).toContain('Fixture post pp_macro_1');
  expect(posts[0].textContent.replace(/ /g, ' ')).toContain('120 000 views');
  expect(section('recent').querySelector('a').getAttribute('href')).toBe(fixture.creator.recent_posts[0].url);
});

test('items are cards with how many of the creator’s posts carried each, as Figures', async () => {
  await creator();
  const cards = [...section('items').querySelectorAll('[data-card]')];
  expect(cards.map((c) => c.querySelector('h3').textContent)).toEqual(['#fixture_ng_owambe', 'fixture ng peaking sound']);
  const counts = [...section('items').querySelectorAll('[data-query-id="q_creator_items"]')];
  expect(counts.map((c) => c.closest('[data-item]').textContent.replace(/ /g, ' '))).toEqual([
    expect.stringContaining('2 posts in 28 days'), expect.stringContaining('2 posts in 28 days'),
  ]);
  /* A card on a named page opens no posts of its own: its posts would come
     from the trend read, which does not keep rule 3 off a named page. */
  /* UI polish, 2 October 2026: restated for the trend line's caption ("Posts a day, ..."); the check is that no Posts button shows. */
  expect([...section('items').querySelectorAll('button')].some((b) => b.textContent.trim() === 'Posts')).toBe(false);
});

test('formats, reach and the community link', async () => {
  await creator();
  const formats = section('formats').textContent.replace(/ /g, ' ');
  expect(formats).toContain('Duet');
  expect(formats).toContain('2 posts in 28 days');
  expect(formats).toContain('Stitch');
  expect(section('formats').querySelectorAll('[data-query-id="q_creator_formats"]').length).toBe(2);
  const reach = section('reach').textContent.replace(/ /g, ' ');
  expect(reach).toContain('75 000');
  expect(reach).toContain('10 posts in 28 days');
  expect(section('reach').querySelectorAll('[data-query-id="q_creator_reach"]').length).toBe(2);
  const community = links().find((l) => l.href && l.href.startsWith('#/communities/'));
  expect(community).toEqual(expect.objectContaining({
    text: fixture.creator.community.label,
    href: '#/communities/' + fixture.creator.community.community_id + '?market=NG',
  }));
});

test('before the sensitive set: the words instead of items and recent posts, and no community', async () => {
  const body = fixture.creator_before;
  expect(body.items).toBeNull();
  expect(body.recent_posts).toBeNull();
  await creator(body);
  expect(section('items').textContent).toBe('Topics' + 'Topics per creator appear once 42 can keep sensitive topics off named pages');
  expect(section('recent').textContent).toContain('Recent posts appear once 42 can keep sensitive topics off named pages');
  expect(section('items').querySelector('[data-card]')).toBeNull();
  expect(section('recent').querySelector('li')).toBeNull();
  expect(host.querySelector('[data-section="community"]')).toBeNull();
  expect(links().some((l) => l.href && l.href.startsWith('#/communities'))).toBe(false);
});

test('the recent-posts words are the API’s own recent_posts_note, not the page’s', async () => {
  const body = clone(fixture.creator_before);
  body.recent_posts_note = 'Words the API chose for this page';
  await creator(body);
  expect(section('recent').textContent).toBe('Recent posts' + 'Words the API chose for this page');
});

test('recent posts cover the last 28 days, so an empty list says that', async () => {
  const body = clone(fixture.creator);
  body.recent_posts = [];
  await creator(body);
  expect(section('recent').textContent).toContain('No posts first seen in the last 28 days');
});

test('formats not yet measured say so; a median with no views says so', async () => {
  const body = clone(fixture.creator);
  body.formats = null;
  body.reach.median_views.value = null;
  await creator(body);
  expect(section('formats').textContent).toContain('Formats are not measured yet');
  expect(section('reach').textContent).toContain('not measured yet');
});

test('never a handle, follower count or profile link the API did not send, and never coord_score', async () => {
  const body = clone(fixture.creator);
  delete body.creator.handle;
  delete body.creator.followers;
  body.creator.profile_url = null;
  body.creator.coord_score = 0.8765;
  await creator(body);
  /* The posts still carry the handle the API sent with each post. */
  const head = section('creator').textContent;
  expect(head).not.toContain('fixture_ng_macro');
  expect(text()).not.toContain('followers');
  expect(links().some((l) => l.text === 'Open the profile')).toBe(false);
  expect(text()).not.toContain('0.8765');
  expect(text().toLowerCase()).not.toContain('coord');
  expect(host.querySelector('h1').textContent).toBe('TikTok creator');
});

test('a profile link that is not a plain web address is not linked', async () => {
  const body = clone(fixture.creator);
  body.creator.profile_url = 'javascript:alert(1)';
  await creator(body);
  expect(links().some((l) => l.text === 'Open the profile')).toBe(false);
  expect(host.innerHTML).not.toContain('javascript:');
});

test('below the page threshold the page shows the API’s own words and no name', async () => {
  await mount(<CreatorPage42 creatorId="c_ng_mid" market="NG" />,
    reply(404, {error: 'not_found', message: '42 shows creators of this size only in totals'}));
  expect(host.querySelector('h1').textContent).toBe('Creator not shown');
  expect(text()).toContain('42 shows creators of this size only in totals');
  expect(text()).not.toContain('c_ng_mid');
});

test('a 401 hands over to the passcode screen', async () => {
  let asked = 0;
  await mount(<CreatorPage42 creatorId="c_ng_macro" market="NG" onAuth={() => { asked += 1; }} />,
    reply(401, {error: 'unauthorized', message: 'Passcode required'}));
  expect(asked).toBe(1);
});

/* Communities. */

test('the community list names each community by its items, with its creator count and the method words', async () => {
  await mount(<CommunitiesPage42 market="NG" />, reply(200, fixture.communities));
  expect(calls.map((c) => c.url)).toEqual(['/api/communities?market=NG']);
  expect(host.querySelector('h1').textContent).toBe('Communities in Nigeria');
  /* Demo polish, 2 October 2026: the method line says in plain words what
     the grouping is and that interaction is not counted yet, when the
     service sends its older line. */
  expect(text()).toContain('Grouped by the topics they share. Replies and mentions between them are not counted yet.');
  expect(text()).not.toContain('not yet measured');
  const [first] = fixture.communities.communities;
  const link = links().find((l) => l.href === '#/communities/' + first.community_id + '?market=NG');
  expect(link.text).toBe(first.label);
  const row = host.querySelector('[data-community="' + first.community_id + '"]');
  expect(row.textContent.replace(/ /g, ' ')).toContain('6 creators');
  expect(row.querySelector('[data-query-id="q_communities"]')).not.toBeNull();
  expect(row.textContent).toContain('Instagram');
  /* Restated 2 October 2026: the list now shows the named creators exactly as
     the API sends them (page tier only, rule 1, after rules 2 and 3), linked
     to their pages; it still builds no name, handle or count of its own. */
  expect([...row.querySelectorAll('a[href^="#/creators/"]')].map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
    ['fixture_ng_mega', '#/creators/c_ng_mega?market=NG'], ['@fixture_ng_macro', '#/creators/c_ng_macro?market=NG'],
  ]);
  for (const unnamed of ['fixture_ng_mid', 'fixture_ng_micro', 'fixture_ng_nano']) expect(text()).not.toContain(unnamed);
});

test('each community on the list shows its top shared topics as links to their topic pages, platforms and languages', async () => {
  await mount(<CommunitiesPage42 market="NG" />, reply(200, fixture.communities));
  const [first] = fixture.communities.communities;
  const row = host.querySelector('[data-community="' + first.community_id + '"]');
  const topics = [...row.querySelectorAll('.cm42-topics a')];
  expect(topics.map((a) => a.textContent)).toEqual(first.top_items.map((c) => c.title));
  expect(topics.map((a) => a.getAttribute('href'))).toEqual(first.top_items.map((c) => '#/t/' + c.item_id + '?market=NG'));
  expect(row.textContent).toContain('English, Nigerian Pidgin, Yoruba');
  expect(row.querySelector('.cm42-size-value').textContent).toBe('6');
});

test('a list before the sensitive set names no one', async () => {
  const before = clone(fixture.communities);
  before.communities.forEach((c) => { c.members = null; });
  await mount(<CommunitiesPage42 market="NG" />, reply(200, before));
  expect(host.querySelector('a[href^="#/creators/"]')).toBeNull();
});

test('a list with more named creators than it shows links to the community instead of counting them', async () => {
  const many = clone(fixture.communities);
  const [first] = many.communities;
  first.members = Array.from({length: 6}, (_, i) => ({creator_id: 'c_' + i, platform: 'tiktok', handle: '@n' + i, tier: 'macro'}));
  await mount(<CommunitiesPage42 market="NG" />, reply(200, many));
  const row = host.querySelector('[data-community="' + first.community_id + '"]');
  expect(row.querySelectorAll('a[href^="#/creators/"]').length).toBe(4);
  const more = [...row.querySelectorAll('a')].find((a) => a.textContent === 'See every named creator');
  expect(more.getAttribute('href')).toBe('#/communities/' + first.community_id + '?market=NG');
  expect(row.textContent).not.toMatch(/\b2 more\b/);
});

test('the market links mark the market on screen', async () => {
  await mount(<CommunitiesPage42 market="NG" />, reply(200, fixture.communities));
  const nav = host.querySelector('nav[aria-label="Market"]');
  expect([...nav.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href'), a.getAttribute('aria-current')])).toEqual([
    ['South Africa', '#/communities?market=ZA', null], ['Nigeria', '#/communities?market=NG', 'page'], ['Kenya', '#/communities?market=KE', null],
  ]);
});

test('an empty list says why it is empty and what fills a community', async () => {
  // Restated 2 October 2026: the empty state now says what fills a community and reads the other markets' counts for real.
  await mount(<CommunitiesPage42 market="KE" />, (url) => reply(200, {...clone(fixture.communities), market: 'KE', communities: url.includes('market=NG') ? fixture.communities.communities : []}));
  expect(text()).toContain('No community of five or more creators in Kenya in the last 28 days');
  expect(text()).toContain('A community forms when five or more creators in one market post about the same topics within 28 days.');
  expect(calls.map((c) => c.url)).toEqual(['/api/communities?market=KE', '/api/communities?market=ZA', '/api/communities?market=NG']);
  const rows = [...host.querySelectorAll('.cm42-elsewhere-row')].map((li) => [li.querySelector('a').getAttribute('href'), li.textContent.replace(/\s+/g, ' ')]);
  expect(rows).toEqual([
    ['#/communities?market=ZA', 'South AfricaNone in 28 days'],
    ['#/communities?market=NG', 'Nigeria' + fixture.communities.communities.length + ' community'],
  ]);
});

/* Design audit, 2 October 2026: inverted pyramid. The empty state leads
   with its one sentence and the definition of a community waits in a closed
   disclosure titled How communities form, with every word kept. */
test('an empty list leads with the state and keeps the definition in an open disclosure', async () => {
  await mount(<CommunitiesPage42 market="KE" />, () => reply(200, {...clone(fixture.communities), market: 'KE', communities: []}));
  const empty = host.querySelector('[data-section="empty"]');
  expect(empty.firstElementChild.textContent).toBe('No community of five or more creators in Kenya in the last 28 days.');
  const about = empty.querySelector('details');
  expect(about !== null, 'the definition sits in a disclosure').toBe(true);
  /* Restated, layout pass 4 October 2026: with no community to show, the definition is the one thing left to read, so it opens by default. */
  expect(about.open).toBe(true);
  expect(about.querySelector('summary').textContent).toBe('How communities form');
  expect(about.textContent.replace(/\s+/g, ' ')).toContain('A community forms when five or more creators in one market post about the same topics within 28 days. 42 links creators by the topics they share, leaving out sensitive, held-back and flagged topics.');
  const outside = [...empty.children].filter((child) => child !== about).map((child) => child.textContent).join(' ');
  expect(outside).not.toContain('A community forms when');
});

test('an other market that does not answer is still a link, with no number', async () => {
  await mount(<CommunitiesPage42 market="KE" />, (url) => (url.includes('market=KE')
    ? reply(200, {...clone(fixture.communities), market: 'KE', communities: []})
    : reply(500, {error: 'internal', message: 'No.'})));
  const rows = [...host.querySelectorAll('.cm42-elsewhere-row')];
  expect(rows.map((li) => li.querySelector('a').getAttribute('href'))).toEqual(['#/communities?market=ZA', '#/communities?market=NG']);
  expect(rows.map((li) => li.querySelector('span').textContent)).toEqual(['Could not be checked', 'Could not be checked']);
  expect(host.querySelector('.cm42-elsewhere').textContent).not.toMatch(/\d/);
});

test('a community page links back to its market list and draws languages on one axis', async () => {
  await mount(<CommunityPage42 communityId={COMMUNITY} market="NG" />, reply(200, fixture.community));
  const back = host.querySelector('.cm42-back');
  expect([back.textContent, back.getAttribute('href')]).toEqual(['All communities in Nigeria', '#/communities?market=NG']);
  const widths = [...section('languages').querySelectorAll('.cm42-bar')].map((bar) => Number(bar.style.getPropertyValue('--cm42-w')));
  const values = fixture.community.community.languages.map((l) => l.posts.value);
  expect(widths).toEqual(values.map((v) => v / Math.max(...values)));
  expect(section('languages').querySelector('.cm42-axis').textContent).toBe('0' + Math.max(...values));
});

test('a pending community read times out, ignores its late result, and can retry', async () => {
  const timers = trackCommunitiesTimeout();
  try {
    let release;
    let attempts = 0;
    await mount(<CommunitiesPage42 market="NG" />, () => {
      attempts += 1;
      return attempts === 1
        ? new Promise((resolve) => { release = resolve; })
        : reply(200, fixture.communities);
    });
    expect(text()).toContain('Loading the communities');
    expect(timers.scheduled).toHaveLength(1);
    expect(timers.scheduled[0].delay).toBe(30000);
    flushSync(() => timers.fire());
    await settle();
    expect(host.querySelector('[role="alert"]').textContent).toBe('The 42 service did not answer.');
    expect(host.querySelector('button').textContent).toBe('Try again');
    expect(calls[0].init.signal.aborted).toBe(true);
    release(reply(200, fixture.communities));
    await settle();
    expect(host.querySelector('h1').textContent).toBe('The communities could not load');
    flushSync(() => host.querySelector('button').dispatchEvent(new MouseEvent('click', {bubbles: true})));
    await settle();
    expect(calls).toHaveLength(2);
    expect(host.querySelector('h1').textContent).toBe('Communities in Nigeria');
  } finally {
    timers.restore();
  }
});

test('a community 401 still calls the passcode handler and clears the timeout', async () => {
  const timers = trackCommunitiesTimeout();
  try {
    let asked = 0;
    await mount(<CommunitiesPage42 market="NG" onAuth={() => { asked += 1; }} />,
      reply(401, {error: 'unauthorized', message: 'Passcode required'}));
    expect(asked).toBe(1);
    expect(text()).toContain('Enter the passcode to read this communities.');
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(timers.scheduled[0].cleared).toBe(true);
  } finally {
    timers.restore();
  }
});

test('a community page: label, count, named members only as sent, top items as cards, platforms, languages, example posts', async () => {
  await mount(<CommunityPage42 communityId={COMMUNITY} market="NG" />, reply(200, fixture.community));
  expect(calls.map((c) => c.url)).toEqual(['/api/communities/' + COMMUNITY + '?market=NG']);
  const body = fixture.community.community;
  expect(host.querySelector('h1').textContent).toBe(body.label);
  /* Demo polish, 2 October 2026: the method line says in plain words what
     the grouping is and that interaction is not counted yet, when the
     service sends its older line. */
  expect(text()).toContain('Grouped by the topics they share. Replies and mentions between them are not counted yet.');
  expect(text()).not.toContain('not yet measured');
  expect(host.querySelector('[data-query-id="q_communities"]').textContent).toBe('6');
  const members = [...section('members').querySelectorAll('li')];
  expect(members.map((m) => m.querySelector('a').getAttribute('href'))).toEqual([
    '#/creators/c_ng_mega?market=NG', '#/creators/c_ng_macro?market=NG',
  ]);
  expect(members[0].textContent).toContain('fixture_ng_mega');
  expect(members[0].textContent).toContain('Instagram');
  expect(members[0].textContent).toContain('Mega');
  expect(links().filter((l) => l.text === 'Open the profile').map((l) => l.href)).toEqual([
    'https://instagram.com/fixture_ng_mega', 'https://tiktok.com/@fixture_ng_macro',
  ]);
  expect([...section('top-items').querySelectorAll('[data-card] h3')].map((h) => h.textContent)).toEqual(body.top_items.map((c) => c.title));
  expect(section('platforms').textContent).toContain('Instagram, TikTok, X, YouTube');
  expect(section('languages').querySelectorAll('[data-query-id="q_community_languages"]').length).toBe(3);
  expect(section('languages').textContent).toContain('English');
  expect(section('examples').querySelectorAll('li').length).toBe(body.example_posts.length);
  /* Only the page-tier members are named: none of the four below macro. */
  for (const unnamed of ['fixture_ng_mid', 'fixture_ng_micro', 'fixture_ng_nano']) expect(text()).not.toContain(unnamed);
});

test('before the sensitive set a community shows counts only: no members and no example posts', async () => {
  const body = fixture.community_before;
  expect(body.community.members).toBeNull();
  await mount(<CommunityPage42 communityId={body.community.community_id} market="NG" />, reply(200, body));
  expect(host.querySelector('[data-section="members"]')).toBeNull();
  expect(host.querySelector('[data-section="examples"]')).toBeNull();
  expect(host.querySelector('a[href^="#/creators/"]')).toBeNull();
  /* Before the set, sensitive items still join creators, so more are counted. */
  expect(host.querySelector('[data-query-id="q_communities"]').textContent).toBe(String(body.community.creators.value));
  expect(text()).toContain('Topics per creator appear once 42 can keep sensitive topics off named pages');
});

test('languages not yet measured say so; a member without a handle is not given one', async () => {
  const body = clone(fixture.community);
  body.community.languages = null;
  delete body.community.members[0].handle;
  body.community.members[0].profile_url = null;
  body.community.members[0].coord_score = 0.8765;
  await mount(<CommunityPage42 communityId={COMMUNITY} market="NG" />, reply(200, body));
  expect(section('languages').textContent).toContain('Languages are not measured yet');
  const first = section('members').querySelector('li');
  expect(first.textContent).not.toContain('fixture_ng_mega');
  expect(first.querySelector('a').textContent).toBe('Instagram creator');
  expect(links().filter((l) => l.text === 'Open the profile').length).toBe(1);
  expect(text()).not.toContain('0.8765');
});

/* Rule 3 (contract section 12.1): no coordination or payment next to a
   name. The API already leaves flagged items off named pages; if one still
   arrives with a flag word or a flag, the page leaves the card out. */

test('a creator page shows no card that carries a flag word or a flag', async () => {
  const body = clone(fixture.creator);
  body.items[0].card.flag = 'likely_coordinated';
  body.items[0].card.flag_word = 'Likely coordinated';
  await creator(body);
  const cards = [...section('items').querySelectorAll('[data-card]')];
  expect(cards.map((c) => c.querySelector('h3').textContent)).toEqual(['fixture ng peaking sound']);
  expect(section('items').querySelectorAll('[data-item]').length).toBe(1);
  expect(host.querySelector('.t42-flag')).toBeNull();
  expect(text()).not.toContain('Likely coordinated');

  const flagged = clone(fixture.creator);
  flagged.items[0].card.flag_word = 'Paid led';
  flagged.items[1].card.flags = ['check_pattern'];
  flushSync(() => root.unmount());
  root = createRoot(host);
  await creator(flagged);
  expect(section('items').querySelectorAll('[data-card]').length).toBe(0);
  expect(host.querySelector('.t42-flag')).toBeNull();
  expect(text()).not.toContain('Paid led');
  expect(section('items').textContent).toContain('No topics 42 can show beside this creator.');
});

test('a creator page drops a card whose only sign is the word Check pattern', async () => {
  const body = clone(fixture.creator);
  body.items[0].card.flag_word = 'Check pattern';
  await creator(body);
  expect(section('items').querySelectorAll('[data-card]').length).toBe(1);
  expect(text()).not.toContain('Check pattern');
});

test('a creator page still shows a card whose flag is not a rule 3 flag', async () => {
  const body = clone(fixture.creator);
  body.items[0].card.flag = 'not_assessed';
  body.items[0].card.flag_word = 'Not assessed (thin sample)';
  await creator(body);
  expect(section('items').querySelectorAll('[data-card]').length).toBe(2);
});

test('a community page shows no card that carries a flag word or a flag', async () => {
  const body = clone(fixture.community);
  body.community.top_items[0].flag = 'check_pattern';
  body.community.top_items[0].flag_word = 'Check the pattern';
  await mount(<CommunityPage42 communityId={COMMUNITY} market="NG" />, reply(200, body));
  expect([...section('top-items').querySelectorAll('[data-card] h3')].map((h) => h.textContent)).toEqual(['fixture ng peaking sound']);
  expect(host.querySelector('.t42-flag')).toBeNull();
  expect(text()).not.toContain('Check the pattern');

  const flagged = clone(fixture.community);
  flagged.community.top_items[0].flag_word = 'Paid led';
  flagged.community.top_items[1].flags = ['likely_coordinated'];
  flushSync(() => root.unmount());
  root = createRoot(host);
  await mount(<CommunityPage42 communityId={COMMUNITY} market="NG" />, reply(200, flagged));
  expect(section('top-items').querySelectorAll('[data-card]').length).toBe(0);
  expect(host.querySelector('.t42-flag')).toBeNull();
  expect(text()).not.toContain('Paid led');
  expect(section('top-items').textContent).toContain('No shared topic 42 can show here.');
});

test('a community id with no market asks without one', async () => {
  await mount(<CommunityPage42 communityId={COMMUNITY} />, reply(200, fixture.community));
  expect(calls.map((c) => c.url)).toEqual(['/api/communities/' + COMMUNITY]);
  expect(links().find((l) => l.href && l.href.startsWith('#/creators/')).href).toBe('#/creators/c_ng_mega?market=NG');
});

test('a community heading keeps each topic whole and breaks only between topics', async () => {
  await mount(<CommunityPage42 communityId={COMMUNITY} market="NG" />, reply(200, fixture.community));
  const parts = [...host.querySelectorAll('h1 .cm42-label-part')].map((el) => el.textContent);
  expect(parts.join(' ')).toBe(fixture.community.community.label);
  expect(parts.length).toBe(fixture.community.community.label.split(', ').length);
});

test('example post thumbnails are decorative, become a plain tile when they fail, and no meta line ends on a dot', async () => {
  await mount(<CommunityPage42 communityId={COMMUNITY} market="NG" />, reply(200, fixture.community));
  const imgs = [...section('examples').querySelectorAll('img')];
  expect(imgs.length).toBeGreaterThan(0);
  expect(imgs.every((img) => img.getAttribute('alt') === '')).toBe(true);
  flushSync(() => imgs[0].dispatchEvent(new Event('error')));
  expect(section('examples').querySelectorAll('.cm42-thumb-none').length).toBe(1);
  const pieces = [...section('examples').querySelector('.cm42-post-meta').children].map((el) => el.textContent);
  expect(pieces[0].startsWith('·')).toBe(false);
  expect(pieces.slice(1).every((p) => p.startsWith('· '))).toBe(true);
  expect(pieces.some((p) => p.trim().endsWith('·'))).toBe(false);
});

test('a community page says its size and market as one line', async () => {
  await mount(<CommunityPage42 communityId={COMMUNITY} market="NG" />, reply(200, fixture.community));
  expect(host.querySelector('.cm42-stats').textContent.replace(/\s+/g, ' ')).toBe('6 creators in Nigeria');
});

/* Visual QA, 5 October 2026 (CM01): Kenya's languages showed the bare code
   "so". Every code enrichment may record has a name. */
test('every language code enrichment records reads as a name on the community list', async () => {
  const list = clone(fixture.communities);
  list.communities[0].languages = ['en', 'sw', 'sheng', 'so', 'luo', 'ki', 'kam'].map((lang) => ({...list.communities[0].languages[0], lang}));
  await mount(<CommunitiesPage42 market="NG" />, reply(200, list));
  const row = host.querySelector('[data-community="' + list.communities[0].community_id + '"]');
  expect(row.textContent).toContain('English, Swahili, Sheng, Somali, Dholuo, Gikuyu, Kikamba');
  expect(row.textContent).not.toMatch(/, so,|, so$/);
});
