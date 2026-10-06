/* Motion, focus, loading and narrow screens on the desk routes.

   Reduced motion is read as computed durations, not as the presence of a
   media block: the lead card, the comparison strips and the evidence drawer
   each have to settle to 0s under the emulated preference. Focus is read as
   a visible ring on the first five tabbable controls of the three desk
   routes. A citation control is read while the reply panel is in its
   loading state, because the rule is that loading never hides or delays a
   receipt. At 390 by 844 the comparison keeps its meaning per strip and the
   stored question keeps its citation reachable, and neither page scrolls
   sideways. At tablet and laptop widths every Compare chart keeps the width
   to draw its line and hold its first and last dates. */
import {expect, test} from '@playwright/test';
import {intelligenceFixture} from '../../src/ui/__tests__/fixtures/general-intelligence.js';
import {DESK_STATES} from './fixtures/42-capability-routes/_desk.js';
import {installFixtures, lensRosterFixture, shellFixtures} from './support/capability-harness.mjs';
import {ENTRIES, LISTEN_QUOTE} from './fixtures/42-capability-routes/creators_network_language.js';
import {readFileSync} from 'node:fs';

const REQUEST_ID = '00000000-0000-4000-8000-000000000001';
const STORED_ROUTE = '/#/console?work=ask&request=' + REQUEST_ID;
const NARROW = {width: 390, height: 844};
const LANDING_READ_FAILURE = {__status: 503, body: {error: 'fixture_landing', message: 'The landing page read is not served in this fixture.'}};

function storedQuestion(intelligence){
  return {contract_version: 'general_question_detail_v1', request_id: REQUEST_ID, observed_state: intelligence.status, question: 'Exact retained question', history: [], selected_market: 'za', requested_window: null, response: {answer: 'Stored answer', sources: [], intelligence}, window_from_plan: true, reserved_microusd: 100000, missing_work: []};
}

function deskFixtures(){
  return {...shellFixtures(), '/api/desk': DESK_STATES.populated()};
}

async function durations(locator){
  const count = await locator.count();
  expect(count, 'the subject is on the page').toBeGreaterThan(0);
  const values = [];
  for (let index = 0; index < count; index += 1){
    values.push(await locator.nth(index).evaluate((node) => {
      const style = getComputedStyle(node);
      return {transition: style.transitionDuration, animation: style.animationDuration};
    }));
  }
  return values;
}

function everyZero(values){
  return values.every(({transition, animation}) => [transition, animation].every((list) => list.split(',').every((value) => value.trim() === '0s')));
}

async function noOverflow(page){
  const measured = await page.evaluate(() => ({scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth}));
  expect(measured.scrollWidth, 'no horizontal page overflow').toBeLessThanOrEqual(measured.innerWidth);
}

/* The ring has to be the focus telling the reader where it is, so the same
   control is read focused and then blurred: an outline or a shadow that is
   already there unfocused is the component's own edge, not a focus ring, and
   only a difference counts. Focus is put back so the tab walk continues from
   where it was. */
async function focusRing(page){
  return page.evaluate(() => {
    const node = document.activeElement;
    if (!node || node === document.body) return {tag: null, ring: false};
    const read = () => { const style = getComputedStyle(node); return {outline: style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0 ? style.outline : 'none', shadow: style.boxShadow}; };
    const focused = read();
    node.blur();
    const blurred = read();
    node.focus();
    const visible = focused.outline !== 'none' || focused.shadow !== 'none';
    const changed = focused.outline !== blurred.outline || focused.shadow !== blurred.shadow;
    return {tag: node.tagName, name: (node.getAttribute('aria-label') || node.textContent || '').trim().slice(0, 40), ring: visible && changed, focused, blurred};
  });
}

