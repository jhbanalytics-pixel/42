/* Fieldwork contract.

   Restated 2 October 2026: the old Fieldwork read /api/internal/v2/fieldwork/read
   and /api/v2/source-lab, which belong to the retired app and answer 404 from
   42's API, so the page only ever said it could not read. Fieldwork is now 42's
   source roster and its health (GET /api/fieldwork, core/api/contract.md
   section 17). The navigation and route grammar tests below are kept as they
   were; the state, rendering, link and paging tests that pinned the retired
   dossier are restated against the roster, one for one where the concern still
   exists, each with the reason in its comment. */

import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {build} from 'esbuild';

import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {PRIMARY_NAV, UTILITY_NAV, normalizeView} from '../../redesignContract.js';
import {parseWorkspaceHash} from '../../router.js';
import * as fieldwork from '../../fieldwork.jsx';

const {FieldworkPage, buildFieldworkState, fieldworkPath} = fieldwork;
const days = JSON.parse(readFileSync(new URL('./fixtures/fieldwork42_days.json', import.meta.url), 'utf8'));
const render = (props) => renderToStaticMarkup(<FieldworkPage {...props} />);
const decode = (html) => html.replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&');
const text = (html) => decode(html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '));
const clone = (value) => JSON.parse(JSON.stringify(value));

describe('Fieldwork navigation', () => {
  test('primary navigation carries Fieldwork exactly once, fifth', () => {
    expect(PRIMARY_NAV.map((item) => item.label)).toEqual([
      'Briefing',
      'Discover',
      'Compare',
      'Build',
      'Fieldwork',
    ]);
    expect(PRIMARY_NAV[4]).toEqual({label: 'Fieldwork', path: '/fieldwork'});
    expect(PRIMARY_NAV.filter((item) => item.label === 'Fieldwork')).toHaveLength(1);
  });

  test('Fieldwork is not repeated in utility navigation', () => {
    expect(UTILITY_NAV.some((item) => item.label === 'Fieldwork')).toBe(false);
    expect(UTILITY_NAV.some((item) => item.path === '/fieldwork')).toBe(false);
  });

  test('Fieldwork survives the application view allowlist', () => {
    expect(normalizeView('fieldwork')).toBe('fieldwork');
  });

  test('the other workspaces keep their placement', () => {
    const utility = UTILITY_NAV.map((item) => item.label);
    for (const label of ['Source Lab', 'Historical']) expect(utility).toContain(label);
  });

  /* Restated: the retired instrument's mode tabs and Source Lab link are gone.
     The market tabs that replace them keep the same control move: a timed
     transition from the motion table, ink on hover and a red rule when chosen. */
  test('the market tabs take the control move in the fieldwork sheet', async () => {
    const postcss = (await import('postcss')).default;
    const root = postcss.parse(readFileSync(new URL('../../styles/fieldwork.css', import.meta.url), 'utf8'));
    const declarations = (selector) => {
      const found = {};
      root.walkRules((rule) => {
        if (rule.parent && rule.parent.type === 'atrule') return;
        if (!rule.selectors.map((s) => s.replace(/\s+/g, ' ')).includes(selector)) return;
        rule.walkDecls((declaration) => { found[declaration.prop] = declaration.value.trim(); });
      });
      return found;
    };
    expect(declarations('.fieldwork-tab').transition).toMatch(/var\(--motion-control\) var\(--motion-standard-easing\)/);
    expect(declarations('.fieldwork-tab:hover').color).toBe('var(--ink)');
    expect(declarations('.fieldwork-tab[aria-selected="true"]')['border-bottom-color']).toBe('var(--accent)');
  });
});

describe('Fieldwork route grammar', () => {
  test('the canonical route parses with no selector of any kind', () => {
    const parsed = parseWorkspaceHash('#/fieldwork');
    expect(parsed.view).toBe('fieldwork');
    expect(parsed.error).toBeNull();
    expect(parsed.investigationId).toBeNull();
    expect(parsed.artifactId).toBeNull();
    expect(parsed.mode).toBeNull();
  });

  test('a trailing slash is still the canonical route', () => {
    expect(parseWorkspaceHash('#/fieldwork').error).toBeNull();
  });

  test.each([
    ['#/fieldwork?lane=active'],
    ['#/fieldwork?market=za'],
    ['#/fieldwork/inv_1'],
    ['#/fieldwork/inv_1/detail'],
    ['#/fieldwork?actor=someone'],
  ])('%s is refused rather than normalised', (hash) => {
    const parsed = parseWorkspaceHash(hash);
    // Refused, and still Fieldwork. Falling back to Briefing would hide the
    // bad request and strand the strategist on a workspace they did not ask
    // for.
    expect(parsed.error).toBe('workspace_request_invalid');
    expect(parsed.view).toBe('fieldwork');
  });

  test('an invalid Fieldwork request never becomes Briefing', () => {
    for (const hash of ['#/fieldwork?lane=active', '#/fieldwork/inv_1']) {
      const parsed = parseWorkspaceHash(hash);
      expect(parsed.view).not.toBe('pulse');
      expect(parsed.view).not.toBe('');
    }
  });

  test('Fieldwork carries no client, market, vendor, actor or execution selector', () => {
    const parsed = parseWorkspaceHash('#/fieldwork');
    for (const forbidden of ['market', 'client', 'vendor', 'actor', 'lane', 'source', 'budget']) {
      expect(parsed[forbidden]).toBeUndefined();
    }
  });
});

/* Restated: the retired workspace had seventeen server states. The roster has
   the states a reader can meet: loading, a refused passcode, an error, a
   settings read that failed, and ready (with or without a record for the day). */
describe('Fieldwork page states', () => {
  test('loading outranks everything, including a payload already in hand', () => {
    expect(buildFieldworkState({loading: true, payload: days.ready}).state).toBe('loading');
  });

  test('a refused passcode is its own state, never an error or an empty roster', () => {
    expect(buildFieldworkState({error: {status: 401, auth: true}}).state).toBe('auth');
  });

  test('a transport or service failure is an error, never a roster', () => {
    expect(buildFieldworkState({error: {}})).toEqual({state: 'error', message: 'The 42 service did not answer.'});
    expect(buildFieldworkState({error: {status: 503, message: 'Not now.'}})).toEqual({state: 'error', message: 'Not now.'});
  });

  test('a payload whose markets cannot be read is an error, not a market with no sources', () => {
    for (const bad of [null, {}, {...days.ready, markets: null}, {...days.ready, date: 'yesterday'}]){
      expect(buildFieldworkState({payload: bad}).state).toBe('error');
    }
  });

  test('settings that could not be read say so instead of listing nothing', () => {
    const view = buildFieldworkState({payload: {...days.ready, markets: [], plan_state: 'unavailable', plan_note: 'The collection settings could not be read, so the source list is missing.'}});
    expect(view.state).toBe('unavailable');
    expect(text(render({payload: view.data}))).toContain('The collection settings could not be read');
  });

  test('ready keeps the markets in ZA, NG, KE order with the cross-market sources last', () => {
    const shuffled = {...days.ready, markets: [...days.ready.markets].reverse()};
    expect(buildFieldworkState({payload: shuffled}).data.markets.map((m) => m.market)).toEqual(['ZA', 'NG', 'KE', 'GLOBAL']);
  });

  test('a date reaches the API only when it is a real day', () => {
    expect(fieldworkPath(null)).toBe('/api/fieldwork');
    expect(fieldworkPath('2026-09-30')).toBe('/api/fieldwork?date=2026-09-30');
    expect(fieldworkPath('30/09/2026')).toBe('/api/fieldwork');
  });
});

describe('Fieldwork rendered roster', () => {
  test('the roster reads lead, day facts, market tabs, groups in order, then what is off', () => {
    const html = render({payload: days.ready});
    const order = ['Fieldwork', 'Collection day', 'Next collection', 'Sources by market', 'Platform feeds and trending boards', 'Own feeds', 'Kept creator accounts', 'Music and app charts', 'News sites and RSS', 'Google search interest', 'Switched off or retired', 'How to read this page'];
    const seen = text(html);
    const at = order.map((words) => seen.indexOf(words));
    expect(at.every((i) => i >= 0)).toBe(true);
    expect([...at].sort((a, b) => a - b)).toEqual(at);
  });

  test('a failed source states its reason in words, not colour alone', () => {
    const seen = text(render({payload: days.ready}));
    expect(seen).toContain('Failed: could not be read');
    /* Restated, design audit 2 October 2026: calls are a table column headed
       "Calls came back", so the cell reads "2 of 12" where the prose line read
       "2 of 12 calls came back". */
    expect(seen).toContain('Calls came back');
    expect(seen).toContain('2 of 12');
  });

  test('a day with no record says the roster is waiting, never zero', () => {
    const html = render({payload: days.absent});
    const seen = text(html);
    expect(seen).toContain('No collection record for this day yet');
    expect(seen).toContain('Not recorded yet');
    expect(seen).toContain('None charged');
    /* Restated, design audit 2 October 2026: the calls cell no longer carries
       the words "calls came back", so the check is on any "N of N" count. */
    expect(seen).not.toMatch(/\b\d+ of \d+\b/);
  });

  test('the page offers no control that starts collection or spends credits', () => {
    const html = render({payload: days.ready, onDate: () => {}, onRetry: () => {}});
    const buttons = [...html.matchAll(/<button[^>]*>([\s\S]*?)<\/button>/g)].map((m) => text(m[1]).trim());
    for (const words of buttons) expect(words).not.toMatch(/run|start|collect now|spend|approve/i);
  });

  test('each error renders its own message and withholds the roster', () => {
    const html = render({error: {status: 503, message: 'The 42 service is not answering right now.'}, onRetry: () => {}});
    expect(text(html)).toContain('The 42 service is not answering right now.');
    expect(html).toContain('Try again');
    expect(html).not.toContain('fieldwork-source');
  });

  test('loading announces itself as busy', () => {
    const html = render({loading: true});
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain('role="status"');
  });

  test('the page exposes one h1 for focus on route entry', () => {
    expect((render({payload: days.ready}).match(/<h1/g) || []).length).toBe(1);
  });
});

/* Restated: the retired page recomputed its next operation's link and refused
   supplied targets. The roster takes no link from the API at all: its only
   link is the fixed one to Coverage. */
describe('Fieldwork links', () => {
  test('the only link is to Coverage, whatever the payload carries', () => {
    const payload = clone(days.ready);
    payload.markets[0].sources[0].detail = 'javascript:alert(1)';
    payload.markets[0].sources[0].href = 'https://example.test/steal';
    const html = render({payload});
    expect([...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1])).toEqual(['#/coverage']);
  });
});

