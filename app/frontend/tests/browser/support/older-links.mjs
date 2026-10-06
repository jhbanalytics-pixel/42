/* Page port, 3 October 2026. Board, Browse, Network and Listen read the desk
   API, which f42-api does not serve, so they left the More menu and their
   links now land on the 42 page that does the same job (src/legacyRoutes.js):
   #/board opens Alerts, #/browse opens Discover, #/network opens Communities
   and #/listen/<term> opens Seed path for that term. An older #/topic/<id>
   that names no 42 item, and any #/creator/<handle>, render OlderLink42
   (src/olderLink42.jsx), which reads nothing. The journeys that drove those
   pages assert the landing with these helpers: the hash it is rewritten to,
   the page that renders there, that no desk read is made, and that what the
   reader had stored (pins, tracked handles, last looks) is left as it was. */
import {expect} from '@playwright/test';
import {readFileSync} from 'node:fs';

const people = JSON.parse(readFileSync(new URL('../../../src/ui/__tests__/fixtures/people42.json', import.meta.url), 'utf8'));

/* Every read the desk pages made. None of them may happen now. */
const DESK_PATHS = ['/api/desk', '/api/voices', '/api/topic/', '/api/creator/', '/api/intel/mentions', '/api/lexicon/', '/api/ask'];
const STORED_KEYS = ['pulse-watch', 'pulse-crm', 'pulse-lastlook'];

/* Records the path and query of every /api request the page sends, whatever
   route answers it. */
export function watchApi(page){
  const reads = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/')) reads.push(request.method() + ' ' + url.pathname + url.search);
  });
  return reads;
}

export function deskReads(reads){
  return reads.filter((entry) => {
    const path = entry.split(' ')[1].split('?')[0];
    return DESK_PATHS.some((prefix) => prefix.endsWith('/') ? path.startsWith(prefix) : path === prefix || path.startsWith(prefix + '/'));
  });
}

/* Serves the 42 reads the landing pages make, registered after a journey's
   own fixture so it answers first: Communities gets the people42 fixture in
   the market it asked for. */
export async function serveLandingReads(page){
  await page.route('**/api/communities?*', (route) => {
    const market = new URL(route.request().url()).searchParams.get('market') || 'ZA';
    return route.fulfill({json: {...people.communities, market}});
  });
}

export function storedState(page){
  return page.evaluate((keys) => Object.fromEntries(keys.map((key) => [key, localStorage.getItem(key)])), STORED_KEYS);
}

const LANDINGS = {
  board: {hash: /#\/alerts$/, heading: 'Alerts'},
  browse: {hash: /#\/explore$/, heading: 'Discover'},
  network: {hash: /#\/communities$/, heading: /^Communities in /},
};

/* Opens an older desk page link and holds where it lands: the rewritten hash,
   the landing page's heading, the same after a reload, no desk read, and the
   stored reader state untouched. Returns the landing page's heading. */
export async function expectDeskPageLanding(page, view, reads){
  const landing = LANDINGS[view];
  await page.goto('/#/' + view);
  await expect(page).toHaveURL(landing.hash);
  const heading = page.locator('#main-content h1');
  await expect(heading).toHaveText(landing.heading);
  const stored = await storedState(page);
  await page.reload();
  await expect(page).toHaveURL(landing.hash);
  await expect(heading).toHaveText(landing.heading);
  expect(await storedState(page)).toEqual(stored);
  await expect(page.locator('a[href^="#/' + view + '"]')).toHaveCount(0);
  expect(deskReads(reads)).toEqual([]);
  return heading;
}

const OLDER = {
  topic: {title: 'This topic link is from an older version', next: [['Open Discover', '#/explore'], ['Open Seed path', '#/seedpath']]},
  creator: {title: 'This creator link is from an older version', next: [['Open Communities', '#/communities'], ['Open Discover', '#/explore']]},
};

/* An older topic or creator link that names no 42 id stays on its hash and
   says it is from an older version, offering the 42 pages for the same job.
   It reads nothing and offers no Watch or Track control. */
export async function expectOlderLink(page, kind, hash, reads){
  const older = OLDER[kind];
  const before = reads.length;
  await page.goto('/' + hash);
  await expect(page).toHaveURL(new RegExp(hash.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$'));
  await expect(page.getByRole('heading', {level: 1, name: older.title, exact: true})).toBeVisible();
  for (const [name, href] of older.next) await expect(page.getByRole('link', {name, exact: true})).toHaveAttribute('href', href);
  await expect(page.getByRole('button', {name: /^(Watch|Watching|Track|Tracking)/})).toHaveCount(0);
  expect(deskReads(reads)).toEqual([]);
  expect(reads.slice(before).filter((entry) => !entry.endsWith(' /api/health'))).toEqual([]);
}
