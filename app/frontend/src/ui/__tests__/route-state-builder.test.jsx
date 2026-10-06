import {describe, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {InstrumentShell, StateView} from 'ogilvy-intelligence-design-system';

import {routeRunFacts, routeStateView} from '../../instrumentRouteModels.js';
import {checkedEpoch, checkedLabel} from '../../App.jsx';

/* Round 2, Task 10. One builder maps a route state to a StateView payload for
   Pulse, Discover and Compare. The desk rows supply the closed window and the
   method, the freshness stamp supplies the checked timestamp, and where the
   payload lacks a field the builder passes nothing so the package writes
   "not supplied" inside Details. Archive 2.0.2 exports StateView and not its
   validator, so validity is proven the way the routes meet it: the package
   renders the payload and either keeps the state or fails closed. */

const RUN = {
  run_id: 'run_20260826_dynamic_apply_v1',
  observation_start: '2026-08-20',
  observation_end: '2026-08-26',
  observation_method: 'dynamic_source_copy_apply_v1',
};
const noop = () => {};
const SENTINEL = 'state_contract_invalid';

function rowText(markup, testId){
  const inner = markup.split(`<dd data-testid="${testId}">`)[1]?.split('</dd>')[0];
  return inner === undefined ? undefined : inner.replace(/<[^>]+>/g, '');
}

function rendered(view){
  const markup = renderToStaticMarkup(<StateView {...view} />);
  return {
    markup,
    state: markup.match(/data-state="([^"]+)"/)?.[1],
    window: rowText(markup, 'window'),
    method: rowText(markup, 'method'),
  };
}

describe('routeRunFacts', () => {
  test('reads the window, method and stamp from the desk payload', () => {
    const facts = routeRunFacts({
      topics: [{signal: {...RUN, signal_id: 'a'}}, {...RUN, signal_id: 'b'}],
      freshness: {status: 'amber', stamp_utc: '2026-08-27T06:30:00+00:00'},
    });
    expect(facts).toEqual({
      window: {start: '2026-08-20', end: '2026-08-26'},
      method: {label: `Run ${RUN.run_id}`, value: RUN.observation_method},
      checkedAt: '2026-08-27T06:30:00.000Z',
    });
  });

  test('claims nothing when the rows disagree or carry no run', () => {
    expect(routeRunFacts({topics: [], freshness: null})).toEqual({window: null, method: null, checkedAt: null});
    const facts = routeRunFacts({
      topics: [{...RUN}, {...RUN, observation_end: '2026-08-25', observation_method: 'other'}],
      freshness: {status: 'green', stamp_utc: 'not a time'},
    });
    expect(facts).toEqual({window: null, method: null, checkedAt: null});
  });

  test('reads a date from a timestamped observation bound and refuses an inverted window', () => {
    expect(routeRunFacts({topics: [{observation_start: '2026-08-01T00:00:00Z', observation_end: '2026-08-26T00:00:00Z'}]}).window)
      .toEqual({start: '2026-08-01', end: '2026-08-26'});
    expect(routeRunFacts({topics: [{observation_start: '2026-08-27', observation_end: '2026-08-26'}]}).window).toBeNull();
  });
});