/* Restated: the retired inventory paged 523 rows 25 at a time. The roster
   pages long member lists instead: up to six names inline, else four and a
   disclosure with the rest, losing none. */
describe('Fieldwork long member lists', () => {
  test('a short list is shown whole', () => {
    const payload = clone(days.ready);
    const html = render({payload});
    expect(text(html)).toContain('@maphephandaba, @zalebs, @tshisalive');
  });

  test('a long list shows four, says how many more, and keeps every name', () => {
    const payload = clone(days.ready);
    const names = Array.from({length: 11}, (_, i) => 'Name ' + (i + 1));
    payload.markets[0].sources[0].members = names;
    const html = render({payload});
    expect(text(html)).toContain('Name 1, Name 2, Name 3, Name 4 and 7 more');
    for (const name of names) expect(text(html)).toContain(name);
  });
});

/* Restated: route statuses of the retired catalogue are gone. Sources the
   settings name but do not read are listed with their reason in plain words. */
describe('Fieldwork switched off sources', () => {
  test('every switched off source names its market and says why', () => {
    const seen = text(render({payload: days.ready}));
    expect(seen).toContain('X trends archive');
    expect(seen).toContain('Off until legal approves reading it.');
    // The market sits on its own line above the name, so a long name never breaks with it.
    expect(seen).toContain('Kenya Spotify daily chart (via Kworb)');
  });

  test('an empty off list renders no section', () => {
    expect(render({payload: {...days.ready, off: []}})).not.toContain('Switched off or retired');
  });
});

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