test.describe('reduced motion settles every desk move at once', () => {
  test('the Briefing lead card, the Compare strips and the evidence drawer run at 0s', async ({page}) => {
    await page.emulateMedia({reducedMotion: 'reduce'});
    const state = await installFixtures(page, deskFixtures());
    await page.goto('/#/pulse');
    const lead = page.locator('.instrument-briefing[data-briefing-state="ready"] .editorial-lead');
    await expect(lead.first()).toBeVisible();
    expect(everyZero(await durations(lead))).toBe(true);

    await page.goto('/#/compare');
    const strips = page.locator('.comparison-instrument__signal');
    await expect(strips.first()).toBeVisible();
    expect(everyZero(await durations(strips))).toBe(true);

    await page.goto('/#/explore');
    await page.getByRole('button', {name: 'Open evidence', exact: true}).first().click();
    const drawer = page.locator('.evidence-drawer');
    await expect(drawer.first()).toBeVisible();
    expect(everyZero(await durations(drawer))).toBe(true);
    expect(state.unexpected).toEqual([]);
    expect(state.errors).toEqual([]);
  });
});

for (const route of ['/#/pulse', '/#/explore', '/#/compare']){
  test(`the first five tabbable controls of ${route} show a focus ring`, async ({page}) => {
    const state = await installFixtures(page, deskFixtures());
    await page.goto(route);
    await expect(page.locator('#main-content')).toBeVisible();
    const seen = [];
    for (let step = 0; step < 5; step += 1){
      await page.keyboard.press('Tab');
      const focused = await focusRing(page);
      seen.push(focused);
    }
    expect(seen.map((entry) => entry.tag).filter(Boolean), 'five controls took focus').toHaveLength(5);
    expect(seen.filter((entry) => !entry.ring), 'every focused control shows a ring').toEqual([]);
    expect(state.unexpected).toEqual([]);
  });
}

/* Ask redesign, 23 Sept 2026: the stored question heads its answer as the
   heading, and the follow-up is asked from the "Ask a follow-up" field under
   it. Sending it opens the continued thread in the console and asks the
   question there, so the journeys type the follow-up before leaving the
   stored view, and the lens select, labelled "Lens", sits beside Send on the
   console's own field. Quiet register, 23 Sept 2026: the select is labelled
   "Client lens" and its default reads "None" in place of "General 42". */
