import {expect, test} from 'bun:test';
import {spawnSync} from 'node:child_process';
import {existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import postcss from 'postcss';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {ComparePanel} from '../../compare.jsx';
import {ExplorePage} from '../../explore.jsx';
import {TodayPage} from '../../today.jsx';

/* Round 2, Task 10 fix round 1. The package state frame dresses itself. A
   host rule under .explore-state may still style the wrapper's own children,
   but nothing it says about a paragraph or a button may reach inside
   [data-state-frame], and the rules that dressed the retired hand-drawn
   state markup are gone rather than left dead. */

const css = readFileSync(fileURLToPath(new URL('../../ogilvy-intelligence.css', import.meta.url)), 'utf8');
const selectors = [];
postcss.parse(css).walkRules((rule) => {
  for (const selector of rule.selectors) selectors.push(selector.trim());
});

const RETIRED = [
  '.explore-state h1',
  '.explore-state time',
  '.explore-state__actions',
  '.explore-state__actions button',
  '.explore-state__actions button:focus-visible',
  '.explore-state--filtered-empty button',
  /* Retired in round 5, task 27: after task 10 every .explore-state wrapper
     renders the package frame and nothing else, so the scoped paragraph
     rule matched nothing. */
  '.explore-state p:not([data-state-frame] p)',
];

test('the rules for the retired hand-drawn state markup are gone', () => {
  for (const retired of RETIRED) expect(selectors, retired).not.toContain(retired);
});

test('no host rule under .explore-state reaches a paragraph or button inside the state frame', () => {
  const reaching = selectors.filter((selector) => (
    /^\.explore-state/.test(selector)
    && /(^|[\s>+~])(p|button)(?![\w-])/.test(selector)
    /* The exclusion counts only when it names a descendant of the frame;
       :not([data-state-frame]) alone excludes the frame element and still
       reaches every paragraph inside it. */
    && !/:not\(\[data-state-frame\]\s+[^)]+\)/.test(selector)
  ));
  expect(reaching).toEqual([]);
});

/* Round 6, task 36a. The 2.0.6 held card is one calm editorial card that
   draws its own boundary, so the host paints no rule of its own above it.
   The Discover wrapper chain in a held branch is div.page.explore-page and
   section.explore-state; a border on either reads as a stray hairline across
   the workspace, which the task 34 daylight capture showed about 48px above
   the card. The first test reads the sheet, the second measures the mount. */

const HELD_WRAPPER = /^\.(?:page|explore-page|explore-state(?:--[\w-]+)?)(?:\.(?:page|explore-page|explore-state(?:--[\w-]+)?))*$/;
const EDGE_PROPERTY = /^(?:border(?:-(?:top|bottom|block|block-start|block-end))?(?:-(?:width|style|color))?|box-shadow|outline(?:-width)?)$/;

test('no host rule on the Discover held wrapper chain paints an edge', () => {
  const painting = [];
  postcss.parse(css).walkRules((rule) => {
    if (!rule.selectors.some((selector) => HELD_WRAPPER.test(selector.trim()))) return;
    rule.walkDecls((decl) => {
      if (!EDGE_PROPERTY.test(decl.prop)) return;
      if (/^(?:0|none|0px|transparent)$/.test(decl.value.trim())) return;
      painting.push(`${rule.selector} { ${decl.prop}: ${decl.value} }`);
    });
  });
  expect(painting).toEqual([]);
  expect(selectors.filter((selector) => /(^|[\s>+~])hr(?![\w-])/.test(selector) && /explore/.test(selector))).toEqual([]);
});

const CHROME = [
  process.env.CHROME_PATH,
  process.env.CHROME_BIN,
  String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
  String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean).find((candidate) => existsSync(candidate)) || null;

const HELD_BRANCHES = {
  loading: {loading: true},
  error: {error: {message: 'source unreadable'}},
  stale: {freshness: {status: 'amber', stamp_utc: '2026-08-27T06:30:00Z'}},
  no_discovery: {freshness: {status: 'green'}},
};