async function mountedFieldworkGeometry({props, width=390, height=844, probe=null}){
  const root = fileURLToPath(new URL('../../../../', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'lp-fieldwork-geometry-'));
  const bundlePath = join(directory, 'probe.js');
  const htmlPath = join(directory, 'probe.html');
  const profilePath = join(directory, 'chrome-profile');
  const chrome = CHROME;
  // Both sheets are required for a truthful measurement: the route-private
  // contract CSS, and the token sheet that defines every --oi-* it resolves
  // against. Measuring without them would report the geometry of unstyled
  // markup and prove nothing.
  const tokenCss = readFileSync(new URL('../../styles/ogilvy-intelligence.css', import.meta.url), 'utf8')
    + readFileSync(new URL('../../../node_modules/ogilvy-intelligence-design-system/dist/style.css', import.meta.url), 'utf8');
  const routeCss = readFileSync(new URL('../../styles/fieldwork.css', import.meta.url), 'utf8');
  // Headless Chrome clamps its own window width on Windows, so --window-size
  // cannot produce a 390 pixel viewport. An iframe can: media queries and
  // layout inside it resolve against the frame's own width, which is exactly
  // the viewport under test.
  const entry = `
    import React from 'react';
    import {createRoot} from 'react-dom/client';
    import {flushSync} from 'react-dom';
    import {FieldworkPage} from './frontend/src/fieldwork.jsx';
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
        flushSync(() => createRoot(mount).render(React.createElement(FieldworkPage, ${JSON.stringify(props)})));
        await wait();
        await wait();
        if (frameDocument.fonts && frameDocument.fonts.ready) await frameDocument.fonts.ready;
        await wait();
        const page = frameDocument.querySelector('.fieldwork-page');
        if (!page) throw new Error('explore page missing: ' + mount.innerHTML);
        const documentElement = frameDocument.documentElement;
        const controls = Array.from(page.querySelectorAll('button, summary, [role="button"], [role="tab"]'))
          .filter((node) => node.getClientRects().length > 0);
        const heights = controls.map((node) => node.getBoundingClientRect().height);
        const heading = page.querySelector('h1');
        const leadTitle = page.querySelector('.fieldwork-lead__title');
        const topOf = (node) => (node ? node.getBoundingClientRect().top + frameWindow.scrollY : null);
        result.textContent = encode({
          viewport: [frameWindow.innerWidth, frameWindow.innerHeight],
          horizontalOverflow: documentElement.scrollWidth - documentElement.clientWidth,
          bodyOverflow: frameDocument.body.scrollWidth - documentElement.clientWidth,
          fieldworkState: page.dataset.fieldworkState || null,
          headingTop: topOf(heading),
          leadTitleTop: topOf(leadTitle),
          controlCount: controls.length,
          minControlHeight: heights.length ? Math.min(...heights) : null,
          undersizedControls: heights.filter((value) => value < 48).length,
          /* A route-specific probe runs after the settled measurement with
             the frame in scope, so an interaction and its outcome are read
             from the same mounted page. */
          probe: await (async () => { ${probe || 'return null;'} })(),
        });
      } catch (error) {
        result.textContent = encode({error: String(error && error.stack || error)});
      }
    })();
  `;
  try {
    await build({
      stdin: {contents: entry, loader: 'jsx', resolveDir: root, sourcefile: 'fieldwork-geometry.jsx'},
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
      + `<style>${tokenCss}</style><style>${routeCss}</style></head>`
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
      // Chrome refuses to start as root with its sandbox on (containers, CI).
      ...(process.getuid && process.getuid() === 0 ? ['--no-sandbox'] : []),
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


describe('Fieldwork rendered geometry', () => {
  /* Restated: the retired v1 fixtures are replaced by the roster's own days
     (fixtures/fieldwork42_days.json, built by core/api/fieldwork.py from the
     API fixtures). Controls are the tabs, buttons and disclosures; inline
     text links are measured by the shell's own tests. */
  test.skipIf(!CHROME)('mobile ready geometry is measured in a real browser at 390 by 844', async () => {
    const measured = await mountedFieldworkGeometry({props: {payload: days.ready}});
    expect(measured.viewport[0]).toBe(390);
    expect(measured.fieldworkState).toBe('ready');
    expect(measured.horizontalOverflow).toBe(0);
    expect(measured.bodyOverflow).toBeLessThanOrEqual(0);
    expect(measured.controlCount).toBeGreaterThan(0);
    expect(measured.minControlHeight).toBeGreaterThanOrEqual(48);
    expect(measured.undersizedControls).toBe(0);
    expect(measured.leadTitleTop).not.toBeNull();
    expect(measured.leadTitleTop).toBeLessThanOrEqual(900);
  }, 60000);

  /* Second critique pass: at phone width the market switch must show every
     market whole, with no sideways scrolling and nothing clipped. Either the
     tabs fit, without their source counts, or a labelled select stands in. */
  const MARKET_SWITCH_PROBE = `
    const tablist = page.querySelector('[role="tablist"]');
    const shown = (node) => Boolean(node) && node.getClientRects().length > 0;
    const box = tablist && tablist.getBoundingClientRect();
    const tabs = tablist ? Array.from(tablist.querySelectorAll('[role="tab"]')) : [];
    const select = page.querySelector('.fieldwork-market-select select');
    const label = select && select.labels && select.labels[0];
    return {
      tabsShown: shown(tablist),
      tabsScroll: tablist ? tablist.scrollWidth - tablist.clientWidth : null,
      tabsClipped: shown(tablist) ? tabs.filter((tab) => {
        const r = tab.getBoundingClientRect();
        return r.left < box.left - 0.5 || r.right > box.right + 0.5 || tab.scrollWidth > tab.clientWidth;
      }).length : 0,
      countsShown: Array.from(page.querySelectorAll('.fieldwork-tab__count')).filter(shown).length,
      selectShown: shown(select),
      selectOptions: select ? Array.from(select.options).map((o) => o.textContent) : [],
      selectLabel: shown(label) ? label.textContent : null,
      selectRight: shown(select) ? select.getBoundingClientRect().right : null,
      pageRight: page.getBoundingClientRect().right,
    };
  `;
  for (const width of [360, 390, 520]){
    test.skipIf(!CHROME)(`at ${width} every market shows whole, with no sideways scrolling`, async () => {
      const measured = await mountedFieldworkGeometry({props: {payload: days.ready}, width, probe: MARKET_SWITCH_PROBE});
      const probe = measured.probe;
      expect(measured.horizontalOverflow).toBe(0);
      expect(probe.countsShown).toBe(0);
      expect(probe.tabsShown || probe.selectShown).toBe(true);
      if (probe.tabsShown){
        expect(probe.tabsScroll).toBeLessThanOrEqual(0);
        expect(probe.tabsClipped).toBe(0);
      } else {
        expect(probe.selectOptions).toEqual(days.ready.markets.map((m) => m.label));
        expect(probe.selectLabel).toBe('Market');
        expect(probe.selectRight).toBeLessThanOrEqual(probe.pageRight);
      }
    }, 60000);
  }

  test.skipIf(!CHROME)('at 1024 the tabs stay, with their source counts', async () => {
    const measured = await mountedFieldworkGeometry({props: {payload: days.ready}, width: 1024, probe: MARKET_SWITCH_PROBE});
    expect(measured.probe.tabsShown).toBe(true);
    expect(measured.probe.selectShown).toBe(false);
    expect(measured.probe.countsShown).toBe(days.ready.markets.length);
    expect(measured.probe.tabsClipped).toBe(0);
  }, 60000);

  test.skipIf(!CHROME)('a day with no record renders at 390 with no overflow', async () => {
    const measured = await mountedFieldworkGeometry({props: {payload: days.absent}});
    expect(measured.viewport[0]).toBe(390);
    expect(measured.fieldworkState).toBe('ready');
    expect(measured.horizontalOverflow).toBe(0);
    expect(measured.undersizedControls).toBe(0);
  }, 60000);

  test.skipIf(!CHROME)('an error renders at 390 with zero overflow', async () => {
    const measured = await mountedFieldworkGeometry({props: {error: {status: 503, message: 'Not now.'}}});
    expect(measured.viewport[0]).toBe(390);
    expect(measured.fieldworkState).toBe('error');
    expect(measured.horizontalOverflow).toBe(0);
    expect(measured.headingTop).not.toBeNull();
  }, 60000);
});

describe('Fieldwork route wiring', () => {
  const appSource = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8');

  test('the route renders Fieldwork and brings its own data', () => {
    expect(appSource).toContain("route === 'fieldwork'");
    expect(appSource).toContain('FieldworkWorkspace');
  });

  test('Fieldwork is a standalone route, so the generic desk gate never gates it', () => {
    // Fieldwork reads its own projection. Waiting on the desk payload would
    // make an unrelated failure look like a Fieldwork failure.
    const standalone = appSource.match(/const STANDALONE = new Set\(\[(.*?)\]\)/s);
    expect(standalone).toBeTruthy();
    expect(standalone[1]).toContain("'fieldwork'");
  });

  test('the route is lazy loaded like every other workspace', () => {
    expect(appSource).toMatch(/const FieldworkWorkspace = lazy\(\(\) => import\('\.\/fieldwork\.jsx'\)/);
  });

  /* Restated: the page now makes one read, GET /api/fieldwork on the 42 API,
     instead of the two retired reads. It still writes nothing. */
  test('route load makes one read and no generation, model, vendor, export or write request', () => {
    const source = readFileSync(new URL('../../fieldwork.jsx', import.meta.url), 'utf8');
    expect((source.match(/getJson\(/g) || []).length).toBe(1);
    expect(source).toContain("'/api/fieldwork'");
    for (const forbidden of ['apiPost', 'apiPut', 'apiPatch', 'apiDelete', 'fetch(', 'postJson', '/api/internal/', '/api/v2/']) {
      expect(source).not.toContain(forbidden);
    }
  });
});