describe('routeStateView', () => {
  const facts = routeRunFacts({topics: [{...RUN}], freshness: {status: 'amber', stamp_utc: '2026-08-27T06:30:00Z'}});
  const bare = routeRunFacts({});

  test('every mapped variant is accepted by the package and carries the run facts it was given', () => {
    const cases = [
      ['loading', {state: 'loading', title: 'Preparing', task: 'Checking', body: 'Reading.'}, 'loading'],
      ['error', {state: 'error', title: 'Unavailable', body: 'Could not read.', code: 'offline'}, 'unavailable'],
      ['stale', {state: 'stale', title: 'Held', body: 'Withheld.', reason: 'The observation is stale.'}, 'stale'],
      ['unavailable', {state: 'unavailable', title: 'Unavailable', body: 'No authority.', reason: 'No authority.', missing: ['evidence_authority']}, 'unavailable'],
      ['empty', {state: 'empty', title: 'Nothing', body: 'No signals.', nextAction: 'Browse evidence.', actions: [{id: 'browse', label: 'Browse', onClick: noop}]}, 'empty'],
    ];
    for (const [name, input, expected] of cases){
      const dated = rendered(routeStateView(input, facts));
      expect(dated.state, `${name} with run facts`).toBe(expected);
      expect(dated.markup).not.toContain(SENTINEL);
      /* Quiet register, 23 Sept 2026: package 2.0.20 writes every ISO date a
         reader reads through readableDates, so the same 20 to 26 August
         window the run facts carry now reads the way a reader reads it. */
      expect(dated.window).toBe('20 to 26 Aug 2026');
      expect(dated.method).toBe(RUN.observation_method);
      const undated = rendered(routeStateView(input, bare));
      expect(undated.state, `${name} without run facts`).toBe(name === 'stale' ? 'unavailable' : expected);
      expect(undated.markup).not.toContain(SENTINEL);
      expect(undated.window).toBe('not supplied');
      if (name !== 'loading') expect(undated.method).toBe('not supplied');
    }
  });

  test('error maps to unavailable with its code as its reason and no invented incident receipt', () => {
    const view = routeStateView({state: 'error', title: 'Unavailable', body: 'Could not read.', code: 'offline', message: 'the desk is down'}, bare);
    expect(view.state).toBe('unavailable');
    expect(view.reason).toBe('offline');
    expect(view.missing).toEqual(['completed_observation']);
    expect(view.incidentReceipt).toBeUndefined();
    expect(view.retryAction).toBeUndefined();
    expect(JSON.stringify(view)).not.toContain('the desk is down');
  });

  test('stale takes the stamp and fails closed to unavailable without one', () => {
    const dated = routeStateView({state: 'stale', title: 'Held', body: 'Withheld.', reason: 'Stale.'}, facts);
    expect(dated.state).toBe('stale');
    expect(dated.checkedAt).toBe('2026-08-27T06:30:00Z');
    const undated = routeStateView({state: 'stale', title: 'Held', body: 'Withheld.', reason: 'Stale.'}, bare);
    expect(undated.state).toBe('unavailable');
    expect(undated.missing).toEqual(['checked_at']);
    expect(rendered(undated).markup).toContain('<dt>Missing</dt><dd>checked_at</dd>');
  });

  test('a route state with no honest variant fails closed in the package', () => {
    expect(rendered(routeStateView({state: 'success', title: 'Done', body: 'Done.'}, bare)).markup).toContain(SENTINEL);
    expect(rendered(routeStateView({state: 'thin', title: 'Thin', body: 'Thin.'}, bare)).markup).toContain(SENTINEL);
    expect(rendered(routeStateView(null, bare)).markup).toContain(SENTINEL);
  });

  test('a withholding state never carries a recommendation field', () => {
    for (const state of ['stale', 'unavailable', 'error']){
      const view = routeStateView({state, title: 'T', body: 'B', reason: 'R', message: 'M', missing: [], possibleResponse: 'Brief one pitch.'}, facts);
      expect(view.possibleResponse).toBeUndefined();
      expect(JSON.stringify(view)).not.toContain('Brief one pitch.');
    }
  });
});

/* Round 6, task 41. From 2.0.10 the package formats a checked stamp only when
   it ends in Z or a numeric offset; a T stamp with no zone designator renders
   as sent. The desk sends its stamp in several shapes, so every shape is
   pushed through the consumer's canonical form and the result must be a zoned
   string or nothing at all, never a raw clock. The package's own rule is
   observed alongside so the proof is against the installed archive, not a
   reading of it. */
