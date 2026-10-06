import {describe, expect, test} from 'bun:test';
import {spawnSync} from 'node:child_process';
import {existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {build} from 'esbuild';

import {ComparePanel} from '../../compare.jsx';
import {ExplorePage} from '../../explore.jsx';
import {FieldworkPage} from '../../fieldwork.jsx';
import * as models from '../../instrumentRouteModels.js';
import {routeStateView} from '../../instrumentRouteModels.js';
import * as listen from '../../listen.jsx';
import {TodayPage} from '../../today.jsx';
import {ROOT_SIGNAL, freshFixture} from './fixtures/instrument-payloads.js';

/* Round 2, Task 10. Pulse, Discover and Compare route every held, withheld,
   loading, error, no-discovery, insufficient and filtered-empty branch through
   the package StateView. The density law: each renders the state frame, the
   closed window it read, the method behind it and a next action, and the
   window and method come from the desk payload wherever the rows carry them.
   No host h1 survives in those branches, the wrapper attributes stay so the
   route assertions and the QA harness keep their handles, and a state that
   withholds a recommendation never starts offering one. Since 2.0.6 the
   package words the frames and an absent fact reads "not supplied" inside
   Details; route-held-copy.test.jsx holds the copy assertions. */

const RUN = Object.freeze({
  run_id: 'run_20260826_dynamic_apply_v1',
  observation_start: '2026-08-20',
  observation_end: '2026-08-26',
  observation_method: 'dynamic_source_copy_apply_v1',
});
const STAMP = '2026-08-27T06:30:00Z';
/* Quiet register, 23 Sept 2026: package 2.0.20 writes the closed window
   through readableDates, so the 2026-08-20 to 2026-08-26 window the rows
   carry reads the way a reader reads it. The window itself does not move. */
const WINDOW_TEXT = '20 to 26 Aug 2026';
const WINDOW_MISSING = 'not supplied';
const METHOD_MISSING = 'not supplied';
const RECOMMENDATION = ROOT_SIGNAL.possible_response;
const {ListenPage} = listen;

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

const stripText = (markup) => markup.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();

const CHROME = [
  process.env.CHROME_PATH,
  process.env.CHROME_BIN,
  String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
  String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean).find((candidate) => existsSync(candidate)) || null;

/* The filtered-empty branch exists only after a reader narrows the admitted
   list, so it is reached the way a reader reaches it: mount Discover with one
   ready signal and press the thin filter. */
async function mountedFilteredEmpty(rows){
  const root = fileURLToPath(new URL('../../../../', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'lp-filtered-empty-'));
  const bundlePath = join(directory, 'probe.js');
  const htmlPath = join(directory, 'probe.html');
  const profilePath = join(directory, 'chrome-profile');
  const entry = `
    import React from 'react';
    import {createRoot} from 'react-dom/client';
    import {flushSync} from 'react-dom';
    import {ExplorePage} from './frontend/src/explore.jsx';
    const wait = (ms=30) => new Promise((resolve) => setTimeout(resolve, ms));
    (async () => {
      try {
        const mount = document.getElementById('root');
        flushSync(() => createRoot(mount).render(React.createElement(ExplorePage, {
          topics: ${JSON.stringify(rows)}, region: 'ZA', freshness: {status: 'green'},
        })));
        await wait();
        const before = mount.innerHTML;
        document.querySelector('[data-filter="thin"]').click();
        await wait(); await wait();
        document.getElementById('result').textContent = btoa(JSON.stringify({before, after: mount.innerHTML}));
      } catch (error) {
        document.getElementById('result').textContent = btoa(JSON.stringify({error: String(error && error.stack || error)}));
      }
    })();
  `;
  try {
    await build({
      stdin: {contents: entry, loader: 'jsx', resolveDir: root, sourcefile: 'filtered-empty.jsx'},
      bundle: true,
      define: {'process.env.NODE_ENV': '"production"'},
      format: 'iife',
      jsx: 'automatic',
      nodePaths: [join(root, 'frontend', 'node_modules')],
      outfile: bundlePath,
      platform: 'browser',
      plugins: [{name: 'ignore-css', setup(builder){
        builder.onResolve({filter: /\\.css$/}, () => ({path: 'empty', namespace: 'css'}));
        builder.onLoad({filter: /.*/, namespace: 'css'}, () => ({contents: '', loader: 'js'}));
      }}],
    });
    writeFileSync(htmlPath, '<!doctype html><html><body><div id="root"></div><pre id="result">pending</pre><script src="./probe.js"></script></body></html>', 'utf8');
    const run = spawnSync(CHROME, [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--virtual-time-budget=5000', '--dump-dom', `--user-data-dir=${profilePath}`,
      pathToFileURL(htmlPath).href,
    ], {encoding: 'utf8', timeout: 20000, windowsHide: true, maxBuffer: 64 * 1024 * 1024});
    expect(run.error).toBeUndefined();
    expect(run.status).toBe(0);
    const encoded = run.stdout.match(/<pre id="result">([^<]+)<\/pre>/)?.[1];
    expect(encoded).toBeTruthy();
    const result = JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'));
    expect(result.error).toBeUndefined();
    return result;
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}

function frameOf(markup){
  const frames = markup.match(/<section class="state-view"[^>]*data-state-frame=""[^>]*>[\s\S]*?<\/section>/g) || [];
  return frames;
}

function expectStateFrame(markup, {wrapper, state, window, method}){
  expect(markup).toContain(wrapper);
  expect(markup).toContain('data-state-frame=""');
  expect(markup).toContain(`data-state="${state}"`);
  expect(markup).toContain('<dt>Closed window</dt>');
  const windowRow = markup.match(/<dd data-testid="window">([\s\S]*?)<\/dd>/);
  const methodRow = markup.match(/<dd data-testid="method">([\s\S]*?)<\/dd>/);
  expect(windowRow, 'closed window row').not.toBeNull();
  expect(methodRow, 'method row').not.toBeNull();
  expect(windowRow[1].replace(/<[^>]+>/g, '')).toEqual(window);
  expect(methodRow[1].replace(/<[^>]+>/g, '')).toEqual(method);
  const next = markup.match(/<div class="state-view__next">([\s\S]*?)<\/div>\s*<\/section>/);
  /* The ruled loading card writes no next action and the routes hand it no
     control, so 2.0.7 renders no next row at all; every other card carries a
     control or a written one. */
  if (state === 'loading') expect(next, 'next action block on a loading card').toBeNull();
  else {
    expect(next, 'next action block').not.toBeNull();
    expect(next[1]).toMatch(/<button|<a |state-view__next-written/);
  }
  expect(markup, 'no host h1 in a state branch').not.toMatch(/<h1[\s>]/);
  expect(frameOf(markup).length).toBeGreaterThanOrEqual(1);
}

describe('Pulse routes its state branches through the package frame', () => {
  test('loading keeps its live region and names its task', () => {
    const markup = renderToStaticMarkup(<TodayPage topics={[]} loading />);
    expectStateFrame(markup, {
      wrapper: 'data-briefing-state="loading"',
      state: 'loading',
      window: expect.stringContaining(WINDOW_MISSING),
      method: 'Checking the completed observation',
    });
    expect(markup).toContain('role="status"');
    expect(markup).toContain('aria-live="polite"');
  });

  test('error keeps one live region and holds the desk message off the frame', () => {
    const markup = renderToStaticMarkup(
      <TodayPage topics={[]} error={{code: 'api_unavailable', message: 'Desk unavailable'}} />,
    );
    expectStateFrame(markup, {
      wrapper: 'data-briefing-state="error"',
      state: 'unavailable',
      window: expect.stringContaining(WINDOW_MISSING),
      method: expect.stringContaining(METHOD_MISSING),
    });
    /* One live region: the package frame's own status role, with no host
       card and no second alert wrapped around it. */
    expect(markup).toMatch(/<div data-briefing-state="error"><section class="state-view"/);
    expect(markup.match(/role="(?:alert|status)"/g)).toHaveLength(1);
    expect(markup).not.toContain('oi-briefing-state');
    expect(markup).not.toContain('Desk unavailable');
    expect(markup).toContain('<dt>Reason</dt><dd>api_unavailable</dd>');
  });

  /* The stale card belongs to a run that carries nothing, so it states no run
     facts of its own: the window and the method come off the rows and there
     are none. A released run is read at any ingest stamp. */
  test('stale dates the wait by the stamp and states no run facts it does not have', () => {
    const markup = renderToStaticMarkup(
      <TodayPage topics={[]} freshness={{status: 'amber', stamp_utc: STAMP}} />,
    );
    expectStateFrame(markup, {
      wrapper: 'data-briefing-state="stale"',
      state: 'stale',
      window: expect.stringContaining(WINDOW_MISSING),
      method: expect.stringContaining(WINDOW_MISSING),
    });
    expect(markup).toContain(STAMP);
    expect(markup).not.toContain(RECOMMENDATION);
    expect(markup).not.toContain('data-instrument-briefing');
  });

  test('a released run outranks the amber stamp and reaches the instrument', () => {
    const markup = renderToStaticMarkup(
      <TodayPage topics={[admittedSignal()]} freshness={{status: 'amber', stamp_utc: STAMP}} />,
    );
    expect(markup).not.toContain('data-briefing-state="stale"');
    expect(markup).toContain('data-instrument-briefing="true"');
  });

  test('stale without a stamp fails closed to unavailable rather than dating the run', () => {
    const markup = renderToStaticMarkup(
      <TodayPage topics={[]} freshness={{status: 'amber'}} />,
    );
    expectStateFrame(markup, {
      wrapper: 'data-briefing-state="stale"',
      state: 'unavailable',
      window: expect.stringContaining(WINDOW_MISSING),
      method: expect.stringContaining(WINDOW_MISSING),
    });
    expect(markup).toContain('<dt>Missing</dt><dd>checked_at</dd>');
    expect(markup).not.toContain(RECOMMENDATION);
  });

  test('unavailable evidence authority keeps its alert role and the run facts', () => {
    const markup = renderToStaticMarkup(
      <TodayPage topics={[legacySignal()]} freshness={{status: 'green'}} />,
    );
    expectStateFrame(markup, {
      wrapper: 'data-briefing-state="unavailable"',
      state: 'unavailable',
      window: WINDOW_TEXT,
      method: RUN.observation_method,
    });
    expect(markup).toMatch(/<div data-briefing-state="unavailable"><section class="state-view"/);
    expect(markup.match(/role="(?:alert|status)"/g)).toHaveLength(1);
    expect(markup).not.toContain('oi-briefing-state');
    expect(markup).toContain('<dt>Missing</dt><dd>evidence_authority</dd>');
    expect(markup).not.toContain(RECOMMENDATION);
  });

  test('no discovery renders the frame in place of the package briefing and points to Discover', () => {
    const markup = renderToStaticMarkup(<TodayPage topics={[]} freshness={{status: 'green'}} deskDate="2026-08-27" />);
    expectStateFrame(markup, {
      wrapper: 'data-briefing-state="no_discovery"',
      state: 'empty',
      window: expect.stringContaining(WINDOW_MISSING),
      method: expect.stringContaining(METHOD_MISSING),
    });
    expect(markup).toContain('data-run-date="2026-08-27"');
    /* Round three, 24 Sept 2026: the no discovery body says the run finished
       without a signal strong enough to show, in a strategist's words rather
       than the engine's "admitted" and "dynamic". Same frame, same assertion. */
    expect(markup).toContain('The run finished without a signal strong enough to show.');
    expect(markup).toMatch(/<button[^>]*class="state-view__action"[^>]*>Open Discover<\/button>/);
    expect(markup).not.toContain('data-instrument-briefing');
    expect(markup).not.toContain(RECOMMENDATION);
  });

  test('a signal without a Ribbon series renders the frame with the run facts', () => {
    const payload = admittedSignal();
    delete payload.ribbon_series;
    const markup = renderToStaticMarkup(<TodayPage topics={[payload]} freshness={{status: 'green'}} />);
    expectStateFrame(markup, {
      wrapper: 'data-briefing-state="unavailable"',
      state: 'unavailable',
      window: WINDOW_TEXT,
      method: RUN.observation_method,
    });
    expect(markup).toContain('<dt>Missing</dt><dd>ribbon_series</dd>');
    expect(markup).not.toContain('data-instrument-briefing');
    expect(markup).not.toContain(RECOMMENDATION);
  });
});

describe('Discover routes its state branches through the package frame', () => {
  const render = (props) => renderToStaticMarkup(<ExplorePage region="ZA" topics={[]} {...props} />);

  test('error reads as unavailable and holds the source message off the frame', () => {
    const markup = render({error: {message: 'source unreadable'}});
    expectStateFrame(markup, {
      wrapper: 'data-explore-state="error"',
      state: 'unavailable',
      window: expect.stringContaining(WINDOW_MISSING),
      method: expect.stringContaining(METHOD_MISSING),
    });
    expect(markup).toContain('Could not read the run');
    expect(markup).not.toContain('source unreadable');
  });

  test('loading keeps its live region', () => {
    const markup = render({loading: true});
    expectStateFrame(markup, {
      wrapper: 'data-explore-state="loading"',
      state: 'loading',
      window: expect.stringContaining(WINDOW_MISSING),
      method: 'Checking admitted dynamic signals',
    });
    expect(markup).toContain('Reading the completed run');
    expect(markup).toContain('role="status"');
    expect(markup).toContain('aria-live="polite"');
  });

  test('stale takes the window and method from the rows and withholds every signal', () => {
    const markup = render({
      topics: [admittedSignal({discovery_mode: 'not-a-declared-mode'})],
      freshness: {status: 'amber', stamp_utc: STAMP},
    });
    expectStateFrame(markup, {
      wrapper: 'data-explore-state="stale"',
      state: 'stale',
      window: WINDOW_TEXT,
      method: RUN.observation_method,
    });
    expect(markup).toContain('Held until the next run');
    expect(markup).toContain(STAMP);
    expect(markup).not.toContain(ROOT_SIGNAL.label);
    expect(markup).not.toContain(RECOMMENDATION);
  });

  test('no discovery keeps both route buttons as the next action', () => {
    const markup = render({freshness: {status: 'green'}});
    expectStateFrame(markup, {
      wrapper: 'data-explore-state="no_discovery"',
      state: 'empty',
      window: expect.stringContaining(WINDOW_MISSING),
      method: expect.stringContaining(METHOD_MISSING),
    });
    expect(markup).toContain('No signals this run');
    /* Round three, 24 Sept 2026: the no discovery body says the run finished
       without a signal strong enough to show, in a strategist's words rather
       than the engine's "admitted" and "dynamic". Same frame, same assertion. */
    expect(markup).toContain('The run finished without a signal strong enough to show.');
    expect(markup).toMatch(/<button[^>]*class="state-view__action"[^>]*>Browse evidence<\/button>/);
    expect(markup).toMatch(/<button[^>]*class="state-view__action"[^>]*>Open Source Lab<\/button>/);
  });

  test.skipIf(!CHROME)('filtered empty keeps the ready wrapper and the count live region and offers the clear control', async () => {
    const {before, after} = await mountedFilteredEmpty([admittedSignal()]);
    expect(before).toContain('class="discover-instrument"');
    expect(before).not.toContain('data-explore-state="filtered-empty"');
    expect(after).toContain('data-explore-state="ready"');
    expect(after).not.toContain('class="discover-instrument"');
    expectStateFrame(after, {
      wrapper: 'data-explore-state="filtered-empty"',
      state: 'empty',
      window: WINDOW_TEXT,
      method: RUN.observation_method,
    });
    /* Quiet register, 23 Sept 2026: the count says what the reader sees, in plain
       words; "admitted" is the engine's word for a signal that passed its gate. */
    expect(after).toMatch(/<p role="status">0 of 1 signals shown<\/p>/);
    expect(after).not.toContain('admitted signals');
    expect(after).toContain('<h2 class="state-view__title">Nothing matches this filter</h2>');
    /* Quiet register, 23 Sept 2026: package 2.0.21 says what to do on an
       empty filter in plain words rather than restating that nothing was
       admitted. */
    expect(after).toContain('<p class="state-view__body">Try another filter, or clear it to see every signal.</p>');
    expect(after).toMatch(/<button class="state-view__action" type="button">Clear the filter<\/button>/);
    expect(after).not.toContain(RECOMMENDATION);
  });
});

describe('Compare routes its state branches through the package frame', () => {
  test('error reads as unavailable and holds the message off the frame', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[]} error={{message: 'desk offline at 03:00'}} />);
    expectStateFrame(markup, {
      wrapper: 'data-compare-state="error"',
      state: 'unavailable',
      window: expect.stringContaining(WINDOW_MISSING),
      method: expect.stringContaining(METHOD_MISSING),
    });
    expect(markup).toContain('Could not read the run');
    expect(markup).not.toContain('desk offline at 03:00');
  });

  test('loading keeps its live region', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[]} loading />);
    expectStateFrame(markup, {
      wrapper: 'data-compare-state="loading"',
      state: 'loading',
      window: expect.stringContaining(WINDOW_MISSING),
      method: 'Checking which discovered signals can be compared',
    });
    expect(markup).toContain('role="status"');
    expect(markup).toContain('aria-live="polite"');
  });

  test('insufficient with one admitted signal takes the run facts from that row', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[admittedSignal()]} />);
    expectStateFrame(markup, {
      wrapper: 'data-compare-state="insufficient"',
      state: 'empty',
      window: WINDOW_TEXT,
      method: RUN.observation_method,
    });
    expect(stripText(markup)).toContain('The completed run admitted 1 signal. Comparison needs two.');
    expect(markup).toMatch(/<button[^>]*class="state-view__action"[^>]*>Open Discover<\/button>/);
    expect(markup).not.toContain(RECOMMENDATION);
  });

  test('insufficient with no admitted signal reads as no discovery', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[]} />);
    expectStateFrame(markup, {
      wrapper: 'data-compare-state="insufficient"',
      state: 'empty',
      window: expect.stringContaining(WINDOW_MISSING),
      method: expect.stringContaining(METHOD_MISSING),
    });
    /* Round three, 24 Sept 2026: the no discovery body says the run finished
       without a signal strong enough to show, in a strategist's words rather
       than the engine's "admitted" and "dynamic". Same frame, same assertion. */
    expect(stripText(markup)).toMatch(/finished without a signal strong enough to show/i);
  });
});

