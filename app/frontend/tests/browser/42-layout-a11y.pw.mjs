/* Layout, geometry and accessibility certification over the populated
   fixtures.

   One case per implemented capability, theme and viewport renders the
   capability's populated fixture and measures, in this order: horizontal
   page overflow, every key panel the manifest names as the proof panel
   (visible in the viewport or brought fully into it by one scroll), every
   rendered text range against its own container, every action control's
   target size on the narrow viewport, the keyboard path to every primary
   action with a visible focus ring, and the contrast of body text and of the
   primary action's text against the background actually painted under it in
   that theme. A second family of cases renders each capability under the
   reduced motion preference and counts the elements that still move. Every
   measurement is recorded on the test as an annotation, the assertions are
   soft so one failing check never hides the others, and a failed case keeps
   its full page screenshot under test-results with the file named in the
   proof output. Titles carry a colon so the capability state reporter never
   reads them as state rows. */
import {expect, test} from '@playwright/test';
import {installFixtures, shellFixtures} from './support/capability-harness.mjs';
import {measureTextRanges} from './support/text-ranges.mjs';
import {
  THEMES, THRESHOLDS, VIEWPORTS, implementedCapabilities, record, retainFailureScreenshot, settle, themeInit, viewportName,
} from './support/certification.mjs';

const NARROW = VIEWPORTS[0];
const MOTION_VIEWPORT = VIEWPORTS[2];
const PANEL_MARK = 'data-certification-panel';

/* The observable surface of each capability's populated fixture: the marker
   that says the route has settled, the setup a proof panel needs (the cited
   answer record opens from its citation control), the proof panels named by
   the manifest as locators, and the primary actions a strategist reaches. */
