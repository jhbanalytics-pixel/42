/* The capability route state matrix.

   One browser case per manifest row and required state, generated from
   app/docs/capability-journeys.json at load time. A string state drives the
   row's route with a fixture built from the producer's real payload shape and
   asserts the observable result the manifest names. An object state asserts
   its inapplicable reason is a sentence and opens no page. A state the
   producer cannot supply runs as an expected failure carrying the gap, so it
   is counted and never hidden. The reporter under support/ turns the titles,
   which are always `<capability_id> <state>`, into the emitted state table. */
import {expect, test} from '@playwright/test';
import {existsSync, readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {CAPABILITY_FIXTURES} from './fixtures/42-capability-routes/index.js';
import {DISAGREEMENT as DESK_DISAGREEMENT, THIN_FLOOR as DESK_THIN_FLOOR, RUN} from './fixtures/42-capability-routes/_desk.js';
import {DISAGREEMENT as QUESTION_DISAGREEMENT, REQUEST_ID, THIN_FLOOR as QUESTION_THIN_FLOOR} from './fixtures/42-capability-routes/_question.js';
import {HELD_GAP} from './fixtures/42-capability-routes/fieldwork.js';
import {DESK_AMBER, HANDLE, LISTEN_QUOTE, LISTEN_TERM, NETWORK_TOPIC, UNCODED_SERVER_FAILURE, UNQUOTED_MENTION, STATUS_CODE, deskPayload, mentionsPayload, voicesPayload} from './fixtures/42-capability-routes/creators_network_language.js';
import {DESK_FAILURE} from './fixtures/42-capability-routes/_desk.js';
import {ENDPOINT as QUESTION_ENDPOINT, ROUTE as QUESTION_ROUTE} from './fixtures/42-capability-routes/_question.js';
import {ARTIFACT_TITLE, THIN_GAP} from './fixtures/42-capability-routes/investigation_build_review.js';
import {installFixtures, readManifest, shellFixtures} from './support/capability-harness.mjs';

/* Quiet register, 23 Sept 2026: the run window 2026-08-18 to 2026-08-24 as
   package 2.0.20 writes it for a reader. */
const RUN_WINDOW_READ = '18 to 24 Aug 2026';

const manifest = readManifest();
const AGE = /\b\d+ (minutes?|hours?|days?)\b|\b\d+[mhd] ago\b/;
const API_ROOT = fileURLToPath(new URL('../../../src/api/', import.meta.url));

/* A refusal names the producer symbol whose limit it claims; the symbol has to
   exist in the file the entry names, so a browser reader can hold the claim
   against the source exactly as the unit twin does. */
function producerText(entryProducer){
  const files = entryProducer.match(/src\/api\/[a-z_]+\.py/g) || [];
  return files.map((file) => readFileSync(API_ROOT + file.slice('src/api/'.length), 'utf8')).join('\n');
}

/* Every chart on the desk routes is the package proof ribbon: a figure whose
   caption names what is charted, whose description states the window and the
   normalization, and whose family controls name the sources. The comparator
   is the axis every ribbon on the page shares and the other signals beside
   it. Each of the five is read from the DOM, not from the fixture. */
async function expectChartDisclosure(main, {minimum = 1, comparator}){
  const figures = main.locator('figure.proof-ribbon');
  const count = await figures.count();
  expect(count, 'at least one chart on the route').toBeGreaterThanOrEqual(minimum);
  for (let index = 0; index < count; index += 1){
    const figure = figures.nth(index);
    await expect(figure.locator('figcaption'), 'question: the caption names what the chart plots').not.toHaveText(/^\s*$/);
    /* Quiet register, 23 Sept 2026: package 2.0.20 sets the ribbon description
       as one sentence instead of four labelled fragments. The window now reads
       "from 18 to 24 Aug 2026", the run's 2026-08-18 to 2026-08-24 written the
       way a reader writes a date, and the unit follows "scaled as" where the
       "Normalization" label stood. Both facts are still required. */
    await expect(figure.locator('.proof-ribbon__description'), 'time window').toContainText(`from ${RUN_WINDOW_READ}`);
    /* Quiet register, 23 Sept 2026: package 2.0.21 writes the unit as what
       the drawing shows, "showing change on a scale of 0 to 1", where it
       wrote "scaled as". The unit is still required after the interval. */
    await expect(figure.locator('.proof-ribbon__description'), 'unit').toContainText(', showing ');
    await expect(figure.locator('[aria-label="Independent source families"] button').first(), 'source').toBeVisible();
  }
  await comparator();
}

async function expectStateFrame(main, {title, reason}){
  await expect(main.getByText(title, {exact: false}).first()).toBeVisible();
  if (reason) await expect(main).toContainText(reason);
}

const desk = {
  async populatedChart(main, comparator){
    await expectChartDisclosure(main, {comparator});
  },
  async loading(main){
    await expect(main.getByText('Reading the completed run')).toBeVisible();
    await expect(main.locator('figure.proof-ribbon')).toHaveCount(0);
  },
  async empty(main, action){
    await expectStateFrame(main, {title: 'No signals this run'});
    /* Round three, 24 Sept 2026: the no discovery body says the run finished
       without a signal strong enough to show, in a strategist's words rather
       than the engine's "admitted" and "dynamic". Same frame, same assertion. */
    await expect(main).toContainText('The run finished without a signal strong enough to show.');
    await expect(main).not.toContainText(/unknown/i);
    await expect(main.getByRole('button', {name: action})).toBeEnabled();
  },
  async stale(main){
    await expectStateFrame(main, {title: 'Held until the next run'});
    await expect(main).toContainText(/\d+ (days|hours|minutes) old/);
    await expect(main.locator('figure.proof-ribbon')).toHaveCount(0);
  },
  async failed(main){
    await expectStateFrame(main, {title: 'Could not read the run', reason: 'desk_read_failed'});
    await expect(main.getByRole('button', {name: 'Try again'})).toBeVisible();
    await expect(main.locator('figure.proof-ribbon')).toHaveCount(0);
  },
};

const CASES = {
  today_briefing: {
    async populated(main){
      await expect(main.locator('.instrument-briefing[data-briefing-state="ready"]')).toBeVisible();
      /* Quiet register, 23 Sept 2026: package 2.0.21 names the receipts
         chapter plainly, "Key evidence" where it said "Decisive receipts",
         and drops the "ready to review" mark; the ready state is still read
         off data-briefing-state above. */
      await expect(main).toContainText('Key evidence');
      await expect(main).not.toContainText('Decisive receipts');
      await expect(main).not.toContainText(/ready to review/i);
      await expect(main.locator(`[data-run-date="${RUN.signal_date}"]`)).toHaveCount(1);
      /* Quiet register, 23 Sept 2026: the run date is read, so it is written
         as a reader says it; the ISO date stays on data-run-date above. */
      await expect(main).toContainText('Run 12 Sept 2026.');
      await expect(main).not.toContainText(`Run ${RUN.signal_date}`);
      await desk.populatedChart(main, async () => {
        await expect(main.getByRole('button', {name: /^Compare /}).first()).toBeVisible();
      });
      const open = main.getByRole('link', {name: /^Open Balcony mix/});
      await expect(open).toBeVisible();
      await expect(open).toHaveAttribute('href', /^#\/topic\/sig_/);
    },
    /* Quiet register, 23 Sept 2026: package 2.0.21 names the receipts chapter
       "Key evidence", so its absence is read under that name. */
    async loading(main){ await desk.loading(main); await expect(main).not.toContainText('Key evidence'); },
    async empty(main){ await desk.empty(main, 'Open Discover'); },
    async thin(main){
      await expect(main).toContainText(/thin evidence, hypothesis only/i);
      await expect(main).toContainText(DESK_THIN_FLOOR);
      await expect(main).toContainText('A response is withheld until the evidence is ready.');
    },
    async stale(main){ await desk.stale(main); },
    async failed(main){ await desk.failed(main); await expect(main.getByRole('link', {name: /^Open /})).toHaveCount(0); },
    async held(main){
      await expectStateFrame(main, {title: 'Held until the evidence authority is checked', reason: 'evidence_authority_unchecked'});
      await expect(main).toContainText('Source independence has not been checked by the engine.');
      /* Quiet register, 23 Sept 2026: package 2.0.21 names the receipts
         chapter "Key evidence", so its absence is read under that name. */
      await expect(main).not.toContainText('Key evidence');
      await expect(main.getByRole('link', {name: /^Open Balcony mix/})).toHaveCount(0);
    },
  },
  discovery_exploration: {
    async populated(main, page, region){
      await expect(main.getByRole('heading', {name: 'Discover', exact: true})).toBeVisible();
      /* Quiet register, 23 Sept 2026: the count is in plain words, without
         the engine's "admitted". */
      await expect(main).toContainText('3 of 3 signals shown');
      await expect(main).not.toContainText('admitted signals');
      /* Quiet register, 23 Sept 2026: package 2.0.21 says each Discover count
         in words beside its figure, "2 source families, 2 qualifying records",
         where it wrote "2 families \u00b7 2 receipts". */
      await expect(main).toContainText('2 source families, 2 qualifying records');
      await expect(main).toContainText(/ready/i);
      /* Quiet register, 23 Sept 2026: the run reference is the bare id a
         reader copies; its summary already names it as the run's. */
      await expect(main.locator('.explore-folio__run-reference code')).toHaveText(RUN.run_id);
      await expect(main.locator('.explore-folio__run')).toHaveText('Released run of 12 Sept 2026');
      await expect(main.locator('.explore-folio__run-reference summary')).toHaveText('Run reference');
      const open = main.getByRole('link', {name: /^Open topic story/});
      await expect(open).toBeVisible();
      await expect(open).toHaveAttribute('href', /^#\/topic\/sig_[0-9a-f]{64}\?region=za$/);
      /* Quiet register, 23 Sept 2026: opening the run reference reveals the id
         on its own line and moves no control beside it. The story link keeps
         its place at the desktop and the phone width alike. Each box is
         read once every animation on the page has finished, because the
         route's entry move is still running after a resize (an endless
         animation is left out, since it never finishes); the check allows
         1px, far under the 159px the old layout moved it. */
      const summary = main.locator('.explore-folio__run-reference summary');
      const settledBox = async () => {
        await page.evaluate(() => Promise.all(document.getAnimations()
          .filter((animation) => animation.effect?.getComputedTiming().endTime !== Infinity)
          .map((animation) => animation.finished.catch(() => null))));
        return open.boundingBox();
      };
      for (const width of [1280, 390]){
        await page.setViewportSize({width, height: 900});
        const closed = await settledBox();
        await summary.click();
        await expect(main.locator('.explore-folio__run-reference code')).toBeVisible();
        const opened = await settledBox();
        expect(Math.abs(opened.x - closed.x), `the story link x at ${width}`).toBeLessThanOrEqual(1);
        expect(Math.abs(opened.y - closed.y), `the story link y at ${width}`).toBeLessThanOrEqual(1);
        await summary.click();
        await expect(main.locator('.explore-folio__run-reference code')).toBeHidden();
      }
      await page.setViewportSize({width: 1280, height: 900});
      await main.getByRole('button', {name: 'Open evidence', exact: true}).first().click();
      await expectChartDisclosure(main, {comparator: async () => {
        await expect(main).toContainText('3 of 3 signals shown');
      }});
    },
    async loading(main){ await desk.loading(main); await expect(main).not.toContainText('signals shown'); },
    async empty(main){ await desk.empty(main, 'Browse evidence'); },
    async thin(main, page){
      await expect(main).toContainText(/thin/i);
      /* Quiet register, 23 Sept 2026: package 2.0.21 says each Discover count
         in words beside its figure, as above. */
      await expect(main).toContainText('1 source family, 1 qualifying record');
      await main.getByRole('button', {name: 'Open evidence', exact: true}).first().click();
      await expect(page.getByText(DESK_THIN_FLOOR).first()).toBeVisible();
    },
    async stale(main){ await desk.stale(main); },
    async failed(main){ await desk.failed(main); await expect(main.getByRole('link', {name: /^Open topic story/})).toHaveCount(0); },
    async held(main){
      await expectStateFrame(main, {title: 'Held until the evidence authority is checked', reason: 'evidence_authority_unchecked'});
      await expect(main).toContainText('Source independence has not been checked by the engine.');
      await expect(main.getByRole('link', {name: /^Open topic story/})).toHaveCount(0);
    },
  },
  compare: {
    async populated(main){
      /* Quiet register, 23 Sept 2026: package 2.0.20 writes the comparison
         axis through readableDates, so the run window reads as a reader
         writes it. The axis must still carry the run's own window. */
      await expect(main.locator('[data-comparison-axis]')).toContainText(RUN_WINDOW_READ);
      await expect(main.locator('article.comparison-instrument__signal')).toHaveCount(3);
      await desk.populatedChart(main, async () => {
        await expect(main.locator('[data-comparison-axis]')).toBeVisible();
        expect(await main.locator('article.comparison-instrument__signal').count()).toBeGreaterThanOrEqual(2);
      });
      await expect(main.getByRole('button', {name: 'Select Balcony mix', exact: true})).toBeEnabled();
    },
    async loading(main){ await desk.loading(main); },
    async empty(main){ await desk.empty(main, 'Open Discover'); },
    async thin(main){
      /* Quiet register, 23 Sept 2026: package 2.0.20 opens the ribbon
         sentence with the state ("Thin evidence") where it wrote "State: Thin". */
      await expect(main.locator('.proof-ribbon__description').first()).toContainText('Thin evidence');
      await expect(main).toContainText(DESK_THIN_FLOOR);
      await expectChartDisclosure(main, {minimum: 2, comparator: async () => { await expect(main.locator('[data-comparison-axis]')).toBeVisible(); }});
    },
    async stale(main){ await desk.stale(main); await expect(main.getByRole('button', {name: /^Select /})).toHaveCount(0); },
    async contradictory(main){
      /* Quiet register, 23 Sept 2026: package 2.0.20 opens the ribbon sentence
         with the state ("Contradictory evidence") where it wrote "State:
         Contradictory". */
      await expect(main.locator('.proof-ribbon__description').first()).toContainText('Contradictory evidence');
      await expect(main).toContainText(DESK_DISAGREEMENT);
      /* Quiet register, 23 Sept 2026: package 2.0.20 names each family's
         direction with the ledger's words, so the accessible name says
         "opposes" and "supports" where it said "opposing" and "supporting".
         Both directions must still be named on a visible control. */
      await expect(main.getByRole('button', {name: /opposes/}).first()).toBeVisible();
      await expect(main.getByRole('button', {name: /supports/}).first()).toBeVisible();
    },
    async failed(main){ await desk.failed(main); await expect(main.getByRole('button', {name: /^Select /})).toHaveCount(0); },
    async held(main){
      await expectStateFrame(main, {title: 'Held until the evidence authority is checked', reason: 'evidence_authority_unchecked'});
      await expect(main).toContainText('admitted 2 signals');
      await expect(main).not.toContainText('Comparison needs two');
      await expect(main.getByRole('button', {name: /^Select /})).toHaveCount(0);
    },
  },
  sources_evidence: {
    async populated(main, page){
      const citation = main.getByRole('button', {name: /^Open R1:/});
      await expect(citation).toBeEnabled();
      await citation.click();
      const reply = main.locator('.general-reply');
      await expect(reply).toContainText('Source record');
      await expect(reply).toContainText('Content record \u00b7 South Africa');
      await expect(reply).toContainText('A repair tutorial shared with the community.');
      await expect(reply).toContainText('Published: 3 Sept 2026');
      await expect(reply.getByRole('link', {name: 'View original source'})).toHaveAttribute('href', 'https://example.test/post_1');
      await expect(reply).toContainText('post_1');
      await expect(reply).toContainText('R1 \u00b7 Instagram');
    },
    async loading(main){ await expect(main.getByText('Reading the stored question.')).toBeVisible(); await expect(main.getByRole('button', {name: /^Open R/})).toHaveCount(0); },
    async empty(main){
      await expect(main).toContainText('No admissible evidence matched this request.');
      await expect(main.getByRole('button', {name: /^Open R/})).toHaveCount(0);
      await expect(main).not.toContainText(/unknown/i);
    },
    async thin(main){ await expect(main.locator('.general-status[data-status="partial"]')).toHaveText('Partial'); await expect(main).toContainText(QUESTION_THIN_FLOOR); },
    async contradictory(main){
      await expect(main).toContainText(QUESTION_DISAGREEMENT);
      await expect(main).toContainText('R1 \u00b7 Instagram');
      await expect(main).toContainText('R2 \u00b7 News');
    },
    async failed(main){
      await expect(main.getByRole('alert')).toContainText('The stored question could not be verified.');
      await expect(main.getByRole('alert')).toContainText('question_store_unavailable');
      await expect(main.getByRole('button', {name: /^Open R/})).toHaveCount(0);
    },
    async held(main){
      await expect(main).toContainText('Held request.');
      await expect(main).toContainText('the metering record for this answer failed to persist');
      await expect(main.getByRole('button', {name: /^Open R1:/})).toBeDisabled();
    },
  },
  creators_network_language: {
    async populated(main){
      await expect(main.getByRole('heading', {name: '@maker_one'})).toBeVisible();
      await expect(main).toContainText('Posts recorded');
      await expect(main).toContainText('2 returned');
      await expect(main.getByRole('region', {name: 'Recorded engagement over time'})).toBeVisible();
      await expect(main.getByRole('link', {name: 'Repair culture'})).toHaveAttribute('href', '#/topic/repair_culture');
      await expect(main.getByRole('link', {name: 'View original post'}).first()).toBeVisible();
    },
    async loading(main){
      await expect(main.getByText('Reading the creator profile')).toBeVisible();
      await expect(main).not.toContainText('Nothing to show for this signal');
      await expect(main.getByRole('heading', {name: '@maker_one'})).toHaveCount(0);
    },
    async failed(main){
      /* creator.py raises inside the handler, so the read fails with a status
         and no coded detail; the page carries the status, not an invented
         producer code. */
      await expectStateFrame(main, {title: 'The profile could not load', reason: STATUS_CODE});
      await expect(main).toContainText('This does not establish that the handle has no activity.');
      await expect(main, 'no invented producer code').not.toContainText('creator_store_unavailable');
      /* The framework answers an uncaught error in plain text, so there is no
         JSON to read a code from; a code this page supplied is not named. */
      await expect(main, 'no code the producer did not send').not.toContainText('desk_unreachable');
      await expect(main, 'no producer is credited with a reason').not.toContainText('Reason the producer gave');
      await expect(main.getByRole('link', {name: 'Repair culture'})).toHaveCount(0);
    },
  },
  /* Ask redesign, 23 Sept 2026: the status is a word at the end of the answer's
     meta line ("Complete", "Partial") rather than a "Response complete"
     header, record dates read without a leading zero, and the follow-up is
     the "Ask a follow-up" field under the answer rather than a button. */
  questions_followups: {
    async populated(main){
      await expect(main.locator('.general-status[data-status="complete"]')).toHaveText('Complete');
      await expect(main).toContainText('Repair tutorials recur in the sampled posts.');
      await expect(main).toContainText('Source records');
      await expect(main).toContainText('Limits of this read');
      await expect(main).toContainText('What is still needed');
      await expect(main.locator('details.general-run')).toContainText(REQUEST_ID);
      await expect(main.getByRole('textbox', {name: 'Ask a follow-up', exact: true})).toBeEnabled();
    },
    async loading(main){ await expect(main.getByText('Reading the stored question.')).toBeVisible(); await expect(main).not.toContainText('Stored response'); },
    async empty(main){
      await expect(main).toContainText(/reply unavailable/i);
      await expect(main).toContainText('No admissible evidence matched this request.');
      await expect(main).not.toContainText(/unknown/i);
    },
    async thin(main){ await expect(main.locator('.general-status[data-status="partial"]')).toHaveText('Partial'); await expect(main).toContainText(QUESTION_THIN_FLOOR); },
    async stale(main){
      await expect(main).toContainText(/stale/i);
      await expect(main).toContainText(AGE);
    },
    async contradictory(main){ await expect(main).toContainText(QUESTION_DISAGREEMENT); await expect(main).toContainText('R2 \u00b7 News'); },
    async failed(main){
      await expect(main.getByRole('alert')).toContainText('question_store_unavailable');
      /* Round three, 24 Sept 2026: the code is present for support but sits
         under a closed Details line, so the reader sees the plain sentence. */
      await expect(main.getByRole('alert').getByText('The stored question could not be verified.', {exact: true})).toBeVisible();
      await expect(main.getByRole('alert').locator('details.workspace-reason:not([open]) code')).toHaveText('question_store_unavailable');
      await expect(main.getByText('question_store_unavailable', {exact: true})).toBeHidden();
      await expect(main.getByRole('textbox', {name: 'Ask a follow-up', exact: true})).toHaveCount(0);
    },
    async held(main){
      await expect(main).toContainText('Held request.');
      await expect(main).toContainText('the metering record for this answer failed to persist');
      await expect(main.getByRole('textbox', {name: 'Ask a follow-up', exact: true})).toHaveCount(0);
    },
  },
  fieldwork: {
    async populated(main){
      await expect(main.getByRole('heading', {name: 'Source and research operations'})).toBeVisible();
      await expect(main).toContainText('Complete 1');
      await expect(main).toContainText('Retained question operation');
      await expect(main).toContainText(/unchecked/i);
      await expect(main.getByRole('link', {name: 'Open question', exact: true})).toHaveAttribute('href', '#/console?work=ask&request=' + REQUEST_ID);
      await expectCollectedSeparateFromReleased(main);
      await expect(main).toContainText('Route status');
    },
    async loading(main){ await expect(main.getByText('Preparing fieldwork')).toBeVisible(); await expect(main).not.toContainText('Retained question operation'); },
    async empty(main){
      await expect(main).toContainText('No fieldwork has been planned yet.');
      await expect(main.locator('.fieldwork-strip li').first()).toContainText('Total 0');
      /* The operations region prints its measured zero; the inventory below
         may carry the package's own unknown cells for unmeasured sources. */
      await expect(main.locator('.fieldwork-strip')).not.toContainText(/unknown/i);
      await expect(main.locator('.fieldwork-state')).not.toContainText(/unknown/i);
    },
    async stale(main){
      await expect(main.getByRole('status')).toContainText(/stale/i);
      await expect(main.getByRole('status')).toContainText(AGE);
    },
    async failed(main){
      await expect(main.getByRole('alert')).toContainText('Fieldwork could not be read.');
      await expect(main.getByRole('alert')).toContainText('fieldwork_read_failed');
      await expect(main.getByRole('link', {name: 'Open question', exact: true})).toHaveCount(0);
    },
    async held(main){
      await expect(main).toContainText('Held 1');
      /* Quiet register, 23 Sept 2026: the lane is named in words, not by its key. */
      await expect(main).toContainText('The Ogilvy funded lane is exhausted for this window.');
      /* Round three, 24 Sept 2026: this pinned the row's gap as the producer
         wrote it, lane key and all, so the reader saw ogilvy_funded under the
         note that names the lane in words. The gap now reads with the lane
         named the same way, and the key is asserted absent from the page. */
      await expect(main).toContainText(HELD_GAP.replace('the ogilvy_funded lane', 'the Ogilvy funded lane'));
      await expect(main).not.toContainText('ogilvy_funded');
      await expect(main.getByRole('link', {name: 'Open question', exact: true})).toHaveCount(0);
    },
  },
  coverage_source_lab: {
    async populated(main){
      await expect(main.getByRole('heading', {name: 'Source Lab', exact: true})).toBeVisible();
      await expect(main).toContainText('Verified snapshot');
      await expect(main).toContainText('cccccccccccc');
      await expect(main).toContainText('Ogilvy funded lane');
      await expect(main).toContainText('Catalogue availability is not activation.');
      await expect(main).toContainText('Attribution');
      /* Quiet register, 23 Sept 2026: states are written in sentence case. */
      await expect(main).toContainText('Complete');
      await expectCollectedSeparateFromReleased(main);
      const evidence = main.getByText('Open evidence', {exact: true});
      expect(await evidence.count()).toBeGreaterThan(0);
      await evidence.first().click();
      await expect(main).toContainText('Route status');
      await expect(main).toContainText('Downstream use');
      await expect(main).toContainText('evidence_receipts');
    },
    async loading(main){ await expect(main.getByText('Loading Source Lab inventory and funding controls.')).toBeVisible(); await expect(main).not.toContainText('Documented sources'); },
    async empty(main){
      await expect(main).toContainText('No sources are documented in this catalogue yet.');
      await expect(main).not.toContainText(/unknown/i);
    },
    async failed(main){
      await expect(main.getByRole('alert')).toContainText('Source Lab could not verify a complete snapshot.');
      await expect(main.getByRole('alert')).toContainText('catalog_unverified');
      await expect(main.getByText('Open evidence', {exact: true})).toHaveCount(0);
    },
  },
  investigation_build_review: {
    async populated(main){
      await expect(main.getByRole('heading', {name: 'Evidence Room', exact: true})).toBeVisible();
      await expect(main).toContainText('Repair tutorials recur in the sampled posts.');
      await expect(main).toContainText('Approved');
      await expect(main).toContainText(ARTIFACT_TITLE);
      await expect(main).toContainText('None recorded.');
      await expect(main.getByText('Open evidence', {exact: true})).toHaveCount(1);
    },
    /* Quiet register, 23 Sept 2026: the claim list heading is now "Claims",
       so loading asserts that heading is absent rather than the old words,
       which no longer appear on any branch and could not fail. */
    async loading(main){ await expect(main.getByText('Loading the investigation plan, claims and proof.')).toBeVisible(); await expect(main.getByRole('heading', {name: 'Claims', exact: true})).toHaveCount(0); },
    async empty(main){
      await expect(main).toContainText('No claims gathered yet.');
      await expect(main.locator('[data-evidence-state="empty"]')).toHaveCount(1);
      await expect(main).not.toContainText(/unknown/i);
    },
    async thin(main){
      await expect(main.locator('[data-evidence-state="thin"]')).toHaveCount(1);
      await main.getByText('Open evidence', {exact: true}).click();
      await expect(main).toContainText('Missing evidence: ' + THIN_GAP);
    },
    async stale(main){ await expect(main.getByRole('alert')).toContainText(/stale/i); await expect(main.getByRole('alert')).toContainText(AGE); },
    async contradictory(main){
      await expect(main.locator('[data-evidence-state="contradictory"]')).toHaveCount(1);
      await expect(main).toContainText('Challenge evidence: ev_2_news');
    },
    async failed(main){
      await expect(main.getByRole('alert')).toContainText('The investigation is unavailable.');
      await expect(main.getByRole('alert')).toContainText('investigation_store_unavailable');
      await expect(main.getByText('Open evidence', {exact: true})).toHaveCount(0);
    },
    async held(main){
      await expect(main).toContainText('Held for human review');
      await expect(main).toContainText('no decision has been recorded');
      await expect(main).not.toContainText('No decision resource exists.');
      await expect(main.locator('.evidence-room-grid')).not.toContainText('Approved');
    },
  },
};

/* Collected volume and promoted signals are two figures with two labels.
   The snapshot prints the observation count as collected volume, and a
   separately labelled released signals figure that stays unmeasured until a
   producer counts it. Neither figure may borrow the other's label. */
async function expectCollectedSeparateFromReleased(main){
  const reading = main.locator('.source-snapshot__row').first();
  await expect(reading.locator('.source-snapshot__value')).toContainText(/\d+ observations/);
  await reading.locator('summary').click();
  await expect(reading).toContainText('Released signals from this route: Unmeasured');
  await expect(reading.locator('.source-snapshot__value')).not.toContainText(/signals/i);
}

/* The entries beside the primary route of a capability. The manifest job for
   the creator row names three entries, so the network graph and the scoped
   listen read are driven here in the same states, against the payloads their
   own producers send. A state an entry's producer cannot supply carries its
   reason in the fixture module and is asserted as a sentence, never skipped. */
const ENTRY_CASES = {
  creators_network_language: {
    network: {
      async populated(main){
        /* The desk answer behind this read carries the amber stamp, which is
           what a daily pipeline serves for most of the day. The graph is the
           run's own answer and is drawn under it; a stamp that switched the
           drawn graph off would blank the route here. */
        await expect(main.locator('.network-page[data-network-state="ready"]')).toBeVisible();
        await expect(main.locator('.network-page[data-network-state="stale"]'),
          'an amber ingest stamp does not withhold a graph that has links').toHaveCount(0);
        const topic = main.locator('.net-node--topic').filter({hasText: NETWORK_TOPIC});
        await expect(topic).toHaveCount(1);
        await expect(topic).toHaveAttribute('href', '#/topic/repair_culture?region=za');
        const voice = main.locator('.net-node--creator').filter({hasText: '@maker_one'});
        await expect(voice).toHaveAttribute('href', '#/creator/maker_one');
        /* Demo polish, 2 October 2026: the voice line names the record
           market in words ("South Africa · ...") rather than "Recorded ZA",
           and the count line says what is shown once ("Showing 2 topics and
           2 voices.") instead of repeating it as records and matches. Both
           are still matched, the count line now in full. */
        await expect(voice, 'the record market, not an account identity').toContainText('South Africa · ');
        await expect(main.locator('[data-network-coverage]')).toContainText('Showing 2 topics and 2 voices.');
        await main.getByText('How these links are matched', {exact: true}).click();
        await expect(main.locator('[data-network-coverage]')).toContainText('Received 2 topic records and 2 voice records');
        await expect(main.getByRole('heading', {name: 'Recorded links across scenes'})).toBeVisible();
        await expect(main, 'no invented demographic authority').not.toContainText(/gen ?z|millennial|boomer|\byouth\b/i);
      },
      async loading(main){
        await expect(main.getByText('Preparing the network')).toBeVisible();
        await expect(main.locator('.net-node')).toHaveCount(0);
        await expect(main.locator('[data-network-coverage]')).toHaveCount(0);
      },
      async empty(main, page){
        await expect(main.locator('.network-page[data-network-state="empty"]')).toBeVisible();
        await expect(main.getByText('No matching links in the returned records', {exact: true})).toBeVisible();
        for (const fact of ['No links drawn', '2 topics on the board', '2 voices on the board']){
          await expect(main, 'a measured zero states its counts').toContainText(fact);
        }
        await expect(main.locator('.net-node')).toHaveCount(0);
        /* The market action is a live control: it moves the shell scope and
           the page comes back reading the other market. Where it lands is
           asserted, so a switch that errors cannot pass as a measured zero. */
        const other = main.getByRole('button', {name: 'See Nigeria instead →'});
        await expect(other).toBeEnabled();
        await other.click();
        /* Quiet register, 23 Sept 2026: the market is named in the page's one
           sentence lead now that the eyebrow is gone. */
        await expect(main.locator('.network-page .net-lead')).toContainText('Nigeria');
        await expect(main.locator('.network-page[data-network-state="empty"]'),
          'the switch lands on a read of Nigeria, not on an error').toBeVisible();
        await expect(main.getByRole('alert')).toHaveCount(0);
        expect(await page.evaluate(() => localStorage.getItem('pulse-region'))).toBe('NG');
      },
      async stale(main){
        /* The run carried no pairing and the desk stamp dates the wait, so
           this is the empty branch with a date on it, the same branch the
           three desk routes reach. Nothing is withheld here: there was
           nothing to draw before the stamp was read. */
        await expect(main.locator('.network-page[data-network-state="stale"]')).toBeVisible();
        await expect(main.getByText('Held until the next run')).toBeVisible();
        await expect(main, 'the run is dated from the freshness stamp the desk sends')
          .toContainText(/\d+ (days?|hours?|minutes?) old/);
        await expect(main.locator('.net-node')).toHaveCount(0);
        await expect(main.locator('[data-network-coverage]'),
          'a run with nothing to draw counts nothing').toHaveCount(0);
        await expect(main.getByRole('button', {name: 'Try again', exact: true})).toBeVisible();
      },
      async failed(main){
        const alert = main.getByRole('alert');
        await expect(alert).toContainText('The network could not load.');
        /* The desk answer sends no coded detail, so the page reads the status
           and says so; it does not dress a status up as a producer reason. */
        await expect(alert, 'a status is shown, not a producer reason').toContainText('the read failed with status:');
        await expect(alert).toContainText(STATUS_CODE);
        await expect(alert, 'no invented producer code').not.toContainText('voices_store_unavailable');
        /* The voices read failed with the framework's plain text body, which
           carries no code; no code is printed under the producer's name. */
        await expect(alert, 'no code the producer did not send').not.toContainText('desk_unreachable');
        await expect(alert, 'no producer is credited with a reason').not.toContainText('Reason the producer gave');
        await expect(alert).toContainText('This does not establish that the market holds none.');
        await expect(main.getByRole('button', {name: 'Try again', exact: true})).toBeEnabled();
        await expect(main.locator('.net-node')).toHaveCount(0);
        await expect(main.locator('[data-network-coverage]')).toHaveCount(0);
        /* Round three, 24 Sept 2026: the sentence stays in view and the
           status waits under a closed Details line. */
        await expect(alert.getByText('This does not establish that the market holds none.', {exact: false})).toBeVisible();
        await expectClosedReason(alert, LABEL.status, STATUS_CODE);
      },
    },
    listen: {
      async populated(main){
        await expect(main.getByRole('textbox', {name: 'Filter mentions'}), 'the scoped term survives the hash').toHaveValue(LISTEN_TERM);
        /* The shell remembers NG, the hash carries ZA, and the read market is
           the hash's, so a pressed ZA proves the hash beat the stored value. */
        /* Demo polish, 2 October 2026: Listen's market tabs name the market
           in words, so South Africa and Nigeria rather than ZA and NG. */
        await expect(main.getByRole('button', {name: 'South Africa', exact: true}), 'the hash market is the read market').toHaveAttribute('aria-pressed', 'true');
        await expect(main.getByRole('button', {name: 'Nigeria', exact: true}), 'the stored market did not win').toHaveAttribute('aria-pressed', 'false');
        const rows = main.locator('.listen-mention-row');
        await expect(rows).toHaveCount(3);
        await expect(rows.first()).toContainText(LISTEN_QUOTE);
        /* Quiet register, 23 Sept 2026: the age is written in words, "12 days
           ago", not the compact "12d" that sat in 11px mono. One takes the
           singular and every other count the plural, so "1 days" fails. */
        await expect(rows.first(), 'every quote carries its age').toContainText(/reddit\.com · (?:1 (?:minute|hour|day)|(?:[2-9]|[1-9]\d+) (?:minutes|hours|days)) ago/);
        await expect(rows.first().getByRole('link', {name: /Read the source/})).toHaveAttribute('href', 'https://example.test/mention_1');
        /* A row the producer sent with no url carries no source link; it says
           the source is not available rather than routing to a search page. */
        const unlinked = rows.filter({hasText: UNQUOTED_MENTION});
        await expect(unlinked).toHaveCount(1);
        await expect(unlinked.getByRole('link', {name: /Read the source/})).toHaveCount(0);
        await expect(unlinked).toContainText('Source not available');
        /* The producer serves one page (has_more is false and no cursor is
           forwarded), so no load-more control is drawn to pretend otherwise. */
        await expect(main.getByRole('button', {name: /Load more/})).toHaveCount(0);
        await expect(main, 'no invented demographic authority').not.toContainText(/gen ?z|millennial|boomer|\byouth\b/i);
      },
      async loading(main){
        await expect(main.getByText('Tuning into South Africa')).toBeVisible();
        await expect(main.locator('.listen-mention-row')).toHaveCount(0);
      },
      async empty(main){
        await expect(main.getByText('Nothing heard in South Africa yet', {exact: true})).toBeVisible();
        await expect(main, 'a measured zero states what was loaded').toContainText('No mentions loaded');
        await expect(main.locator('.listen-mention-row')).toHaveCount(0);
        await expect(main.getByRole('button', {name: 'Try Nigeria instead →'})).toBeEnabled();
      },
      async failed(main){
        const alert = main.getByRole('alert');
        await expect(alert).toContainText('The mention feed could not be read for South Africa.');
        await expect(alert, 'a status is shown, not a producer reason').toContainText('the read failed with status:');
        await expect(alert).toContainText(STATUS_CODE);
        await expect(alert, 'no invented producer code').not.toContainText('listen_feed_unavailable');
        await expect(alert, 'no code the producer did not send').not.toContainText('desk_unreachable');
        await expect(alert, 'no producer is credited with a reason').not.toContainText('Reason the producer gave');
        await expect(alert).toContainText('This does not establish that the market is quiet.');
        await expect(main.getByRole('button', {name: 'Try again', exact: true})).toBeEnabled();
        await expect(main.locator('.listen-mention-row')).toHaveCount(0);
        /* Round three, 24 Sept 2026: the sentences stay in view and the
           status waits under a closed Details line. */
        await expect(alert.getByText('This does not establish that the market is quiet.', {exact: false})).toBeVisible();
        await expectClosedReason(alert, LABEL.status, STATUS_CODE);
      },
    },
  },
};

const UNIMPLEMENTED = {
  async history(main){
    await expect(main.getByRole('alert')).toContainText('Historical evidence is unavailable for this investigation.');
    await expect(main.getByRole('alert')).toContainText('historical_backend_gate_open');
    await expect(main.locator('.historical-workspace')).toHaveCount(0);
  },
  async bsa_configuration(main){
    await expect(main.getByRole('alert').filter({hasText: 'No configured research persona is served.'})).toBeVisible();
    await expect(main).not.toContainText('bsa_research');
    await expect(main).not.toContainText(/Loading\u2026/);
  },
};

/* Page port, 3 October 2026: the routes below were pages written for the
   desk API and the older console, which f42-api does not serve, so their
   links now land on the 42 page that does the same job (src/legacyRoutes.js)
   or, for an older creator link, say they are from an older version
   (src/olderLink42.jsx). The older page no route reaches cannot be driven
   through its states any more, so each state of these rows holds the landing
   instead, with the state's own producer payload still installed: the route
   is rewritten to its 42 page, that page renders its heading and makes its
   own first read, none of the older producer's endpoints is read in any
   state, and no unexpected read or page error happens. The titles stay
   `<capability_id> <state>` for the state table reporter. Landing reads are
   answered with a plain failure, except Communities, which is served the
   people42 fixture so it renders its market. */
const LANDING_READ_FAILURE = {__status: 503, body: {error: 'fixture_landing', message: 'The landing page read is not served in this fixture.'}};
const COMMUNITIES = JSON.parse(readFileSync(new URL('../../src/ui/__tests__/fixtures/people42.json', import.meta.url), 'utf8')).communities;
const HISTORY_LANDING = {to: /#\/history$/, heading: 'History', reads: ['/api/history/asks']};
const LANDINGS = {
  bsa_configuration: {to: /#\/console$/, heading: 'Build', reads: ['/api/investigations', '/api/dossiers', '/api/history/asks', '/api/history/briefs', '/api/schedules']},
  coverage_source_lab: {to: /#\/fieldwork$/, heading: 'Fieldwork', reads: ['/api/fieldwork']},
  history: HISTORY_LANDING,
  questions_followups: HISTORY_LANDING,
  sources_evidence: HISTORY_LANDING,
  investigation_build_review: {to: /#\/investigations\/inv_fixture$/, heading: 'Investigation', reads: ['/api/investigations/inv_fixture']},
  creators_network_language: {to: new RegExp('#/creator/' + HANDLE + '$'), heading: 'This creator link is from an older version', reads: []},
};
const ENTRY_LANDINGS = {
  creators_network_language: {
    network: {to: /#\/communities$/, heading: 'Communities in South Africa', reads: ['/api/communities'], fixtures: {'/api/communities': {...COMMUNITIES, market: 'ZA'}}},
    listen: {to: new RegExp('#/seedpath/' + LISTEN_TERM + '\\?region=za$'), heading: 'Seed path', reads: ['/api/seed-path'], fixtures: {}},
  },
};

function landingFixtures(landing){
  return {...Object.fromEntries(landing.reads.map((path) => [path, LANDING_READ_FAILURE])), ...(landing.fixtures || {})};
}

async function expectLanding(page, landing, installed, olderFixtures){
  await expect(page).toHaveURL(landing.to);
  await expect(page.locator('#main-content h1')).toHaveText(landing.heading);
  for (const path of landing.reads) await expect.poll(() => installed.requested.includes(path), 'the landing page made its own read of ' + path).toBe(true);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const older = Object.keys(olderFixtures);
  expect(installed.requested.filter((key) => older.includes(key)), 'no older producer endpoint is read').toEqual([]);
}

for (const row of manifest){
  const module = CAPABILITY_FIXTURES[row.capability_id];
  test.describe(row.capability_id, () => {
    if (row.producer === 'unimplemented'){
      test(`${row.capability_id} unimplemented`, async ({page}) => {
        const fixtures = {...shellFixtures(), ...module.fixtures()};
        if (row.capability_id === 'bsa_configuration'){
          fixtures['/api/research/behaviours'] = {__status: 503, body: {detail: {code: 'behaviour_scan_unavailable', message: 'The behaviour scan is not served in this fixture run.'}}};
        }
        const landing = LANDINGS[row.capability_id];
        const state = await installFixtures(page, landing ? {...fixtures, ...landingFixtures(landing)} : fixtures);
        await page.goto('/' + module.route);
        if (landing) await expectLanding(page, landing, state, module.fixtures());
        else await UNIMPLEMENTED[row.capability_id](page.locator('#main-content'));
        expect(state.unexpected).toEqual([]);
        expect(state.errors).toEqual([]);
      });
      return;
    }
    for (const entry of row.required_states){
      if (typeof entry !== 'string'){
        test(`${row.capability_id} ${entry.state}`, () => {
          expect(typeof entry.inapplicable_reason).toBe('string');
          expect(entry.inapplicable_reason.trim().length).toBeGreaterThan(20);
          expect(entry.inapplicable_reason.trim()).toMatch(/\.$/);
        });
        continue;
      }
      test(`${row.capability_id} ${entry}`, async ({page}) => {
        const gap = module.gaps[entry];
        if (gap){
          test.fail(true, gap);
          expect(gap, 'the producer supplies no ' + entry + ' payload for this route').toBeUndefined();
          return;
        }
        const landing = LANDINGS[row.capability_id];
        const state = await installFixtures(page, {...shellFixtures(), ...module.fixtures(entry), ...(landing ? landingFixtures(landing) : {})});
        await page.goto('/' + module.route);
        if (landing) await expectLanding(page, landing, state, module.fixtures(entry));
        else await CASES[row.capability_id][entry](page.locator('#main-content'), page);
        expect(state.unexpected).toEqual([]);
        expect(state.errors).toEqual([]);
      });
    }
    for (const companion of module.entries || []){
      for (const state of companion.states){
        test(`${row.capability_id} ${companion.name} ${state}`, async ({page}) => {
          /* The shell stores the companion's own market, which for the scoped
             listen read is a different market than the hash carries, so a read
             that used the stored value rather than the hash would be seen. */
          const landing = ENTRY_LANDINGS[row.capability_id]?.[companion.name];
          const installed = await installFixtures(page, {...shellFixtures(), ...companion.fixtures(state), ...(landing ? landingFixtures(landing) : {})}, {region: companion.shellMarket || 'ZA'});
          await page.goto('/' + companion.route);
          if (landing) await expectLanding(page, landing, installed, companion.fixtures(state));
          else await ENTRY_CASES[row.capability_id][companion.name][state](page.locator('#main-content'), page);
          expect(installed.unexpected).toEqual([]);
          expect(installed.errors).toEqual([]);
        });
      }
      const producer = producerText(companion.producer);
      for (const [state, refusal] of Object.entries(companion.reasons)){
        test(`${row.capability_id} ${companion.name} ${state} reason`, () => {
          expect(companion.states, `${companion.name} cannot both run and refuse ${state}`).not.toContain(state);
          const {symbol, reason} = refusal;
          expect(typeof symbol, `${companion.name} ${state} names a producer symbol`).toBe('string');
          expect(new RegExp(`(?:^|[^\\w])${symbol}(?:[^\\w]|$)`, 'm').test(producer),
            `${companion.name} ${state} names ${symbol}, which ${companion.producer} does not define`).toBe(true);
          expect(reason.trim().length).toBeGreaterThan(20);
          expect(reason.trim()).toMatch(/\.$/);
          expect(reason.includes(symbol), `${companion.name} ${state} says what ${symbol} cannot supply`).toBe(true);
        });
      }
    }
  });
}

/* Who a failure is attributed to. Each label is pinned here as the words the
   reader sees, not read back from the module that prints it, so a label that
   drifts toward crediting the producer fails here. A code is the producer's
   only when the producer's own body carried it; a status, a dropped read and
   a refusal the page derived from the records it read are each announced as
   what they are, and none of them is printed under the producer's name. */
const LABEL = Object.freeze({
  producer: 'Reason the producer gave:',
  status: 'The producer sent no reason; the read failed with status:',
  transport: 'The producer sent no reason and no answer arrived:',
  derived: 'No producer said this; this page derived it from the records it read:',
});
const DROPPED = Object.freeze({__abort: true});
const INVENTED = ['desk_unreachable', 'completed_run_unreadable', 'workspace_unavailable', 'record_invalid'];

function count(requested, key){
  return requested.filter((entry) => entry === key).length;
}

async function expectNoInventedCode(scope, except = []){
  for (const code of INVENTED.filter((code) => !except.includes(code))){
    await expect(scope, `no ${code} the producer did not send`).not.toContainText(code);
  }
}

/* Round three, 24 Sept 2026: the Listen and Network failures put the origin
   label and code behind the same closed Details line Ask, Fieldwork and
   Historical use. The reader sees the plain sentences and the retry; the
   label and code stay in the DOM for support, hidden until Details is
   opened, and the Details line is a 48px control. The flex summary loses the
   browser's own marker, so the line draws its own cue that it opens and turns
   it once open. */
async function detailsCue(summary){
  return summary.evaluate((node) => {
    const cue = getComputedStyle(node, '::before');
    const marker = getComputedStyle(node).display === 'list-item' && getComputedStyle(node).listStyleType !== 'none';
    return {drawn: marker || (cue.content !== 'none' && parseFloat(cue.borderInlineStartWidth) > 0 && cue.borderInlineStartColor !== 'rgba(0, 0, 0, 0)'), transform: cue.transform};
  });
}

async function expectClosedReason(alert, label, code){
  const reason = alert.locator('details.workspace-reason');
  await expect(reason).toHaveCount(1);
  await expect(reason).not.toHaveAttribute('open', /.*/);
  const summary = reason.locator('summary');
  await expect(summary).toHaveText('Details');
  /* The route entrance moves the page by a fraction of a pixel while it
     runs, and a box read mid move can come back a hair under its laid out
     size, so the target is read once the finite animations have settled. */
  await summary.evaluate(() => Promise.all(document.getAnimations()
    .filter((animation) => Number.isFinite(animation.effect?.getComputedTiming().endTime))
    .map((animation) => animation.finished.catch(() => null))));
  const box = await summary.boundingBox();
  expect(box.height, 'the Details line is a 48px target').toBeGreaterThanOrEqual(48);
  expect(box.width, 'the Details line is a 48px target').toBeGreaterThanOrEqual(48);
  await expect(reason).toContainText(label + ' ' + code);
  await expect(reason.locator('code')).toHaveText(code);
  await expect(alert.locator('code'), 'the code is printed once').toHaveCount(1);
  await expect(reason.locator('code')).toBeHidden();
  const closed = await detailsCue(summary);
  expect(closed.drawn, 'the Details line shows that it opens').toBe(true);
  await summary.click();
  await expect(reason.locator('code')).toBeVisible();
  await expect.poll(async () => (await detailsCue(summary)).transform, 'the cue turns once Details is open').not.toBe(closed.transform);
}

/* Page port, 3 October 2026: Network and Listen read the desk API and the
   mention feed, which f42-api does not serve, so #/network now opens
   Communities and #/listen opens Seed path (src/legacyRoutes.js). Neither
   older page is reachable, so their failure labels, retries and Try again
   targets cannot be drawn. Each case keeps its own failing or held producer
   replies installed and holds the landing instead: the 42 page renders, its
   own read is made, none of the older replies is read, no alert or Try again
   of the older page appears, and nothing unexpected is read. The old title
   is kept in the comment above each test. */
const BARE_LISTEN_LANDING = {to: /#\/seedpath$/, heading: 'Seed path', reads: ['/api/discover']};
async function expectEntryLanding(page, route, olderFixtures, {region = 'ZA'} = {}){
  const landing = route === 'network' ? ENTRY_LANDINGS.creators_network_language.network : BARE_LISTEN_LANDING;
  const state = await installFixtures(page, {...shellFixtures(), ...olderFixtures, ...landingFixtures(landing)}, {region});
  await page.goto('/#/' + route);
  await expectLanding(page, landing, state, olderFixtures);
  const main = page.locator('#main-content');
  await expect(main.locator('.network-page, .listen, .listen-mention-row, .net-node')).toHaveCount(0);
  await expect(main.locator('details.workspace-reason')).toHaveCount(0);
  for (const code of ['scope_mismatch', 'desk_read_failed', 'network_unreachable', 'checked_at_missing', STATUS_CODE]) await expect(main).not.toContainText(code);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
  return state;
}

test.describe('creators_network_language failure labels on the network and listen entries', () => {
  /* Was: a refusal the network page derived is labelled as derived and carries its own code. */
  test('with a wrong-market voices reply installed, the network link opens Communities and reads neither reply', async ({page}) => {
    await expectEntryLanding(page, 'network', {'/api/desk': deskPayload(['@' + HANDLE, '@fixer_two']), '/api/voices': voicesPayload({market: 'NG'})});
  });

  /* Was: a coded desk failure on the network page carries the desk code as the producer reason. */
  test('with a coded desk failure installed, the network link opens Communities and prints no desk code', async ({page}) => {
    await expectEntryLanding(page, 'network', {'/api/desk': DESK_FAILURE, '/api/voices': voicesPayload()});
  });

  /* Was: a dropped voices read on the network page is labelled as no answer arriving. */
  test('with a dropped voices read installed, the network link opens Communities and labels no transport failure', async ({page}) => {
    await expectEntryLanding(page, 'network', {'/api/desk': deskPayload(['@' + HANDLE]), '/api/voices': DROPPED});
  });

  /* Was: Try again on the network failure re-reads both the desk and the voices. */
  test('with both network reads failing, the network link opens Communities and a reload reads neither', async ({page}) => {
    const older = {'/api/desk': DESK_FAILURE, '/api/voices': UNCODED_SERVER_FAILURE};
    const state = await expectEntryLanding(page, 'network', older);
    await page.reload();
    await expect(page).toHaveURL(/#\/communities$/);
    await expect(page.locator('#main-content h1')).toHaveText('Communities in South Africa');
    expect(count(state.requested, '/api/desk'), 'the desk was never read').toBe(0);
    expect(count(state.requested, '/api/voices'), 'the voices were never read').toBe(0);
    expect(count(state.requested, '/api/communities'), 'Communities read again on reload').toBeGreaterThanOrEqual(2);
  });

  /* Was: Try again on the network stale card re-reads both the desk and the voices. */
  test('with an amber empty desk installed, the network link opens Communities with no stale card and no desk read', async ({page}) => {
    const state = await expectEntryLanding(page, 'network', {'/api/desk': deskPayload([], {freshness: DESK_AMBER}), '/api/voices': voicesPayload()});
    await expect(page.getByText('Held until the next run')).toHaveCount(0);
    expect(count(state.requested, '/api/desk')).toBe(0);
  });

  /* Was: an amber desk with a failed voices read shows the failure and not a stale card beside it. */
  test('with an amber desk and a failed voices read installed, the network link opens Communities with neither card', async ({page}) => {
    await expectEntryLanding(page, 'network', {'/api/desk': deskPayload([], {freshness: DESK_AMBER}), '/api/voices': UNCODED_SERVER_FAILURE});
    await expect(page.getByText('Held until the next run')).toHaveCount(0);
    await expect(page.locator('#main-content').getByRole('button', {name: 'Try again', exact: true})).toHaveCount(0);
  });

  /* Was: an amber desk with no stamp and nothing to draw is unavailable for want of a stamp, as on the desk routes. */
  test('with an undated amber empty desk installed, the network link opens Communities and no unavailable frame', async ({page}) => {
    await expectEntryLanding(page, 'network', {'/api/desk': deskPayload([], {freshness: {status: 'amber', age_hours: 5}}), '/api/voices': voicesPayload()});
    await expect(page.locator('#main-content [data-state="unavailable"]')).toHaveCount(0);
  });

  /* Was: an amber desk with no stamp still draws a graph that has links. */
  test('with an undated amber desk that has links installed, the network link opens Communities and draws no graph', async ({page}) => {
    await expectEntryLanding(page, 'network', {'/api/desk': deskPayload(['@' + HANDLE, '@fixer_two'], {freshness: {status: 'amber', age_hours: 5}}), '/api/voices': voicesPayload()});
    await expect(page.locator('#main-content .net-edge')).toHaveCount(0);
  });

  /* Was: the listen entry drops the prior quotes while a new sentiment read is outstanding. */
  test('with mention replies installed, the listen link opens Seed path and shows no quote or count', async ({page}) => {
    await expectEntryLanding(page, 'listen', {'/api/intel/mentions?market=za': mentionsPayload([1, 2]), '/api/intel/mentions?market=za&sentiment=negative': {__hold: true}});
    const main = page.locator('#main-content');
    await expect(main).not.toContainText(LISTEN_QUOTE);
    await expect(main).not.toContainText('mentions loaded');
    await expect(main).not.toContainText('posts loaded');
  });

  /* Was: the listen entry drops the prior market quotes on a market switch. */
  test('with mention replies for two markets installed, the listen link opens Seed path and a market switch reads no mentions', async ({page}) => {
    const state = await expectEntryLanding(page, 'listen', {'/api/intel/mentions?market=za': mentionsPayload([1, 2]), '/api/intel/mentions?market=ng': {__hold: true}});
    const main = page.locator('#main-content');
    await main.getByRole('group', {name: 'Market', exact: true}).getByRole('button', {name: 'Nigeria', exact: true}).click();
    await expect(main.getByRole('group', {name: 'Market', exact: true}).getByRole('button', {name: 'Nigeria', exact: true})).toHaveAttribute('aria-pressed', 'true');
    await expect(main).not.toContainText(LISTEN_QUOTE);
    expect(state.requested.filter((key) => key.startsWith('/api/intel/mentions'))).toEqual([]);
    expect(state.unexpected).toEqual([]);
  });

  /* Was: a dropped listen read is labelled as no answer arriving. */
  test('with a dropped mention read installed, the listen link opens Seed path and labels no transport failure', async ({page}) => {
    await expectEntryLanding(page, 'listen', {'/api/intel/mentions': DROPPED});
  });

  /* Was: Try again on the ${route} failure is a 48px secondary action with a focus ring. */
  for (const [route, fixtures] of [
    ['listen', {'/api/intel/mentions': DROPPED}],
    ['network', {'/api/desk': DESK_FAILURE, '/api/voices': UNCODED_SERVER_FAILURE}],
  ]) test(`with the ${route} failure installed, its link opens the 42 page and offers no older Try again`, async ({page}) => {
    await page.emulateMedia({reducedMotion: 'reduce'});
    await expectEntryLanding(page, route, fixtures);
    await expect(page.locator('#main-content').getByRole('alert').getByRole('button', {name: 'Try again', exact: true})).toHaveCount(0);
    await expect(page.locator('a[href^="#/' + route + '"]')).toHaveCount(0);
  });
});

/* The pages beside the capability routes that print a failure code. Each
   prints the code the producer body carried where it carried one, the status
   or the transport's truth otherwise, and never a code of its own making. */
test.describe('creators_network_language failure codes on the desk, lexicon, historical and stored question pages', () => {
  for (const [name, reply, code] of [
    ['a coded desk failure', DESK_FAILURE, 'desk_read_failed'],
    ['the plain text desk failure', UNCODED_SERVER_FAILURE, STATUS_CODE],
    ['a dropped desk read', DROPPED, 'network_unreachable'],
  ]){
    test(`the desk error frame on Browse carries ${name} as ${code}`, async ({page}) => {
      await installFixtures(page, {...shellFixtures(), '/api/desk': reply});
      await page.goto('/#/browse');
      const frame = page.locator('#main-content [data-state="unavailable"]').first();
      await expect(frame).toContainText('The desk could not load');
      await expect(frame).toContainText(code);
      await expectNoInventedCode(frame);
    });

    /* Page port, 3 October 2026: Lexicon reads its own /api/lexicon, so the
       same failures are served there. */
    test(`the lexicon error frame carries ${name} as ${code}`, async ({page}) => {
      await installFixtures(page, {...shellFixtures(), '/api/lexicon': reply});
      await page.goto('/#/lexicon');
      const frame = page.locator('#main-content [data-state-frame="error"]');
      await expect(frame).toContainText('The lexicon could not be read');
      await expect(frame).toContainText(code);
      await expectNoInventedCode(frame);
    });
  }

  const HISTORICAL = '/api/internal/v2/investigations/inv_fixture/historical/read';
  /* Page port, 3 October 2026: the historical workspace read the older
     investigation's historical endpoint, and #/historical links now open
     History (src/legacyRoutes.js), so the workspace and its failure labels
     are unreachable. Each case keeps its failing reply installed and holds
     the landing: History renders and makes its own read, the historical
     endpoint is never read, no historical failure label or code is printed,
     and nothing unexpected is read. The old title is kept above each test. */
  for (const [name, reply, label, code] of [
    ['a coded failure', {__status: 501, body: {detail: {code: 'historical_backend_gate_open', message: 'The historical backend gate has not passed.'}}}, LABEL.producer, 'historical_backend_gate_open'],
    ['the plain text failure', UNCODED_SERVER_FAILURE, LABEL.status, STATUS_CODE],
    ['a dropped read', DROPPED, LABEL.transport, 'network_unreachable'],
  ]){
    /* Was: the historical workspace labels ${name} and prints ${code}. */
    test(`a historical link with ${name} installed opens History and prints no ${code}`, async ({page}) => {
      const state = await installFixtures(page, {...shellFixtures(), [HISTORICAL]: reply, ...landingFixtures(HISTORY_LANDING)});
      await page.goto('/#/historical/inv_fixture/analogue');
      await expectLanding(page, HISTORY_LANDING, state, {[HISTORICAL]: reply});
      const main = page.locator('#main-content');
      await expect(main).not.toContainText('Historical evidence is unavailable for this investigation.');
      await expect(main).not.toContainText(label + ' ' + code);
      await expect(main).not.toContainText(code);
      await expectNoInventedCode(main);
      expect(state.unexpected).toEqual([]);
      expect(state.errors).toEqual([]);
    });
  }

  /* A route the historical page refused now opens History as well: no
     historical read is made, and no frame ever credits anything to the
     producer, from the first frame the reader sees to the last. */
  for (const [route, code] of [
    ['#/historical/inv_case/analogue/extra', 'workspace_request_invalid'],
    ['#/historical/inv_case/diffusion', 'historical_mode_ineligible'],
  ]){
    /* Was: the historical workspace announces the refused route ${route} as derived and prints ${code}. */
    test(`the refused historical route ${route} opens History, reads nothing historical and prints no ${code}`, async ({page}) => {
      const state = await installFixtures(page, {...shellFixtures(), ...landingFixtures(HISTORY_LANDING)});
      await page.addInitScript((producer) => {
        window.__creditedToProducer = [];
        /* A text the page replaced before this callback ran is still seen:
           its old value rides on the record. */
        new MutationObserver((records) => {
          for (const record of records){
            if (record.oldValue && record.oldValue.includes(producer)) window.__creditedToProducer.push(record.oldValue);
          }
          const main = document.getElementById('main-content');
          const text = main ? main.textContent : '';
          if (text.includes(producer)) window.__creditedToProducer.push(text);
        }).observe(document, {childList: true, subtree: true, characterData: true, characterDataOldValue: true});
      }, LABEL.producer);
      await page.goto('/' + route);
      await expectLanding(page, HISTORY_LANDING, state, {});
      const main = page.locator('#main-content');
      await expect(main).not.toContainText(code);
      await expectNoInventedCode(main);
      expect(await page.evaluate(() => window.__creditedToProducer), 'no frame ever credited anything to the producer').toEqual([]);
      expect(state.requested.filter((key) => key.includes('/historical/')), 'a refused route reads nothing historical').toEqual([]);
      expect(state.unexpected).toEqual([]);
    });
  }

  /* Page port, 3 October 2026: a stored question link
     (#/console?work=ask&request=<id>) names an older record, and it now
     opens History (src/legacyRoutes.js), so the stored question frame and
     its Reload control are unreachable. Each case keeps its reply installed
     and holds the landing: History renders and makes its own read, the
     stored question endpoint is never read, and no stored question label,
     code or Reload control appears. The old title is kept above each test. */
  for (const [name, reply, label, code] of [
    ['a dropped read', DROPPED, LABEL.transport, 'network_unreachable'],
    ['the plain text failure', UNCODED_SERVER_FAILURE, LABEL.status, STATUS_CODE],
    ['a record that fails validation', {contract_version: 'general_question_detail_v1'}, LABEL.derived, 'record_invalid'],
  ]){
    /* Was: the stored question labels ${name} and prints ${code}. */
    test(`a stored question link with ${name} installed opens History and prints no ${code}`, async ({page}) => {
      const state = await installFixtures(page, {...shellFixtures(), '/api/chat/lenses': {__hold: true}, [QUESTION_ENDPOINT]: reply, ...landingFixtures(HISTORY_LANDING)});
      await page.goto('/' + QUESTION_ROUTE);
      await expectLanding(page, HISTORY_LANDING, state, {[QUESTION_ENDPOINT]: reply});
      const main = page.locator('#main-content');
      await expect(main.getByRole('button', {name: 'Reload stored request', exact: true})).toHaveCount(0);
      await expect(main.getByText('The stored question could not be verified.', {exact: true})).toHaveCount(0);
      await expect(main).not.toContainText(label + ' ' + code);
      await expect(main.locator('details.workspace-reason')).toHaveCount(0);
      await expectNoInventedCode(main);
      expect(state.unexpected).toEqual([]);
    });
  }

  /* Was: the stored question Reload control is a 48px secondary action with
     a focus ring that re-reads. History's own Try again under its failed read
     is held to the same standard: a 48px secondary action, never red at rest,
     with a visible focus ring, and it re-reads History, not the stored
     question. */
  test('a stored question link opens History, whose Try again is a 48px secondary action with a focus ring that re-reads', async ({page}) => {
    const fixtures = {...shellFixtures(), '/api/chat/lenses': {__hold: true}, [QUESTION_ENDPOINT]: DROPPED, ...landingFixtures(HISTORY_LANDING)};
    const state = await installFixtures(page, fixtures);
    await page.emulateMedia({reducedMotion: 'reduce'});
    await page.goto('/' + QUESTION_ROUTE);
    await expectLanding(page, HISTORY_LANDING, state, {[QUESTION_ENDPOINT]: DROPPED});
    const reload = page.locator('#main-content').getByRole('button', {name: 'Try again', exact: true}).first();
    await expect(reload).toBeVisible();
    const box = await reload.boundingBox();
    expect(box.height, 'a 48px target').toBeGreaterThanOrEqual(48);
    expect(box.width, 'a 48px target').toBeGreaterThanOrEqual(48);
    const colours = await reload.evaluate((node) => {
      const probe = document.createElement('span');
      probe.style.color = 'var(--accent)';
      document.body.append(probe);
      const red = [getComputedStyle(probe).color];
      probe.style.color = 'var(--accent-text)';
      red.push(getComputedStyle(probe).color);
      probe.remove();
      const style = getComputedStyle(node);
      return {red, painted: [style.color, style.backgroundColor, style.borderTopColor, style.borderBottomColor]};
    });
    for (const colour of colours.painted) expect(colours.red, 'no red at rest on a secondary action').not.toContain(colour);
    const border = await reload.evaluate((node) => parseFloat(getComputedStyle(node).borderTopWidth));
    expect(border, 'the secondary action draws its edge').toBeGreaterThan(0);
    await reload.focus();
    await page.keyboard.press('Shift+Tab');
    await page.keyboard.press('Tab');
    await expect(reload).toBeFocused();
    const ring = await page.evaluate(() => {
      const node = document.activeElement;
      const read = () => { const style = getComputedStyle(node); return {outline: style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0 ? style.outline : 'none', shadow: style.boxShadow}; };
      const focused = read();
      node.blur();
      const blurred = read();
      return {visible: focused.outline !== 'none' || focused.shadow !== 'none', changed: focused.outline !== blurred.outline || focused.shadow !== blurred.shadow};
    });
    expect(ring, 'a visible focus ring').toEqual({visible: true, changed: true});
    const before = count(state.requested, '/api/history/asks');
    await reload.click();
    await expect.poll(() => count(state.requested, '/api/history/asks'), 'Try again reads History again').toBeGreaterThan(before);
    expect(count(state.requested, QUESTION_ENDPOINT), 'the stored question is never read').toBe(0);
    expect(state.unexpected).toEqual([]);
  });
});

/* The Evidence Room and Fieldwork print the code the read recorded under the
   label that says whose it is, and a finding either page derived from a
   payload it read is announced as derived. Neither prints workspace_unavailable
   for a read that recorded something else. */
test.describe('invented codes on the evidence room and fieldwork', () => {
  const review = CAPABILITY_FIXTURES.investigation_build_review;
  const BASE = '/api/v2/investigations/inv_fixture';
  function evidenceFixtures(over){
    return {...shellFixtures(), '/api/chat/lenses': {__hold: true}, ...review.fixtures('populated'), ...over};
  }

  /* Page port, 3 October 2026: the evidence room is the older console's
     investigation view (#/console?work=brief&investigation=<id>...), and that
     link now opens the 42 investigation page #/investigations/<id>
     (src/legacyRoutes.js), so the evidence room and its failure labels are
     unreachable. Each case keeps its failing reply installed and holds the
     landing: the investigation page renders and makes its own read, no
     /api/v2/investigations read of the evidence room is made, and no
     evidence room label or code is printed. The old title is kept above each
     test. */
  const INVESTIGATION_LANDING = LANDINGS.investigation_build_review;
  async function expectEvidenceRoomLanding(page, over, code){
    const older = evidenceFixtures(over);
    const state = await installFixtures(page, {...older, ...landingFixtures(INVESTIGATION_LANDING)});
    await page.goto('/' + review.route);
    await expectLanding(page, INVESTIGATION_LANDING, state, review.fixtures('populated'));
    expect(state.requested.filter((key) => key.startsWith(BASE)), 'no evidence room read').toEqual([]);
    const main = page.locator('#main-content');
    await expect(main.getByText('The investigation is unavailable.')).toHaveCount(0);
    await expect(main.getByText('Requested artifact is unavailable.')).toHaveCount(0);
    await expect(main).not.toContainText(code);
    await expectNoInventedCode(main);
    expect(state.unexpected).toEqual([]);
    expect(state.errors).toEqual([]);
  }

  for (const [name, reply, label, code] of [
    ['the plain text failure', UNCODED_SERVER_FAILURE, LABEL.status, STATUS_CODE],
    ['a dropped read', DROPPED, LABEL.transport, 'network_unreachable'],
    ['a coded failure', {__status: 500, body: {detail: {code: 'investigation_store_unavailable', message: 'The investigation store did not answer.'}}}, LABEL.producer, 'investigation_store_unavailable'],
  ]){
    /* Was: the evidence room labels ${name} on the status read and prints ${code}. */
    test(`an evidence room link with ${name} on the status read opens the investigation and prints no ${code}`, async ({page}) => {
      await expectEvidenceRoomLanding(page, {[BASE + '/status']: reply}, label + ' ' + code);
    });

    /* Was: the evidence room labels ${name} on the artifact read and prints ${code}. */
    test(`an evidence room link with ${name} on the artifact read opens the investigation and prints no ${code}`, async ({page}) => {
      await expectEvidenceRoomLanding(page, {[BASE + '/artifacts/ra_fixture/read']: reply}, label + ' ' + code);
      await expect(page.locator('#main-content details').filter({hasText: 'Artifact read details'})).toHaveCount(0);
    });
  }

  /* Was: the evidence room announces a claim resource without its list as derived. */
  test('an evidence room link with a claim resource missing its list opens the investigation and derives nothing', async ({page}) => {
    await expectEvidenceRoomLanding(page, {[BASE + '/claims/read']: {}}, 'claim_list_missing');
    await expect(page.locator('#main-content')).not.toContainText(LABEL.derived);
  });

  const fieldwork = CAPABILITY_FIXTURES.fieldwork;
  const FIELDWORK_READ = Object.keys(fieldwork.fixtures('populated')).find((key) => key.includes('/fieldwork/read'));
  for (const [name, reply, label, code] of [
    ['the plain text failure', UNCODED_SERVER_FAILURE, LABEL.status, STATUS_CODE],
    ['a dropped read', DROPPED, LABEL.transport, 'network_unreachable'],
    ['a coded failure', {__status: 500, body: {detail: {code: 'fieldwork_read_failed', message: 'The fieldwork projection could not be read.'}}}, LABEL.producer, 'fieldwork_read_failed'],
  ]){
    test(`fieldwork labels ${name} and prints ${code}`, async ({page}) => {
      const state = await installFixtures(page, {...shellFixtures(), ...fieldwork.fixtures('populated'), [FIELDWORK_READ]: reply});
      await page.goto('/' + fieldwork.route);
      const alert = page.locator('#main-content').getByRole('alert');
      await expect(alert).toContainText(label + ' ' + code);
      await expectNoInventedCode(alert);
      expect(state.unexpected).toEqual([]);
    });
  }
});

/* A contract version this build cannot read is the page's own reading of the
   payload, not a reason the producer gave, and a coded producer body that
   names the same state keeps the producer label. */
test.describe('invented codes on fieldwork contract versions', () => {
  const fieldwork = CAPABILITY_FIXTURES.fieldwork;
  const FIELDWORK_READ = Object.keys(fieldwork.fixtures('populated')).find((key) => key.includes('/fieldwork/read'));

  test('fieldwork announces an unknown contract version as derived', async ({page}) => {
    const payload = {...fieldwork.fixtures('populated')[FIELDWORK_READ], contract_version: 'fieldwork_workspace_v9'};
    const state = await installFixtures(page, {...shellFixtures(), ...fieldwork.fixtures('populated'), [FIELDWORK_READ]: payload});
    await page.goto('/' + fieldwork.route);
    const alert = page.locator('#main-content').getByRole('alert');
    await expect(alert).toContainText('This build cannot read the fieldwork contract this service reports.');
    await expect(alert).toContainText(LABEL.derived + ' contract_version_unsupported');
    await expect(alert).not.toContainText(LABEL.producer);
    expect(state.unexpected).toEqual([]);
  });

  test('fieldwork keeps the producer label for a producer body that names an unsupported contract', async ({page}) => {
    const reply = {__status: 400, body: {detail: {code: 'contract_version_unsupported', message: 'The requested contract version is not served.'}}};
    const state = await installFixtures(page, {...shellFixtures(), ...fieldwork.fixtures('populated'), [FIELDWORK_READ]: reply});
    await page.goto('/' + fieldwork.route);
    const alert = page.locator('#main-content').getByRole('alert');
    await expect(alert).toContainText(LABEL.producer + ' contract_version_unsupported');
    expect(state.unexpected).toEqual([]);
  });
});

/* Round three, 24 Sept 2026: the Fieldwork source column reads as a column
   of prose under its own heading. The Source Lab hand-off is a link inside a
   sentence, so the line that holds it keeps the paragraph's own leading
   while the link keeps a 48px target; the column opens on "Fieldwork
   intelligence" rather than on the sentences that describe it; and the
   inventory ends on one rule, not a row rule with the panel's rule under
   it. */
test.describe('fieldwork source column', () => {
  const fieldwork = CAPABILITY_FIXTURES.fieldwork;

  async function openFieldwork(page, width){
    await page.setViewportSize({width, height: 900});
    const state = await installFixtures(page, {...shellFixtures(), ...fieldwork.fixtures('populated')});
    await page.goto('/' + fieldwork.route);
    const column = page.locator('#main-content .fieldwork-body__sources');
    await expect(column.locator('.oi-inventory-row').first()).toBeVisible();
    return {state, column};
  }

  for (const width of [1280, 1024, 390]){
    test(`the Source Lab hand-off keeps the sentence's line spacing and a 48px target at ${width}`, async ({page}) => {
      const {state} = await openFieldwork(page, width);
      const link = page.locator('#main-content .fieldwork-inventory__absent a');
      await link.scrollIntoViewIfNeeded();
      const read = await link.evaluate((anchor) => {
        const paragraph = anchor.closest('p');
        const range = document.createRange();
        const tops = [];
        let linkTop = Infinity;
        const walker = document.createTreeWalker(paragraph, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()){
          range.selectNodeContents(node);
          for (const rect of range.getClientRects()){
            if (!rect.width) continue;
            tops.push(rect.top);
            if (anchor.contains(node)) linkTop = Math.min(linkTop, rect.top);
          }
        }
        const lines = [...new Set(tops.map((top) => Math.round(top * 2) / 2))].sort((a, b) => a - b)
          .filter((top, index, all) => index === 0 || top - all[index - 1] > 2);
        const line = lines.findIndex((top) => Math.abs(top - linkTop) <= 2);
        const box = anchor.getBoundingClientRect();
        const x = box.left + box.width / 2;
        const hit = [box.top + 1, box.bottom - 1].map((y) => anchor.contains(document.elementFromPoint(x, y)));
        const others = [...document.querySelectorAll('#main-content a, #main-content button, #main-content summary, #main-content [role="tab"]')]
          .filter((node) => node !== anchor)
          .map((node) => node.getBoundingClientRect())
          .filter((other) => other.width && other.height && other.left < box.right && other.right > box.left && other.top < box.bottom && other.bottom > box.top);
        return {lineHeight: parseFloat(getComputedStyle(paragraph).lineHeight), lines, line, height: box.height, hit, overlaps: others.length, paragraph: paragraph.getBoundingClientRect().height};
      });
      expect(read.line, 'the link sits on a line after the sentence opens').toBeGreaterThan(0);
      expect(Math.abs(read.lines[read.line] - read.lines[read.line - 1] - read.lineHeight), 'the link line against the line above it').toBeLessThanOrEqual(1);
      for (let index = 1; index < read.lines.length; index += 1){
        expect(Math.abs(read.lines[index] - read.lines[index - 1] - read.lineHeight), `line ${index + 1} of the sentence`).toBeLessThanOrEqual(1);
      }
      expect(read.height, 'the hand-off hit area').toBeGreaterThanOrEqual(48);
      expect(read.hit, 'the hand-off takes a press at the top and foot of its box').toEqual([true, true]);
      expect(read.overlaps, 'the hand-off box covers no other control').toBe(0);

      /* From the keyboard the ring, at the package offset, rings the words
         on their own line, and nothing on the page moves for it. */
      await link.focus();
      await page.keyboard.press('Shift+Tab');
      await page.keyboard.press('Tab');
      await expect(link).toBeFocused();
      const ring = await link.evaluate((anchor) => {
        const paragraph = anchor.closest('p');
        const style = getComputedStyle(anchor);
        const box = anchor.getBoundingClientRect();
        return {
          visible: anchor.matches(':focus-visible') && style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0,
          height: box.height,
          lineHeight: parseFloat(getComputedStyle(paragraph).lineHeight),
          paragraph: paragraph.getBoundingClientRect().height,
        };
      });
      expect(ring.visible, 'the keyboard ring is drawn').toBe(true);
      expect(Math.abs(ring.height - ring.lineHeight), 'the ring holds to the line').toBeLessThanOrEqual(1);
      expect(Math.abs(ring.paragraph - read.paragraph), 'the sentence does not move under the ring').toBeLessThanOrEqual(0.5);
      expect(state.unexpected).toEqual([]);
    });

    test(`the source column opens on its heading at ${width}`, async ({page}) => {
      const {state, column} = await openFieldwork(page, width);
      const heading = column.getByRole('heading', {name: 'Fieldwork intelligence', exact: true});
      await expect(heading).toHaveCount(1);
      await expect(heading).toBeVisible();
      await expect(heading).toHaveJSProperty('tagName', 'H2');
      const read = await column.evaluate((node) => {
        const title = [...node.querySelectorAll('h1, h2, [role="heading"]')].find((candidate) => candidate.textContent.trim() === 'Fieldwork intelligence' && candidate.getClientRects().length);
        const bottom = title.getBoundingClientRect().bottom;
        const prose = [...node.querySelectorAll('p')].filter((paragraph) => paragraph.getClientRects().length);
        const count = node.querySelector('.fieldwork-inventory__count');
        return {
          above: prose.filter((paragraph) => paragraph.getBoundingClientRect().top < bottom).map((paragraph) => paragraph.textContent.trim()),
          headingFirst: Boolean(title.compareDocumentPosition(count) & Node.DOCUMENT_POSITION_FOLLOWING),
        };
      });
      expect(read.above, 'no sentence of the column sits above its heading').toEqual([]);
      expect(read.headingFirst, 'a screen reader meets the heading before the sentences').toBe(true);
      expect(state.unexpected).toEqual([]);
    });

    test(`the source inventory ends on one rule at ${width}`, async ({page}) => {
      const {state, column} = await openFieldwork(page, width);
      const rules = await column.evaluate((node) => {
        const width = node.getBoundingClientRect().width;
        const rows = node.querySelectorAll('.oi-inventory-row');
        const last = rows[rows.length - 1];
        const content = Math.max(...[...last.children].map((child) => child.getBoundingClientRect().bottom));
        const found = [];
        for (const element of node.querySelectorAll('*')){
          const box = element.getBoundingClientRect();
          if (box.width < width / 2) continue;
          const style = getComputedStyle(element);
          if (style.borderTopStyle !== 'none' && parseFloat(style.borderTopWidth) > 0) found.push(Math.round(box.top));
          if (style.borderBottomStyle !== 'none' && parseFloat(style.borderBottomWidth) > 0) found.push(Math.round(box.bottom));
        }
        return found.filter((y) => y > content);
      });
      expect(rules, 'rules under the last source row').toHaveLength(1);
      expect(state.unexpected).toEqual([]);
    });
  }
});

/* Round three, 25 Sept 2026: the last users of the red command style drew
   under 48px, as Try again on Listen and Network did. The Refine control
   under an opened research brief, the instruction field beside it and the
   refine suggestions are each read here at 1280 and 390 in both themes,
   once the finite animations have settled, as a 48px target. The sweep of
   every control the suite serves found one more standalone row under the
   floor, the brief's scan options at 44px, and it is read the same way. */
async function settledSmallTargets(scope, selector){
  await scope.evaluate(() => Promise.all(document.getAnimations()
    .filter((animation) => Number.isFinite(animation.effect?.getComputedTiming().endTime))
    .map((animation) => animation.finished.catch(() => null))));
  const controls = scope.locator(selector);
  const small = [];
  for (const control of await controls.all()){
    const name = (await control.getAttribute('placeholder')) || (await control.textContent());
    const box = await control.boundingBox();
    if (box.height < 48 || box.width < 48) small.push(`${name} ${Math.round(box.width)}x${Math.round(box.height)}`);
  }
  return {count: await controls.count(), small};
}

test.describe('refine controls under an opened research brief', () => {
  const ARTIFACT = 'ra_refine_fixture';
  const fixtures = () => ({...shellFixtures(),
    '/api/research/personas': {personas: []},
    '/api/research/availability': {},
    ['/api/research/' + ARTIFACT]: {artifact_id: ARTIFACT, doc: {json: {title: 'Weekend repair brief'}, markdown: 'Repair tutorials recur in the sampled posts.', sources: []}},
  });

  /* Page port, 3 October 2026: the older console's brief links
     (#/console?work=brief, with or without an artifact) now open Build, 42's
     own page for asking, investigating and building a dossier
     (src/legacyRoutes.js), so the Refine bar and the scan options are
     unreachable. Each case holds the landing instead, at the same widths and
     themes and to the same 48px floor: Build renders, every control it
     draws in the workspace is a 48px target once the finite animations have
     settled, the older research reads are never made, and nothing
     unexpected is read. The old title is kept above each test. */
  const BUILD_LANDING = LANDINGS.bsa_configuration;
  async function expectBuildTargets(page, route, older){
    const state = await installFixtures(page, {...older, ...landingFixtures(BUILD_LANDING)});
    await page.goto(route);
    await expectLanding(page, BUILD_LANDING, state, Object.fromEntries(Object.entries(older).filter(([key]) => key.startsWith('/api/research/'))));
    const main = page.locator('#main-content');
    await expect(main.locator('.research-refine')).toHaveCount(0);
    await expect(main.getByRole('group', {name: 'Scan options'})).toHaveCount(0);
    const {count, small} = await settledSmallTargets(main, 'button, input, select, textarea');
    expect(count, 'Build draws its two composers and their actions').toBeGreaterThanOrEqual(4);
    expect(small, 'each Build control is a 48px target').toEqual([]);
    expect(state.unexpected).toEqual([]);
    expect(state.errors).toEqual([]);
  }

  for (const width of [1280, 390]) for (const theme of ['midnight', 'daylight']){
    /* Was: the Refine controls are 48px targets at ${width} in ${theme}. */
    test(`a brief artifact link opens Build, whose controls are 48px targets at ${width} in ${theme}`, async ({page}) => {
      await page.setViewportSize({width, height: 900});
      await page.addInitScript((value) => localStorage.setItem('oi-theme', value), theme);
      await expectBuildTargets(page, '/#/console?work=brief&artifact=' + ARTIFACT, fixtures());
    });

    /* Was: the brief's scan options are 48px targets at ${width} in ${theme}. */
    test(`a brief link opens Build, with no scan options and controls that are 48px targets at ${width} in ${theme}`, async ({page}) => {
      await page.setViewportSize({width, height: 900});
      await page.addInitScript((value) => localStorage.setItem('oi-theme', value), theme);
      await expectBuildTargets(page, '/#/console?work=brief', {...shellFixtures(),
        '/api/research/personas': {personas: []},
        '/api/research/behaviours': {behaviours: []},
        '/api/research/availability': {brief_writing: {available: true, code: null, message: null}, behaviour_scan: {available: true, code: null, message: null}},
      });
    });
  }
});