/* Every stylesheet the built Discover route loads, in load order: the package
   sheet, the boot sheet, the token sheet, the app sheet and the route sheet. The mount is the
   shell workspace element from App.jsx around the static markup of each held
   branch, under the .oi-product scope the package shell carries its tokens
   on, and the probe reports every element outside the package frame whose
   computed border, outline or shadow paints, plus every hr. */
function heldWorkspaceMarkup(){
  return '<div class="oi-product">' + Object.entries(HELD_BRANCHES).map(([state, props]) => (
    `<div id="main-content" class="route-enter" data-held-branch="${state}">`
    + renderToStaticMarkup(<ExplorePage region="ZA" topics={[]} {...props} />)
    + '</div>'
  )).join('\n') + '</div>';
}

/* Mounts the markup under the four sheets in headless Chrome inside a
   viewport of the given width, runs the probe script against that document,
   and returns whatever the script wrote into #result as base64 JSON. The
   viewport is a srcdoc iframe sized to the width rather than the Chrome
   window, because headless Chrome on Windows will not open a window under
   about 500px wide, and the narrow cell is 390. The script runs in the outer
   page with `document` bound to the iframe document, so media queries and
   bounding boxes both read the requested width. */
const escapeAttribute = (value) => value.replace(/&/g, '&amp;').replace(/"/g, '&quot;');

function probeInChrome(markup, script, width = 1280){
  const src = fileURLToPath(new URL('../../', import.meta.url));
  const sheets = [
    join(src, '..', 'node_modules', 'ogilvy-intelligence-design-system', 'dist', 'style.css'),
    join(src, 'styles', 'boot.css'),
    join(src, 'styles', 'ogilvy-intelligence.css'),
    join(src, 'app.css'),
    join(src, 'ogilvy-intelligence.css'),
  ].map((path) => readFileSync(path, 'utf8')).join('\n');
  const directory = mkdtempSync(join(tmpdir(), 'lp-held-probe-'));
  const htmlPath = join(directory, 'probe.html');
  const inner = `<!doctype html><html><head><meta name="viewport" content="width=device-width"><style>${sheets}</style></head><body>${markup}</body></html>`;
  const outer = `<!doctype html><html><body style="margin:0">`
    + `<iframe id="viewport" style="display:block;width:${width}px;height:1400px;border:0" srcdoc="${escapeAttribute(inner)}"></iframe>`
    + `<pre id="result">pending</pre>`
    + `<script>document.getElementById('viewport').addEventListener('load', () => {`
    + `const run = new Function('document', 'result', ${JSON.stringify(script)});`
    + `run(document.getElementById('viewport').contentDocument, document.getElementById('result'));`
    + `});</script></body></html>`;
  writeFileSync(htmlPath, outer, 'utf8');
  try {
    const run = spawnSync(CHROME, [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--hide-scrollbars',
      `--window-size=${width + 100},1500`,
      '--virtual-time-budget=3000', '--dump-dom', `--user-data-dir=${join(directory, 'chrome-profile')}`,
      pathToFileURL(htmlPath).href,
    ], {encoding: 'utf8', timeout: 20000, windowsHide: true, maxBuffer: 64 * 1024 * 1024});
    expect(run.error).toBeUndefined();
    expect(run.status).toBe(0);
    const encoded = run.stdout.match(/<pre id="result">([^<]+)<\/pre>/)?.[1];
    expect(encoded).toBeTruthy();
    return JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'));
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}

function mountedEdges(markup){
  const script = `
    const paints = (value) => value.split(' ').some((part) => parseFloat(part) > 0);
    const found = [];
    for (const mount of document.querySelectorAll('#main-content')){
      const frame = mount.querySelector('[data-state-frame]');
      for (const element of [mount, ...mount.querySelectorAll('*')]){
        if (frame && (element === frame || frame.contains(element))) continue;
        const style = getComputedStyle(element);
        const edges = [];
        if (element.tagName === 'HR') edges.push('hr');
        for (const side of ['top', 'bottom']){
          if (parseFloat(style.getPropertyValue('border-' + side + '-width')) > 0 && style.getPropertyValue('border-' + side + '-style') !== 'none') edges.push('border-' + side);
        }
        if (parseFloat(style.outlineWidth) > 0 && style.outlineStyle !== 'none') edges.push('outline');
        if (style.boxShadow !== 'none') edges.push('box-shadow');
        if (edges.length) found.push(mount.dataset.heldBranch + ' ' + element.tagName.toLowerCase() + '.' + element.className + ': ' + edges.join(','));
      }
      if (!frame) found.push(mount.dataset.heldBranch + ': no package frame rendered');
    }
    result.textContent = btoa(JSON.stringify(found));
  `;
  return probeInChrome(markup, script);
}

test.skipIf(!CHROME)('nothing between the shell workspace and the package frame paints an edge in a held Discover branch', () => {
  expect(mountedEdges(heldWorkspaceMarkup())).toEqual([]);
});

/* Round 6, task 36b. The card carries the width law, not the sentence: every
   held frame is at most 780px wide and centred in its workspace, on every
   route. Discover's .explore-state wrapper had the law since round 2 and
   Compare shares that class; the Briefing wrapper is a bare div carrying
   data-briefing-state, which the route assertions pin, so the boot sheet
   reaches it by attribute. Each route's held branches mount in the shell workspace
   element at four viewport widths and the package frame's box is measured
   against the workspace. Above the 680px breakpoint the frame is at most
   780 wide with equal side gaps; at 390 the wrapper is the workspace less
   the 32px the narrow rule leaves and the frame sits inside its 20px side
   padding, so the frame is the workspace less 72, still centred. */

/* Neither Briefing's nor Discover's loading branch is in this table. Task 86
   scoped the width law to held frames: a held state is the answer and earns
   the centring, a loading frame only holds the place of the answer and takes
   the settle's corner instead. Task 89 widened that to Discover once the
   controller's 6 September ruling moved the origin claim onto the composition
   every settle shares. Both are measured on their own terms below. Compare
   keeps its loading branch here, because its settle has not been ruled on. */
const WIDTH_LAW_ROUTES = {
  briefing: {
    render: (props) => <TodayPage topics={[]} {...props} />,
    branches: {
      error: {error: {code: 'api_unavailable', message: 'unreadable'}},
      stale: {freshness: {status: 'amber', stamp_utc: '2026-08-27T06:30:00Z'}},
      no_discovery: {freshness: {status: 'green'}},
    },
  },
  discover: {
    render: (props) => <ExplorePage region="ZA" topics={[]} {...props} />,
    branches: Object.fromEntries(Object.entries(HELD_BRANCHES).filter(([branch]) => branch !== 'loading')),
  },
  compare: {
    render: (props) => <ComparePanel topics={[]} {...props} />,
    branches: {
      loading: {loading: true},
      error: {error: {code: 'api_unavailable', message: 'unreadable'}},
      insufficient: {},
    },
  },
};

const WIDE_VIEWPORTS = [1024, 1440, 1730];
const NARROW_VIEWPORT = 390;
const NARROW_GUTTER = 32 + 2 * 20;

function widthLawMarkup(){
  return '<div class="oi-product">' + Object.entries(WIDTH_LAW_ROUTES).flatMap(([route, {render, branches}]) => (
    Object.entries(branches).map(([branch, props]) => (
      `<div id="main-content" class="route-enter" data-held-route="${route}" data-held-branch="${branch}">`
      + renderToStaticMarkup(render(props))
      + '</div>'
    ))
  )).join('\n') + '</div>';
}

function measuredFrames(width){
  const script = `
    const found = [];
    for (const mount of document.querySelectorAll('#main-content')){
      const frame = mount.querySelector('[data-state-frame]');
      const box = mount.getBoundingClientRect();
      const cell = {route: mount.dataset.heldRoute, branch: mount.dataset.heldBranch, workspace: box.width};
      if (frame){
        const rect = frame.getBoundingClientRect();
        Object.assign(cell, {width: rect.width, left: rect.left - box.left, right: box.right - rect.right});
      }
      found.push(cell);
    }
    result.textContent = btoa(JSON.stringify(found));
  `;
  return probeInChrome(widthLawMarkup(), script, width);
}

const expectedCells = Object.entries(WIDTH_LAW_ROUTES).flatMap(([route, {branches}]) => Object.keys(branches).map((branch) => `${route}/${branch}`));

for (const viewport of WIDE_VIEWPORTS){
  test.skipIf(!CHROME)(`every held frame is at most 780px wide and centred in the workspace at ${viewport}`, () => {
    const cells = measuredFrames(viewport);
    expect(cells.map((cell) => `${cell.route}/${cell.branch}`)).toEqual(expectedCells);
    const breaches = cells
      .filter((cell) => cell.width === undefined || cell.width > 780.5 || Math.abs(cell.left - cell.right) > 1)
      .map((cell) => `${cell.route}/${cell.branch} width ${cell.width} left ${cell.left} right ${cell.right} in ${cell.workspace}`);
    expect(breaches).toEqual([]);
  });
}

/* Round 18, task 86, widened to Discover in round 19 by task 89. The width law
   governs held frames and not loading frames. A held card is a composed answer
   and earns its 780px centring because the held state is the answer. A loading
   frame only holds the place of what is coming, so it has to take the geometry
   of the settle rather than of a sibling state.

   Task 86 exempted Briefing alone and left Discover under the law, because the
   origin was then measured against whichever settle the fixture reached and
   Discover's was the selection column of a two column layout, which a full
   width loading frame stands further from than a centred card does. The
   controller's 6 September ruling replaced that question: where the origin
   claim and the shared composition claim conflict the shared composition wins,
   and the origin is measured against the part every settle of the route
   shares. Below the page element that part starts at the workspace's own top
   left corner on both routes, article.instrument-briefing on one and
   div.explore-layout on the other, so both loading frames start there too and
   the reader watches the answer grow once from that corner instead of sliding
   across. The measure is left alone on both: the card is prose and the
   workspace is 1032px wide at 1280.

   Two boxes are read per route and the wrapper is the one the claim is about,
   because the wrapper is the element the page puts where the settle's own
   first element will stand. The frame inside it is read as well, since a
   wrapper at the corner holding a frame that is not tells the reader the same
   lie the law exists to stop. Measured in the browser at both edges of the
   matrix, because a declaration is not a rendered box. */
const LOADING_ROUTES = {
  briefing: {page: ".page[data-screen-label='Briefing']", render: () => <TodayPage topics={[]} loading />},
  discover: {page: '.page.explore-page', render: () => <ExplorePage region="ZA" topics={[]} loading />},
};

function measuredLoadingFrames(width){
  const script = `
    const found = [];
    for (const mount of document.querySelectorAll('#main-content')){
      const page = mount.querySelector(mount.dataset.loadingPage);
      const wrapper = page && page.firstElementChild;
      const frame = mount.querySelector('[data-state-frame]');
      const box = mount.getBoundingClientRect();
      const cell = {route: mount.dataset.loadingRoute, workspace: box.width};
      if (wrapper){
        const rect = wrapper.getBoundingClientRect();
        Object.assign(cell, {wrapper: wrapper.tagName.toLowerCase(), wrapperLeft: rect.left - box.left, wrapperTop: rect.top - box.top});
      }
      if (frame){
        const rect = frame.getBoundingClientRect();
        Object.assign(cell, {width: rect.width, left: rect.left - box.left, top: rect.top - box.top});
      }
      found.push(cell);
    }
    result.textContent = btoa(JSON.stringify(found));
  `;
  const markup = '<div class="oi-product">' + Object.entries(LOADING_ROUTES).map(([route, {page, render}]) => (
    `<div id="main-content" class="route-enter" data-loading-route="${route}" data-loading-page="${page}">`
    + renderToStaticMarkup(render())
    + '</div>'
  )).join('\n') + '</div>';
  return probeInChrome(markup, script, width);
}

for (const viewport of [1280, NARROW_VIEWPORT]){
  test.skipIf(!CHROME)(`every loading frame starts at the workspace corner at ${viewport}`, () => {
    const cells = measuredLoadingFrames(viewport);
    expect(cells.map((cell) => cell.route)).toEqual(Object.keys(LOADING_ROUTES));
    const breaches = [];
    for (const cell of cells){
      if (cell.wrapper === undefined) breaches.push(`${cell.route} put nothing inside its page`);
      if (cell.width === undefined) breaches.push(`${cell.route} rendered no package frame`);
      if (Math.abs(cell.wrapperLeft) > 1) breaches.push(`${cell.route} wrapper left ${cell.wrapperLeft}`);
      if (Math.abs(cell.wrapperTop) > 1) breaches.push(`${cell.route} wrapper top ${cell.wrapperTop}`);
      if (Math.abs(cell.left) > 1) breaches.push(`${cell.route} frame left ${cell.left}`);
      if (Math.abs(cell.top) > 1) breaches.push(`${cell.route} frame top ${cell.top}`);
      if (cell.width > 780.5) breaches.push(`${cell.route} width ${cell.width} past the frame's own measure`);
      if (cell.width > cell.workspace + 0.5) breaches.push(`${cell.route} width ${cell.width} past the workspace ${cell.workspace}`);
    }
    expect(breaches).toEqual([]);
  });
}

/* Round 6, task 38. From 2.0.9 the sentence has no inner measure: the card
   carries the width law alone, so the body paragraph's content box is the
   card's content box on every held branch. 2.0.7 capped the sentence at
   640px, which at 1730 left a ragged right margin inside the card. */

function measuredBodies(width){
  const script = `
    const content = (element) => {
      const style = getComputedStyle(element);
      return element.getBoundingClientRect().width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
        - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth);
    };
    const found = [];
    for (const mount of document.querySelectorAll('#main-content')){
      const frame = mount.querySelector('[data-state-frame]');
      const body = frame && frame.querySelector('.state-view__body');
      const cell = {route: mount.dataset.heldRoute, branch: mount.dataset.heldBranch};
      if (frame) cell.card = content(frame);
      if (body) cell.body = content(body);
      found.push(cell);
    }
    result.textContent = btoa(JSON.stringify(found));
  `;
  return probeInChrome(widthLawMarkup(), script, width);
}

test('the package sheet gives the state sentence no measure of its own', () => {
  const packageCss = readFileSync(fileURLToPath(new URL('../../../node_modules/ogilvy-intelligence-design-system/dist/style.css', import.meta.url)), 'utf8');
  const measures = [];
  postcss.parse(packageCss).walkRules((rule) => {
    if (!rule.selectors.some((selector) => /\.state-view__body/.test(selector))) return;
    rule.walkDecls(/^(?:max-inline-size|max-width|inline-size|width)$/, (decl) => measures.push(`${rule.selector} { ${decl.prop}: ${decl.value} }`));
  });
  expect(measures).toEqual([]);
});

test.skipIf(!CHROME)('the body paragraph fills the card content width on every held branch at 1730', () => {
  const cells = measuredBodies(1730);
  expect(cells.map((cell) => `${cell.route}/${cell.branch}`)).toEqual(expectedCells);
  const breaches = cells
    .filter((cell) => cell.card === undefined || cell.body === undefined || Math.abs(cell.body - cell.card) > 0.5)
    .map((cell) => `${cell.route}/${cell.branch} body ${cell.body} card ${cell.card}`);
  expect(breaches).toEqual([]);
});

test.skipIf(!CHROME)(`every held frame fills the workspace less the narrow gutter at ${NARROW_VIEWPORT}`, () => {
  const cells = measuredFrames(NARROW_VIEWPORT);
  expect(cells.map((cell) => `${cell.route}/${cell.branch}`)).toEqual(expectedCells);
  const breaches = cells
    .filter((cell) => cell.width === undefined || Math.abs(cell.width - (cell.workspace - NARROW_GUTTER)) > 1 || Math.abs(cell.left - cell.right) > 1)
    .map((cell) => `${cell.route}/${cell.branch} width ${cell.width} left ${cell.left} right ${cell.right} in ${cell.workspace}`);
  expect(breaches).toEqual([]);
});
