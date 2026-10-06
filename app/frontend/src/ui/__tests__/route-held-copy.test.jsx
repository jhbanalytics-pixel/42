import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {StateView} from 'ogilvy-intelligence-design-system';

import {ComparePanel} from '../../compare.jsx';
import {ExplorePage} from '../../explore.jsx';
import {routeRunFacts, routeStateView, tryAgainAction} from '../../instrumentRouteModels.js';
import {TodayPage} from '../../today.jsx';
import {ROOT_SIGNAL, freshFixture} from './fixtures/instrument-payloads.js';

/* Round 6, task 34. Archive 2.0.6 gives every held frame its ruled copy: a
   title, one sentence in the reader register, a closed Details disclosure
   holding the run facts, and one next action. The routes stop writing their
   own sentences and pass structure only, so what a reader sees on Pulse,
   Discover and Compare is the package's copy, and every machine string, the
   reason code, the missing field names, the desk's failure message, sits
   inside Details or nowhere. */

const RUN = Object.freeze({
  run_id: 'run_20260826_dynamic_apply_v1',
  observation_start: '2026-08-20',
  observation_end: '2026-08-26',
  observation_method: 'dynamic_source_copy_apply_v1',
});
const THIRTY_HOURS = 30 * 60 * 60 * 1000;
const STAMP = new Date(Date.now() - THIRTY_HOURS).toISOString();
const RULED = Object.freeze({
  loading: ['Reading the completed run', 'One moment. The last completed observation is being read.'],
  stale: ['Held until the next run', /^The last observation is \d+ (?:minutes|hours|days) old, so nothing here is shown as current\. It resumes when the next run lands\.$/],
  unavailable: ['Nothing to show for this signal', 'This signal has no closed evidence behind it yet.'],
  error: ['Could not read the run', 'The completed run could not be read just now.'],
  /* Round three, 24 Sept 2026: the no discovery body says the run finished
     without a signal strong enough to show, in a strategist's words rather
     than the engine's "admitted" and "dynamic". Same frame, same assertion. */
  noDiscovery: ['No signals this run', 'The run finished without a signal strong enough to show.'],
  insufficient: ['Not enough to compare', 'The completed run admitted 1 signal. Comparison needs two.'],
  /* Quiet register, 23 Sept 2026: package 2.0.21 says what to do on an empty
     filter in plain words rather than restating that nothing was admitted. */
  filteredEmpty: ['Nothing matches this filter', 'Try another filter, or clear it to see every signal.'],
});

const admittedSignal = (overrides = {}) => ({
  ...freshFixture(ROOT_SIGNAL),
  ...RUN,
  signal_id: 'sig_' + 'a'.repeat(64),
  discovery_mode: 'phrase',
  evidence_state: 'ready',
  qualities: {
    velocity: 'moderate', novelty: 'moderate', breadth: 'moderate',
    independence: 'moderate', history: 'unmeasured', geo_confidence: 'moderate',
    topic_tags: [],
  },
  ...overrides,
});

const legacySignal = () => {
  const row = admittedSignal();
  delete row.evidence_summary;
  delete row.ribbon_series;
  delete row.audience_basis;
  return row;
};

const detailsOf = (markup) => markup.match(/<details class="state-view__disclosure">[\s\S]*?<\/details>/)?.[0] ?? '';
const surfaceOf = (markup) => markup
  .replace(/<details class="state-view__disclosure">[\s\S]*?<\/details>/g, '')
  .replace(/<[^>]*>/g, ' ')
  .replace(/\s+/g, ' ')
  .trim();
const titleOf = (markup) => markup.match(/<h2 class="state-view__title">([^<]*)<\/h2>/)?.[1];
const sentenceOf = (markup) => markup.match(/<p class="state-view__body">([^<]*)<\/p>/)?.[1];
const buttonsOf = (markup) => [...markup.matchAll(/<button class="state-view__action" type="button">([^<]*)<\/button>/g)].map((hit) => hit[1]);
/* From 2.0.8 the package writes no next action when a supplied control carries
   the same label, so a card whose first control is its next action reads the
   label once, on the control. */
const writtenNextOf = (markup) => markup.match(/<p class="state-view__next-written">([^<]*)<\/p>/)?.[1];