/* Round 3, Task 18. Fieldwork waits 1.9 to 4.0 seconds on Source Lab, and
   for that window the workspace held a host kicker and a host title and
   nothing that reserved the instrument's geometry. The loading branch now
   renders the package frame through the same builder, with a band the
   height of the settled instrument's header: that header measures 58px, the
   package reserves in 24px rows, so three rows is the smallest band that
   covers it. */
describe('Fieldwork routes its loading branch through the package frame', () => {
  /* Restated 2 October 2026: 42's Fieldwork no longer reads the package
     instrument, so its loading branch is the page's own: the lead every
     settle shares, its standing title, a busy region and one status line,
     and none of the roster it has not read yet. */
  test('loading renders the frame with no band, under the route lead', () => {
    const markup = renderToStaticMarkup(<FieldworkPage loading />);
    expect(markup).toContain('data-fieldwork-state="loading"');
    expect(markup).toContain('aria-busy="true"');
    expect(markup).toContain('role="status"');
    expect(markup).not.toContain('state-view__reserved');
    expect(markup).toContain('fieldwork-lead');
    expect(markup).toContain('>Fieldwork</h1>');
    expect(markup).not.toContain('fieldwork-day');
    expect(markup).not.toContain('fieldwork-source');
  });

  test('the builder honours a reserved row count between one and five and holds three otherwise', () => {
    expect(routeStateView({state: 'loading', title: 'x', task: 'y', reservedRows: 3}).reservedRows).toBe(3);
    expect(routeStateView({state: 'loading', title: 'x', task: 'y', reservedRows: 5}).reservedRows).toBe(5);
    expect(routeStateView({state: 'loading', title: 'x', task: 'y', reservedRows: 9}).reservedRows).toBe(3);
    expect(routeStateView({state: 'loading', title: 'x', task: 'y', reservedRows: 0}).reservedRows).toBe(3);
    expect(routeStateView({state: 'loading', title: 'x', task: 'y'}).reservedRows).toBe(3);
  });
});

