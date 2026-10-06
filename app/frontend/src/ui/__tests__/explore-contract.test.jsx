import {describe, expect, test} from 'bun:test';
import {spawnSync} from 'node:child_process';
import {existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {build} from 'esbuild';
import postcss from 'postcss';

import {
  DISCOVERY_MODES,
  READINESS_STATES,
  ExplorePage,
  admitExploreSignals,
  filterExploreSignals,
  buildExploreState,
  evidenceForSignal,
  initialExploreSelection,
  normalizeExploreReceipts,
  resetExploreSelection,
  selectExploreSignal,
} from '../../explore.jsx';
import {BrowsePage} from '../../views.jsx';
import App, {exploreDynamicProps} from '../../App.jsx';
import {canonicalizeDiscoverHash, parseHash} from '../../router.js';
import {splitInstrumentStylesheet} from '../../instrumentStylesheetSplit.mjs';
import {requestEvidenceClose, restoreEvidenceFocus} from '../EvidenceDrawer.jsx';
import {MEASURED_AUDIENCE, READY_RIBBON, READY_SUMMARY} from './fixtures/instrument-payloads.js';

const SIG_A = `sig_${'a'.repeat(64)}`;
const SIG_B = `sig_${'b'.repeat(64)}`;
const RECEIPT_A = ['TikTok', '@runner', 'Clubs are meeting weekly', '18 posts', '2d', 'https://example.test/a'];

const CURRENT_DESK_TOPIC = Object.freeze({
  id: 'sports_street_football',
  region: 'ZA',
  regionName: 'South Africa',
  flag: '🇿🇦',
  topic: 'Street football',
  label: 'SPORTS · ZA',
  momentum: 'rising',
  score: 0.72,
  seed: 0.61,
  delta: 0.08,
  sources: 4,
  creators: 12,
  sentiment: 0.22,
  social_mood: 'Positive',
  velocity: 'High',
  age: '2h',
  why: 'Participation moved into weekly neighbourhood tournaments.',
  series: [0.31, 0.44, 0.52, 0.72],
  platforms: [['TikTok', 7], ['Instagram', 4]],
  creators_list: ['@touchlineza'],
  voices: [['TikTok', 'Five-a-side is back after work', '2h']],
  brief: {
    trend: 'Street football clips are rising.',
    relevance: 'The format repeats across source channels.',
    idea: {tool: 'Editorial response', text: 'Give the format a local stage.'},
    prompt: {nano: 'A neighbourhood football sequence.', lyria: ''},
  },
  receipts: [RECEIPT_A],
  reach: 18400,
  mentions: 312,
  sov: 18.6,
});

function signal(overrides={}){
  const row = {
    test_provenance: 'fixture-only',
    contract_version: 'desk_dynamic_signal_v2',
    signal_id: SIG_A,
    discovery_mode: 'phrase',
    evidence_state: 'ready',
    signal_name: 'Neighbourhood running clubs',
    label: 'Neighbourhood running clubs',
    why_now: 'Weekly meetups now repeat across two source families.',
    possible_response: 'Host one route with local club captains.',
    receipts: [RECEIPT_A],
    observation_start: '2026-08-01T00:00:00Z',
    observation_end: '2026-08-28T00:00:00Z',
    observation_method: 'closed completed-run observation',
    qualities: {
      velocity: 'moderate',
      novelty: 'moderate',
      breadth: 'moderate',
      independence: 'moderate',
      history: 'unmeasured',
      geo_confidence: 'moderate',
      topic_tags: [],
    },
    evidence_summary: structuredClone(READY_SUMMARY),
    ribbon_series: structuredClone(READY_RIBBON),
    audience_basis: structuredClone(MEASURED_AUDIENCE),
    ...overrides,
  };
  if (!Object.hasOwn(overrides, 'label')) row.label = row.signal_name;
  return row;
}

/* Where the browser lives, without assuming one desk. A hardcoded Windows
   path fails on any other runner, and these suites are intended for CI. */
function resolveChrome(){
  return [
    process.env.CHROME_PATH,
    process.env.CHROME_BIN,
    String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
    String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
    '/usr/bin/google-chrome',
    '/usr/bin/chromium-browser',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean).find((candidate) => existsSync(candidate)) || null;
}

const CHROME = resolveChrome();

async function mountedEvidenceInteraction({disableSharedRestore=false}={}){
  const root = fileURLToPath(new URL('../../../../', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'lp-explore-mounted-focus-'));
  const bundlePath = join(directory, 'probe.js');
  const htmlPath = join(directory, 'probe.html');
  const profilePath = join(directory, 'chrome-profile');
  const chrome = CHROME;
  const topic = signal();
  const entry = `
    import React from 'react';
    import {createRoot} from 'react-dom/client';
    import {flushSync} from 'react-dom';
    import {ExplorePage} from './frontend/src/explore.jsx';
    const result = document.getElementById('result');
    const wait = (milliseconds=25) => new Promise((resolve) => setTimeout(resolve, milliseconds));
    const encode = (value) => btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(value))));
    (async () => {
      try {
        const mount = document.getElementById('root');
        flushSync(() => createRoot(mount).render(React.createElement(ExplorePage, {
          topics: [${JSON.stringify(topic)}],
          freshness: {status: 'green', stamp_utc: '2026-08-28T06:30:00Z'},
          region: 'ZA',
        })));
        await wait();
        await wait();
        const invoker = document.querySelector('.explore-selection .discover-instrument__action[data-action="primary"]');
        if (!invoker) throw new Error('Proof invoker missing: ' + mount.innerHTML);
        const invokerLabel = invoker.getAttribute('aria-label') || invoker.textContent.trim();
        invoker.dataset.exactProofInvoker = 'true';
        invoker.focus();
        invoker.click();
        await wait();
        await wait();
        const dialog = document.querySelector('dialog');
        const opened = Boolean(dialog && dialog.open);
        const autoFocusOwner = document.activeElement && document.activeElement.getAttribute('aria-label');
        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}));
        await wait();
        await wait();
        await wait();
        const payload = {
          opened,
          autoFocusOwner,
          closed: !document.querySelector('dialog'),
          restoredExactInvoker: document.activeElement === invoker,
          invokerLabel,
          restoredLabel: document.activeElement && (
            document.activeElement.getAttribute('aria-label') || document.activeElement.textContent.trim()
          ),
        };
        result.textContent = encode(payload);
      } catch (error) {
        result.textContent = encode({error: String(error && error.stack || error)});
      }
    })();
  `;
  try {
    await build({
      stdin: {contents: entry, loader: 'jsx', resolveDir: root, sourcefile: 'explore-mounted-focus.jsx'},
      bundle: true,
      define: {'process.env.NODE_ENV': '"production"'},
      format: 'iife',
      jsx: 'automatic',
      nodePaths: [join(root, 'frontend', 'node_modules')],
      outfile: bundlePath,
      platform: 'browser',
      plugins: [{
        name: 'ignore-css-for-focus-probe',
        setup(builder){
          builder.onResolve({filter: /\\.css$/}, () => ({path: 'empty-css', namespace: 'focus-probe'}));
          builder.onLoad({filter: /.*/, namespace: 'focus-probe'}, () => ({contents: '', loader: 'js'}));
        },
      }, {
        name: 'route-owned-focus-probe',
        setup(builder){
          if (!disableSharedRestore) return;
          builder.onLoad({filter: /EvidenceDrawer\.jsx$/}, (args) => {
            const source = readFileSync(args.path, 'utf8');
            const target = '      restoreEvidenceFocus(previousFocus);';
            if (!source.includes(target)) throw new Error('Shared restoration target missing');
            return {contents: source.replace(target, '      void previousFocus;'), loader: 'jsx'};
          });
        },
      }],
    });
    writeFileSync(htmlPath, '<!doctype html><html><body><div id="root"></div><pre id="result">pending</pre><script src="./probe.js"></script></body></html>', 'utf8');
    const run = spawnSync(chrome, [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--run-all-compositor-stages-before-draw',
      '--virtual-time-budget=5000', '--dump-dom', `--user-data-dir=${profilePath}`,
      pathToFileURL(htmlPath).href,
    ], {encoding: 'utf8', timeout: 20000, windowsHide: true});
    expect(run.error).toBeUndefined();
    expect(run.status).toBe(0);
    const encoded = run.stdout.match(/<pre id="result">([^<]+)<\/pre>/)?.[1];
    expect(encoded).toBeTruthy();
    return JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'));
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}

async function mountedGeometry({props, width=390, height=844}){
  const root = fileURLToPath(new URL('../../../../', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'lp-explore-geometry-'));
  const bundlePath = join(directory, 'probe.js');
  const htmlPath = join(directory, 'probe.html');
  const profilePath = join(directory, 'chrome-profile');
  const chrome = CHROME;
  /* The application's own sheet set, in the application's own order. main.jsx
     loads the package's critical half and then boot.css; the Discover chunk
     loads the package's deferred half, which is what
     styles/instrument-route-surfaces.css resolves to in the build, and then
     the route sheet; main.jsx's deferred import adds the legacy globals last.
     A harness that injects anything else measures a page the reader never
     sees: src/styles/ogilvy-intelligence.css, which no module imports,
     redefines the instrument type tokens on .oi-product at the package's own
     specificity, and injecting it after the package sheet resolved Consolas
     and Georgia where the product renders Recursive Mono and Newsreader. */
  const localSheet = (name) => readFileSync(new URL(`../../${name}`, import.meta.url), 'utf8');
  const {critical: packageCritical, deferred: packageDeferred} = splitInstrumentStylesheet(
    readFileSync(new URL('../../../node_modules/ogilvy-intelligence-design-system/dist/style.css', import.meta.url), 'utf8'),
  );
  const applicationSheets = [
    packageCritical,
    localSheet('styles/boot.css'),
    packageDeferred,
    localSheet('ogilvy-intelligence.css'),
    localSheet('app.css'),
    localSheet('styles/empty-states.css'),
    localSheet('styles/dossier.css'),
  ];
  // Headless Chrome clamps its own window width on Windows, so --window-size
  // cannot produce a 390 pixel viewport. An iframe can: media queries and
  // layout inside it resolve against the frame's own width, which is exactly
  // the viewport under test.
  const entry = `
    import React from 'react';
    import {createRoot} from 'react-dom/client';
    import {flushSync} from 'react-dom';
    import {ExplorePage} from './frontend/src/explore.jsx';
    const result = document.getElementById('result');
    const wait = (milliseconds=25) => new Promise((resolve) => setTimeout(resolve, milliseconds));
    const encode = (value) => btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(value))));
    (async () => {
      try {
        const frame = document.getElementById('frame');
        /* Wait for the frame to parse before reading it. A srcdoc document
           parses independently of this script, so under load the mount is not
           there yet and the probe reports a missing mount as though the page
           were broken. That is a race in the harness, not a fault in the page,
           and it is what made this suite flaky. */
        const deadline = Date.now() + 10000;
        let frameDocument = null;
        let mount = null;
        while (Date.now() < deadline){
          frameDocument = frame.contentDocument;
          mount = frameDocument && frameDocument.getElementById('root');
          if (mount && frameDocument.readyState !== 'loading') break;
          await wait(25);
        }
        if (!mount) throw new Error('frame mount never appeared within 10s');
        const frameWindow = frame.contentWindow;
        flushSync(() => createRoot(mount).render(React.createElement(ExplorePage, ${JSON.stringify(props)})));
        await wait();
        await wait();
        if (frameDocument.fonts && frameDocument.fonts.ready) await frameDocument.fonts.ready;
        await wait();
        const page = frameDocument.querySelector('.explore-page');
        if (!page) throw new Error('explore page missing: ' + mount.innerHTML);
        const documentElement = frameDocument.documentElement;
        /* Before anything is measured: the sheets have to resolve the product's
           own typefaces on a real element. A harness that renders Consolas and
           Georgia where the product renders Recursive Mono and Newsreader turns
           every font reading into a declaration check, so this fails the probe
           rather than reporting a number taken off the wrong page. */
        const product = frameDocument.body;
        const productStyle = frameWindow.getComputedStyle(product);
        const resolvedFonts = {
          serif: productStyle.getPropertyValue('--oi-serif').trim(),
          mono: productStyle.getPropertyValue('--oi-mono').trim(),
          sample: frameWindow.getComputedStyle(
            page.querySelector('.explore-folio h1, .state-view__title'),
          ).fontFamily,
        };
        if (!/^"?Newsreader"?,/.test(resolvedFonts.serif)
          || !/^"?Recursive Mono"?,/.test(resolvedFonts.mono)
          || !/^"?Newsreader"?,/.test(resolvedFonts.sample)){
          throw new Error('harness sheets resolve the wrong typefaces: ' + JSON.stringify(resolvedFonts));
        }
        const controls = Array.from(page.querySelectorAll('button'))
          .filter((node) => node.getClientRects().length > 0);
        const heights = controls.map((node) => node.getBoundingClientRect().height);
        /* A state branch carries the package frame's h2 title and no host h1. */
        const heading = page.querySelector('h1, .state-view__title');
        /* Round 9, item 3: the package specimen alone owns the proposition. */
        const leadTitle = page.querySelector('.discover-instrument__proposition');
        const folioTitle = page.querySelector('.explore-folio h1');
        const folioLines = folioTitle
          ? Math.round(folioTitle.getBoundingClientRect().height / parseFloat(frameWindow.getComputedStyle(folioTitle).lineHeight))
          : null;
        const kicker = page.querySelector('.explore-state__kicker');
        const topOf = (node) => (node ? node.getBoundingClientRect().top + frameWindow.scrollY : null);
        result.textContent = encode({
          resolvedFonts,
          viewport: [frameWindow.innerWidth, frameWindow.innerHeight],
          horizontalOverflow: documentElement.scrollWidth - documentElement.clientWidth,
          bodyOverflow: frameDocument.body.scrollWidth - documentElement.clientWidth,
          exploreState: page.dataset.exploreState || null,
          headingTop: topOf(heading),
          leadTitleTop: topOf(leadTitle),
          h1Count: page.querySelectorAll('h1').length,
          folioLines,
          folioScrollWidth: folioTitle ? folioTitle.scrollWidth : null,
          folioClientWidth: folioTitle ? folioTitle.clientWidth : null,
          kickerVariation: kicker ? frameWindow.getComputedStyle(kicker).fontVariationSettings : null,
          controlCount: controls.length,
          minControlHeight: heights.length ? Math.min(...heights) : null,
          undersizedControls: heights.filter((value) => value < 48).length,
        });
      } catch (error) {
        result.textContent = encode({error: String(error && error.stack || error)});
      }
    })();
  `;
  try {
    await build({
      stdin: {contents: entry, loader: 'jsx', resolveDir: root, sourcefile: 'explore-geometry.jsx'},
      bundle: true,
      define: {'process.env.NODE_ENV': '"production"'},
      format: 'iife',
      jsx: 'automatic',
      nodePaths: [join(root, 'frontend', 'node_modules')],
      outfile: bundlePath,
      platform: 'browser',
      plugins: [{
        name: 'css-injected-by-the-harness',
        setup(builder){
          builder.onResolve({filter: /\.css$/}, () => ({path: 'empty-css', namespace: 'geometry-probe'}));
          builder.onLoad({filter: /.*/, namespace: 'geometry-probe'}, () => ({contents: '', loader: 'js'}));
        },
      }],
    });
    const frameDocument = `<!doctype html><html><head>`
      + `<meta name="viewport" content="width=device-width,initial-scale=1">`
      + `<style>*{margin:0}html,body{width:100%}</style>`
      + applicationSheets.map((css) => `<style>${css}</style>`).join('')
      + `</head>`
      + `<body class="oi-product"><div id="root"></div></body></html>`;
    writeFileSync(
      htmlPath,
      `<!doctype html><html><head><style>*{margin:0;padding:0}</style></head><body>`
        + `<iframe id="frame" style="width:${width}px;height:${height}px;border:0"`
        + ` srcdoc="${frameDocument.replace(/"/g, '&quot;')}"></iframe>`
        + `<pre id="result">pending</pre><script src="./probe.js"></script></body></html>`,
      'utf8',
    );
    const run = spawnSync(chrome, [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--run-all-compositor-stages-before-draw', '--hide-scrollbars',
      `--window-size=${Math.max(width, 900)},${Math.max(height, 900)}`,
      '--virtual-time-budget=6000', '--dump-dom', `--user-data-dir=${profilePath}`,
      pathToFileURL(htmlPath).href,
    ], {encoding: 'utf8', timeout: 25000, windowsHide: true});
    expect(run.error).toBeUndefined();
    expect(run.status).toBe(0);
    const encoded = run.stdout.match(/<pre id="result">([^<]+)<\/pre>/)?.[1];
    expect(encoded).toBeTruthy();
    const measured = JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'));
    expect(measured.error).toBeUndefined();
    return measured;
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}

function markup(props={}){
  return renderToStaticMarkup(<ExplorePage
    topics={[signal()]}
    freshness={{status: 'green', stamp_utc: '2026-08-28T06:30:00Z'}}
    region="ZA"
    {...props}
  />);
}

function declaration(css, selector, property, media){
  const root = postcss.parse(css);
  let scope = root;
  if (media){
    const matches = [];
    root.walkAtRules('media', (rule) => { if (rule.params === media) matches.push(rule); });
    expect(matches).toHaveLength(1);
    scope = matches[0];
  }
  const rules = [];
  scope.walkRules((rule) => {
    if (rule.parent === scope && rule.selectors.includes(selector)) rules.push(rule);
  });
  expect(rules).toHaveLength(1);
  const values = rules[0].nodes.filter((node) => node.type === 'decl' && node.prop === property);
  expect(values).toHaveLength(1);
  return values[0].value;
}

function activeSources(directory){
  const files = [];
  for (const entry of readdirSync(directory)){
    if (entry === '__tests__' || entry === 'v3-design-system.md') continue;
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) files.push(...activeSources(path));
    else if (/\.(?:js|jsx)$/.test(entry)) files.push(path);
  }
  return files;
}

describe('Open Discover admission and receipts', () => {
  test('curated, seed, and incomplete identities fail closed', () => {
    const rows = [
      {id: 'topic_1', label: 'Curated category'},
      {id: 'seed_1', signal_id: 'seed_1', discovery_mode: 'phrase', evidence_state: 'ready', signal_name: 'Seed row'},
      signal({signal_id: undefined}),
      signal({discovery_mode: undefined}),
      signal({evidence_state: undefined}),
      signal({signal_name: ' ', title: ' ', label: ' '}),
    ];
    expect(admitExploreSignals(rows)).toEqual({signals: [], diagnostic: {duplicateCount: 0}});
  });

  test('every approved mode and readiness is admitted from root or nested signal', () => {
    expect(DISCOVERY_MODES).toEqual([
      'phrase', 'hashtag', 'sound', 'creator', 'entity',
    ]);
    expect(READINESS_STATES).toEqual(['ready', 'thin', 'contradictory', 'unchecked']);
    const rows = DISCOVERY_MODES.flatMap((mode, modeIndex) => READINESS_STATES.map((readiness, readinessIndex) => ({
      signal: {
        contract_version: 'desk_dynamic_signal_v2',
        signal_id: `sig_${(modeIndex * READINESS_STATES.length + readinessIndex).toString(16).padStart(64, '0')}`,
        discovery_mode: mode,
        evidence_state: readiness,
        signal_name: `${mode} ${readiness}`,
        qualities: signal().qualities,
      },
      receipts: [RECEIPT_A],
    })));
    expect(admitExploreSignals(rows).signals).toHaveLength(DISCOVERY_MODES.length * READINESS_STATES.length);
  });

  test('unsupported modes and readiness values are excluded', () => {
    expect(admitExploreSignals([
      signal({discovery_mode: 'manual'}),
      signal({evidence_state: 'complete'}),
    ]).signals).toEqual([]);
  });

  test('duplicate IDs collapse to the first engine row without sorting', () => {
    const first = signal({signal_name: 'Engine first', reach: 1, score: 0.01});
    const duplicate = signal({signal_name: 'High score duplicate', reach: 999, score: 0.99});
    const second = signal({signal_id: SIG_B, signal_name: 'Engine second', reach: 500, score: 0.5});
    const result = admitExploreSignals([first, duplicate, second]);
    expect(result.signals.map(({title}) => title)).toEqual(['Engine first', 'Engine second']);
    expect(result.diagnostic).toEqual({duplicateCount: 1});
  });

  test('receipt normalizer rejects malformed values and freezes exact six-string tuples', () => {
    const tuple = ['Forum', '@maker', 'A repair sequence', '7 posts', '3d', 'https://example.test/receipt'];
    const normalized = normalizeExploreReceipts([
      {},
      ['partial', 'tuple'],
      ['Forum', '@maker', 'Bad URL', '7 posts', '3d', 'javascript:alert(1)'],
      ['Forum', '@maker', 'Non-string', 7, '3d', 'https://example.test/non-string'],
      {id: 'receipt-object', url: 'not a URL', platform: 'News'},
      tuple,
      tuple,
      {evidence_id: 'ev_1', url: 'https://example.test/object', platform: 'News', author_label: '@desk', excerpt: 'Observed text', metric_label: '4 posts', age: '1d'},
      {receipt_id: 'ev_1', url: 'https://example.test/duplicate'},
    ]);
    expect(normalized).toEqual([
      Object.freeze({id: 'receipt-object', url: '', platform: 'News', author: '', snippet: '', metric: '', age: ''}),
      Object.freeze({id: tuple[5], url: tuple[5], platform: 'Forum', author: '@maker', snippet: 'A repair sequence', metric: '7 posts', age: '3d'}),
      Object.freeze({id: 'ev_1', url: 'https://example.test/object', platform: 'News', author: '@desk', snippet: 'Observed text', metric: '4 posts', age: '1d'}),
    ]);
    expect(Object.isFrozen(normalized[0])).toBe(true);
  });

  test('receipt identity and URL candidates fall through shadowing invalid values', () => {
    const normalized = normalizeExploreReceipts([
      {
        id: 'x'.repeat(257),
        evidence_id: 'ev_fallback',
        receipt_id: 'receipt_later',
        url: 'javascript:alert(1)',
        source_url: 'https://example.test/source-fallback',
        uri: 'https://example.test/uri-later',
      },
      {
        id: '',
        evidence_id: ' ',
        receipt_id: 'receipt_valid',
        url: 'not a URL',
        source_url: 'also invalid',
        uri: 'https://example.test/uri-fallback',
      },
    ]);
    expect(normalized.map(({id, url}) => ({id, url}))).toEqual([
      {id: 'ev_fallback', url: 'https://example.test/source-fallback'},
      {id: 'receipt_valid', url: 'https://example.test/uri-fallback'},
    ]);
  });

  test('no normalized receipt forces unchecked proof', () => {
    const admitted = admitExploreSignals([signal({evidence_state: 'ready', receipts: [{}, ['partial']]})]).signals[0];
    expect(admitted.receipts).toEqual([]);
    expect(admitted.readiness).toBe('unchecked');
  });

  test('engine text is bounded by code points, so an emoji never hides present prose', () => {
    // The engine contract bounds why_now and possible_response at 2000 Unicode
    // code points. Counting UTF-16 code units instead would drop a conformant
    // value the moment it carries one astral character, turning present engine
    // prose into Unavailable.
    const atLimit = `${'a'.repeat(1999)}\u{1F600}`;
    expect([...atLimit].length).toBe(2000);
    expect(atLimit.length).toBe(2001);
    const admitted = admitExploreSignals([
      signal({why_now: atLimit, possible_response: atLimit}),
    ]).signals[0];
    expect(admitted.whyNow).toBe(atLimit);
    expect(admitted.response).toBe(atLimit);
  });

  test('engine text past the code-point limit is still refused', () => {
    const overLimit = `${'a'.repeat(2000)}\u{1F600}`;
    expect([...overLimit].length).toBe(2001);
    const admitted = admitExploreSignals([
      signal({why_now: overLimit, possible_response: overLimit}),
    ]).signals[0];
    expect(admitted.whyNow).toBe('Unavailable');
    expect(admitted.response).toBe('Unavailable');
  });
});

describe('Open Discover route states and rendering', () => {
  /* Readiness belongs to the released run. The ingest stamp only dates a run
     that admitted nothing, so it sits below ready and above no discovery. */
  test('state precedence is error, loading, ready, stale, no discovery', () => {
    const valid = [signal()];
    const stale = {status: 'amber', stamp_utc: '2026-08-28T06:30:00Z'};
    expect(buildExploreState({topics: valid, freshness: stale, loading: true, error: {message: 'offline'}}).state).toBe('error');
    expect(buildExploreState({topics: valid, freshness: stale, loading: true}).state).toBe('loading');
    expect(buildExploreState({topics: valid, freshness: stale}).state).toBe('ready');
    expect(buildExploreState({topics: [{id: 'curated'}], freshness: stale}))
      .toEqual({state: 'stale', checkedAt: stale.stamp_utc});
    expect(buildExploreState({topics: [{id: 'curated'}], freshness: {status: 'green'}}).state).toBe('no_discovery');
    expect(buildExploreState({topics: valid, freshness: {status: 'green'}}).state).toBe('ready');
  });

  test('error is bounded and stale withholds signal content', () => {
    const errorMarkup = markup({error: {message: 'x'.repeat(500)}, loading: true});
    expect(errorMarkup).toContain('data-explore-state="error"');
    expect(errorMarkup).not.toContain('x'.repeat(241));
    expect(errorMarkup).not.toContain('Neighbourhood running clubs');
    const staleMarkup = markup({
      topics: [signal({discovery_mode: 'not-a-declared-mode'})],
      freshness: {status: 'amber', stamp_utc: '2026-08-28T06:30:00Z'},
    });
    expect(staleMarkup).toContain('data-explore-state="stale"');
    expect(staleMarkup).toContain('2026-08-28T06:30:00Z');
    expect(staleMarkup).not.toContain('Neighbourhood running clubs');
  });

  test('loading and a complete current desk-shaped row render honest non-ready states', () => {
    expect(markup({loading: true})).toContain('data-explore-state="loading"');
    const liveMarkup = markup({topics: [CURRENT_DESK_TOPIC]});
    expect(liveMarkup).toContain('data-explore-state="no_discovery"');
    /* Round three, 24 Sept 2026: the no discovery body says the run finished
       without a signal strong enough to show, in a strategist's words rather
       than the engine's "admitted" and "dynamic". Same frame, same assertion. */
    expect(liveMarkup).toContain('The run finished without a signal strong enough to show.');
    expect(liveMarkup).toContain('Browse evidence');
    expect(liveMarkup).toContain('Open Source Lab');
    expect(liveMarkup).not.toContain(CURRENT_DESK_TOPIC.topic);
  });

  test('fixture-only dynamic identity is the only ready proof in this suite', () => {
    const fixture = signal();
    expect(fixture.test_provenance).toBe('fixture-only');
    expect(markup({topics: [fixture]})).toContain('data-explore-state="ready"');
  });

  test('ready markup composes the proposition once: the specimen owns it and no host lead repeats it', () => {
    /* Round 9, item 3. The route carried three h1 elements: the folio, the
       host EditorialLead and the package specimen, and the Why Now sentence
       twice. Discover is for choosing a candidate and Briefing carries the
       lead, so the route now holds the folio h1 plus the specimen's. */
    const ready = markup({topics: [signal({
      graph_score: 0.99,
      dataset: 'hidden_table',
      gen_z_fit: 0.9,
      taxonomy: 'internal group',
    })]});
    for (const text of ['Neighbourhood running clubs', 'discover-instrument__proposition', 'Open evidence']) {
      expect(ready).toContain(text);
    }
    expect(ready.match(/<h1[\s>]/g)).toHaveLength(2);
    expect(ready).not.toContain('editorial-lead');
    expect(ready).not.toContain('Written evidence state');
    /* The Why Now sentence is said once, by the specimen. */
    expect(ready.split('Weekly meetups now repeat across two source families.').length - 1).toBe(1);
    expect(ready).not.toMatch(/graph score|hidden_table|Gen Z|taxonomy|watchlist|new seed|BigQuery|Gemini|viral|broad enough to trust|what the search is telling you/i);
  });

  test('missing Why Now, response, receipts, and observation window stay unavailable', () => {
    const legacy = signal({
      why_now: undefined,
      possible_response: undefined,
      receipts: [],
      observation_start: undefined,
      observation_end: undefined,
      observation_method: undefined,
      date: '2026-08-28',
    });
    delete legacy.evidence_summary;
    delete legacy.ribbon_series;
    delete legacy.audience_basis;
    delete legacy.why_now;
    delete legacy.possible_response;
    delete legacy.observation_start;
    delete legacy.observation_end;
    delete legacy.observation_method;
    const ready = markup({topics: [legacy]});
    expect(ready).toContain('Unavailable');
    /* copy-register.json, DiscoverInstrument.jsx: "Select a valid candidate"
       became "Pick a candidate". Same claim, no status annotation, approved. */
    expect(ready).toContain('Pick a candidate');
    expect(ready).not.toContain('data-evidence-state="unchecked"');
    expect(ready).not.toContain('data-evidence-state="ready"');
    expect(ready).not.toContain('2026-08-28');

    const missingStartRow = signal({
      observation_start: undefined,
      observation_end: '2026-08-28T00:00:00Z',
      observation_method: 'closed completed-run observation',
      date: '2026-08-01',
    });
    delete missingStartRow.observation_start;
    const missingStart = markup({topics: [missingStartRow]});
    /* Quiet register, 23 Sept 2026: package 2.0.20 writes the 2026-08-18 to
       2026-08-24 window through readableDates, so it reads as one readable
       range. The row date still may not stand in for the missing start, in
       either the ISO form or the readable one. */
    expect(missingStart).toContain('18 to 24 Aug 2026');
    expect(missingStart).not.toContain('2026-08-01');
    expect(missingStart).not.toMatch(/(?<!\d)1 (?:to \d{1,2} )?Aug 2026/);
  });

  test('complete observation window renders only when all three owned fields exist', () => {
    const ready = markup();
    /* Quiet register, 23 Sept 2026: package 2.0.20 writes the 2026-08-18 to
       2026-08-24 window through readableDates, so it reads as one readable
       range. The row's own 1 to 28 August bounds stay out in either form. */
    expect(ready).toContain('18 to 24 Aug 2026');
    expect(ready).not.toContain('2026-08-01T00:00:00Z');
    expect(ready).not.toMatch(/(?<!\d)1 (?:to \d{1,2} )?Aug 2026/);
    expect(ready).not.toContain('closed completed-run observation');
  });
});

describe('Open Discover selection and evidence interaction', () => {
  test.skipIf(!CHROME)('mounted Escape close restores the exact Proof invoker after drawer autoFocus', async () => {
    /* The invoker is the specimen's primary action. Round 9 removed the host
       lead from Discover, so the selection holds the package instrument alone
       and its Open evidence control opens the drawer. Two facts are: Escape returns
       focus to the exact element that opened the drawer, and that element still
       carries the label it carried before the drawer opened, captured then and
       compared now. A relabelled or empty control fails both checks. */
    const settled = {opened: true, autoFocusOwner: 'Close evidence', closed: true, restoredExactInvoker: true};
    for (const options of [{}, {disableSharedRestore: true}]){
      const result = await mountedEvidenceInteraction(options);
      expect(result).toMatchObject(settled);
      expect(result.invokerLabel.length).toBeGreaterThan(0);
      expect(result.restoredLabel).toBe(result.invokerLabel);
    }
  }, 30000);

  test('selection preserves URL, focuses the lead, and market reset returns to first', () => {
    const selected = [];
    const focused = [];
    const leadHeading = {focus: (options) => focused.push(options)};
    const queried = [];
    const leadRoot = {querySelector: (selector) => { queried.push(selector); return leadHeading; }};
    const fakeWindow = {location: {hash: '#/explore'}};
    const originalWindow = globalThis.window;
    globalThis.window = fakeWindow;
    try {
      selectExploreSignal(SIG_B, (id) => selected.push(id), leadRoot, (callback) => callback());
      expect(selected).toEqual([SIG_B]);
      expect(focused).toEqual([{preventScroll: true}]);
      expect(fakeWindow.location.hash).toBe('#/explore');
      /* Round 9, item 3: with the host lead gone, focus lands on the specimen. */
      expect(queried).toEqual(['.discover-instrument__proposition, .dossier-lead__title']);
    } finally {
      globalThis.window = originalWindow;
    }
    const signals = admitExploreSignals([signal(), signal({signal_id: SIG_B, signal_name: 'Second'})]).signals;
    expect(initialExploreSelection(signals)).toBe(SIG_A);
    const resets = [];
    resetExploreSelection(signals, (id) => resets.push(id), (open) => resets.push(open));
    expect(resets).toEqual([SIG_A, false]);
  });

  test('drawer data belongs only to the selected signal and normalizes receipts', () => {
    const signals = admitExploreSignals([
      signal(),
      signal({signal_id: SIG_B, signal_name: 'Second', receipts: [['News', '@desk', 'Second evidence', '4 posts', '1d', 'https://example.test/b']]}),
    ]).signals;
    expect(evidenceForSignal(signals, SIG_B).map(({snippet}) => snippet)).toEqual(['Second evidence']);
    expect(evidenceForSignal(signals, SIG_A).map(({snippet}) => snippet)).toEqual(['Clubs are meeting weekly']);
  });

  test('evidence close handles Escape and restores the invoking control', () => {
    const events = [];
    requestEvidenceClose((reason) => events.push(reason), 'keyboard', {preventDefault: () => events.push('prevented')});
    const trigger = {isConnected: true, focus: () => events.push('restored')};
    restoreEvidenceFocus(trigger);
    expect(events).toEqual(['prevented', 'keyboard', 'restored']);
  });

  /* The second row's name is a written line, not a single token. Package
     2.0.12 admits a candidate title as a proposition only when it carries
     whitespace, so a one word name renders no title control and this test
     would measure the naming rule instead of the selection controls it is
     about. */
  test('ready markup exposes local selection and proof controls without persistent signal URLs', () => {
    const ready = markup({topics: [signal(), signal({signal_id: SIG_B, signal_name: 'Second running route'})]});
    expect(ready).toContain('Candidate Second running route');
    expect(ready).toContain('Open evidence');
    expect(ready).not.toMatch(/#\/explore\/sig_|signal=/);
  });
});

describe('Open Discover route, Browse, and responsive contract', () => {
  test('legacy direct hash canonicalizes and Browse remains its search utility', () => {
    expect(canonicalizeDiscoverHash('#/discover')).toBe('#/explore');
    expect(canonicalizeDiscoverHash('#/discover/za?status=pending')).toBe('#/explore');
    expect(canonicalizeDiscoverHash('#/browse')).toBe('#/browse');
    const originalWindow = globalThis.window;
    const replacements = [];
    globalThis.window = {
      location: {hash: '#/discover'},
      history: {replaceState: (_state, _title, hash) => replacements.push(hash)},
    };
    try {
      expect(parseHash().view).toBe('explore');
      expect(replacements).toEqual(['#/explore']);
    } finally {
      globalThis.window = originalWindow;
    }
    const browse = renderToStaticMarkup(<BrowsePage region="ZA" />);
    expect(browse).toContain('Browse · the post archive');
    expect(browse).toContain('Search what people actually said');
    expect(browse).not.toContain('Choose a lens');
  });

  test('active product sources have no retired route or page import', () => {
    const src = fileURLToPath(new URL('../..', import.meta.url));
    const offenders = activeSources(src).flatMap((path) => {
      const source = readFileSync(path, 'utf8');
      return /#\/discover|['"]\/discover(?:['"/?])|DiscoverPage|Discover candidates|graph proposals|graph-born proposals/i.test(source)
        ? [path]
        : [];
    });
    expect(offenders).toEqual([]);
  });

  test('route CSS has zero overflow, 48 pixel controls, and reduced-motion overrides', () => {
    const css = readFileSync(fileURLToPath(new URL('../../ogilvy-intelligence.css', import.meta.url)), 'utf8');
    expect(declaration(css, '.app:has(.explore-page)', 'overflow-x')).toBe('clip');
    expect(declaration(css, '.explore-page', 'min-width')).toBe('0');
    expect(declaration(css, '.explore-page', 'max-width')).toBe('100%');
    expect(declaration(css, '.explore-page :is(a, button)', 'min-width')).toBe('48px');
    expect(declaration(css, '.explore-page :is(a, button)', 'min-height')).toBe('48px');
    expect(declaration(css, '.explore-selection', 'scroll-margin-top')).toBe('96px');
    expect(declaration(css, '.explore-selection', 'transition')).not.toBe('none');
    expect(declaration(css, '.explore-selection', 'transition', '(prefers-reduced-motion: reduce)')).toBe('none');
    expect(declaration(css, '.explore-page', 'scroll-behavior', '(prefers-reduced-motion: reduce)')).toBe('auto');
    expect(declaration(css, '.explore-page .evidence-drawer', 'transition', '(prefers-reduced-motion: reduce)')).toBe('none');
  });

  test.skipIf(!CHROME)('mobile ready geometry is measured in a real browser at 390 by 844', async () => {
    // The sibling CSS test reads declarations. This one renders and measures,
    // because a declaration cannot prove a rendered overflow or target size.
    const measured = await mountedGeometry({
      props: {
        topics: [signal()],
        freshness: {status: 'green', stamp_utc: '2026-08-28T06:30:00Z'},
        region: 'ZA',
      },
    });
    expect(measured.viewport[0]).toBe(390);
    expect(measured.exploreState).toBe('ready');
    expect(measured.horizontalOverflow).toBe(0);
    expect(measured.bodyOverflow).toBeLessThanOrEqual(0);
    expect(measured.controlCount).toBeGreaterThan(0);
    expect(measured.minControlHeight).toBeGreaterThanOrEqual(48);
    expect(measured.undersizedControls).toBe(0);
    expect(measured.leadTitleTop).not.toBeNull();
    expect(measured.leadTitleTop).toBeLessThanOrEqual(900);
  }, 60000);

  /* Round 9, items 3, 4 and 6, measured. The folio title "Discover" broke
     into "Disc / over" at type-8 inside the 196px folio column at 1730; the
     route carried three h1 elements; the kicker resolved the mono family at
     "MONO" 0, the sans cut of the variable font. */
  for (const width of [1730, 1280]){
    test.skipIf(!CHROME)(`ready geometry at ${width}: one folio line, two h1 elements, no kicker`, async () => {
      const measured = await mountedGeometry({
        width,
        height: 1000,
        props: {
          topics: [signal()],
          freshness: {status: 'green', stamp_utc: '2026-08-28T06:30:00Z'},
          region: 'ZA',
        },
      });
      expect(measured.viewport[0]).toBe(width);
      expect(measured.exploreState).toBe('ready');
      expect(measured.h1Count).toBe(2);
      /* The measurement is only worth its bytes if the probe rendered the
         product's own typefaces, so the resolved families are read back here
         as well as gated inside the probe. */
      expect(measured.resolvedFonts.serif).toMatch(/^"?Newsreader"?,/);
      expect(measured.resolvedFonts.mono).toMatch(/^"?Recursive Mono"?,/);
      expect(measured.resolvedFonts.sample).toMatch(/^"?Newsreader"?,/);
      expect(measured.folioLines).toBe(1);
      expect(measured.folioScrollWidth).toBeLessThanOrEqual(measured.folioClientWidth);
      /* Quiet register, 23 Sept 2026: the ready folio has no kicker at all;
         the heading says what the page is, so there is no eyebrow to measure. */
      expect(measured.kickerVariation).toBeNull();
    }, 60000);
  }

  test.skipIf(!CHROME)('mobile no-discovery geometry is measured in a real browser at 390 by 844', async () => {
    const measured = await mountedGeometry({
      props: {
        topics: [],
        freshness: {status: 'green', stamp_utc: '2026-08-28T06:30:00Z'},
        region: 'ZA',
      },
    });
    expect(measured.viewport[0]).toBe(390);
    expect(measured.horizontalOverflow).toBe(0);
    expect(measured.headingTop).not.toBeNull();
    expect(measured.headingTop).toBeLessThanOrEqual(900);
    expect(measured.undersizedControls).toBe(0);
  }, 60000);

  test('the contract CSS is owned by the open-intelligence routes and nothing else', () => {
    /* Compare reads the same admitted signals through the same grammar, so it
       loads the same sheet. Any third owner would mean a route styling itself
       against a contract it does not render. */
    const src = fileURLToPath(new URL('../..', import.meta.url));
    const owners = activeSources(src).filter((path) => (
      readFileSync(path, 'utf8').includes("import './ogilvy-intelligence.css';")
    )).sort();
    expect(owners).toEqual([join(src, 'compare.jsx'), join(src, 'explore.jsx')].sort());
  });
});

describe('Open Discover producer wiring', () => {
  const CURATED = [{id: 'music_amapiano', topic: 'Amapiano', label: 'MUSIC · ZA'}];
  const DYNAMIC = {
    signal: {
      contract_version: 'desk_dynamic_signal_v2',
      signal_id: SIG_A,
      discovery_mode: 'phrase',
      evidence_state: 'ready',
      signal_name: 'Repair tutorials moving into weekend routines',
    },
  };

  function props(dynamic_discovery, curated = CURATED) {
    return exploreDynamicProps({topics: curated, dynamic_discovery});
  }

  test('a ready run with no signal list is malformed, not an empty discovery', () => {
    /* Ready asserts the run completed and observed something. If the signal
       list is missing or is not a list, the payload is malformed, and
       reporting it as a successful run that found nothing is exactly the
       "completed run that observed nothing" this function exists to prevent. */
    for (const signals of [undefined, null, 'not a list', {}, 42]) {
      const result = props({
        contract_version: 'desk_dynamic_signal_v2',
        status: 'ready',
        requested_market: 'za',
        run: null,
        signals,
      });
      expect(result.topics).toEqual([]);
      expect(result.error).toBeTruthy();
    }
  });

  test('a no_discovery run with no signal list is still an honest empty run', () => {
    /* Unlike ready, no_discovery asserts the run completed and observed
       nothing, so an absent list agrees with the status rather than
       contradicting it. */
    const result = props({
      contract_version: 'desk_dynamic_signal_v2',
      status: 'no_discovery',
      requested_market: 'za',
      run: null,
    });
    expect(result.topics).toEqual([]);
    expect(result.error).toBeNull();
  });

  test('an older backend with no dynamic member is unavailable, never curated topics', () => {
    // The producer contract is explicit: a missing member "is treated as
    // unavailable. It never falls back to curated topics."
    const result = props(undefined);
    expect(result.topics).toEqual([]);
    expect(result.error).toBeTruthy();
    expect(result.error.code).toBe('no_dynamic_discovery_member');
    expect(JSON.stringify(result)).not.toContain('music_amapiano');
  });

  test('an unsettled desk load does not invent a missing-member error', () => {
    const result = exploreDynamicProps(null, true);
    expect(result).toEqual({topics: [], error: null});
  });

  test('an unavailable status becomes the bounded error prop', () => {
    const result = props({
      contract_version: 'desk_dynamic_signal_v2',
      status: 'unavailable',
      requested_market: 'za',
      run: null,
      signals: [],
      error: {code: 'no_released_closed_run', message: 'Open discovery is unavailable.', retryable: true},
    });
    expect(result.topics).toEqual([]);
    expect(result.error.code).toBe('no_released_closed_run');
    expect(result.error.message).toBe('Open discovery is unavailable.');
  });

  test('an unsupported contract version is refused rather than rendered', () => {
    const result = props({
      /* v1 is the retired desk contract: it carried no qualities, so a client
         that rendered it would present unmeasured signals as measured ones. */
      contract_version: 'desk_dynamic_signal_v1',
      status: 'ready',
      signals: [DYNAMIC],
      error: null,
    });
    expect(result.topics).toEqual([]);
    expect(result.error.code).toBe('unsupported_contract_version');
  });

  test('no_discovery passes an empty signal list with no error', () => {
    // Only this path may render the completed-run-found-nothing copy.
    const result = props({
      contract_version: 'desk_dynamic_signal_v2',
      status: 'no_discovery',
      requested_market: 'za',
      run: {run_id: 'oi_1'},
      signals: [],
      error: null,
    });
    expect(result.topics).toEqual([]);
    expect(result.error).toBeNull();
  });

  test('ready passes engine signals through unchanged and in engine order', () => {
    const second = {signal: {...DYNAMIC.signal, signal_id: SIG_B, signal_name: 'Second'}};
    const result = props({
      contract_version: 'desk_dynamic_signal_v2',
      status: 'ready',
      requested_market: 'za',
      run: {run_id: 'oi_1'},
      signals: [DYNAMIC, second],
      error: null,
    });
    expect(result.error).toBeNull();
    expect(result.topics).toEqual([DYNAMIC, second]);
    // Engine order is preserved exactly, with no client-side sort.
    expect(result.topics[0].signal.signal_id).toBe(SIG_A);
  });

  test('curated desk topics can never reach Discover in any state', () => {
    for (const dynamic of [
      undefined,
      {contract_version: 'desk_dynamic_signal_v2', status: 'unavailable', signals: [], error: {code: 'x', message: 'y'}},
      {contract_version: 'desk_dynamic_signal_v2', status: 'no_discovery', signals: [], error: null},
      {contract_version: 'desk_dynamic_signal_v2', status: 'ready', signals: [DYNAMIC], error: null},
    ]){
      expect(JSON.stringify(props(dynamic).topics)).not.toContain('music_amapiano');
    }
  });
});

describe('Engine order is the discovery order', () => {
  /* Named in the contract's mutation list as "sort by reach or score", and
     nothing caught it. The order signals arrive in is the engine's judgement
     about what moved. Re-sorting locally by anything the browser can see, a
     title, a score, a receipt count, silently changes which signal reads as
     the lead, which is the one thing on the page a strategist takes as given. */
  const ordered = (titles) =>
    titles.map((title, index) => signal({
      signal_id: `sig_${String(index)}${'a'.repeat(63)}`,
      signal_name: title,
    }));

  test('admission returns signals in the order they arrived', () => {
    const titles = ['Zebra crossing repair', 'Alpha repair clinics', 'Middle repair meetups'];
    const {signals} = admitExploreSignals(ordered(titles));
    expect(signals.map((s) => s.title)).toEqual(titles);
  });

  test('a reverse ordered payload keeps its reverse order', () => {
    const titles = ['Ccc', 'Bbb', 'Aaa'];
    const {signals} = admitExploreSignals(ordered(titles));
    expect(signals.map((s) => s.title)).toEqual(titles);
  });

  test('the rendered index presents signals in engine order', () => {
    const titles = ['Zebra crossing repair', 'Alpha repair clinics', 'Middle repair meetups'];
    const markup = renderToStaticMarkup(
      <ExplorePage topics={ordered(titles)} freshness={{status: 'green'}} region="ZA" />,
    );
    const positions = titles.map((title) => markup.indexOf(title));
    expect(positions.every((at) => at > -1)).toBe(true);
    expect([...positions].sort((a, b) => a - b)).toEqual(positions);
  });

  test('the first admitted signal leads, whatever its title or receipt count', () => {
    /* The lead is the first admitted row, not the loudest one. */
    const rows = ordered(['Zebra crossing repair', 'Alpha repair clinics']);
    rows[1].receipts = [
      ['TikTok', '@a', 'One', '18 posts', '2d', 'https://example.test/a'],
      ['TikTok', '@b', 'Two', '20 posts', '1d', 'https://example.test/b'],
    ];
    const {signals} = admitExploreSignals(rows);
    expect(signals[0].title).toBe('Zebra crossing repair');
    expect(signals[0].receiptCount).toBeLessThan(signals[1].receiptCount);
  });
});

describe('Open Discover filters', () => {
  const filterRows = () => [
    signal({signal_id: 'sig_' + 'a'.repeat(64), signal_name: 'Ready phrase move', discovery_mode: 'phrase', evidence_state: 'ready'}),
    signal({signal_id: 'sig_' + 'b'.repeat(64), signal_name: 'Thin creator move', discovery_mode: 'creator', evidence_state: 'thin'}),
    signal({signal_id: 'sig_' + 'c'.repeat(64), signal_name: 'Contradictory sound move', discovery_mode: 'sound', evidence_state: 'contradictory'}),
  ];

  test('filterExploreSignals narrows by readiness and by discovery mode without reordering', () => {
    const {signals} = admitExploreSignals(filterRows());
    expect(filterExploreSignals(signals, {readiness: 'all', mode: 'all'})).toHaveLength(3);
    const thin = filterExploreSignals(signals, {readiness: 'thin', mode: 'all'});
    expect(thin.map((s) => s.title)).toEqual(['Thin creator move']);
    const sound = filterExploreSignals(signals, {readiness: 'all', mode: 'sound'});
    expect(sound.map((s) => s.title)).toEqual(['Contradictory sound move']);
    const none = filterExploreSignals(signals, {readiness: 'ready', mode: 'sound'});
    expect(none).toHaveLength(0);
  });

  test('the rendered index carries a filter bar that states its result count honestly', () => {
    const markup = renderToStaticMarkup(
      <ExplorePage topics={filterRows()} freshness={{status: 'green'}} region="ZA" />,
    );
    expect(markup).toContain('discover-instrument');
    expect(markup).toContain('Filter discovery');
    /* Quiet register, 23 Sept 2026: the count says what the reader sees, in plain
       words; "admitted" is the engine's word for a signal that passed its gate. */
    expect(markup).toContain('3 of 3 signals shown');
    expect(markup).not.toContain('admitted signals');
  });

  /* Quiet register, 23 Sept 2026: the folio is the page title, the run's
     date and the run reference. The "Open intelligence" eyebrow repeated the
     heading and the "Market scope · ZA" line repeated the shell header in a
     code, so both are gone. The reference is the bare run id a reader copies,
     keeping every character, and it breaks only at its underscores in the
     narrow folio. */
  test('the Discover folio carries no eyebrow and no repeated market code, and the run id breaks at its joints', () => {
    const runId = 'run_20260912_dynamic_apply_v2_r1';
    const rows = filterRows().map((row) => ({...row, run_id: runId}));
    const markup = renderToStaticMarkup(<ExplorePage topics={rows} freshness={{status: 'green'}} region="ZA" />);
    const folio = markup.match(/<header class="explore-folio">([^]*?)<\/header>/)?.[1];
    expect(folio, 'the folio renders').toBeDefined();
    expect(folio).toContain('<h1>Discover</h1>');
    expect(folio).not.toContain('explore-state__kicker');
    expect(folio).not.toContain('Open intelligence');
    expect(folio).not.toContain('explore-folio__market');
    expect(folio).not.toContain('Market scope');
    expect(folio).toContain('<p class="explore-folio__run">Released run of 12 Sept 2026</p>');
    const code = folio.match(/<code>([^]*?)<\/code>/)?.[1];
    expect(code).toBe('run_<wbr/>20260912_<wbr/>dynamic_<wbr/>apply_<wbr/>v2_<wbr/>r1');
    expect(code.replace(/<wbr\/>/g, '')).toBe(runId);
  });

  test('the filtered-empty page names its market in words, not as a code', () => {
    const source = readFileSync(new URL('../../explore.jsx', import.meta.url), 'utf8');
    expect(source).toContain('regionLabel(');
    expect(source).not.toMatch(/Market scope/);
    expect(source).not.toMatch(/Open intelligence/);
  });

  test('a filter that empties the index says so instead of pretending no discovery', () => {
    const {signals} = admitExploreSignals(filterRows());
    const filtered = filterExploreSignals(signals, {readiness: 'unchecked', mode: 'all'});
    expect(filtered).toHaveLength(0);
    /* The page-level empty-filter state is asserted through the pure helper:
       state stays ready, the filter result is what emptied, and the UI keys
       its message off filtered.length with the admitted count still stated. */
    expect(signals).toHaveLength(3);
  });
});

describe('Signal qualities as words', () => {
  const withQualities = (qualities) => [signal({qualities})];

  test('admission carries the banded qualities and optional tags', () => {
    const {signals} = admitExploreSignals(withQualities({
      velocity: 'moderate', novelty: 'high', breadth: 'low', independence: 'moderate',
      history: 'unmeasured', geo_confidence: 'high', topic_tags: ['street_football'],
    }));
    expect(signals[0].qualities).toEqual({
      velocity: 'moderate', novelty: 'high', breadth: 'low', independence: 'moderate',
      history: 'unmeasured', geo_confidence: 'high',
    });
    expect(signals[0].topicTags).toEqual(['street_football']);
  });

  test('a signal without qualities is rejected', () => {
    const {signals} = admitExploreSignals([signal({qualities: undefined})]);
    expect(signals).toEqual([]);
  });

  test('a numeric quality poisons the signal so no engine score can reach the reader', () => {
    const {signals} = admitExploreSignals(withQualities({...signal().qualities, novelty: 0.82}));
    expect(signals).toEqual([]);
  });

  test('raw quality fields do not bypass the reviewed adapter into package presentation', () => {
    const markup = renderToStaticMarkup(
      <ExplorePage
        topics={withQualities({velocity: 'moderate', novelty: 'high', breadth: 'low', independence: 'moderate', history: 'unmeasured', geo_confidence: 'high', topic_tags: ['street_football']})}
        freshness={{status: 'green'}}
        region="ZA"
      />,
    );
    expect(markup).not.toContain('signal-qualities');
    expect(markup).not.toContain('street_football');
    expect(markup).not.toContain('Novelty');
    expect(markup).not.toContain('Geographic confidence');
  });
});

/* The host makes Discover's column count follow its own container rather than
   the viewport, because the package switches on viewport width and the host
   gives Discover a fraction of it. The changeover sits at 700px of container:
   below it the detail column is too narrow for its tiles and they overflow the
   page. The threshold is only worth having if it is pinned, so both sides of it
   are measured in a real browser with the real stylesheets. */
describe('Discover column count follows its container', () => {
  const CONTAINER_BOUNDARY = 700;

  async function measureAtContainerWidths(widths){
    const frontendRoot = fileURLToPath(new URL('../../../', import.meta.url));
    const packageCss = readFileSync(join(
      frontendRoot, 'node_modules', 'ogilvy-intelligence-design-system', 'dist', 'style.css',
    ), 'utf8');
    const hostCss = readFileSync(new URL('../../ogilvy-intelligence.css', import.meta.url), 'utf8');
    const discover = markup();
    const directory = mkdtempSync(join(tmpdir(), 'lp-discover-container-'));
    try {
      for (const width of widths){
        /* The layout grid is neutralised so the forced width is the container's
           own inline size, and the viewport stays wide so every package width
           rule is at its widest. The host rule has to win on container size
           alone, not because the viewport happens to be narrow. */
        writeFileSync(join(directory, `frame-${width}.html`),
          `<!doctype html><html data-dir="midnight"><head><meta charset="utf-8"><style>`
          + `html,body{margin:0}${packageCss.replace(/@font-face\{[^}]*\}/g, '')}${hostCss}`
          + `.explore-layout{display:block;padding:0}.explore-selection{width:${width}px}`
          + `</style></head><body>${discover}</body></html>`, 'utf8');
      }
      const pagePath = join(directory, 'measure.html');
      writeFileSync(pagePath, '<!doctype html><html><head><meta charset="utf-8"></head><body>'
        + widths.map((width) => `<iframe data-width="${width}" src="./frame-${width}.html" `
          + 'style="width:1600px;height:2400px;border:0"></iframe>').join('')
        + '<pre id="measured">pending</pre><script>'
        + 'const out=document.getElementById("measured");let tries=0;'
        + 'const read=()=>{tries+=1;try{'
        + 'const rows=[...document.querySelectorAll("iframe")].map((frame)=>{'
        + 'const view=frame.contentWindow;const doc=view.document;'
        + 'const instrument=doc.querySelector(".discover-instrument");'
        + 'if(!instrument)throw new Error("discover never rendered");'
        + 'return{container:Number(frame.dataset.width),'
        + 'columns:view.getComputedStyle(instrument).gridTemplateColumns};});'
        + 'out.textContent=JSON.stringify(rows);'
        + '}catch(error){if(tries<100){setTimeout(read,25);return}out.textContent=JSON.stringify({error:String(error)})}};'
        + 'read();</script></body></html>', 'utf8');
      const run = spawnSync(CHROME, [
        '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
        '--allow-file-access-from-files', '--hide-scrollbars',
        '--window-size=1700,1000', '--virtual-time-budget=6000', '--dump-dom',
        `--user-data-dir=${join(directory, 'profile')}`,
        pathToFileURL(pagePath).href,
      ], {encoding: 'utf8', timeout: 40000, windowsHide: true});
      const measured = JSON.parse(run.stdout.match(/<pre id="measured">([^<]+)<\/pre>/)[1]);
      if (measured.error) throw new Error(measured.error);
      return measured;
    } finally {
      rmSync(directory, {recursive: true, force: true});
    }
  }

  test.skipIf(!CHROME)('the container boundary stacks below it and splits at it', async () => {
    const below = CONTAINER_BOUNDARY - 1;
    const [stacked, split] = await measureAtContainerWidths([below, CONTAINER_BOUNDARY]);

    expect([stacked.container, split.container]).toEqual([below, CONTAINER_BOUNDARY]);

    /* The viewport is 1600 in both frames, so every package width rule is at its
       widest and only the container size can account for the difference. */
    const tracks = (value) => value.trim().split(/\s+/).length;
    expect(tracks(stacked.columns), `${below}px container: ${stacked.columns}`).toBe(1);
    expect(tracks(split.columns), `${CONTAINER_BOUNDARY}px container: ${split.columns}`).toBe(2);

    /* The reason the boundary sits where it does: below it the detail column is
       narrower than one tile track, `calc(var(--space-8) * 3)` or 192px, and the
       tiles overflow it. At the boundary the detail column can hold one. */
    const detail = Number.parseFloat(split.columns.trim().split(/\s+/)[1]);
    expect(detail, `${CONTAINER_BOUNDARY}px container detail column: ${split.columns}`)
      .toBeGreaterThanOrEqual(192);
  }, 60000);
});