function expectRuledFrame(markup, {wrapper, state, copy, buttons}){
  const [title, sentence] = copy;
  expect(markup).toContain(wrapper);
  expect(markup).toContain(`data-state="${state}"`);
  expect(titleOf(markup)).toBe(title);
  if (sentence instanceof RegExp) expect(sentenceOf(markup)).toMatch(sentence);
  else expect(sentenceOf(markup)).toBe(sentence);
  expect(markup).toMatch(/<details class="state-view__disclosure"><summary class="state-view__summary">Details<\/summary>/);
  expect(markup).toContain('<dt>Closed window</dt>');
  expect(markup).toContain('<dt>Checked</dt>');
  expect(buttonsOf(markup)).toEqual(buttons);
  const surface = surfaceOf(markup);
  expect(surface, 'no machine token on the reader surface').not.toMatch(/\b[a-z0-9]+_[a-z0-9_]+\b/);
  expect(surface).not.toContain('Missing:');
  expect(surface).not.toContain('supplied');
  expect(markup.match(/role="(?:alert|status)"/g) || []).toHaveLength(state === 'empty' ? 0 : 1);
}

describe('Pulse lets the package word its held frames', () => {
  test('loading', () => {
    const markup = renderToStaticMarkup(<TodayPage topics={[]} loading />);
    expectRuledFrame(markup, {wrapper: 'data-briefing-state="loading"', state: 'loading', copy: RULED.loading, buttons: []});
    expect(markup).toContain('role="status"');
    expect(markup).toContain('aria-live="polite"');
    expect(detailsOf(markup)).toContain('<dt>Task</dt><dd data-testid="method">Checking the completed observation</dd>');
  });

  /* The stale card holds a run that carries nothing. A released run outranks
     the ingest stamp, so a run with signals is read at any stamp. */
  test('stale carries the age of the stamp and the Browse evidence control', () => {
    const markup = renderToStaticMarkup(
      <TodayPage topics={[]} freshness={{status: 'amber', stamp_utc: STAMP}} />,
    );
    expectRuledFrame(markup, {wrapper: 'data-briefing-state="stale"', state: 'stale', copy: RULED.stale, buttons: ['Browse evidence']});
    expect(sentenceOf(markup)).toMatch(/\b(?:29|30) hours\b/);
    expect(detailsOf(markup)).toContain(`<time dateTime="${STAMP}">`);
  });

  test('an amber stamp does not hold a run that carries a signal', () => {
    const markup = renderToStaticMarkup(
      <TodayPage topics={[admittedSignal()]} freshness={{status: 'amber', stamp_utc: STAMP}} />,
    );
    expect(markup).not.toContain('data-briefing-state="stale"');
    expect(markup).toContain('data-instrument-briefing="true"');
  });

  test('stale without a stamp reads as absence and names the stamp only inside Details', () => {
    const markup = renderToStaticMarkup(
      <TodayPage topics={[]} freshness={{status: 'amber'}} />,
    );
    expectRuledFrame(markup, {wrapper: 'data-briefing-state="stale"', state: 'unavailable', copy: RULED.unavailable, buttons: ['Browse evidence']});
    expect(detailsOf(markup)).toContain('<dt>Missing</dt><dd>checked_at</dd>');
  });

  test('unavailable names the missing field only inside Details', () => {
    const markup = renderToStaticMarkup(
      <TodayPage topics={[legacySignal()]} freshness={{status: 'green'}} />,
    );
    expectRuledFrame(markup, {wrapper: 'data-briefing-state="unavailable"', state: 'unavailable', copy: RULED.unavailable, buttons: ['Open Discover']});
    expect(detailsOf(markup)).toContain('<dt>Reason</dt><dd>evidence_authority_missing</dd>');
    expect(detailsOf(markup)).toContain('<dt>Missing</dt><dd>evidence_authority</dd>');
  });

  test('a signal without a Ribbon series names the series only inside Details', () => {
    const payload = admittedSignal();
    delete payload.ribbon_series;
    const markup = renderToStaticMarkup(<TodayPage topics={[payload]} freshness={{status: 'green'}} />);
    expectRuledFrame(markup, {wrapper: 'data-briefing-state="unavailable"', state: 'unavailable', copy: RULED.unavailable, buttons: ['Open Discover']});
    expect(detailsOf(markup)).toContain('<dt>Missing</dt><dd>ribbon_series</dd>');
  });

  test('error keeps the desk message off the surface and offers Try again', () => {
    const markup = renderToStaticMarkup(
      <TodayPage topics={[]} error={{code: 'api_unavailable', message: 'the desk is offline right now.'}} />,
    );
    expectRuledFrame(markup, {wrapper: 'data-briefing-state="error"', state: 'unavailable', copy: RULED.error, buttons: ['Try again']});
    expect(markup).not.toContain('the desk is offline right now.');
    expect(detailsOf(markup)).toContain('<dt>Reason</dt><dd>api_unavailable</dd>');
    expect(detailsOf(markup)).toContain('<dt>Missing</dt><dd>completed_observation</dd>');
  });

  test('no discovery carries the ruled copy and points to Discover', () => {
    const markup = renderToStaticMarkup(<TodayPage topics={[]} freshness={{status: 'green'}} deskDate="2026-08-27" />);
    expectRuledFrame(markup, {wrapper: 'data-briefing-state="no_discovery"', state: 'empty', copy: RULED.noDiscovery, buttons: ['Open Discover']});
    expect(writtenNextOf(markup)).toBeUndefined();
  });
});