/* Round 4, task 26. Three frame matters the walk left open.

   P7. Briefing and Discover reserved three rows for the instrument that a
   ready run paints, but a cold load on the staging desk lands on the held
   frame, whose one checked line sits where the band sat, and the page
   shifted 24px when it landed. The state builder now names the rows the
   held frame lays under the body, and both loading frames reserve exactly
   that, so the band and the line it stands in for are the same height.

   P32. Listen painted a still drum figure inside a status region while the
   first page of mentions was read, then grew by the whole page when they
   landed. It now renders the package loading frame with a reserved band,
   inside a host wrapper that holds the first page's geometry.

   P33. Fieldwork re-read its two projections whenever the app re-rendered
   above it, and each re-read replaced the painted instrument with the
   loading frame. A re-read now keeps the instrument, marks the region busy
   and says so in a status line. */
describe('the loading frames match the frames that settle over them', () => {
  test('the state builder names the rows the held frame lays under its body', () => {
    expect(models.HELD_FRAME_ROWS, 'HELD_FRAME_ROWS is exported').toBeDefined();
    expect(Number.isInteger(models.HELD_FRAME_ROWS.stale)).toBe(true);
    expect(models.HELD_FRAME_ROWS.stale).toBeGreaterThanOrEqual(1);
    expect(models.HELD_FRAME_ROWS.stale).toBeLessThanOrEqual(5);
  });

  test('Briefing and Discover render the loading card with no band since 2.0.6 retired it', () => {
    const briefing = renderToStaticMarkup(<TodayPage topics={[]} loading />);
    const discover = renderToStaticMarkup(<ExplorePage region="ZA" topics={[]} loading />);
    for (const markup of [briefing, discover]){
      expect(markup).toContain('data-state="loading"');
      expect(markup).not.toContain('state-view__reserved');
    }
  });
});