test('a receipt Open control stays enabled while the reply panel shows its loading state', async ({page}) => {
  const intelligence = intelligenceFixture();
  const state = await installFixtures(page, {
    ...shellFixtures(),
    '/api/internal/v2/fieldwork/question': storedQuestion(intelligence),
    '/api/chat/lenses': lensRosterFixture(),
    '/api/chat/send': {__hold: true},
  });
  await page.goto(STORED_ROUTE);
  await expect(page.getByRole('link', {name: 'Back to Fieldwork', exact: true})).toBeVisible();
  const open = page.getByRole('button', {name: /^Open R1:/});
  await expect(open).toBeEnabled();
  await page.getByRole('textbox', {name: 'Ask a follow-up', exact: true}).fill('Which source carried the repair routine first?');
  await page.getByRole('button', {name: 'Send', exact: true}).click();
  await expect(page).toHaveURL(/#\/console\?work=ask$/);
  await expect(page.getByText('Submitting the question', {exact: true})).toBeVisible();
  const reachable = page.getByRole('button', {name: /^Open R1:/});
  await expect(reachable).toBeVisible();
  await expect(reachable).toBeEnabled();
  const usable = await reachable.evaluate((node) => {
    const style = getComputedStyle(node);
    return {pointerEvents: style.pointerEvents, opacity: parseFloat(style.opacity), visibility: style.visibility};
  });
  expect(usable.pointerEvents).not.toBe('none');
  expect(usable.opacity).toBeGreaterThan(0.5);
  expect(usable.visibility).toBe('visible');
  await reachable.click();
  await expect(page.getByRole('link', {name: 'View original source'}).first()).toHaveAttribute('href', intelligence.receipts[0].url);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

/* An unreadable lens roster leaves the console on general 42. Only a link that
   names a lens is refused; the question, the Send control and the receipt it
   cites are not held back by a roster read they do not depend on. */
test('a failed lens roster read keeps general 42 and leaves Send and the receipt Open control enabled', async ({page}) => {
  const intelligence = intelligenceFixture();
  const state = await installFixtures(page, {
    ...shellFixtures(),
    '/api/internal/v2/fieldwork/question': storedQuestion(intelligence),
    '/api/chat/lenses': {__status: 500, body: {detail: 'client_lens_unavailable'}},
    '/api/chat/send': {__hold: true},
  });
  await page.goto(STORED_ROUTE);
  await expect(page.getByRole('button', {name: /^Open R1:/})).toBeEnabled();
  await page.getByRole('textbox', {name: 'Ask a follow-up', exact: true}).fill('Which source carried the repair routine first?');
  const send = page.getByRole('button', {name: 'Send', exact: true});
  await expect(send).toBeEnabled();
  await send.click();
  await expect(page).toHaveURL(/#\/console\?work=ask$/);
  await expect.poll(() => state.requested.includes('/api/chat/lenses'), {message: 'the roster was read'}).toBe(true);
  await expect(page.getByText('Submitting the question', {exact: true})).toBeVisible();
  expect(state.requested).toContain('/api/chat/send');
  const lens = page.getByRole('combobox', {name: 'Client lens', exact: true});
  await expect(lens).toBeEnabled();
  await expect(lens.locator('option:checked')).toHaveText('None');
  await expect(page.getByText(/client lenses this scope authorizes could not be read/)).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Stop waiting', exact: true})).toBeEnabled();
  const reachable = page.getByRole('button', {name: /^Open R1:/});
  await expect(reachable).toBeEnabled();
  await reachable.click();
  await expect(page.getByRole('link', {name: 'View original source'}).first()).toHaveAttribute('href', intelligence.receipts[0].url);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test.describe('narrow screens keep the comparison meaning and the citation reachable', () => {
  test.use({viewport: NARROW});

  test('at 390 by 844 every Compare strip keeps its name, unit, window and a source control, without sideways scroll', async ({page}) => {
    const state = await installFixtures(page, deskFixtures());
    await page.goto('/#/compare');
    const strips = page.locator('.comparison-instrument__signal');
    await expect(strips.first()).toBeVisible();
    const count = await strips.count();
    expect(count).toBeGreaterThanOrEqual(2);
    for (let index = 0; index < count; index += 1){
      const strip = strips.nth(index);
      /* A strip keeps its meaning when it has the width to state it: stacked
         one under another, not squeezed into a third of the viewport. */
      const stripBox = await strip.boundingBox();
      expect(stripBox, 'strip has a box').not.toBeNull();
      expect(stripBox.width, 'strip spans the narrow viewport').toBeGreaterThanOrEqual(NARROW.width * 0.8);
      await expect(strip.locator('.comparison-instrument__signal-head h2'), 'comparator name').toBeVisible();
      const description = strip.locator('.proof-ribbon__description');
      /* Quiet register, 23 Sept 2026: package 2.0.20 sets the ribbon
         description as one sentence, so the window reads as a written date
         range after "from" and the unit follows "scaled as", where the
         "Window" and "Normalization" labels stood. Both facts are required. */
      await expect(description, 'window').toContainText(/from \d{1,2}(?: [A-Z][a-z]{2,3}(?: \d{4})?)? to \d{1,2} [A-Z][a-z]{2,3} \d{4}/);
      /* Quiet register, 23 Sept 2026: package 2.0.21 writes the unit as what
         the drawing shows, "showing change on a scale of 0 to 1", where it
         wrote "scaled as". The unit is still required. */
      await expect(description, 'unit').toContainText(', showing ');
      await expect(description).toBeVisible();
      const source = strip.locator('[aria-label="Independent source families"] button').first();
      await expect(source, 'source inspection control').toBeVisible();
      const box = await source.boundingBox();
      expect(box, 'source control has a box').not.toBeNull();
      expect(box.x + box.width, 'source control inside the viewport width').toBeLessThanOrEqual(NARROW.width);
      expect(box.width, 'source control is a real target').toBeGreaterThanOrEqual(24);
    }
    await noOverflow(page);
    expect(state.unexpected).toEqual([]);
    expect(state.errors).toEqual([]);
  });

  /* Page port, 3 October 2026: a stored question link
     (#/console?work=ask&request=<id>) names an older record and now opens
     History (src/legacyRoutes.js), so the stored answer and its citation are
     unreachable from it. Held instead at 390 by 844: the link lands on
     History, History's action stays inside the viewport and works, nothing
     scrolls sideways, and the stored question is never read.
     Was: at 390 by 844 the stored question keeps its citation Open control reachable without sideways scroll. */
  test('at 390 by 844 a stored question link opens History with its action reachable without sideways scroll', async ({page}) => {
    const intelligence = intelligenceFixture();
    const state = await installFixtures(page, {...shellFixtures(), '/api/internal/v2/fieldwork/question': storedQuestion(intelligence), '/api/history/asks': LANDING_READ_FAILURE});
    await page.goto(STORED_ROUTE);
    await expect(page).toHaveURL(/#\/history$/);
    await expect(page.locator('#main-content h1')).toHaveText('History');
    const action = page.locator('#main-content').getByRole('button', {name: 'Try again', exact: true}).first();
    await expect(action).toBeVisible();
    await expect(action).toBeEnabled();
    const box = await action.boundingBox();
    expect(box).not.toBeNull();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(NARROW.width);
    await noOverflow(page);
    const before = state.requested.filter((key) => key === '/api/history/asks').length;
    await action.click();
    await expect.poll(() => state.requested.filter((key) => key === '/api/history/asks').length).toBeGreaterThan(before);
    expect(state.requested.filter((key) => key === '/api/internal/v2/fieldwork/question'), 'the stored question is never read').toEqual([]);
    expect(state.unexpected).toEqual([]);
    expect(state.errors).toEqual([]);
  });
});

/* A strip squeezed into a third of a tablet column keeps its words but loses
   its chart: the end values take their own width first and the drawing is
   left with what remains, so the dates under it run past its edges. Each
   strip is read with one of them selected, because the selected strip gives
   up the width of its marker. The dates must sit inside the time scale, the
   time scale inside the chart, and the chart must be wider than the two
   dates it carries. From 1440 the three signals stand side by side in one
   row, as the package draws them. A classic scrollbar, 17px on Windows
   Chrome, takes its width from the page, and headless Chrome draws none, so
   1423 stands in for a 1440 window with one: the three columns hold one row
   there too.
   Round three, 24 Sept 2026: package 2.0.22 drops a ribbon's end values
   under its chart once the plot is narrower than 440px, so a Compare chart
   keeps the full width of its column and a column no longer has to be wide
   enough for a plot and its values side by side. The three signals now stand
   in one row on a 1280 laptop window, where a strategist reads them across
   rather than down. At 1024 a third of the row is 248px, narrower than the
   ribbon's one line hidden caption, so two signals stand up with the third
   starting the next band, and a 768 tablet keeps one signal to a row beside
   the rail. The row counts are pinned at each width, and the dates are held
   inside every chart as before.
   Round three, 24 Sept 2026: the three columns start at 1240, where each is
   320px, not at about 1256, so 1240 and 1260 are pinned as well. The hidden
   caption paints nothing; 1024 kept two up only because the layout check
   read it as running past its column, a false positive the check no longer
   makes. From 1024 the three signals share one row, 248px columns whose
   charts still hold their dates, so every signal is read across at once. A
   768 tablet keeps one signal to a row beside the rail.
   Round three, 24 Sept 2026: the selected signal's red rule and the space
   beside it took about 21px from that column alone, so its chart drew
   narrower and shorter than the other two, its dates sat about 10px higher
   and its end value dropped to a line of its own at 1024. Where the three
   stand in one row, their plots now start on one line, share one width
   within 1px, and carry their dates on one line. */
const THREE_UP = 1423;
const ONE_ROW_OF_THREE = new Set([1024, 1240, 1260, 1280, THREE_UP, 1440]);
for (const viewport of [{width: 768, height: 1024}, {width: 1024, height: 768}, {width: 1240, height: 800}, {width: 1260, height: 800}, {width: 1280, height: 800}, {width: THREE_UP, height: 1000}, {width: 1440, height: 1000}]){
  test.describe(`at ${viewport.width} by ${viewport.height} every Compare chart has the width to hold its dates`, () => {
    test.use({viewport});

    test(`at ${viewport.width} by ${viewport.height} each Compare chart draws its line and holds its first and last dates`, async ({page}) => {
      const state = await installFixtures(page, deskFixtures());
      await page.goto('/#/compare');
      const strips = page.locator('article.comparison-instrument__signal');
      await expect(strips).toHaveCount(3);
      await page.getByRole('button', {name: 'Select Balcony mix', exact: true}).click();
      await expect(strips.first()).toHaveAttribute('data-selected', 'true');
      const count = await strips.count();
      const boxes = await strips.evaluateAll((nodes) => nodes.map((node) => {
        const box = node.getBoundingClientRect();
        return {top: Math.round(box.top), left: Math.round(box.left)};
      }));
      if (ONE_ROW_OF_THREE.has(viewport.width)){
        expect(new Set(boxes.map((box) => box.top)).size, `at ${viewport.width} the three signals share one row`).toBe(1);
        expect(new Set(boxes.map((box) => box.left)).size, `at ${viewport.width} each signal has its own column`).toBe(3);
        const plots = await strips.evaluateAll((nodes) => nodes.map((node) => {
          const plot = node.querySelector('.proof-ribbon__plot').getBoundingClientRect();
          const scale = node.querySelector('.proof-ribbon__time-scale').getBoundingClientRect();
          return {top: plot.top, width: plot.width, dates: scale.top};
        }));
        for (const key of ['top', 'width', 'dates']){
          const values = plots.map((plot) => plot[key]);
          expect(Math.max(...values) - Math.min(...values), `at ${viewport.width} the three plots match in ${key}: ${values.map(Math.round).join(', ')}`).toBeLessThanOrEqual(1);
        }
      } else {
        expect(new Set(boxes.map((box) => box.top)).size, `at ${viewport.width} each signal has a row of its own`).toBe(3);
        expect(new Set(boxes.map((box) => box.left)).size, `at ${viewport.width} the signals share one column`).toBe(1);
      }
      for (let index = 0; index < count; index += 1){
        const measured = await strips.nth(index).evaluate((strip) => {
          const stage = strip.querySelector('.proof-ribbon__stage').getBoundingClientRect();
          const scaleNode = strip.querySelector('.proof-ribbon__time-scale');
          const scale = scaleNode.getBoundingClientRect();
          const dates = [...scaleNode.children].map((node) => {
            const box = node.getBoundingClientRect();
            return {text: node.textContent.trim(), left: box.left, right: box.right, width: box.width};
          });
          return {
            name: strip.querySelector('.comparison-instrument__signal-head h2').textContent.trim(),
            stage: {left: stage.left, right: stage.right, width: stage.width},
            scale: {left: scale.left, right: scale.right, overflow: scaleNode.scrollWidth - scaleNode.clientWidth},
            dates,
          };
        });
        const where = `${measured.name} at ${viewport.width}`;
        expect(measured.dates.length, `${where}: the chart names its first and last dates`).toBeGreaterThanOrEqual(2);
        expect(measured.stage.width, `${where}: the chart is wider than the dates it carries`).toBeGreaterThan(measured.dates.reduce((sum, date) => sum + date.width, 0));
        expect(measured.scale.overflow, `${where}: the dates fit their time scale`).toBeLessThanOrEqual(1);
        expect(measured.scale.left, `${where}: the time scale starts inside the chart`).toBeGreaterThanOrEqual(measured.stage.left - 1);
        expect(measured.scale.right, `${where}: the time scale ends inside the chart`).toBeLessThanOrEqual(measured.stage.right + 1);
        for (const date of measured.dates){
          expect(date.left, `${where}: ${date.text} starts inside the time scale`).toBeGreaterThanOrEqual(measured.scale.left - 1);
          expect(date.right, `${where}: ${date.text} ends inside the time scale`).toBeLessThanOrEqual(measured.scale.right + 1);
        }
      }
      await noOverflow(page);
      expect(state.unexpected).toEqual([]);
      expect(state.errors).toEqual([]);
    });
  });
}

/* The two entries of the creator capability beside the profile: the network
   graph and the scoped listen read. Their fixtures are the ones the capability
   suite drives, so motion, focus and the narrow screen are read on the same
   payloads the state matrix uses.

   Page port, 3 October 2026: Network and Listen read the desk API and the
   mention feed, which f42-api does not serve, so #/network now opens
   Communities and #/listen/<term> opens Seed path for that term
   (src/legacyRoutes.js). The older pages are unreachable, so motion, focus
   and the narrow screen are read on the pages these links land on, with the
   older payloads still installed and never read. Communities is served the
   people42 fixture in the stored market; Seed path's own read is answered
   with a plain failure. The old title is kept above each test. */
const ENTRY_ROUTES = Object.fromEntries(ENTRIES.map((entry) => [entry.name, entry]));
const COMMUNITIES = JSON.parse(readFileSync(new URL('../../src/ui/__tests__/fixtures/people42.json', import.meta.url), 'utf8')).communities;
const LANDING = {
  network: {to: /#\/communities$/, heading: 'Communities in South Africa', fixtures: {'/api/communities': {...COMMUNITIES, market: 'ZA'}}, moving: '.cm42-topic, .cm42-market'},
  listen: {to: /#\/seedpath\/repair\?region=za$/, heading: 'Seed path', fixtures: {'/api/seed-path': LANDING_READ_FAILURE}, moving: '.loop-stage, .legacy-chip'},
};

function entryFixtures(name, state){
  return {...shellFixtures(), ...ENTRY_ROUTES[name].fixtures(state), ...LANDING[name].fixtures};
}

async function openEntry(page, name){
  await page.goto('/' + ENTRY_ROUTES[name].route);
  await expect(page).toHaveURL(LANDING[name].to);
  await expect(page.locator('#main-content h1')).toHaveText(LANDING[name].heading);
}

function olderReads(state, name){
  const older = Object.keys(ENTRY_ROUTES[name].fixtures('populated')).filter((key) => !(key in shellFixtures()));
  return state.requested.filter((key) => older.includes(key) || key === '/api/desk' || key.startsWith('/api/intel/mentions'));
}

/* Read twice: the same elements are measured with the preference unset and
   then with it set. Without the first reading a route that simply has no
   motion would pass as a route that respects the preference.
   Was: the network graph and the mention feed carry motion, and settle at 0s under reduced motion. */
test('the pages the network and listen links open carry motion, and settle at 0s under reduced motion', async ({page}) => {
  const network = await installFixtures(page, entryFixtures('network', 'populated'));
  await openEntry(page, 'network');
  const links = page.locator('#main-content').locator(LANDING.network.moving);
  await expect(links.first()).toBeVisible();
  expect(everyZero(await durations(links)), 'the Communities links move before the preference is read').toBe(false);
  await page.emulateMedia({reducedMotion: 'reduce'});
  expect(everyZero(await durations(links))).toBe(true);
  expect(olderReads(network, 'network')).toEqual([]);
  expect(network.unexpected).toEqual([]);

  await page.emulateMedia({reducedMotion: 'no-preference'});
  const listen = await installFixtures(page, entryFixtures('listen', 'populated'));
  await openEntry(page, 'listen');
  const controls = page.locator('#main-content').locator(LANDING.listen.moving);
  await expect(controls.first()).toBeVisible();
  expect(everyZero(await durations(controls)), 'the Seed path controls move before the preference is read').toBe(false);
  await page.emulateMedia({reducedMotion: 'reduce'});
  expect(everyZero(await durations(controls))).toBe(true);
  await expect(page.locator('.listen-mention-row')).toHaveCount(0);
  expect(olderReads(listen, 'listen')).toEqual([]);
  expect(listen.errors).toEqual([]);
});

for (const name of ['network', 'listen']){
  /* Was: the first five tabbable controls of the ${name} entry show a focus ring. */
  test(`the first five tabbable controls of the page the ${name} link opens show a focus ring`, async ({page}) => {
    const state = await installFixtures(page, entryFixtures(name, 'populated'));
    await openEntry(page, name);
    await expect(page.locator('#main-content')).toBeVisible();
    const seen = [];
    for (let step = 0; step < 5; step += 1){
      await page.keyboard.press('Tab');
      seen.push(await focusRing(page));
    }
    expect(seen.map((entry) => entry.tag).filter(Boolean), 'five controls took focus').toHaveLength(5);
    expect(seen.filter((entry) => !entry.ring), 'every focused control shows a ring').toEqual([]);
    expect(olderReads(state, name)).toEqual([]);
    expect(state.unexpected).toEqual([]);
  });
}

test.describe('narrow screens keep the recorded relationships and the quoted source reachable', () => {
  test.use({viewport: NARROW});

  /* Was: at 390 by 844 the network keeps every voice under the topic it was recorded with, without sideways scroll. */
  test('at 390 by 844 the network link opens Communities with every community link inside the viewport, without sideways scroll', async ({page}) => {
    const state = await installFixtures(page, entryFixtures('network', 'populated'));
    await openEntry(page, 'network');
    const links = page.locator('#main-content a');
    await expect(links.first()).toBeVisible();
    const count = await links.count();
    expect(count).toBeGreaterThanOrEqual(1);
    for (let index = 0; index < count; index += 1){
      const box = await links.nth(index).boundingBox();
      if (!box) continue;
      expect(box.x, 'the link starts inside the viewport').toBeGreaterThanOrEqual(0);
      expect(box.x + box.width, 'the link stays inside the viewport').toBeLessThanOrEqual(NARROW.width);
    }
    await expect(page.locator('.net-topic-links')).toHaveCount(0);
    await noOverflow(page);
    expect(olderReads(state, 'network')).toEqual([]);
    expect(state.unexpected).toEqual([]);
    expect(state.errors).toEqual([]);
  });

  /* Was: at 390 by 844 every quote keeps its age and its source, without sideways scroll. */
  test('at 390 by 844 the listen link opens Seed path with its term and controls inside the viewport, without sideways scroll', async ({page}) => {
    const state = await installFixtures(page, entryFixtures('listen', 'populated'));
    await openEntry(page, 'listen');
    const field = page.getByRole('textbox', {name: 'Keyword to trace'});
    await expect(field).toHaveValue('repair');
    await expect(page.locator('#main-content')).not.toContainText(LISTEN_QUOTE);
    const controls = page.locator('#main-content').locator('input, button');
    const count = await controls.count();
    expect(count).toBeGreaterThanOrEqual(2);
    for (let index = 0; index < count; index += 1){
      const box = await controls.nth(index).boundingBox();
      if (!box) continue;
      expect(box.x, 'the control starts inside the viewport').toBeGreaterThanOrEqual(0);
      expect(box.x + box.width, 'the control stays inside the viewport').toBeLessThanOrEqual(NARROW.width);
    }
    await noOverflow(page);
    expect(olderReads(state, 'listen')).toEqual([]);
    expect(state.unexpected).toEqual([]);
    expect(state.errors).toEqual([]);
  });
});

/* Round three, 24 Sept 2026: the selected Compare signal paints its red rule
   as an inset shadow, and forced colours drop every shadow, so the current
   item lost its only mark there. With forced colours on, the selected signal
   draws the rule as a border of the same width and the others draw none, and
   its text still starts where the others' does. */
test.describe('under forced colours the selected Compare signal keeps its rule', () => {
  test.use({viewport: {width: 1280, height: 800}});

  test('the selected signal alone draws a solid start rule and keeps its text in line', async ({page}) => {
    const state = await installFixtures(page, deskFixtures());
    await page.emulateMedia({forcedColors: 'active'});
    await page.goto('/#/compare');
    const strips = page.locator('article.comparison-instrument__signal');
    await expect(strips).toHaveCount(3);
    await page.getByRole('button', {name: 'Select Balcony mix', exact: true}).click();
    await expect(strips.first()).toHaveAttribute('data-selected', 'true');
    const read = await strips.evaluateAll((nodes) => nodes.map((node) => {
      const style = getComputedStyle(node);
      const title = node.querySelector('h2, h3, h4') || node.firstElementChild;
      return {
        selected: node.getAttribute('data-selected') === 'true',
        width: parseFloat(style.borderInlineStartWidth),
        style: style.borderInlineStartStyle,
        inset: title.getBoundingClientRect().left - node.getBoundingClientRect().left,
      };
    }));
    const selected = read.filter((strip) => strip.selected);
    const others = read.filter((strip) => !strip.selected);
    expect(await page.evaluate(() => matchMedia('(forced-colors: active)').matches)).toBe(true);
    expect(selected).toHaveLength(1);
    expect(selected[0].style).toBe('solid');
    expect(selected[0].width).toBeGreaterThanOrEqual(4);
    for (const strip of others) expect(strip.width).toBe(0);
    const insets = read.map((strip) => strip.inset);
    expect(Math.max(...insets) - Math.min(...insets), `text starts at ${insets.map(Math.round).join(', ')}`).toBeLessThanOrEqual(1);
    expect(state.unexpected).toEqual([]);
    expect(state.errors).toEqual([]);
  });
});
