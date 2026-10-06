/* The rail carries the five jobs and nothing else.

   Round 4, task 25 gave the shell a utility slot so every view was reachable
   from Briefing by a rendered link. Round 5, task 32 withdraws that: sixteen
   rail entries overwhelmed the reader, so the shell renders the five job
   links and no utility slot, at every width and inside the 390 menu. The
   eleven routes outside the jobs stay addressable by hash and render as
   before, but no rail, menu, strip or in-page shell link opens them. Two
   views were never link destinations: research is the Build job under its
   old name and resolves to the console, and topic takes a signal id and
   opens from a Briefing card. */
import {expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {InstrumentShell} from 'ogilvy-intelligence-design-system';

import * as appModule from '../../App.jsx';
import {hostRouteForInstrumentRoute, instrumentRouteForHostRoute, railPlaceForHostRoute, resolveHostRoute} from '../../App.jsx';
import {normalizeView} from '../../redesignContract.js';

const JOBS = ['pulse', 'explore', 'compare', 'console', 'fieldwork'];
const HASH_ONLY = ['method', 'source-lab', 'historical', 'network', 'lexicon', 'board', 'browse', 'map', 'seeds', 'seedpath', 'listen'];
const VIEWS = [...JOBS, ...HASH_ONLY, 'topic', 'research'];
const ALIASED = {research: 'console'};
const PARAMETERISED = ['topic'];
/* Quiet register, 23 Sept 2026: the hash-only pages that mark themselves. */
const MARKED_PAGES = {'source-lab': '/source-lab', historical: '/historical', network: '/network', listen: '/listen'};

/* The shell as App hands it the route. Quiet register, 23 Sept 2026: App
   also hands it the page the reader is on when that page is outside the
   jobs, as the one utility entry. */
function shellAt(route){
  const place = railPlaceForHostRoute(route);
  return renderToStaticMarkup(
    <InstrumentShell route={place.job} utilityLinks={place.currentPage ? [place.currentPage] : undefined} marketScope={['ZA']}>
      <h1>Briefing</h1>
    </InstrumentShell>,
  );
}

function railHrefs(markup){
  return [...markup.matchAll(/<a class="instrument-(?:job|utility)-link[^"]*" href="([^"]+)"/g)].map((match) => match[1]);
}

test('the eighteen views are the host views', () => {
  expect(VIEWS).toHaveLength(18);
  for (const view of VIEWS) expect(normalizeView(view)).toBe(view);
  expect(normalizeView('nowhere')).toBe('pulse');
});

test('Briefing renders exactly five rail links, the five jobs, and no utility slot', () => {
  const markup = shellAt('pulse');
  const hrefs = railHrefs(markup);
  expect(hrefs).toEqual(['/briefing', '/discover', '/compare', '/build', '/fieldwork']);
  expect(hrefs.map((href) => hostRouteForInstrumentRoute(href))).toEqual(JOBS);
  expect(markup).not.toContain('aria-label="Utilities"');
  expect(markup).not.toContain('instrument-utility-link');
  expect(markup).not.toContain('instrument-utility-rail');
});

/* Quiet register, 23 Sept 2026: no route links another hash-only page. A
   hash-only page the reader is on adds only its own entry, marked current,
   so the rail says where the reader is without bringing the list back. */
test('the shell links no other page outside the jobs, marks no job outside the five, and marks the page the reader is on', () => {
  for (const route of VIEWS){
    const markup = shellAt(route);
    const own = MARKED_PAGES[route];
    expect(railHrefs(markup)).toHaveLength(own ? 6 : 5);
    if (!own){
      expect(markup).not.toContain('aria-label="Utilities"');
      expect(markup).not.toContain('instrument-utility-link');
    }
    for (const legacy of HASH_ONLY) if ('/' + legacy !== own) expect(markup).not.toContain(`href="/${legacy}"`);
    const marked = [...markup.matchAll(/<a class="instrument-job-link[^"]*" href="([^"]+)" aria-current="page"/g)].map((match) => match[1]);
    expect(marked).toEqual(JOBS.includes(route) ? ['/' + instrumentRouteForHostRoute(route)] : []);
    const markedPage = [...markup.matchAll(/<a class="instrument-utility-link[^"]*" href="([^"]+)" aria-current="page"/g)].map((match) => match[1]);
    expect(markedPage).toEqual(own ? [own] : []);
  }
});

test('the eleven legacy routes are reachable by hash only', () => {
  expect(HASH_ONLY).toHaveLength(11);
  for (const route of HASH_ONLY){
    expect(normalizeView(route)).toBe(route);
    expect(resolveHostRoute(route)).toBe(route);
    expect(instrumentRouteForHostRoute(route)).toBeNull();
  }
  for (const [alias, target] of Object.entries(ALIASED)) expect(resolveHostRoute(alias)).toBe(target);
  for (const view of PARAMETERISED) expect(instrumentRouteForHostRoute(view)).toBeNull();
  expect(JOBS.length + HASH_ONLY.length + Object.keys(ALIASED).length + PARAMETERISED.length).toBe(18);
});

/* Quiet register, 23 Sept 2026: App passes the shell at most the current
   page, never a list of utilities, and exports no utility list builder. */
test('App passes the shell only the current page and exports no utility link builder', async () => {
  const source = await Bun.file(new URL('../../App.jsx', import.meta.url)).text();
  expect(source).toContain('utilityLinks={railPlace.currentPage ? [railPlace.currentPage] : undefined}');
  expect(source.match(/utilityLinks=/g)).toHaveLength(1);
  expect(source).not.toContain('UTILITY_NAV');
  expect(appModule.utilityLinksForHostRoute).toBeUndefined();
});