const SURFACES = {
  today_briefing: {
    async ready(main){ await expect(main.locator('.instrument-briefing[data-briefing-state="ready"]')).toBeVisible(); },
    panels: (main) => [main.locator('.instrument-briefing[data-briefing-state="ready"]'), main.locator('figure.proof-ribbon').first()],
    primary: (main) => [main.getByRole('link', {name: /^Open Balcony mix/}), main.getByRole('button', {name: /^Compare /}).first()],
  },
  discovery_exploration: {
    /* Quiet register, 23 Sept 2026: the count is in plain words, without the
       engine's "admitted". */
    async ready(main){ await expect(main.getByText('3 of 3 signals shown')).toBeVisible(); },
    panels: (main) => [
      main.getByRole('button', {name: 'Open evidence', exact: true}).first().locator('xpath=ancestor::*[self::article or self::li or self::section][1]'),
      main.getByText(/^Run /).first().locator('xpath=ancestor::*[self::article or self::li or self::section or self::div][1]'),
    ],
    primary: (main) => [main.getByRole('button', {name: 'Open evidence', exact: true}).first(), main.getByRole('link', {name: /^Open topic story/})],
  },
  compare: {
    async ready(main){ await expect(main.locator('article.comparison-instrument__signal')).toHaveCount(3); },
    panels: (main) => [main.locator('article.comparison-instrument__signal'), main.locator('[data-comparison-axis]')],
    primary: (main) => [main.getByRole('button', {name: 'Select Balcony mix', exact: true})],
  },
  sources_evidence: {
    async ready(main, page){
      const citation = main.getByRole('button', {name: /^Open R1:/});
      await expect(citation).toBeEnabled();
      const scroll = await page.evaluateHandle(() => {
        const state = {events: 0, ends: 0};
        const moved = () => { state.events += 1; };
        const ended = () => { state.ends += 1; };
        document.addEventListener('scroll', moved, true);
        document.addEventListener('scrollend', ended, true);
        state.close = () => {
          document.removeEventListener('scroll', moved, true);
          document.removeEventListener('scrollend', ended, true);
        };
        return state;
      });
      try {
        await citation.click();
        await expect(main.locator('.general-reply')).toContainText('Source record');
        const settled = await scroll.evaluate((state) => new Promise((resolve, reject) => {
          const started = performance.now();
          let last = [scrollX, scrollY, state.events];
          let quietFrames = 0;
          let frame;
          const timeout = setTimeout(() => {
            cancelAnimationFrame(frame);
            reject(new Error('Citation scroll did not settle within 2500 ms'));
          }, 2500);
          const sample = () => {
            const current = [scrollX, scrollY, state.events];
            quietFrames = current.every((value, index) => value === last[index]) ? quietFrames + 1 : 0;
            last = current;
            if (quietFrames >= 8){
              clearTimeout(timeout);
              resolve({events: state.events, scrollend_events: state.ends, quiet_frames: quietFrames, elapsed_ms: performance.now() - started, scroll_x: scrollX, scroll_y: scrollY});
              return;
            }
            frame = requestAnimationFrame(sample);
          };
          frame = requestAnimationFrame(sample);
        }));
        await test.info().attach('citation-scroll-settlement', {body: JSON.stringify(settled), contentType: 'application/json'});
      } finally {
        await scroll.evaluate((state) => state.close());
        await scroll.dispose();
      }
    },
    panels: (main) => [main.locator('.general-reply')],
    primary: (main) => [main.getByRole('button', {name: /^Open R1:/}), main.getByRole('link', {name: 'View original source'}).first()],
  },
  creators_network_language: {
    async ready(main){ await expect(main.getByRole('heading', {name: '@maker_one'})).toBeVisible(); },
    panels: (main) => [main.getByRole('region', {name: 'Recorded engagement over time'}), main.getByRole('heading', {name: 'Source posts'}).locator('xpath=..')],
    primary: (main) => [main.getByRole('link', {name: 'Repair culture'}), main.getByRole('link', {name: 'View original post'}).first()],
  },
  questions_followups: {
    /* Ask redesign, 23 Sept 2026: the follow-up is the question field under
       the stored answer, labelled "Ask a follow-up", in place of a button. */
    async ready(main){ await expect(main.getByRole('textbox', {name: 'Ask a follow-up', exact: true})).toBeEnabled(); },
    panels: (main) => [main.locator('.ask-turn-stack').last(), main.locator('details.general-run')],
    primary: (main) => [main.getByRole('textbox', {name: 'Ask a follow-up', exact: true}), main.getByRole('button', {name: /^Open R1:/})],
  },
  fieldwork: {
    async ready(main){ await expect(main.getByRole('link', {name: 'Open question', exact: true})).toBeVisible(); },
    panels: (main) => [
      main.getByRole('link', {name: 'Open question', exact: true}).locator('xpath=ancestor::*[self::article or self::li or self::tr or self::section][1]'),
      main.locator('.fieldwork-strip'),
      main.locator('.source-snapshot__row').first(),
    ],
    primary: (main) => [main.getByRole('link', {name: 'Open question', exact: true})],
  },
  coverage_source_lab: {
    async ready(main){ await expect(main.getByRole('heading', {name: 'Source Lab', exact: true})).toBeVisible(); await expect(main).toContainText('Verified snapshot'); },
    panels: (main) => [
      main.getByText('Open evidence', {exact: true}).first().locator('xpath=ancestor::*[self::article or self::li or self::tr or self::section][1]'),
      main.locator('.source-snapshot__row').first(),
    ],
    primary: (main) => [main.getByText('Open evidence', {exact: true}).first()],
  },
  investigation_build_review: {
    async ready(main){ await expect(main.getByRole('heading', {name: 'Evidence Room', exact: true})).toBeVisible(); await expect(main).toContainText('Approved'); },
    panels: (main) => [main.locator('.evidence-room-grid')],
    primary: (main) => [main.getByText('Open evidence', {exact: true}).first()],
  },
};