describe('checked stamp admission', () => {
  const ZONED = /^\d{4}-\d{2}-\d{2}T.*(Z|[+-]\d{2}:\d{2})$/;
  const STALE = {state: 'stale', title: 'Held', body: 'Withheld.', reason: 'Stale.'};
  const shapes = [
    ['ISO with Z', '2026-09-04T06:30:00Z', '2026-09-04T06:30:00Z'],
    ['ISO with milliseconds and Z', '2026-09-04T06:30:00.250Z', '2026-09-04T06:30:00.250Z'],
    ['ISO with offset', '2026-09-04T08:30:00+02:00', '2026-09-04T06:30:00.000Z'],
    ['ISO with zero offset', '2026-09-04T06:30:00+00:00', '2026-09-04T06:30:00.000Z'],
    ['ISO without zone', '2026-09-04T06:30:00', 'zoned'],
    ['epoch seconds as a number', 1756967400, null],
    ['epoch seconds as a string', '1756967400', null],
    ['date only', '2026-09-04', '2026-09-04T00:00:00.000Z'],
    ['null', null, null],
  ];

  function checkedCell(markup){
    return markup.split('<dd data-testid="checked">')[1]?.split('</dd>')[0];
  }

  test('every desk stamp shape canonicalises to a zoned string or nothing, under each freshness key', () => {
    for (const [name, sent, want] of shapes){
      for (const key of ['checkedAt', 'checked_at', 'stamp_utc']){
        const {checkedAt} = routeRunFacts({topics: [{...RUN}], freshness: {status: 'amber', [key]: sent}});
        if (want === null) expect(checkedAt, name + ' via ' + key).toBeNull();
        else if (want === 'zoned') expect(checkedAt, name + ' via ' + key).toMatch(ZONED);
        else expect(checkedAt, name + ' via ' + key).toBe(want);
        if (checkedAt !== null) expect(checkedAt, name + ' via ' + key).toMatch(ZONED);
      }
    }
  });

  test('a stale state carrying its own stamp in any shape reaches the package zoned or fails closed', () => {
    for (const [name, sent, want] of shapes){
      const view = routeStateView({...STALE, checkedAt: sent}, routeRunFacts({}));
      if (want === null){
        expect(view.state, name).toBe('unavailable');
        expect(view.missing, name).toEqual(['checked_at']);
      } else {
        expect(view.state, name).toBe('stale');
        expect(view.checkedAt, name).toMatch(ZONED);
        const cell = checkedCell(renderToStaticMarkup(<StateView {...view} />));
        /* Quiet register, 23 Sept 2026: package 2.0.20 takes the check time
           month from the one list its readable dates use, which spells
           September "Sept", so the month is matched against that list.
           Round three, 24 Sept 2026: package 2.0.22 writes the check time in
           the desk's own zone, South African time, and names it SAST where it
           wrote UTC. The time is still a readable day, month and year with a
           named zone. */
        expect(cell, name).toMatch(/^<time dateTime="[^"]+">\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept|Oct|Nov|Dec) \d{4}, \d{2}:\d{2} SAST<\/time>$/);
      }
    }
  });

  /* The shell strip hands its stamp straight to the package renderer, with no
     state contract in front of it, so it is where the 2.0.10 rule is visible:
     a zoned stamp is formatted, a T stamp with no zone is written as sent. */
  test('the installed package formats a zoned shell stamp and writes an unzoned one as sent', () => {
    /* Quiet register, 23 Sept 2026: package 2.0.21 marks a check older than
       the desk's three hour edge as stale on the strip and writes its age in
       words beside the time, whole hours under two days and whole days from
       there, and a host's own short age keeps its words. So the strip is read
       with its stale mark, and the age a stamp gets is matched by its form,
       since it is measured against the clock at render. */
    const strip = (checkedAt) => {
      const found = renderToStaticMarkup(
        <InstrumentShell route="briefing" marketScope={['ZA']} checkedAt={checkedAt}><h1>Workspace</h1></InstrumentShell>,
      ).match(/<span class="instrument-checked-at"( data-checked-stale="")?>Checked ((?:[^<]|<(?!\/?span)|<span[^>]*>[^<]*<\/span>)*)<\/span>/);
      return found ? {stale: Boolean(found[1]), text: found[2]} : undefined;
    };
    const AGE = '<span class="instrument-checked-stale instrument-nobreak">\\d+ (?:hours|days) ago</span>';
    /* Quiet register, 23 Sept 2026: package 2.0.20 spells September "Sept"
       in a check time, from the same month list its readable dates use.
       Round three, 24 Sept 2026: package 2.0.22 writes a zoned check time in
       South African time and names the zone SAST where it wrote UTC, so
       06:30 UTC reads 08:30 SAST. Both zoned shapes still land on the same
       instant, and the dateTime attribute still carries the stamp as sent. */
    expect(strip('2026-09-04T06:30:00Z').stale).toBe(true);
    expect(strip('2026-09-04T06:30:00Z').text).toMatch(new RegExp(`^<time dateTime="2026-09-04T06:30:00Z">4 Sept 2026, 08:30 SAST</time>, ${AGE}$`));
    expect(strip('2026-09-04T08:30:00+02:00').stale).toBe(true);
    expect(strip('2026-09-04T08:30:00+02:00').text).toMatch(new RegExp(`^<time dateTime="2026-09-04T08:30:00\\+02:00">4 Sept 2026, 08:30 SAST</time>, ${AGE}$`));
    expect(strip('2026-09-04T06:30:00')).toEqual({stale: false, text: '2026-09-04T06:30:00'});
    expect(strip('9h ago')).toEqual({stale: true, text: '<span class="instrument-checked-stale">9h ago</span>'});
  });

  /* The state frame admits only the consumer's canonical form. An unzoned or
     offset stamp handed to it raw fails the whole frame closed, so the
     canonical form is what keeps a stale frame on screen at all. */
  test('the state frame fails closed on any stamp the consumer would have canonicalised', () => {
    for (const sent of ['2026-09-04T06:30:00', '2026-09-04T08:30:00+02:00', '2026-09-04']){
      const markup = renderToStaticMarkup(<StateView {...STALE} checkedAt={sent} />);
      expect(markup, sent).toContain(SENTINEL);
      expect(checkedCell(markup), sent).toBe('not supplied');
    }
  });

  test('the shell freshness label is an age, never a stamp', () => {
    const now = Date.UTC(2026, 8, 4, 12, 0, 0);
    for (const ageHours of [0, 0.2, 9, 47, 48, 200]){
      const label = checkedLabel(checkedEpoch({age_hours: ageHours}, now), now);
      expect(label).toMatch(/^\d+[mhd] ago$/);
      expect(label).not.toMatch(/\d{4}-\d{2}-\d{2}T/);
    }
    expect(checkedLabel(checkedEpoch({age_hours: 'soon'}), now)).toBe('');
    expect(checkedLabel(null, now)).toBe('');
  });
});
