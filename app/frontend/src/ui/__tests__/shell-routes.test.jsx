/* The shell routes read as one instrument.

   Round 4, task 23. The host maps its own routes onto the five jobs the
   package shell knows, and the rail marks only a job. Build has one landing.
   Research is the Build job under its old name. The utility strip keeps the
   last checked time it knew while the next desk read is on its way. Each is
   a pure mapping, so each is held here at source.

   Round 5, task 32. The rail carries the five jobs and nothing else. The
   shell gets no utility links, so it renders no utility slot on any route,
   and the routes outside the jobs are reached by hash alone. */
import {describe, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {InstrumentShell} from 'ogilvy-intelligence-design-system';

import {
  checkedEpoch,
  checkedLabel,
  hostRouteForInstrumentRoute,
  instrumentRouteForHostRoute,
  railPlaceForHostRoute,
  resolveHostRoute,
  updatedLabel,
} from '../../App.jsx';
import {PRIMARY_NAV} from '../../redesignContract.js';

const JOBS = {pulse: 'briefing', explore: 'discover', compare: 'compare', console: 'build', fieldwork: 'fieldwork'};
const OUTSIDE = ['browse', 'method', 'research', 'seeds', 'seedpath', 'lexicon', 'map', 'board', 'listen', 'network', 'historical', 'source-lab', 'topic'];
/* Quiet register, 23 Sept 2026: the pages outside the jobs a reader lands on
   and needs marked, each with the path its one rail entry carries. */
const MARKED_PAGES = {listen: '/listen', network: '/network', historical: '/historical', 'source-lab': '/source-lab'};

/* The shell as App hands it the route: the job it marks, and the one page
   outside the jobs it marks instead, as its only utility entry. */
function shellFor(route, place = railPlaceForHostRoute(route)){
  return renderToStaticMarkup(
    <InstrumentShell route={place.job} utilityLinks={place.currentPage ? [place.currentPage] : undefined} marketScope={['ZA']}>
      <h1>Workspace</h1>
    </InstrumentShell>,
  );
}

function currentLinks(markup){
  return [...markup.matchAll(/<a class="instrument-(job|utility)-link[^"]*" href="([^"]+)" aria-current="page"/g)]
    .map((match) => [match[1], match[2]]);
}

describe('the rail marks a job or nothing', () => {
  test('each of the five jobs maps to its package route', () => {
    for (const [host, job] of Object.entries(JOBS)) expect(instrumentRouteForHostRoute(host)).toBe(job);
  });

  test('a route outside the five jobs is no job, never Fieldwork', () => {
    for (const route of OUTSIDE) expect(instrumentRouteForHostRoute(route)).toBeNull();
    expect(instrumentRouteForHostRoute(undefined)).toBeNull();
  });
});

/* Quiet register, 23 Sept 2026: the rail still links only the five jobs. A
   page outside them that a reader lands on adds exactly one entry, itself,
   marked current; every other route renders no utility slot. */
describe('the rail carries the five jobs and, at most, the page the reader is on', () => {
  test('every route renders the five job links, plus only its own marked entry where it has one', () => {
    for (const route of [...Object.keys(JOBS), ...OUTSIDE]){
      const markup = shellFor(route);
      const hrefs = [...markup.matchAll(/<a class="instrument-(?:job|utility)-link[^"]*" href="([^"]+)"/g)].map((match) => match[1]);
      const own = MARKED_PAGES[route];
      expect(hrefs).toEqual(['/briefing', '/discover', '/compare', '/build', '/fieldwork', ...(own ? [own] : [])]);
      if (own){
        expect(markup.match(/instrument-utility-link/g)).toHaveLength(1);
      } else {
        expect(markup).not.toContain('aria-label="Utilities"');
        expect(markup).not.toContain('instrument-utility-rail');
      }
    }
  });

  test('Ask marks Ask, not the Build job, and Build brief still marks Build', () => {
    expect(currentLinks(shellFor('console', railPlaceForHostRoute('console', {askOpen: true})))).toEqual([['utility', '/ask']]);
    expect(currentLinks(shellFor('console', railPlaceForHostRoute('console', {askOpen: false})))).toEqual([['job', '/build']]);
    expect(railPlaceForHostRoute('console', {askOpen: true}).currentPage.label).toBe('Ask');
  });

  test('each job link returns to its own host route', () => {
    for (const [host, job] of Object.entries(JOBS)) expect(hostRouteForInstrumentRoute('/' + job)).toBe(host);
  });
});

describe('the rendered rail marks a job or nothing', () => {
  test('each job route marks its job', () => {
    for (const [host, job] of Object.entries(JOBS)){
      expect(currentLinks(shellFor(host))).toEqual([['job', '/' + job]]);
    }
  });

  /* Quiet register, 23 Sept 2026: a route outside the jobs never marks a
     job; Source Lab, Listen, Network and Historical mark their own entry. */
  test('each route outside the jobs marks no job, marks its own page where it has one, and still renders', () => {
    for (const route of OUTSIDE){
      const markup = shellFor(route);
      expect(markup).not.toContain('That route is not part of 42.');
      expect(currentLinks(markup)).toEqual(MARKED_PAGES[route] ? [['utility', MARKED_PAGES[route]]] : []);
    }
  });

  /* Quiet register, 23 Sept 2026: App hands the shell the rail place, whose
     utility entry is only ever the current page. */
  test('App hands the shell the rail place and at most the current page, with no Fieldwork fallback', async () => {
    const source = await Bun.file(new URL('../../App.jsx', import.meta.url)).text();
    expect(source).toContain('route={railPlace.job}');
    expect(source).toContain('utilityLinks={railPlace.currentPage ? [railPlace.currentPage] : undefined}');
    expect(source).not.toContain("instrumentRouteForHostRoute(route) || 'fieldwork'");
    expect(source).not.toContain('UTILITY_NAV');
  });
});

describe('Build has one landing', () => {
  test('the package rail and the declared primary navigation both land Build on the console workbench', () => {
    expect(hostRouteForInstrumentRoute('/build')).toBe('console');
    expect(PRIMARY_NAV.find((item) => item.label === 'Build').path).toBe('/console');
    expect(PRIMARY_NAV.some((item) => item.path.includes('work=brief'))).toBe(false);
  });
});

describe('research is the Build job under its old name', () => {
  test('the research view resolves to the console and the old aliases still hold', () => {
    expect(resolveHostRoute('research')).toBe('console');
    expect(resolveHostRoute('chat')).toBe('console');
    expect(resolveHostRoute('today')).toBe('pulse');
    expect(resolveHostRoute('intel')).toBe('explore');
    expect(resolveHostRoute('console')).toBe('console');
    expect(resolveHostRoute('nowhere')).toBe('pulse');
  });
});

describe('the checked time survives the desk load', () => {
  const NOW = Date.parse('2026-09-04T09:00:00Z');

  test('a desk age becomes the moment it names, and reads back as the same age', () => {
    const epoch = checkedEpoch({age_hours: 21}, NOW);
    expect(epoch).toBe(NOW - 21 * 3600000);
    expect(checkedLabel(epoch, NOW)).toBe('21h ago');
  });

  test('the label ages with the clock rather than freezing at the last read', () => {
    const epoch = checkedEpoch({age_hours: 21}, NOW);
    expect(checkedLabel(epoch, NOW + 5 * 3600000)).toBe('26h ago');
    expect(checkedLabel(epoch, NOW + 40 * 3600000)).toBe('3d ago');
  });

  /* The desk calls a check amber from 3 hours (FRESHNESS_AMBER_HOURS in
     app/src/api/bq.py) and the shell reads the age back out of the short text
     it is handed ("2h ago"), marking it stale from the same 3 hours. A rounded
     hour turned 2.5 hours into "3h ago", so the shell read amber while the
     desk was still green. The written age is whole elapsed units, so it never
     reaches the edge before the desk does, and reaches it exactly when the
     desk does. */
  test('the written age never crosses the 3 hour amber edge before the desk does', () => {
    const AMBER_HOURS = 3;
    const UNIT_HOURS = {m: 1 / 60, h: 1, d: 24};
    const shellHours = (label) => {
      const written = /^(\d+)([mhd]) ago$/.exec(label);
      expect(written, label).toBeTruthy();
      return Number(written[1]) * UNIT_HOURS[written[2]];
    };
    for (let step = 0; step <= 6 * 60; step += 1){
      const ageHours = step / 60;
      const label = checkedLabel(checkedEpoch({age_hours: ageHours}, NOW), NOW);
      expect(shellHours(label) >= AMBER_HOURS, `${ageHours}h reads ${label}`).toBe(ageHours >= AMBER_HOURS);
    }
    expect(checkedLabel(checkedEpoch({age_hours: 2.5}, NOW), NOW)).toBe('2h ago');
    expect(checkedLabel(checkedEpoch({age_hours: 2.99}, NOW), NOW)).toBe('2h ago');
    expect(checkedLabel(checkedEpoch({age_hours: 3}, NOW), NOW)).toBe('3h ago');
    expect(updatedLabel({age_hours: 2.5})).toBe('Updated 2h ago');
  });

  test('no stamp is no claim', () => {
    expect(checkedEpoch(null, NOW)).toBeNull();
    expect(checkedEpoch({}, NOW)).toBeNull();
    expect(checkedLabel(null, NOW)).toBe('');
    expect(checkedLabel(Number.NaN, NOW)).toBe('');
  });
});

/* Quiet register, 23 Sept 2026: the rail reads the console's own hash to know
   whether the reader is on Ask or on Build brief. */
describe('the rail knows when the console shows Ask', () => {
  test('work=ask, a question request and a question in the path are Ask; the landing and Build brief are not', async () => {
    const {consoleShowsAsk, WORKBENCH_NAV_EVENT_NAME} = await import('../../App.jsx');
    expect(consoleShowsAsk('#/console?work=ask')).toBe(true);
    expect(consoleShowsAsk('#/console?request=req_1')).toBe(true);
    expect(consoleShowsAsk('#/console/what%20moved')).toBe(true);
    expect(consoleShowsAsk('#/console')).toBe(false);
    expect(consoleShowsAsk('#/console?work=brief&persona_id=bsa_research')).toBe(false);
    expect(consoleShowsAsk('')).toBe(false);
    const {WORKBENCH_NAV_EVENT} = await import('../../researchLib.jsx');
    expect(WORKBENCH_NAV_EVENT_NAME).toBe(WORKBENCH_NAV_EVENT);
  });

  test('the current page entry hands back its own path, which App keeps rather than moving to Fieldwork', async () => {
    const source = await Bun.file(new URL('../../App.jsx', import.meta.url)).text();
    expect(source).toMatch(/if \(railPlace\.currentPage && nextRoute === railPlace\.currentPage\.href\) return;/);
  });
});