/* Round 5, task 27, P32. The Listen reserve is not a fixture constant: the
   settled render remembers its mention row count and row height per market
   for the session, and the next load of that market reserves that geometry
   under the frame. A cold load with nothing remembered reserves only the
   package frame's default band, because a guessed row height is what grew
   main by 2,832px on staging. */
describe('Listen reserves the last settled page, not a constant', () => {
  const storage = (entries = {}) => ({
    getItem: (key) => (key in entries ? entries[key] : null),
    setItem: (key, value) => { entries[key] = String(value); },
  });

  test('the reserve helpers read and write one record per market', () => {
    expect(typeof listen.listenReserve).toBe('function');
    expect(typeof listen.rememberListenReserve).toBe('function');
    const store = storage();
    expect(listen.listenReserve('ZA', store)).toBeNull();
    listen.rememberListenReserve('ZA', 30, 5540, store);
    expect(listen.listenReserve('ZA', store)).toEqual({rows: 30, rowHeight: 185});
    expect(listen.listenReserve('NG', store)).toBeNull();
    /* A settled page with no rows, or a storage that throws, remembers nothing. */
    listen.rememberListenReserve('KE', 0, 0, store);
    expect(listen.listenReserve('KE', store)).toBeNull();
    expect(listen.listenReserve('ZA', {getItem: () => { throw new Error('blocked'); }})).toBeNull();
    expect(listen.listenReserve('ZA', storage({'listen-reserve:ZA': 'not json'}))).toBeNull();
  });

  test('a cold load with nothing remembered reserves the package frame default and no host band', () => {
    const before = globalThis.sessionStorage;
    globalThis.sessionStorage = storage();
    try {
      const markup = renderToStaticMarkup(<ListenPage region="ZA" setRegion={() => {}} session={null} onAuth={() => {}} />);
      expect(markup).toContain('data-state-frame=""');
      expect(markup).toContain('data-state="loading"');
      expect(markup).toContain('role="status"');
      expect(markup).not.toContain('state-view__reserved');
      expect(markup).toMatch(/class="listen-wait"(?![^>]*data-reserved-mentions)/);
      expect(markup).not.toContain('--reserved-mention');
      expect(markup).not.toContain('ld-drum');
      expect(markup).not.toContain('es-title');
    } finally {
      globalThis.sessionStorage = before;
    }
  });

  test('a load that remembers the market reserves its rows at its row height', () => {
    const before = globalThis.sessionStorage;
    globalThis.sessionStorage = storage({'listen-reserve:ZA': JSON.stringify({rows: 30, rowHeight: 185})});
    try {
      const markup = renderToStaticMarkup(<ListenPage region="ZA" setRegion={() => {}} session={null} onAuth={() => {}} />);
      expect(markup).toMatch(/class="listen-wait"[^>]*data-reserved-mentions="30"/);
      expect(markup).toMatch(/style="--reserved-mentions:30;--reserved-mention-height:185px"/);
      expect(markup).not.toContain('state-view__reserved');
    } finally {
      globalThis.sessionStorage = before;
    }
  });

  test('the wrapper reserves from the remembered geometry and carries no fixture row height', () => {
    const css = readFileSync(new URL('../../styles/lexlisten.css', import.meta.url), 'utf8');
    expect(css).not.toMatch(/133px/);
    expect(css).toMatch(/\.listen-wait\[data-reserved-mentions\]\s*\{[^}]*min-block-size:\s*calc\(var\(--reserved-mentions\) \* var\(--reserved-mention-height\)\)/);
  });
});