describe('Discover lets the package word its held frames', () => {
  const render = (props) => renderToStaticMarkup(<ExplorePage region="ZA" topics={[]} {...props} />);

  test('loading', () => {
    const markup = render({loading: true});
    expectRuledFrame(markup, {wrapper: 'data-explore-state="loading"', state: 'loading', copy: RULED.loading, buttons: []});
    expect(detailsOf(markup)).toContain('<dt>Task</dt><dd data-testid="method">Checking admitted dynamic signals</dd>');
  });

  test('stale', () => {
    const markup = render({
      topics: [admittedSignal({discovery_mode: 'not-a-declared-mode'})],
      freshness: {status: 'amber', stamp_utc: STAMP},
    });
    expectRuledFrame(markup, {wrapper: 'data-explore-state="stale"', state: 'stale', copy: RULED.stale, buttons: ['Browse evidence']});
    expect(markup).not.toContain(ROOT_SIGNAL.label);
  });

  test('an amber stamp does not hold a run that admitted a signal', () => {
    const markup = render({topics: [admittedSignal()], freshness: {status: 'amber', stamp_utc: STAMP}});
    expect(markup).toContain('data-explore-state="ready"');
    expect(markup).not.toContain('data-explore-state="stale"');
  });

  test('error keeps the source message off the surface', () => {
    const markup = render({error: {code: 'source_unreadable', message: 'source unreadable at 03:00'}});
    expectRuledFrame(markup, {wrapper: 'data-explore-state="error"', state: 'unavailable', copy: RULED.error, buttons: ['Try again']});
    expect(markup).not.toContain('source unreadable at 03:00');
    expect(detailsOf(markup)).toContain('<dt>Reason</dt><dd>source_unreadable</dd>');
  });

  /* No read recorded a code here, so the frame says the reason is unstated
     rather than naming one: completed_run_unreadable was a code no read ever
     produced, printed as though one had. */
  test('an error without a code still carries a reason code inside Details', () => {
    const markup = render({error: {message: 'the desk is offline right now.'}});
    expectRuledFrame(markup, {wrapper: 'data-explore-state="error"', state: 'unavailable', copy: RULED.error, buttons: ['Try again']});
    expect(detailsOf(markup)).toContain('<dt>Reason</dt><dd>unstated</dd>');
    expect(markup).not.toContain('completed_run_unreadable');
  });

  test('no discovery keeps both route controls', () => {
    const markup = render({freshness: {status: 'green'}});
    expectRuledFrame(markup, {wrapper: 'data-explore-state="no_discovery"', state: 'empty', copy: RULED.noDiscovery, buttons: ['Browse evidence', 'Open Source Lab']});
    expect(writtenNextOf(markup)).toBeUndefined();
  });

  /* The filter lives in route state, so a static render cannot reach the
     filtered-empty frame through ExplorePage; the Chrome mount in
     route-state-frame does. This renders the frame from the payload the route
     builds for it, and pins the route to that payload shape: the empty state
     and the clear control, no title, sentence or written next of its own. */
  test('a filter that admits nothing carries the ruled filtered-empty copy and the clear control', () => {
    const source = readFileSync(fileURLToPath(new URL('../../explore.jsx', import.meta.url)), 'utf8');
    const payload = source.match(/state="filtered-empty"[^]*?view=\{\{([^]*?)\}\}/)?.[1];
    expect(payload, 'the filtered-empty payload in explore.jsx').toBeDefined();
    expect(payload).toContain("state: 'empty'");
    expect(payload).toContain("label: 'Clear the filter'");
    expect(payload).not.toMatch(/\b(?:title|body|nextAction):/);
    const facts = routeRunFacts({topics: [admittedSignal()], freshness: {status: 'green'}});
    const markup = renderToStaticMarkup(
      <StateView {...routeStateView({state: 'empty', actions: [{id: 'clear-filters', label: 'Clear the filter', onClick: () => {}}]}, facts)} />,
    );
    expect(markup).toContain('data-state="empty"');
    expect(titleOf(markup)).toBe(RULED.filteredEmpty[0]);
    expect(sentenceOf(markup)).toBe(RULED.filteredEmpty[1]);
    expect(writtenNextOf(markup)).toBeUndefined();
    expect(buttonsOf(markup)).toEqual(['Clear the filter']);
    expect(surfaceOf(markup)).not.toMatch(/\b[a-z0-9]+_[a-z0-9_]+\b/);
    expect(markup.match(/role="(?:alert|status)"/g) || []).toHaveLength(0);
  });
});