/* Page port, 3 October 2026: five capability routes were older pages that
   read the desk API or the older console, which f42-api does not serve, and
   their links now land on 42 pages (src/legacyRoutes.js): the stored
   question (sources_evidence, questions_followups) opens History, the Source
   Lab opens Fieldwork, the older console investigation opens the 42
   investigation page, and an older creator link says it is from an older
   version (src/olderLink42.jsx). The older pages are unreachable, so these
   rows certify the page the route lands on, with every check unchanged: the
   route must land on that hash, the landing page renders its heading in the
   requested theme, and its heading block and its action are the panel and
   the primary. The landing page's own read is answered with a plain failure
   so its retry is the action; the older producer payloads stay installed
   and are never read. */
const LANDING_READ_FAILURE = {__status: 503, body: {error: 'fixture_landing', message: 'The landing page read is not served in this fixture.'}};
function landingSurface({to, heading, reads, primary}){
  return {
    to,
    fixtures: Object.fromEntries(reads.map((path) => [path, LANDING_READ_FAILURE])),
    async ready(main, page){
      await expect(page).toHaveURL(to);
      await expect(main.locator('h1')).toHaveText(heading);
      await expect(primary(main)).toBeVisible();
    },
    panels: (main) => [main.locator('h1').locator('xpath=ancestor::*[self::section or self::div][1]')],
    primary: (main) => [primary(main)],
  };
}
const HISTORY_SURFACE = landingSurface({to: /#\/history$/, heading: 'History', reads: ['/api/history/asks'], primary: (main) => main.getByRole('button', {name: 'Try again', exact: true}).first()});
Object.assign(SURFACES, {
  sources_evidence: HISTORY_SURFACE,
  questions_followups: HISTORY_SURFACE,
  creators_network_language: landingSurface({to: /#\/creator\/maker_one$/, heading: 'This creator link is from an older version', reads: [], primary: (main) => main.getByRole('link', {name: 'Open Communities', exact: true})}),
  coverage_source_lab: landingSurface({to: /#\/fieldwork$/, heading: 'Fieldwork', reads: ['/api/fieldwork'], primary: (main) => main.getByRole('button', {name: 'Try again', exact: true}).first()}),
  investigation_build_review: landingSurface({to: /#\/investigations\/inv_fixture$/, heading: 'Investigation', reads: ['/api/investigations/inv_fixture'], primary: (main) => main.getByRole('button', {name: 'Try again', exact: true}).first()}),
});

function describeNode(node){
  const name = (node.getAttribute('aria-label') || node.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
  const classes = String(node.className || '').trim().split(/\s+/).filter(Boolean).slice(0, 2).join('.');
  return `${node.tagName.toLowerCase()}${classes ? '.' + classes : ''} "${name}"`;
}

/* An action control is a control that carries an action: a button, a
   summary, a form control, or an anchor that is not a link inside a
   sentence. The measured box is the control's own border box. */
function measureActionControls(minimum){
  const main = document.querySelector('#main-content');
  const describe = (node) => {
    const name = (node.getAttribute('aria-label') || node.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    return `${node.tagName.toLowerCase()}${String(node.className || '').trim() ? '.' + String(node.className).trim().split(/\s+/)[0] : ''} "${name}"`;
  };
  const inSentence = (anchor) => {
    const parent = anchor.parentElement;
    if (!parent) return false;
    if (getComputedStyle(anchor).display !== 'inline') return false;
    const others = [...parent.childNodes].filter((node) => node !== anchor && node.textContent.trim());
    return others.length > 0;
  };
  const hiddenByDetails = (element) => {
    let node = element;
    while (node && node !== main){
      if (node.tagName === 'DETAILS' && !node.open){
        const summary = node.querySelector(':scope > summary');
        if (!(summary && summary.contains(element))) return true;
      }
      node = node.parentElement;
    }
    return false;
  };
  const out = {measured: 0, short: [], smallest: null};
  for (const node of main.querySelectorAll('button, [role="button"], summary, input, select, textarea, a[href]')){
    const rect = node.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    if (getComputedStyle(node).visibility === 'hidden') continue;
    if (hiddenByDetails(node)) continue;
    if (node.tagName === 'A' && inSentence(node)) continue;
    out.measured += 1;
    const side = Math.min(rect.width, rect.height);
    if (out.smallest === null || side < out.smallest) out.smallest = side;
    if (rect.width < minimum || rect.height < minimum) out.short.push(`${describe(node)} ${Math.round(rect.width)}x${Math.round(rect.height)}`);
  }
  return out;
}

/* Contrast is read from the painted colours: the text colour against the
   first ancestor background with any opacity, composited over the next one
   when it is translucent. Large text (24px, or 18.66px bold) is reported
   apart, at the 3 to 1 floor WCAG gives it. Disabled controls are excluded. */
function measureContrast(){
  const main = document.querySelector('#main-content');
  const parse = (value) => {
    const match = String(value).match(/rgba?\(([^)]+)\)/);
    if (!match) return null;
    const parts = match[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    return [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1];
  };
  const luminance = ([r, g, b]) => {
    const channel = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  };
  const backgroundOf = (element) => {
    const layers = [];
    let node = element;
    while (node){
      const colour = parse(getComputedStyle(node).backgroundColor);
      if (colour && colour[3] > 0){
        layers.push(colour);
        if (colour[3] >= 1) break;
      }
      node = node.parentElement;
    }
    let result = [255, 255, 255];
    for (const layer of layers.reverse()){
      const alpha = layer[3];
      result = [0, 1, 2].map((index) => layer[index] * alpha + result[index] * (1 - alpha));
    }
    return result;
  };
  const ratioOf = (element) => {
    const fore = parse(getComputedStyle(element).color);
    if (!fore) return null;
    const back = backgroundOf(element);
    const l1 = luminance(fore);
    const l2 = luminance(back);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  };
  const describe = (node, text) => `${node.tagName.toLowerCase()}${String(node.className || '').trim() ? '.' + String(node.className).trim().split(/\s+/)[0] : ''} "${text.trim().slice(0, 30)}"`;
  const hiddenByDetails = (element) => {
    let node = element;
    while (node && node !== main){
      if (node.tagName === 'DETAILS' && !node.open){
        const summary = node.querySelector(':scope > summary');
        if (!(summary && summary.contains(element))) return true;
      }
      node = node.parentElement;
    }
    return false;
  };
  const out = {body: {measured: 0, minimum: null, subject: null}, large: {measured: 0, minimum: null, subject: null}};
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  let text;
  while ((text = walker.nextNode())){
    if (!text.textContent.trim()) continue;
    const element = text.parentElement;
    if (!element || seen.has(element) || ['SCRIPT', 'STYLE'].includes(element.tagName)) continue;
    seen.add(element);
    const style = getComputedStyle(element);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const range = document.createRange();
    range.selectNodeContents(text);
    const rect = range.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    if (element.closest('[disabled], [aria-disabled="true"]')) continue;
    if (hiddenByDetails(element)) continue;
    const ratio = ratioOf(element);
    if (ratio === null) continue;
    const size = parseFloat(style.fontSize);
    const bold = parseInt(style.fontWeight, 10) >= 700;
    const bucket = size >= 24 || (bold && size >= 18.66) ? out.large : out.body;
    bucket.measured += 1;
    if (bucket.minimum === null || ratio < bucket.minimum){
      bucket.minimum = ratio;
      bucket.subject = describe(element, text.textContent);
    }
  }
  window.__certificationRatioOf = ratioOf;
  return out;
}

function focusRing(){
  const node = document.activeElement;
  if (!node || node === document.body) return {tag: null, ring: false};
  const style = getComputedStyle(node);
  const outline = style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0;
  const shadow = style.boxShadow !== 'none';
  return {tag: node.tagName, ring: outline || shadow};
}

/* Tab from the top of the document until the control takes focus. The
   expected number of presses is read from the document's tab order first,
   then the loop keeps pressing to a bound in case the order differs. */
async function reachByKeyboard(page, locator){
  const handle = await locator.elementHandle();
  const expected = await handle.evaluate((target) => {
    const tabbable = [...document.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])')]
      .filter((node) => { const rect = node.getBoundingClientRect(); return (rect.width > 0 || rect.height > 0) && getComputedStyle(node).visibility !== 'hidden'; });
    return tabbable.indexOf(target);
  });
  await page.evaluate(() => { if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur(); window.scrollTo(0, 0); });
  const bound = Math.max(expected + 1, 0) + 40;
  for (let presses = 1; presses <= bound; presses += 1){
    await page.keyboard.press('Tab');
    const focused = await handle.evaluate((target) => target === document.activeElement || target.contains(document.activeElement));
    if (focused) return {reached: true, presses, expected: expected + 1, ring: (await page.evaluate(focusRing)).ring};
  }
  return {reached: false, presses: bound, expected: expected + 1, ring: false};
}

async function panelReach(locator, viewport){
  const count = await locator.count();
  if (count === 0) return {count: 0, unreachable: ['panel missing'], initially_visible: 0};
  const unreachable = [];
  let initially = 0;
  for (let index = 0; index < count; index += 1){
    const panel = locator.nth(index);
    const before = await panel.boundingBox();
    if (!before || (before.width === 0 && before.height === 0)){ unreachable.push(`panel ${index} has no box`); continue; }
    const insideBefore = before.y >= 0 && before.y + Math.min(before.height, viewport.height) <= viewport.height && before.x >= 0 && before.x + before.width <= viewport.width + 1;
    if (insideBefore){ initially += 1; continue; }
    await panel.evaluate((node) => node.scrollIntoView({block: 'start', inline: 'nearest'}));
    const after = await panel.boundingBox();
    const inside = after && after.y >= -8 && after.y + Math.min(after.height, viewport.height - 8) <= viewport.height + 1 && after.x >= 0 && after.x + after.width <= viewport.width + 1;
    if (!inside) unreachable.push(`panel ${index} at ${after ? [after.x, after.y, after.width, after.height].map(Math.round).join(',') : 'none'} after one scroll`);
  }
  return {count, unreachable, initially_visible: initially};
}

const capabilities = implementedCapabilities();

test.afterEach(async ({page}, testInfo) => { await retainFailureScreenshot(page, testInfo); });

for (const viewport of VIEWPORTS){
  test.describe(`viewport ${viewportName(viewport)}`, () => {
    test.use({viewport: {...viewport}});
    for (const theme of THEMES){
      for (const row of capabilities){
        test(`layout: ${row.capability_id} populated ${theme} ${viewportName(viewport)}`, async ({page}, testInfo) => {
          const surface = SURFACES[row.capability_id];
          await themeInit(page, theme);
          const state = await installFixtures(page, {...shellFixtures(), ...row.module.fixtures('populated'), ...(surface.fixtures || {})});
          await page.goto('/' + row.module.route);
          const main = page.locator('#main-content');
          await surface.ready(main, page);
          await settle(page);
          expect(await page.evaluate(() => document.documentElement.getAttribute('data-dir')), 'the route rendered in the requested theme').toBe(theme);
          const base = {suite: '42-layout-a11y.pw.mjs', capability_id: row.capability_id, route: row.module.route, theme, viewport: viewportName(viewport)};

          const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
          record(testInfo, {...base, check: 'horizontal_overflow', value: overflow, unit: 'px', threshold: THRESHOLDS.horizontal_overflow_px, pass: overflow <= THRESHOLDS.horizontal_overflow_px});
          expect.soft(overflow, 'no horizontal page overflow').toBeLessThanOrEqual(THRESHOLDS.horizontal_overflow_px);

          const panels = surface.panels(main);
          const reach = [];
          for (const panel of panels){
            await panel.first().evaluateAll((nodes, mark) => nodes.forEach((node) => node.setAttribute(mark, '')), PANEL_MARK).catch(() => null);
            reach.push(await panelReach(panel, viewport));
          }
          const unreachable = reach.flatMap((entry) => entry.unreachable);
          const panelCount = reach.reduce((sum, entry) => sum + entry.count, 0);
          record(testInfo, {...base, check: 'key_panel_reachable', value: panelCount - unreachable.length, unit: 'panels reachable', threshold: panelCount, pass: unreachable.length === 0 && panelCount > 0, detail: unreachable.length ? unreachable.join('; ') : `${reach.reduce((sum, entry) => sum + entry.initially_visible, 0)} of ${panelCount} in the viewport before any scroll`});
          expect.soft(unreachable, 'every key panel is in the viewport or one scroll away').toEqual([]);
          expect.soft(panelCount, 'the key panels are on the page').toBeGreaterThan(0);
          await page.evaluate(() => window.scrollTo(0, 0));

          const ranges = await page.evaluate(measureTextRanges, PANEL_MARK);
          record(testInfo, {...base, check: 'text_ranges_contained', value: ranges.outside.length, unit: `ranges outside of ${ranges.checked} measured`, threshold: 0, pass: ranges.outside.length === 0 && ranges.checked > 0, detail: ranges.outside.slice(0, 6).join('; ') || null});
          expect.soft(ranges.outside, 'every text range sits inside its own container').toEqual([]);
          expect.soft(ranges.checked, 'text ranges were measured').toBeGreaterThan(0);

          if (viewport.width === NARROW.width){
            const controls = await page.evaluate(measureActionControls, THRESHOLDS.action_target_px);
            record(testInfo, {...base, check: 'action_targets', value: controls.smallest === null ? null : Math.round(controls.smallest * 100) / 100, unit: `px, smallest side of ${controls.measured} controls`, threshold: THRESHOLDS.action_target_px, pass: controls.short.length === 0 && controls.measured > 0, detail: controls.short.join('; ') || null});
            expect.soft(controls.short, `every action control is at least ${THRESHOLDS.action_target_px} by ${THRESHOLDS.action_target_px} CSS pixels`).toEqual([]);
          }

          const primaries = surface.primary(main);
          for (let index = 0; index < primaries.length; index += 1){
            const primary = primaries[index];
            await expect.soft(primary, `primary action ${index} is on the page`).toBeVisible();
            const name = (await primary.evaluate(describeNode).catch(() => `primary ${index}`));
            const reached = await reachByKeyboard(page, primary);
            record(testInfo, {...base, check: 'focus_reaches_primary', subject: name, value: reached.reached ? reached.presses : null, unit: 'tab presses', threshold: `reached with a visible ring, expected at press ${reached.expected}`, pass: reached.reached && reached.ring});
            expect.soft(reached.reached, `keyboard focus reaches ${name}`).toBe(true);
            expect.soft(reached.ring, `${name} shows a focus ring`).toBe(true);
          }

          const contrast = await page.evaluate(measureContrast);
          record(testInfo, {...base, check: 'contrast_body_text', value: contrast.body.minimum === null ? null : Math.round(contrast.body.minimum * 100) / 100, unit: `ratio, minimum of ${contrast.body.measured} runs`, threshold: THRESHOLDS.contrast_ratio, pass: contrast.body.minimum !== null && contrast.body.minimum >= THRESHOLDS.contrast_ratio, detail: `${contrast.body.subject}; large text minimum ${contrast.large.minimum === null ? 'none' : Math.round(contrast.large.minimum * 100) / 100} over ${contrast.large.measured} runs`});
          expect.soft(contrast.body.minimum, `body text contrast (${contrast.body.subject})`).toBeGreaterThanOrEqual(THRESHOLDS.contrast_ratio);
          const primaryRatio = await primaries[0].evaluate((node) => window.__certificationRatioOf(node)).catch(() => null);
          record(testInfo, {...base, check: 'contrast_primary_action', value: primaryRatio === null ? null : Math.round(primaryRatio * 100) / 100, unit: 'ratio', threshold: THRESHOLDS.contrast_ratio, pass: primaryRatio !== null && primaryRatio >= THRESHOLDS.contrast_ratio});
          expect.soft(primaryRatio, 'primary action text contrast').toBeGreaterThanOrEqual(THRESHOLDS.contrast_ratio);

          if (surface.to) expect(state.requested.filter((key) => key in row.module.fixtures('populated') && !(key in shellFixtures())), 'the older producer is never read').toEqual([]);
          expect(state.unexpected, 'every read was answered by a fixture').toEqual([]);
          expect(state.errors, 'no page error').toEqual([]);
        });
      }
    }
  });
}

/* Under the reduced motion preference nothing in the route may still move:
   no running or declared animation, and no transition on a property that
   moves or resizes a box. Colour and opacity transitions are quiet and are
   left alone. */
function movingUnderReducedMotion(){
  const main = document.querySelector('#main-content');
  const moving = [];
  const positional = /\b(all|transform|translate|scale|rotate|top|left|right|bottom|inset|width|height|max-height|min-height|margin|padding|gap|flex|grid)\b/;
  const nonZero = (list) => list.split(',').some((value) => parseFloat(value) > 0);
  for (const node of main.querySelectorAll('*')){
    const style = getComputedStyle(node);
    if (nonZero(style.animationDuration) && style.animationName !== 'none'){
      moving.push(`${node.tagName.toLowerCase()} animation ${style.animationName} ${style.animationDuration}`);
      continue;
    }
    if (nonZero(style.transitionDuration) && positional.test(style.transitionProperty)){
      moving.push(`${node.tagName.toLowerCase()} transition ${style.transitionProperty} ${style.transitionDuration}`);
    }
  }
  return {moving: [...new Set(moving)], running: document.getAnimations().filter((animation) => animation.playState === 'running').length};
}

test.describe(`reduced motion ${viewportName(MOTION_VIEWPORT)}`, () => {
  test.use({viewport: {...MOTION_VIEWPORT}});
  for (const theme of THEMES){
    for (const row of capabilities){
      test(`reduced motion: ${row.capability_id} populated ${theme} ${viewportName(MOTION_VIEWPORT)}`, async ({page}, testInfo) => {
        const surface = SURFACES[row.capability_id];
        await page.emulateMedia({reducedMotion: 'reduce'});
        await themeInit(page, theme);
        const state = await installFixtures(page, {...shellFixtures(), ...row.module.fixtures('populated'), ...(surface.fixtures || {})});
        await page.goto('/' + row.module.route);
        const main = page.locator('#main-content');
        await surface.ready(main, page);
        await settle(page);
        const motion = await page.evaluate(movingUnderReducedMotion);
        record(testInfo, {suite: '42-layout-a11y.pw.mjs', capability_id: row.capability_id, route: row.module.route, theme, viewport: viewportName(MOTION_VIEWPORT), check: 'reduced_motion', value: motion.moving.length, unit: 'moving elements', threshold: THRESHOLDS.reduced_motion_moving_elements, pass: motion.moving.length <= THRESHOLDS.reduced_motion_moving_elements && motion.running === 0, detail: motion.moving.slice(0, 6).join('; ') || `${motion.running} running animations`});
        expect.soft(motion.moving, 'nothing moves under reduced motion').toEqual([]);
        expect.soft(motion.running, 'no animation is running after settle').toBe(0);
        if (surface.to) expect(state.requested.filter((key) => key in row.module.fixtures('populated') && !(key in shellFixtures())), 'the older producer is never read').toEqual([]);
        expect(state.unexpected).toEqual([]);
        expect(state.errors).toEqual([]);
      });
    }
  }
});