/* The unavailable frame on Fieldwork names the retry the workspace already
   owns, so the busy branch is reachable from the page rather than from a
   test alone. */
/* Restated 2 October 2026 against 42's Fieldwork (GET /api/fieldwork): the
   frames that can use a retry offer Try again, wired to onRetry, and a re-read
   keeps the painted roster instead of mounting the loading frame. */
describe('Fieldwork offers its retry from the frames that can use one', () => {
  const days = JSON.parse(readFileSync(new URL('./fixtures/fieldwork42_days.json', import.meta.url), 'utf8'));
  const unavailable = {...days.ready, markets: [], plan_state: 'unavailable', plan_note: 'The collection settings could not be read, so the source list is missing.'};

  test('the unavailable settings frame carries a Try again control wired to onRetry', () => {
    const markup = renderToStaticMarkup(<FieldworkPage payload={unavailable} onRetry={() => {}} />);
    expect(markup).toMatch(/data-fieldwork-state="unavailable"/);
    expect(markup).toMatch(/<div class="fieldwork-problem">[\s\S]*?<button type="button" class="fieldwork-button">Try again<\/button>[\s\S]*?<\/div>/);
  });

  test('the error frame carries the same control, and neither renders one without a handler', () => {
    const errored = renderToStaticMarkup(<FieldworkPage error={{status: 503, message: 'Not now.'}} onRetry={() => {}} />);
    expect(errored).toMatch(/data-fieldwork-state="error"/);
    expect(errored).toMatch(/<div class="fieldwork-problem">[\s\S]*?role="alert"[\s\S]*?<button type="button" class="fieldwork-button">Try again<\/button>/);
    for (const markup of [
      renderToStaticMarkup(<FieldworkPage payload={unavailable} />),
      renderToStaticMarkup(<FieldworkPage error={{status: 503, message: 'Not now.'}} />),
      renderToStaticMarkup(<FieldworkPage error={{status: 401, auth: true}} onRetry={() => {}} />),
    ]){
      expect(markup).not.toContain('Try again');
    }
  });

  test('the workspace hands the page its retry', () => {
    const source = readFileSync(new URL('../../fieldwork.jsx', import.meta.url), 'utf8');
    expect(source).toMatch(/export function FieldworkPage\(\{[^}]*onRetry/);
    expect(source).toMatch(/<FieldworkPage \{\.\.\.load\} onDate=\{setDate\} onRetry=\{/);
  });
});

describe('Fieldwork keeps its painted roster through a re-read', () => {
  const days = JSON.parse(readFileSync(new URL('./fixtures/fieldwork42_days.json', import.meta.url), 'utf8'));

  test('a busy re-read marks the region and says so, and never mounts the loading frame', () => {
    const markup = renderToStaticMarkup(<FieldworkPage payload={days.ready} busy />);
    expect(markup).toMatch(/<section class="page fieldwork-page" data-fieldwork-state="ready" aria-busy="true"/);
    expect(markup).toContain('fieldwork-source');
    expect(markup).toMatch(/<p class="fieldwork-state__note fieldwork-state__note--busy" role="status">Reading the day again<\/p>/);
  });

  test('a settled page carries no busy mark and no re-read line', () => {
    const markup = renderToStaticMarkup(<FieldworkPage payload={days.ready} />);
    expect(markup).not.toContain('aria-busy');
    expect(markup).not.toContain('Reading the day again');
  });

  test('the workspace re-reads on a new day or a retry alone, and a re-read keeps the painted view', () => {
    const source = readFileSync(new URL('../../fieldwork.jsx', import.meta.url), 'utf8');
    const workspace = source.slice(source.indexOf('export function FieldworkWorkspace'));
    expect(workspace).toMatch(/\}, \[date, tick\]\);/);
    expect(workspace).not.toMatch(/\[date, tick, onAuth\]/);
    expect(workspace).toMatch(/busy: true/);
  });
});

test('the Listen reserve is remembered only from an unfiltered first page', () => {
  const source = readFileSync(fileURLToPath(new URL('../../listen.jsx', import.meta.url)), 'utf8');
  const effect = source.slice(source.indexOf('rememberListenReserve(market, page.length'), source.indexOf('rememberListenReserve(market, page.length') + 400);
  const guard = source.slice(source.lastIndexOf('useEffect(() => {', source.indexOf('rememberListenReserve(market, page.length')), source.indexOf('rememberListenReserve(market, page.length'));
  expect(guard).toMatch(/if \(first\.state !== 'ready' \|\| needle \|\| sentiment \|\| category/);
  expect(effect).toMatch(/\[first\.state, rows, needle, sentiment, category, market\]/);
});
