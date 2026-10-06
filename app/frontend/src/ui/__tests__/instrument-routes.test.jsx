import {expect, test} from 'bun:test';
import {spawnSync} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {build} from 'esbuild';

import {ComparePanel} from '../../compare.jsx';
import {ExplorePage} from '../../explore.jsx';
import {TodayPage} from '../../today.jsx';
import {READY_SUMMARY, ROOT_SIGNAL, freshFixture} from './fixtures/instrument-payloads.js';

const CHROME = [
  process.env.CHROME_PATH,
  process.env.CHROME_BIN,
  String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
  String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean).find((candidate) => existsSync(candidate)) || null;

async function mountedShellInteraction(){
  const root = fileURLToPath(new URL('../../../../', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'lp-instrument-shell-'));
  const bundlePath = join(directory, 'probe.js');
  const htmlPath = join(directory, 'probe.html');
  const profilePath = join(directory, 'chrome-profile');
  const entry = `
    import React, {useState} from 'react';
    import {createRoot} from 'react-dom/client';
    import {flushSync} from 'react-dom';
    import {InstrumentShell} from 'ogilvy-intelligence-design-system';
    import {hostRouteForInstrumentRoute, marketScopeForRegion, regionForMarketScope} from './frontend/src/App.jsx';
    const wait = (ms=30) => new Promise((resolve) => setTimeout(resolve, ms));
    const events = [];
    function Harness({initialRegion='ZA'}){
      const [region, setRegion] = useState(initialRegion);
      const [route, setRoute] = useState('briefing');
      const applyMarketScope = (scope) => {
        const next = regionForMarketScope(region, scope);
        events.push(['market', region, scope.join(','), next]);
        setRegion(next);
      };
      return React.createElement(InstrumentShell, {
        route,
        marketScope: marketScopeForRegion(region),
        onMarketScopeChange: applyMarketScope,
        onRouteChange: (path) => {
          events.push(['route', path, hostRouteForInstrumentRoute(path)]);
          setRoute(path.slice(1));
        },
      }, React.createElement(React.Fragment, null,
        React.createElement('h1', null, 'Workspace'),
        React.createElement('button', {type: 'button', 'data-test-scope': 'one', onClick: () => applyMarketScope(['ZA'])}, 'Apply one'),
        React.createElement('button', {type: 'button', 'data-test-scope': 'all', onClick: () => applyMarketScope(['ZA', 'NG', 'KE'])}, 'Apply all')
      ));
    }
    (async () => {
      try {
        const first = document.getElementById('first');
        flushSync(() => createRoot(first).render(React.createElement(Harness, {initialRegion: 'ZA'})));
        await wait();
        const currentJob = () => {
          const active = first.querySelector('.instrument-job-link[aria-current="page"]');
          return active ? active.textContent.trim() : null;
        };
        /* Quiet register, 23 Sept 2026: package 2.0.21 writes the market scope
           as names ("Nigeria", "All markets") on the trigger and on each
           option, keeps "Market scope: ..." as the trigger's accessible name,
           and carries the market code on each option's data-market. So an
           option is found by its code and the scope is read off the accessible
           name, with the visible names recorded beside it. */
        const marketLabel = () => first.querySelector('.instrument-market-trigger').getAttribute('aria-label');
        const routeCurrentBefore = currentJob();
        const marketTriggerBefore = marketLabel();
        const menu = first.querySelector('.instrument-menu');
        menu.click(); await wait();
        const openedFocus = document.activeElement && document.activeElement.textContent.trim();
        const discover = [...first.querySelectorAll('.instrument-job-link')].find((node) => node.textContent.trim() === 'Discover');
        discover.click(); await wait();
        const routeCurrentAfter = currentJob();
        const routeFocusReturned = document.activeElement === menu;
        menu.click(); await wait();
        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); await wait();
        const escapeFocusReturned = document.activeElement === menu;
        const market = first.querySelector('.instrument-market-trigger');
        market.click(); await wait();
        first.querySelector('.instrument-market-option[data-market="NG"]').click(); await wait();
        const marketTriggerAfter = marketLabel();

        const second = document.getElementById('second');
        flushSync(() => createRoot(second).render(React.createElement(Harness, {initialRegion: 'ALL'})));
        await wait();
        const secondChecks = () => [...second.querySelectorAll('.instrument-market-option')].map((node) => [node.dataset.market, node.textContent.trim(), node.getAttribute('aria-checked')]);
        const secondLabel = () => second.querySelector('.instrument-market-trigger').getAttribute('aria-label');
        const secondText = () => second.querySelector('.instrument-market-trigger').textContent.trim();
        second.querySelector('.instrument-market-trigger').click(); await wait();
        second.querySelector('.instrument-market-option[data-market="NG"]').click(); await wait();
        const secondAfterNG = {checks: secondChecks(), label: secondLabel(), text: secondText()};
        second.querySelector('.instrument-market-option[data-market="ZA"]').click(); await wait();
        const secondAfterZA = {checks: secondChecks(), label: secondLabel(), text: secondText()};

        const third = document.getElementById('third');
        flushSync(() => createRoot(third).render(React.createElement(Harness, {initialRegion: 'ZA'})));
        await wait();
        third.querySelector('[data-test-scope="one"]').click(); await wait();
        third.querySelector('[data-test-scope="all"]').click(); await wait();
        document.getElementById('result').textContent = btoa(JSON.stringify({events, openedFocus, routeFocusReturned, escapeFocusReturned, routeCurrentBefore, routeCurrentAfter, marketTriggerBefore, marketTriggerAfter, secondAfterNG, secondAfterZA}));
      } catch (error) {
        document.getElementById('result').textContent = btoa(JSON.stringify({error: String(error && error.stack || error)}));
      }
    })();
  `;
  try {
    await build({
      stdin: {contents: entry, loader: 'jsx', resolveDir: root, sourcefile: 'shell-interaction.jsx'},
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
    writeFileSync(htmlPath, '<!doctype html><html><body><div id="first"></div><div id="second"></div><div id="third"></div><pre id="result">pending</pre><script src="./probe.js"></script></body></html>', 'utf8');
    const run = spawnSync(CHROME, [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--window-size=390,844', '--virtual-time-budget=5000', '--dump-dom',
      `--user-data-dir=${profilePath}`, pathToFileURL(htmlPath).href,
    ], {encoding: 'utf8', timeout: 20000, windowsHide: true});
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

async function mountedCompareSelection(initialRows, unavailableRows, reappearingRows){
  const root = fileURLToPath(new URL('../../../../', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'lp-compare-selection-'));
  const bundlePath = join(directory, 'probe.js');
  const htmlPath = join(directory, 'probe.html');
  const profilePath = join(directory, 'chrome-profile');
  const entry = `
    import React, {useState} from 'react';
    import {createRoot} from 'react-dom/client';
    import {flushSync} from 'react-dom';
    import {ComparePanel} from './frontend/src/compare.jsx';
    const wait = (ms=30) => new Promise((resolve) => setTimeout(resolve, ms));
    const selected = () => [...document.querySelectorAll('[data-comparison-signal][data-selected="true"]')]
      .map((node) => node.querySelector('h2').textContent.trim());
    function Harness(){
      const [rows, setRows] = useState(${JSON.stringify(initialRows)});
      window.setCompareRows = setRows;
      return React.createElement(ComparePanel, {topics: rows, region: 'ALL'});
    }
    (async () => {
      try {
        flushSync(() => createRoot(document.getElementById('root')).render(React.createElement(Harness)));
        await wait();
        for (const button of [...document.querySelectorAll('.comparison-instrument__signal-head button')].slice(0, 5)){
          button.click(); await wait();
        }
        const initiallySelected = selected();
        window.setCompareRows(${JSON.stringify(unavailableRows)}); await wait(); await wait();
        const afterUnavailable = selected();
        [...document.querySelectorAll('.comparison-instrument__signal-head button')]
          .find((button) => button.textContent.includes('Signal 5')).click();
        await wait();
        const afterReplacement = selected();
        window.setCompareRows(${JSON.stringify(reappearingRows)}); await wait(); await wait();
        const afterReappearing = selected();
        [...document.querySelectorAll('.comparison-instrument__signal-head button')]
          .find((button) => button.textContent.includes('Signal 2')).click();
        await wait();
        const afterReselect = selected();
        document.getElementById('result').textContent = btoa(JSON.stringify({
          initiallySelected, afterUnavailable, afterReplacement, afterReappearing, afterReselect,
        }));
      } catch (error) {
        document.getElementById('result').textContent = btoa(JSON.stringify({error: String(error && error.stack || error)}));
      }
    })();
  `;
  try {
    await build({
      stdin: {contents: entry, loader: 'jsx', resolveDir: root, sourcefile: 'compare-selection.jsx'},
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
    ], {encoding: 'utf8', timeout: 20000, windowsHide: true});
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

const secondSignal = (overrides = {}) => ({
  ...freshFixture(ROOT_SIGNAL),
  signal_id: 'sig_' + 'b'.repeat(64),
  label: 'Neighbourhood repair clubs',
  ...overrides,
});

const admittedSignal = (overrides = {}) => ({
  ...freshFixture(ROOT_SIGNAL),
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

test('Briefing renders the package InstrumentBriefing without local chapter navigation', () => {
  const markup = renderToStaticMarkup(
    <TodayPage topics={[freshFixture(ROOT_SIGNAL)]} freshness={{status: 'green'}} />,
  );
  expect(markup).toContain('data-instrument-briefing');
  expect(markup).not.toContain('oi-briefing__chapter-nav');
});

/* Quiet register, 23 Sept 2026: the lead's run date is read, so it is written
   "12 Sept 2026" and never as the ISO date; the ISO date stays on
   data-run-date for the hosts that read it. */
test('Briefing writes the lead run date as a reader says it', () => {
  const markup = renderToStaticMarkup(
    <TodayPage topics={[freshFixture(ROOT_SIGNAL)]} freshness={{status: 'green'}} deskDate="2026-09-12" />,
  );
  expect(markup).toContain('data-run-date="2026-09-12"');
  expect(markup).toMatch(/<p class="briefing-lead-action">Run 12 Sept 2026\. <a/);
  expect(markup).not.toContain('Run 2026-09-12');
});

test('Briefing with explicit ready Ribbon series renders the package Ribbon', () => {
  const markup = renderToStaticMarkup(
    <TodayPage topics={[freshFixture(ROOT_SIGNAL)]} freshness={{status: 'green'}} />,
  );
  expect(markup).toContain('data-instrument-briefing');
  expect(markup).toContain('data-ribbon-strand');
});

test('Briefing without explicit Ribbon series renders package unavailable state only', () => {
  const payload = freshFixture(ROOT_SIGNAL);
  delete payload.ribbon_series;
  const markup = renderToStaticMarkup(<TodayPage topics={[payload]} freshness={{status: 'green'}} />);
  expect(markup).toContain('class="state-view"');
  expect(markup).toContain('data-state="unavailable"');
  expect(markup).not.toContain('data-instrument-briefing');
  expect(markup).not.toContain('proof-ribbon');
});

test('Briefing with no Ribbon series withholds thin contradictory and unchecked presentation', () => {
  for (const state of ['thin', 'contradictory', 'unchecked']){
    const payload = freshFixture(ROOT_SIGNAL);
    payload.ribbon_series = [];
    payload.evidence_summary = structuredClone(READY_SUMMARY);
    payload.evidence_summary.state = state;
    payload.evidence_summary.limitations = [`${state} evidence`];
    if (state === 'thin'){
      payload.evidence_summary.receipts = [payload.evidence_summary.receipts[0]];
      payload.evidence_summary.independence.familyCount = 1;
      payload.evidence_summary.direction.supportingReceiptIds = ['receipt-forum'];
    } else if (state === 'contradictory'){
      payload.evidence_summary.receipts[1].direction = 'opposing';
      payload.evidence_summary.direction = {
        status: 'mixed', supportingReceiptIds: ['receipt-forum'], opposingReceiptIds: ['receipt-video'],
      };
    } else {
      payload.evidence_summary.receipts = [];
      payload.evidence_summary.independence = {status: 'unvalidated', familyCount: 0, groupingAuthority: null};
      payload.evidence_summary.direction = {status: 'unknown', supportingReceiptIds: [], opposingReceiptIds: []};
    }
    const markup = renderToStaticMarkup(<TodayPage topics={[payload]} freshness={{status: 'green'}} />);
    expect(markup).toContain('data-state="unavailable"');
    expect(markup).not.toContain(`data-briefing-state="${state}"`);
    expect(markup).not.toContain('data-instrument-briefing');
  }
});

test('Briefing renders a legacy row as unavailable without unchecked authority', () => {
  const legacy = admittedSignal();
  delete legacy.evidence_summary;
  delete legacy.ribbon_series;
  delete legacy.audience_basis;
  const markup = renderToStaticMarkup(<TodayPage topics={[legacy]} freshness={{status: 'green'}} />);
  expect(markup).toContain('data-briefing-state="unavailable"');
  expect(markup).not.toContain('data-briefing-state="unchecked"');
  expect(markup).not.toContain('data-evidence-state="unchecked"');
});

test('Discover renders package candidates with the package evidence action', () => {
  const markup = renderToStaticMarkup(
    <ExplorePage topics={[admittedSignal()]} freshness={{status: 'green'}} region="ZA" />,
  );
  expect(markup).toContain('class="discover-instrument"');
  expect(markup).toContain('Open evidence');
  expect(markup).not.toContain('dossier-lead');
});

test('Compare rejects adapter models whose Ribbon axes do not match', () => {
  const shiftedRibbon = freshFixture(ROOT_SIGNAL).ribbon_series.map((strand) => ({
    ...strand,
    axis: {...strand.axis, start: '2026-08-19', end: '2026-08-25'},
    points: strand.points.map((point, index) => ({...point, at: index ? '2026-08-25T09:00:00Z' : '2026-08-19T09:00:00Z'})),
    receiptAnchors: strand.receiptAnchors.map((anchor) => ({...anchor, at: '2026-08-25T09:00:00Z'})),
  }));
  const mismatch = secondSignal({
    ribbon_series: shiftedRibbon,
    evidence_summary: {
      ...READY_SUMMARY,
      window: {...READY_SUMMARY.window, start: '2026-08-19', end: '2026-08-25'},
      checkedAt: '2026-08-26T12:00:00Z',
    },
  });
  const markup = renderToStaticMarkup(
    <ComparePanel topics={[admittedSignal(), admittedSignal(mismatch)]} region="ALL" />,
  );
  /* copy-register.json, ComparisonInstrument.jsx: "Comparison axes do not
     match." became "These signals were measured on different axes, so they
     cannot sit side by side." Same claim, no status annotation, approved. */
  expect(markup).toContain('These signals were measured on different axes, so they cannot sit side by side.');
  expect(markup).not.toContain('comparison-strip');
});

test('Compare adapts before taking the first five comparable models', () => {
  const rows = [];
  for (let index = 0; index < 8; index += 1){
    const row = admittedSignal({
      signal_id: `sig_${index.toString(16).padStart(64, '0')}`,
      label: `Signal ${index}`,
    });
    if (index < 2) delete row.ribbon_series;
    rows.push(row);
  }
  const markup = renderToStaticMarkup(<ComparePanel topics={rows} region="ALL" />);
  for (const index of [2, 3, 4, 5, 6]) expect(markup).toContain(`Signal ${index}`);
  for (const index of [0, 1, 7]) expect(markup).not.toContain(`Signal ${index}`);
  expect(markup.match(/data-comparison-signal=/g)).toHaveLength(5);
});

test.skipIf(!CHROME)('mounted shell preserves host routes markets and mobile focus return', async () => {
  const result = await mountedShellInteraction();
  expect(result.openedFocus).toBe('Briefing');
  expect(result.routeFocusReturned).toBe(true);
  expect(result.escapeFocusReturned).toBe(true);
  expect(result.events).toContainEqual(['route', '/discover', 'explore']);
  /* Task 39: a second chip from one market returns the scope to ALL, and a
     click on ALL narrows to the chip the reader touched. */
  expect(result.events).toContainEqual(['market', 'ZA', 'ZA,NG', 'ALL']);
  expect(result.events).toContainEqual(['market', 'ALL', 'ZA,KE', 'NG']);
  expect(result.events).toContainEqual(['market', 'NG', 'ZA,NG', 'ALL']);
  expect(result.events).toContainEqual(['market', 'ZA', 'ZA', 'ZA']);
  expect(result.events).toContainEqual(['market', 'ZA', 'ZA,NG,KE', 'ALL']);
  expect(result.routeCurrentBefore).toBe('Briefing');
  expect(result.routeCurrentAfter).toBe('Discover');
  /* Quiet register, 23 Sept 2026: package 2.0.21 names the markets rather
     than printing their codes, and says "All markets" when all three are in
     scope, so the scope is read as those words. */
  expect(result.marketTriggerBefore).toBe('Market scope: South Africa');
  expect(result.marketTriggerAfter).toBe('Market scope: All markets');
  /* The second mount starts on ALL. NG narrows to NG alone; ZA then brings
     all three back, chips and masthead label together. */
  expect(result.secondAfterNG.checks).toEqual([['ZA', 'South Africa', 'false'], ['NG', 'Nigeria', 'true'], ['KE', 'Kenya', 'false']]);
  expect(result.secondAfterNG.label).toBe('Market scope: Nigeria');
  expect(result.secondAfterNG.text).toBe('Nigeria');
  expect(result.secondAfterZA.checks).toEqual([['ZA', 'South Africa', 'true'], ['NG', 'Nigeria', 'true'], ['KE', 'Kenya', 'true']]);
  expect(result.secondAfterZA.label).toBe('Market scope: All markets');
  expect(result.secondAfterZA.text).toBe('All markets');
});

test.skipIf(!CHROME)('Compare reconciles stale selected IDs across eligibility changes', async () => {
  const initial = Array.from({length: 6}, (_, index) => admittedSignal({
    signal_id: `sig_${index.toString(16).padStart(64, '0')}`,
    label: `Signal ${index}`,
  }));
  const unavailable = structuredClone(initial);
  delete unavailable[2].ribbon_series;
  const result = await mountedCompareSelection(initial, unavailable, initial);
  expect(result.initiallySelected).toEqual(['Signal 0', 'Signal 1', 'Signal 2', 'Signal 3', 'Signal 4']);
  expect(result.afterUnavailable).toEqual(['Signal 0', 'Signal 1', 'Signal 3', 'Signal 4']);
  expect(result.afterReplacement).toEqual(['Signal 0', 'Signal 1', 'Signal 3', 'Signal 4', 'Signal 5']);
  expect(result.afterReappearing).toEqual(['Signal 0', 'Signal 1', 'Signal 3', 'Signal 4']);
  expect(result.afterReselect).toEqual(['Signal 0', 'Signal 1', 'Signal 2', 'Signal 3', 'Signal 4']);
});
