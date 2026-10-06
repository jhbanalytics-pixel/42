/* U01: the Briefing lead's action and Discover's "Open topic story" open
   #/topic/<released signal id>. The topic API holds curated topics only, so
   the page reads the signal from the same released run the Briefing and
   Discover render. No /api/topic fixture is installed: any read of it is an
   unexpected path and fails the case, so a 404 reply can never pass as the
   story. */
import {expect, test} from '@playwright/test';
import {DESK_STATES, RUN} from './fixtures/42-capability-routes/_desk.js';
import {installFixtures, shellFixtures} from './support/capability-harness.mjs';

const RELEASED = DESK_STATES.populated().dynamic_discovery.signals.map((row) => row.signal);
const LEAD = RELEASED[0];

function deskFixtures(){
  return {...shellFixtures(), '/api/desk': DESK_STATES.populated()};
}

async function expectSignalStory(page, signal){
  const main = page.locator('#main-content');
  await expect(main.getByRole('heading', {level: 1, name: signal.signal_name, exact: true})).toBeVisible();
  await expect(main.locator('[data-signal-id="' + signal.signal_id + '"][data-run-date="' + RUN.signal_date + '"]')).toHaveCount(1);
  await expect(main).toContainText('Run 12 Sept 2026. South Africa. Observed 18 to 24 Aug 2026.');
  await expect(main.getByText(signal.why_now, {exact: true})).toBeVisible();
  for (const receipt of signal.receipts){
    await expect(main.getByText(receipt.excerpt, {exact: true})).toBeVisible();
  }
  const sources = main.getByRole('link', {name: 'View source', exact: true});
  await expect(sources).toHaveCount(signal.receipts.length);
  await expect(sources.first()).toHaveAttribute('href', signal.receipts[0].url);
  /* Every target on the page is at least 48px tall, the source links too. */
  for (const box of await sources.evaluateAll((links) => links.map((link) => link.getBoundingClientRect().height))) expect(box).toBeGreaterThanOrEqual(48);
  for (const wrong of ['The topic could not load', 'No signal for this topic', 'This signal is not in the released run']) await expect(main).not.toContainText(wrong);
}

test('the Briefing lead action opens the released signal story, which survives reload', async ({page}) => {
  const state = await installFixtures(page, deskFixtures());
  await page.goto('/#/pulse');
  await page.getByRole('link', {name: 'Open ' + LEAD.signal_name, exact: true}).click();
  await expect(page).toHaveURL(new RegExp('#/topic/' + LEAD.signal_id + '$'));
  await expectSignalStory(page, LEAD);
  await page.reload();
  await expect(page).toHaveURL(new RegExp('#/topic/' + LEAD.signal_id + '$'));
  await expectSignalStory(page, LEAD);
  await page.goBack();
  await expect(page).toHaveURL(/#\/pulse$/);
  await expect(page.getByRole('heading', {name: LEAD.signal_name, exact: true})).toBeVisible();
  expect(state.requested.filter((path) => path.startsWith('/api/topic'))).toEqual([]);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('a Briefing comparison opens that released signal story, which survives reload', async ({page}) => {
  const state = await installFixtures(page, deskFixtures());
  const other = RELEASED[1];
  await page.goto('/#/pulse');
  await page.getByRole('button', {name: 'Compare ' + other.signal_name, exact: true}).click();
  await expect(page).toHaveURL(new RegExp('#/topic/' + other.signal_id + '$'));
  await expectSignalStory(page, other);
  await page.reload();
  await expectSignalStory(page, other);
  expect(state.requested.filter((path) => path.startsWith('/api/topic'))).toEqual([]);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test("Discover's Open topic story opens the selected released signal, which survives reload", async ({page}) => {
  const state = await installFixtures(page, deskFixtures());
  await page.goto('/#/explore');
  const open = page.getByRole('link', {name: 'Open topic story', exact: true});
  await open.focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(new RegExp('#/topic/' + LEAD.signal_id + '\\?region=za$'));
  await expectSignalStory(page, LEAD);
  await page.reload();
  await expectSignalStory(page, LEAD);
  expect(state.requested.filter((path) => path.startsWith('/api/topic'))).toEqual([]);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

/* Page port, 3 October 2026: an older #/topic/<id> link that names no 42
   item opens OlderLink42 (src/olderLink42.jsx), which says the link is from
   an older version and offers Discover and Seed path; the released signal
   story no longer renders on #/topic. An absent signal's link is held to
   that: the notice, not a dead end, cold and after reload, with Discover one
   step on, and no topic or desk read.
   Was: a signal absent from the released run reads as unavailable with a way on, not a dead end. */
test('a signal absent from the released run opens the older link notice with a way on, not a dead end', async ({page}) => {
  const state = await installFixtures(page, deskFixtures());
  const absent = 'sig_' + 'f'.repeat(64);
  await page.goto('/#/topic/' + absent);
  const main = page.locator('#main-content');
  for (const round of ['cold', 'reloaded']){
    await expect(main.getByRole('heading', {level: 1, name: 'This topic link is from an older version', exact: true})).toBeVisible();
    await expect(page).toHaveURL(new RegExp('#/topic/' + absent + '$'));
    await expect(main).not.toContainText('The topic could not load');
    await expect(main).not.toContainText(LEAD.why_now);
    if (round === 'cold') await page.reload();
  }
  await expect(main.getByRole('link', {name: 'Open Seed path', exact: true})).toHaveAttribute('href', '#/seedpath');
  await main.getByRole('link', {name: 'Open Discover', exact: true}).click();
  await expect(page).toHaveURL(/#\/explore$/);
  expect(state.requested.filter((path) => path.startsWith('/api/topic'))).toEqual([]);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});