describe('Compare lets the package word its held frames', () => {
  test('loading', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[]} loading />);
    expectRuledFrame(markup, {wrapper: 'data-compare-state="loading"', state: 'loading', copy: RULED.loading, buttons: []});
  });

  test('error', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[]} error={{code: 'api_unavailable', message: 'unreadable'}} />);
    expectRuledFrame(markup, {wrapper: 'data-compare-state="error"', state: 'unavailable', copy: RULED.error, buttons: ['Try again']});
    expect(markup).not.toContain('unreadable');
  });

  test('no admitted signal reads as no discovery', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[]} />);
    expectRuledFrame(markup, {wrapper: 'data-compare-state="insufficient"', state: 'empty', copy: RULED.noDiscovery, buttons: ['Open Discover']});
  });

  test('one admitted signal reads as not enough to compare', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[admittedSignal()]} />);
    expectRuledFrame(markup, {wrapper: 'data-compare-state="insufficient"', state: 'empty', copy: RULED.insufficient, buttons: ['Open Discover']});
    expect(writtenNextOf(markup)).toBeUndefined();
  });
});

describe('the route builder passes structure and the ruled error copy', () => {
  const bare = routeRunFacts({});

  test('a failed read carries a reason code, never the desk message', () => {
    const view = routeStateView({state: 'error', code: 'api_unavailable', message: 'the desk is offline right now.'}, bare);
    expect(view).toMatchObject({state: 'unavailable', reason: 'api_unavailable', missing: ['completed_observation']});
    expect(view.title).toBe(RULED.error[0]);
    expect(view.body).toBe(RULED.error[1]);
    expect(JSON.stringify(view)).not.toContain('offline');
    /* A caller that hands over no code gets unstated, never a code no read
       recorded. */
    expect(routeStateView({state: 'error', message: 'offline'}, bare).reason).toBe('unstated');
  });

  test('stale and unavailable pass no title or sentence of their own', () => {
    const dated = routeStateView({state: 'stale', checkedAt: STAMP}, bare);
    expect(dated).toMatchObject({state: 'stale', checkedAt: STAMP, reason: 'freshness_amber'});
    expect(dated.title).toBeUndefined();
    expect(dated.body).toBeUndefined();
    const undated = routeStateView({state: 'stale'}, bare);
    expect(undated).toMatchObject({state: 'unavailable', reason: 'checked_at_missing', missing: ['checked_at']});
    expect(undated.title).toBeUndefined();
    const unavailable = routeStateView({state: 'unavailable', missing: ['evidence_authority']}, bare);
    expect(unavailable).toMatchObject({state: 'unavailable', reason: 'closed_evidence_missing', missing: ['evidence_authority']});
    expect(unavailable.body).toBeUndefined();
  });

  test('a host title still passes through for the routes that keep one', () => {
    const view = routeStateView({state: 'loading', title: 'Tuning into the market', task: 'Reading'}, bare);
    expect(view.title).toBe('Tuning into the market');
    expect(view.body).toBeNull();
  });

  test('the Try again control hands over a supplied re-read', () => {
    const onRetry = () => {};
    expect(tryAgainAction(onRetry)).toMatchObject({id: 'try-again', label: 'Try again', onClick: onRetry});
    expect(typeof tryAgainAction().onClick).toBe('function');
  });
});

test('the consumer sources carry no desk sentence and no missing-field sentence', () => {
  for (const file of ['api.js', 'views.jsx', 'today.jsx', 'explore.jsx', 'compare.jsx', 'instrumentRouteModels.js']){
    const source = readFileSync(fileURLToPath(new URL(`../../${file}`, import.meta.url)), 'utf8');
    expect(source, file).not.toContain('the desk is offline right now.');
    expect(source, file).not.toMatch(/Missing: /);
  }
});
