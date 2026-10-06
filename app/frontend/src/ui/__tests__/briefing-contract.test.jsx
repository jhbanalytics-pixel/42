import {expect, test} from 'bun:test';
import {spawnSync} from 'node:child_process';
import {existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import postcss from 'postcss';
import {InstrumentShell} from 'ogilvy-intelligence-design-system';
import {build as viteBuild} from 'vite';

import {updatedLabel} from '../../App.jsx';
import {splitInstrumentStylesheet} from '../../instrumentStylesheetSplit.mjs';
import {
  LIVE_MARGIN_CHAPTERS,
  chapterProgress,
  scrollBehavior,
} from '../../liveMarginContract.js';
import {
  observeLiveMarginChapters,
  scrollToLiveMarginChapter,
  useLiveMargin,
} from '../useLiveMargin.js';
import {
  buildBriefingModel,
  buildBriefingQueue,
  buildBriefingState,
} from '../../briefingContract.js';
import {PasscodeScreen} from '../../passcode.jsx';
import {TodayPage} from '../../today.jsx';
import {OgilvyMasthead, OgilvyShell, RedThreadBriefing} from '../index.js';
import {ROOT_SIGNAL, freshFixture} from './fixtures/instrument-payloads.js';

function parsedCssRule(css, selector, mediaParams){
  const root = postcss.parse(css);
  let scope = root;
  if (mediaParams){
    const media = [];
    root.walkAtRules('media', (atRule) => {
      if (atRule.params === mediaParams) media.push(atRule);
    });
    if (media.length !== 1) throw new Error(`Expected one media rule: ${mediaParams}`);
    scope = media[0];
  }
  const rules = [];
  scope.walkRules((rule) => {
    if (rule.parent === scope && rule.selectors.includes(selector)) rules.push(rule);
  });
  if (rules.length !== 1) throw new Error(`Expected one CSS rule: ${selector}`);
  return rules[0];
}

function parsedCssValue(css, selector, property, mediaParams){
  const declarations = parsedCssRule(css, selector, mediaParams).nodes.filter((node) => (
    node.type === 'decl' && node.prop === property
  ));
  if (declarations.length !== 1) throw new Error(`Expected one ${property} declaration: ${selector}`);
  return declarations[0].value;
}

test('Live Margin contract maps each approved chapter to its reading progress', () => {
  expect(LIVE_MARGIN_CHAPTERS).toEqual(['why-now', 'proof', 'precedent', 'response']);
  expect(LIVE_MARGIN_CHAPTERS.map(chapterProgress)).toEqual([0.25, 0.5, 0.75, 1]);
  expect(() => chapterProgress('retired')).toThrow('Unknown Live Margin chapter');
});

test('Live Margin navigation selects an immediate scroll for reduced motion', () => {
  expect(scrollBehavior(false)).toBe('smooth');
  expect(scrollBehavior(true)).toBe('auto');
});

function LiveMarginHarness(){
  const {activeChapter, navigateToChapter, progress, registerChapter} = useLiveMargin();
  const proofRef = registerChapter('proof');
  return <output
    data-active-chapter={activeChapter}
    data-has-navigate={typeof navigateToChapter}
    data-progress={progress}
    data-has-register={typeof registerChapter}
    data-register-stable={String(proofRef === registerChapter('proof'))}
  />;
}

function withWindow(value, run){
  const hadWindow = Object.prototype.hasOwnProperty.call(globalThis, 'window');
  const originalWindow = globalThis.window;
  globalThis.window = value;
  try {
    return run();
  } finally {
    if (hadWindow) globalThis.window = originalWindow;
    else delete globalThis.window;
  }
}

test('Live Margin hook renders safely without browser globals', () => {
  const markup = renderToStaticMarkup(<LiveMarginHarness />);

  expect(markup).toContain('data-active-chapter="why-now"');
  expect(markup).toContain('data-progress="0.25"');
  expect(markup).toContain('data-has-register="function"');
  expect(markup).toContain('data-has-navigate="function"');
  expect(markup).toContain('data-register-stable="true"');
});

test('Live Margin observer skips missing browser support and missing targets', () => {
  withWindow({}, () => {
    const cleanup = observeLiveMarginChapters(new Map([['why-now', null]]), () => {});
    expect(typeof cleanup).toBe('function');
    cleanup();
  });

  withWindow({
    IntersectionObserver: class {
      constructor(){ throw new Error('an observer must not be created without targets'); }
    },
  }, () => {
    observeLiveMarginChapters(new Map([['why-now', null]]), () => {})();
  });
});

test('Live Margin observer shares one instance and disconnects on cleanup', () => {
  const observers = [];
  class TestObserver {
    constructor(callback){
      this.callback = callback;
      this.observed = [];
      this.disconnected = false;
      observers.push(this);
    }

    observe(node){ this.observed.push(node); }
    disconnect(){ this.disconnected = true; }
  }
  const whyNow = {};
  const proof = {};
  let activeChapter = '';

  withWindow({IntersectionObserver: TestObserver}, () => {
    const cleanup = observeLiveMarginChapters(
      new Map([['why-now', whyNow], ['proof', proof], ['precedent', null]]),
      (chapter) => { activeChapter = chapter; },
    );

    expect(observers).toHaveLength(1);
    expect(observers[0].observed).toEqual([whyNow, proof]);
    observers[0].callback([{isIntersecting: true, target: proof}]);
    expect(activeChapter).toBe('proof');
    cleanup();
    expect(observers[0].disconnected).toBe(true);
  });
});

function observeLiveMarginWithProbe(chapterTargets, onActiveChapter){
  let observer;
  class TestObserver {
    constructor(callback){
      this.callback = callback;
      this.observed = [];
      this.disconnected = false;
      observer = this;
    }

    observe(node){ this.observed.push(node); }
    disconnect(){ this.disconnected = true; }
  }

  const cleanup = withWindow({IntersectionObserver: TestObserver}, () => (
    observeLiveMarginChapters(chapterTargets, onActiveChapter)
  ));
  return {cleanup, observer};
}

test('Live Margin observer selects the visible chapter nearest the reading edge', () => {
  const whyNow = {};
  const proof = {};
  let activeChapter = '';
  const {observer} = observeLiveMarginWithProbe(
    new Map([['why-now', whyNow], ['proof', proof]]),
    (chapter) => { activeChapter = chapter; },
  );
  const whyNowEntry = {isIntersecting: true, target: whyNow, boundingClientRect: {top: 24}};
  const proofEntry = {isIntersecting: true, target: proof, boundingClientRect: {top: 180}};

  observer.callback([proofEntry, whyNowEntry]);
  expect(activeChapter).toBe('why-now');
  activeChapter = '';
  observer.callback([whyNowEntry, proofEntry]);
  expect(activeChapter).toBe('why-now');
});

test('Live Margin observer advances through visible chapters across separate deliveries', () => {
  const proof = {};
  const precedent = {};
  const response = {};
  const emitted = [];
  const {observer} = observeLiveMarginWithProbe(
    new Map([['proof', proof], ['precedent', precedent], ['response', response]]),
    (chapter) => emitted.push(chapter),
  );

  observer.callback([
    {isIntersecting: true, target: proof, boundingClientRect: {top: 20}},
    {isIntersecting: true, target: precedent, boundingClientRect: {top: 180}},
  ]);
  observer.callback([
    {isIntersecting: false, target: proof, boundingClientRect: {top: -120}},
  ]);
  observer.callback([
    {isIntersecting: false, target: precedent, boundingClientRect: {top: -80}},
    {isIntersecting: true, target: response, boundingClientRect: {top: 120}},
  ]);

  expect(emitted).toEqual(['proof', 'precedent', 'response']);
  expect(emitted.map(chapterProgress)).toEqual([0.5, 0.75, 1]);
});

function createNaturalScrollProbe({
  positions={proof: 180, precedent: 600, response: 760},
  scrollHeight=1071,
  viewportHeight=900,
}={}){
  let scrollY = 0;
  let scrollListener;
  let removedScrollListener;
  let scrollOptions;
  let nextFrameId = 0;
  let observer;
  const cancelledFrames = [];
  const frames = new Map();
  class TestObserver {
    constructor(callback){
      this.callback = callback;
      observer = this;
    }
    observe(){}
    disconnect(){}
  }
  const targetAt = (absoluteTop) => ({
    getBoundingClientRect: () => ({top: absoluteTop - scrollY}),
  });
  const probeWindow = {
    IntersectionObserver: TestObserver,
    innerHeight: viewportHeight,
    document: {documentElement: {scrollHeight}},
    requestAnimationFrame(callback){
      nextFrameId += 1;
      frames.set(nextFrameId, callback);
      return nextFrameId;
    },
    cancelAnimationFrame(frameId){
      cancelledFrames.push(frameId);
      frames.delete(frameId);
    },
    addEventListener(type, listener, options){
      if (type === 'scroll'){
        scrollListener = listener;
        scrollOptions = options;
      }
    },
    removeEventListener(type, listener){
      if (type === 'scroll') removedScrollListener = listener;
    },
  };
  Object.defineProperty(probeWindow, 'scrollY', {get: () => scrollY});
  return {
    cancelledFrames,
    flushFrame(){
      const pending = [...frames.values()];
      frames.clear();
      for (const callback of pending) callback();
    },
    pendingFrames: () => frames.size,
    observer: () => observer,
    removedScrollListener: () => removedScrollListener,
    scrollListener: () => scrollListener,
    scrollOptions: () => scrollOptions,
    setScroll: (nextScrollY) => { scrollY = nextScrollY; },
    targets: new Map(Object.entries(positions).map(([chapterId, top]) => (
      [chapterId, targetAt(top)]
    ))),
    window: probeWindow,
  };
}

test('Live Margin natural scroll reads current chapter rectangles without false exits', () => {
  const probe = createNaturalScrollProbe();
  const emitted = [];

  withWindow(probe.window, () => {
    const cleanup = observeLiveMarginChapters(probe.targets, (chapter) => {
      if (emitted.at(-1) !== chapter) emitted.push(chapter);
    });
    expect(typeof probe.scrollListener()).toBe('function');
    for (const scrollY of [0, 60, 120, 171]){
      probe.setScroll(scrollY);
      probe.scrollListener()();
      probe.scrollListener()();
      expect(probe.pendingFrames()).toBe(1);
      probe.flushFrame();
    }
    cleanup();
  });

  expect(emitted).toEqual(['proof', 'precedent', 'response']);
  expect(emitted.map(chapterProgress)).toEqual([0.5, 0.75, 1]);
});

test('Live Margin scroll evaluation is passive throttled and cancelled on cleanup', () => {
  const probe = createNaturalScrollProbe();

  withWindow(probe.window, () => {
    const cleanup = observeLiveMarginChapters(probe.targets, () => {});
    expect(probe.scrollOptions()).toEqual({passive: true});
    probe.scrollListener()();
    probe.scrollListener()();
    expect(probe.pendingFrames()).toBe(1);
    cleanup();
  });

  expect(probe.cancelledFrames).toHaveLength(1);
  expect(probe.pendingFrames()).toBe(0);
  expect(probe.removedScrollListener()).toBe(probe.scrollListener());
});

test('Live Margin visibility fallback cannot overwrite live end of document selection', () => {
  const probe = createNaturalScrollProbe();
  const emitted = [];

  withWindow(probe.window, () => {
    const cleanup = observeLiveMarginChapters(probe.targets, (chapter) => emitted.push(chapter));
    probe.setScroll(171);
    probe.scrollListener()();
    probe.flushFrame();
    probe.observer().callback([{
      isIntersecting: true,
      target: probe.targets.get('proof'),
      boundingClientRect: {top: -28},
    }]);
    probe.flushFrame();
    cleanup();
  });

  expect(emitted.at(-1)).toBe('response');
});

test('Live Margin reverse scroll resets response to why now at the reading origin', () => {
  const probe = createNaturalScrollProbe({
    positions: {'why-now': 600, proof: 734, precedent: 1144, response: 1411},
    scrollHeight: 1612,
    viewportHeight: 844,
  });
  const emitted = [];

  withWindow(probe.window, () => {
    const cleanup = observeLiveMarginChapters(probe.targets, (chapter) => emitted.push(chapter));
    for (const scrollY of [768, 0]){
      probe.setScroll(scrollY);
      probe.scrollListener()();
      expect(probe.pendingFrames()).toBe(1);
      probe.flushFrame();
    }
    cleanup();
  });

  expect(emitted).toEqual(['response', 'why-now']);
  expect(emitted.map(chapterProgress)).toEqual([1, 0.25]);
});

test('Live Margin observer ignores queued deliveries after cleanup', () => {
  const proof = {};
  let activeChapter = '';
  const {cleanup, observer} = observeLiveMarginWithProbe(
    new Map([['proof', proof]]),
    (chapter) => { activeChapter = chapter; },
  );

  cleanup();
  observer.callback([{isIntersecting: true, target: proof, boundingClientRect: {top: 20}}]);
  expect(observer.disconnected).toBe(true);
  expect(activeChapter).toBe('');
});

test('Live Margin observer rejects IDs outside the chapter contract', () => {
  const retired = {};
  const proof = {};
  const emitted = [];
  const {observer} = observeLiveMarginWithProbe(
    new Map([['retired', retired], ['proof', proof]]),
    (chapter) => emitted.push(chapter),
  );

  expect(observer.observed).toEqual([proof]);
  observer.callback([{isIntersecting: true, target: retired, boundingClientRect: {top: 10}}]);
  expect(emitted).toEqual([]);
  observer.callback([{isIntersecting: true, target: proof, boundingClientRect: {top: 20}}]);
  expect(emitted).toEqual(['proof']);
});

test('Live Margin navigation rejects unknown or missing targets before scrolling', () => {
  const calls = [];
  const target = {scrollIntoView: (options) => calls.push(options)};

  expect(scrollToLiveMarginChapter('retired', target, false)).toBe(false);
  expect(scrollToLiveMarginChapter('proof', null, false)).toBe(false);
  expect(calls).toEqual([]);
  expect(scrollToLiveMarginChapter('proof', target, true)).toBe(true);
  expect(calls).toEqual([{behavior: 'auto', block: 'start'}]);
});

function elementText(node){
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(elementText).join('');
  return elementText(node.props && node.props.children);
}

function collectElements(node, type, found=[]){
  if (node === null || node === undefined || typeof node === 'boolean') return found;
  if (Array.isArray(node)){
    for (const child of node) collectElements(child, type, found);
    return found;
  }
  if (typeof node !== 'object') return found;
  if (node.type === type) found.push(node);
  collectElements(node.props && node.props.children, type, found);
  return found;
}

test('global shell renders the approved Batch 1 jobs and market scope only', () => {
  const markup = renderToStaticMarkup(
    <OgilvyShell
      route="compare"
      setRoute={() => {}}
      region="NG"
      setRegion={() => {}}
      freshness="Updated 8m ago"
    >
      <div>Current route</div>
    </OgilvyShell>,
  );

  expect(markup).toContain('42');
  expect(markup).toContain('Ogilvy Intelligence');
  for (const label of ['Briefing', 'Discover', 'Compare', 'Build', 'ZA', 'NG', 'KE', 'ALL']){
    expect(markup).toMatch(new RegExp(`<button[^>]*>${label}</button>`));
  }
  expect(markup).toMatch(/<button[^>]*aria-current="page"[^>]*>Compare<\/button>/);
  expect(markup).toMatch(/<button[^>]*aria-current="page"[^>]*>NG<\/button>/);
  expect(markup).toContain('Current route');
  for (const retired of ['Theme', 'Colour', 'Motion', 'Map', 'My Board']){
    expect(markup).not.toContain(retired);
  }
  expect(markup).not.toMatch(/<button[^>]*\stitle=/);
});

test('global shell actions dispatch every approved route and market', () => {
  const routes = [];
  const regions = [];
  const masthead = OgilvyMasthead({
    route: 'pulse',
    setRoute: (path) => routes.push(path),
    region: 'ZA',
    setRegion: (market) => regions.push(market),
    freshness: '',
  });
  const buttons = collectElements(masthead, 'button');

  expect(buttons.every((button) => typeof button.props.onClick === 'function')).toBe(true);
  for (const label of ['Briefing', 'Discover', 'Compare', 'Build']){
    buttons.find((button) => elementText(button) === label).props.onClick();
  }
  for (const label of ['ZA', 'NG', 'KE', 'ALL']){
    buttons.find((button) => elementText(button) === label).props.onClick();
  }

  expect(routes).toEqual(['/pulse', '/explore', '/compare', '/console']);
  expect(regions).toEqual(['ZA', 'NG', 'KE', 'ALL']);
});

test('global shell CSS fixes control size, focus and masthead structure', async () => {
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();

  const controls = css.match(/\.oi-shell__action\s*\{([^}]*)\}/);
  expect(controls).not.toBeNull();
  expect(controls[1]).toMatch(/min-height:\s*48px/);
  expect(controls[1]).toMatch(/min-width:\s*48px/);

  const focus = css.match(/\.oi-shell__action:focus-visible\s*\{([^}]*)\}/);
  expect(focus).not.toBeNull();
  expect(focus[1]).toMatch(/outline:\s*[^;]+/);

  const masthead = css.match(/\.oi-shell__masthead\s*\{([^}]*)\}/);
  expect(masthead).not.toBeNull();
  expect(masthead[1]).toMatch(/background:\s*var\(--oi-ink\)/);
  expect(masthead[1]).toMatch(/border-bottom:\s*1px solid/);
  expect(masthead[1]).not.toMatch(/box-shadow/);

  const active = css.match(/\.oi-shell__job\[aria-current="page"\]::after\s*\{([^}]*)\}/);
  expect(active).not.toBeNull();
  expect(active[1]).toMatch(/background:\s*var\(--oi-red\)/);
});

test('selected market focus uses an Ogilvy red ring distinct from its background', async () => {
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();
  const selected = css.match(/\.oi-shell__market\[aria-current="page"\]\s*\{([^}]*)\}/);
  const selectedFocus = css.match(/\.oi-shell__market\[aria-current="page"\]:focus-visible\s*\{([^}]*)\}/);

  expect(selected).not.toBeNull();
  expect(selected[1]).toMatch(/background:\s*var\(--oi-white\)/);
  expect(selectedFocus).not.toBeNull();
  expect(selectedFocus[1]).toMatch(/outline:\s*3px solid var\(--oi-red\)/);
});

test('mobile shell keeps the Ogilvy Intelligence owner visible', async () => {
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();
  const mobileIdentity = css.match(
    /@media\s*\(max-width:\s*560px\)\s*\{[\s\S]*?\.oi-shell__identity\s*\{([^}]*)\}/,
  );

  expect(mobileIdentity).not.toBeNull();
  expect(mobileIdentity[1]).toMatch(/font-size:\s*[^;]+/);
  expect(mobileIdentity[1]).not.toMatch(/display:\s*none|visibility:\s*hidden|clip(?:-path)?\s*:/);
});

test('brand action names its identity and Briefing destination exactly', () => {
  const markup = renderToStaticMarkup(
    <OgilvyMasthead
      route="pulse"
      setRoute={() => {}}
      region="ZA"
      setRegion={() => {}}
      freshness=""
    />,
  );

  expect(markup).toMatch(
    /<button[^>]*aria-label="42 Ogilvy Intelligence, open Briefing"[^>]*>/,
  );
});

test('passcode gate renders the Ogilvy Intelligence identity with accessible controls', () => {
  const markup = renderToStaticMarkup(
    <PasscodeScreen onSubmit={() => {}} failed />,
  );

  expect(markup).toContain('42');
  expect(markup).toContain('Ogilvy Intelligence');
  expect(markup).toContain('Open cultural intelligence');
  expect(markup).toContain('Open discovery');
  expect(markup).toContain('Evidence first');
  expect(markup).toContain('Audience neutral');
  expect(markup).not.toContain('Google');
  expect(markup).not.toContain('Signal desk');
  expect(markup).not.toContain('gate-v3-card');
  expect(markup).toContain('class="gate-v4-field"');
  expect(markup).toContain('class="gate-v4-access"');
  expect(markup).toMatch(/<label[^>]*for="gate-passcode"[^>]*>Access key<\/label>/);
  expect(markup).toContain('autoComplete="current-password"');
  expect(markup).toContain('aria-describedby="gate-status"');
  expect(markup).toContain('style="--gate-entry-progress:0"');
  expect(markup).toContain('role="alert"');
  expect(markup).toContain('aria-live="assertive"');
});

/* Design audit 2 October 2026: the gate is a tool, not a landing page. Words
   that look like controls but do nothing are a false affordance (Norman, The
   Design of Everyday Things), and numbered principles are a template idiom. */
test('passcode gate carries no inert mast or footer words and no numbered principles', () => {
  const markup = renderToStaticMarkup(<PasscodeScreen onSubmit={() => {}} failed={false} />);
  for (const word of ['ZA', 'NG', 'KE', 'Internal', 'Briefing', 'Discover', 'Compare', 'Build']) {
    expect(markup).not.toContain(`<span>${word}</span>`);
    expect(markup).not.toContain(`<strong>${word}</strong>`);
  }
  expect(markup).not.toContain('gate-v4-foot');
  expect(markup).not.toMatch(/<dt>0[1-3]<\/dt>/);
  expect(markup).toContain('<dt>Open discovery</dt>');
  expect(markup).toContain('<dt>Evidence first</dt>');
  expect(markup).toContain('<dt>Audience neutral</dt>');
  expect(markup).toMatch(/<h1 id="gate-title"[^>]*>Open cultural intelligence\.<\/h1>/);
  expect(markup).toMatch(/<h2 id="gate-access-title"/);
  expect(markup).toContain('<strong>42</strong><span>Ogilvy Intelligence</span>');
});

/* Design audit 2 October 2026: visual weight follows task priority, so the
   access heading is the one headline and the brand statement is a deck
   (NN/g, visual hierarchy). */
test('gate sets one sans page title headline and keeps serif to the 42 mark', async () => {
  const gateCss = await Bun.file(new URL('../../styles/gate.css', import.meta.url)).text();
  expect(parsedCssValue(gateCss, '.gate-v4-access h2', 'font')).toBe('600 var(--type-page-title, 32px)/var(--leading-page-title, 1.2) var(--oi-sans)');
  expect(parsedCssValue(gateCss, '.gate-v4-hero h1', 'font-family')).toBe('var(--oi-sans)');
  const deckSize = parsedCssValue(gateCss, '.gate-v4-hero h1', 'font-size');
  const deckBounds = deckSize.match(/^clamp\((\d+)px, [^,]+, (\d+)px\)$/);
  expect(deckBounds).not.toBeNull();
  expect(Number(deckBounds[1])).toBeGreaterThanOrEqual(20);
  expect(Number(deckBounds[2])).toBeLessThanOrEqual(24);
  const markSize = parsedCssValue(gateCss, '.gate-v4-number', 'font-size');
  expect(Math.max(...markSize.match(/\d+(?=px)/g).map(Number))).toBeLessThanOrEqual(96);
  /* The serif appears only on the 42 mark, in the hero and the lockup, and on
     the separate boot fallback screen. */
  const serifRules = [];
  postcss.parse(gateCss).walkDecls((decl) => {
    if (decl.value.includes('--oi-serif')) serifRules.push(decl.parent.selector);
  });
  expect(serifRules.sort()).toEqual(['.gate-v4-mast p:first-child strong', '.gate-v4-number', '.gate-v4-style-error h1'].sort());
  expect(gateCss).not.toContain('gate-v4-foot');
  expect(gateCss).not.toMatch(/box-shadow/);
});

test('App waits for the health probe before choosing an access gate', async () => {
  const {default: App} = await import('../../App.jsx');
  const priorWindow = globalThis.window;
  const priorStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const renderWith = (storage) => {
    globalThis.window = {
      location: {hash: ''},
      history: {replaceState: () => {}},
    };
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: storage,
    });
    return renderToStaticMarkup(<App />);
  };

  try {
    const missing = renderWith({
      getItem: () => null,
      setItem: () => {},
      removeItem: () => {},
    });
    const stored = renderWith({
      getItem: (key) => key === 'pulse_passcode' ? 'stored' : null,
      setItem: () => {},
      removeItem: () => {},
    });
    const denied = renderWith({
      getItem: () => { throw new Error('storage denied'); },
      setItem: () => { throw new Error('storage denied'); },
      removeItem: () => { throw new Error('storage denied'); },
    });

    expect(missing).not.toContain('class="gate-v4 oi-product"');
    expect(stored).not.toContain('class="gate-v4 oi-product"');
    expect(denied).not.toContain('class="gate-v4 oi-product"');
    expect(missing).toContain('data-state="loading"');
    expect(stored).toContain('data-state="loading"');
    expect(denied).toContain('data-state="loading"');
    expect(missing).not.toContain('state-view__reserved');
  } finally {
    globalThis.window = priorWindow;
    if (priorStorage) Object.defineProperty(globalThis, 'localStorage', priorStorage);
    else delete globalThis.localStorage;
  }
});

test('API requests fail closed to an empty passcode when storage is denied', async () => {
  const {apiGet, clearCache} = await import('../../api.js');
  const priorFetch = globalThis.fetch;
  const priorStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  let passcodeHeader = null;
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {getItem: () => { throw new Error('storage denied'); }},
  });
  globalThis.fetch = async (_path, options) => {
    passcodeHeader = options.headers['X-Passcode'];
    return {ok: true, status: 200, json: async () => ({ok: true})};
  };

  try {
    clearCache();
    await apiGet('/api/storage-denied-first-paint-test');
    expect(passcodeHeader).toBe('');
  } finally {
    clearCache();
    globalThis.fetch = priorFetch;
    if (priorStorage) Object.defineProperty(globalThis, 'localStorage', priorStorage);
    else delete globalThis.localStorage;
  }
});

function gatePairContrast(css, theme, foreground){
  const selector = `.oi-product[data-dir="${theme}"]`;
  const channel = (value) => {
    const srgb = value / 255;
    return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
  };
  const luminance = (property) => {
    const hex = parsedCssValue(css, selector, property);
    const values = hex.slice(1).match(/.{2}/g).map(value => channel(parseInt(value, 16)));
    return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2];
  };
  const ink = luminance(foreground);
  const canvas = luminance("--canvas");
  return (Math.max(ink, canvas) + 0.05) / (Math.min(ink, canvas) + 0.05);
}

/* Restated, design audit 2 October 2026: the Internal tag and its red marker
   were removed from the mast as a false affordance, so the test now holds the
   remaining lockup name to themed ink and checks the old rule is gone. Was:
   .gate-v4-mast p:last-child strong color var(--ink) with a 2px solid
   var(--oi-red) border-bottom. */
test('gate masthead lockup uses themed ink contrast with no inert Internal tag', async () => {
  const gateCss = await Bun.file(new URL('../../styles/gate.css', import.meta.url)).text();
  const hostCss = await Bun.file(new URL('../../app.css', import.meta.url)).text();
  expect(parsedCssValue(gateCss, '.gate-v4-mast p:first-child span', 'color')).toBe('var(--ink)');
  expect(gateCss).not.toContain('.gate-v4-mast p:last-child');
  for (const theme of ['midnight', 'daylight']) expect(gatePairContrast(hostCss, theme, '--ink')).toBeGreaterThanOrEqual(4.5);
});

test('production gate keeps its render-blocking CSS below 80 kilobytes', async () => {
  const frontendRoot = fileURLToPath(new URL('../../../', import.meta.url));
  const output = mkdtempSync(join(tmpdir(), 'lp-gate-build-'));
  try {
    await viteBuild({
      root: frontendRoot,
      logLevel: 'silent',
      build: {
        outDir: output,
        emptyOutDir: true,
      },
    });
    const index = readFileSync(join(output, 'index.html'), 'utf8');
    const linked = [...index.matchAll(/href="([^"]+\.css)"/g)]
      .map((match) => match[1].replace(/^\//, ''));
    const initialBytes = linked.reduce(
      (total, relativePath) => total + readFileSync(join(output, relativePath)).byteLength,
      0,
    );
    const initialCss = linked
      .map((relativePath) => readFileSync(join(output, relativePath), 'utf8'))
      .join('\n');
    const cssAssets = readdirSync(join(output, 'assets'))
      .filter((name) => name.endsWith('.css'));
    const entryPath = [...index.matchAll(/src="([^"]+\.js)"/g)]
      .map((match) => match[1].replace(/^\//, ''))[0];
    const entry = readFileSync(join(output, entryPath), 'utf8');

    expect(linked.length).toBeGreaterThan(0);
    expect(initialBytes).toBeLessThan(80_000);
    expect(cssAssets.length).toBeGreaterThan(linked.length);
    for (const selector of ['.gate-v4', '.page']) {
      expect(initialCss).toContain(selector);
    }
    expect(initialCss).toContain('.instrument-shell');
    expect(initialCss).toContain('.proof-ribbon');
    expect(initialCss).toContain('.dossier-lead');
    expect(initialCss).toContain('prefers-reduced-motion');
    expect(initialCss).not.toContain('.ld-rings');
    expect(entry).toMatch(/assets\/app-[^"'\]]+\.css/);
    expect(entry).toMatch(/assets\/empty-states-[^"'\]]+\.css/);
    expect(entry).toMatch(/assets\/dossier-[^"'\]]+\.css/);
  } finally {
    rmSync(output, {recursive: true, force: true});
  }
}, 60000);

test('deferred style scheduler falls back retries once and reports terminal failure', async () => {
  let scheduleDeferredStyles;
  try {
    ({scheduleDeferredStyles} = await import('../../deferredStyles.js'));
  } catch (_error) {}
  expect(typeof scheduleDeferredStyles).toBe('function');

  const callbacks = [];
  const marks = [];
  let attempts = 0;
  let failures = 0;
  let ready = 0;
  scheduleDeferredStyles({
    frame: undefined,
    timer: (callback) => { callbacks.push(callback); return callbacks.length; },
    load: async () => {
      attempts += 1;
      if (attempts < 2) throw new Error('transient');
    },
    onReady: () => { ready += 1; marks.push('applied'); },
    onFailure: () => { failures += 1; },
    mark: (name) => { marks.push(name); },
  });
  while (callbacks.length) await callbacks.shift()();
  expect(attempts).toBe(2);
  expect(ready).toBe(1);
  expect(failures).toBe(0);
  /* The ready mark is written once the ready callback has succeeded. */
  expect(marks).toEqual(['product-styles:loading', 'applied', 'product-styles:ready']);

  scheduleDeferredStyles({
    frame: (callback) => { callbacks.push(callback); return 1; },
    timer: (callback) => { callbacks.push(callback); return callbacks.length; },
    load: async () => { throw new Error('terminal'); },
    onFailure: () => { failures += 1; },
  });
  while (callbacks.length) await callbacks.shift()();
  expect(ready).toBe(1);
  expect(failures).toBe(1);
});

test('deferred style scheduler reports a missing critical package stylesheet before route styles load', async () => {
  const {scheduleDeferredStyles} = await import('../../deferredStyles.js');
  const callbacks = [];
  const failures = [];
  let loads = 0;

  scheduleDeferredStyles({
    frame: (callback) => { callbacks.push(callback); return 1; },
    timer: (callback) => { callbacks.push(callback); return callbacks.length; },
    criticalStylesReady: () => false,
    load: async () => { loads += 1; },
    onFailure: (error) => { failures.push(error); },
  });
  while (callbacks.length) await callbacks.shift()();

  expect(loads).toBe(0);
  expect(failures).toHaveLength(1);
  expect(failures[0].message).toBe('Critical package stylesheet unavailable');
});

test('deferred style scheduler reports a throwing ready callback without retrying the load', async () => {
  const {scheduleDeferredStyles} = await import('../../deferredStyles.js');
  const callbacks = [];
  const failures = [];
  const marks = [];
  const readyError = new Error('ready callback failed');
  let loads = 0;
  let readyCalls = 0;

  scheduleDeferredStyles({
    frame: (callback) => { callbacks.push(callback); return 1; },
    timer: (callback) => { callbacks.push(callback); return callbacks.length; },
    load: async () => { loads += 1; },
    onReady: () => { readyCalls += 1; throw readyError; },
    onFailure: (error) => { failures.push(error); },
    mark: (name) => { marks.push(name); },
  });
  while (callbacks.length) await callbacks.shift()();

  expect(loads).toBe(1);
  expect(readyCalls).toBe(1);
  expect(failures).toEqual([readyError]);
  /* A throwing ready callback leaves a failed mark on the timeline and no
     ready mark, so the marks agree with the attribute the failure sets. */
  expect(marks).toEqual(['product-styles:loading', 'product-styles:failed']);
});

test('gate and empty state styles have separate static ownership', async () => {
  const gateCss = await Bun.file(new URL('../../styles/gate.css', import.meta.url)).text();
  const emptyFile = Bun.file(new URL('../../styles/empty-states.css', import.meta.url));
  const main = await Bun.file(new URL('../../main.jsx', import.meta.url)).text();

  expect(gateCss).toContain('var(--oi-ink)');
  expect(gateCss).toContain('var(--oi-paper)');
  /* Restated, design audit 2 October 2026: var(--oi-red) only drew the marker
     under the removed Internal tag. The gate's red now lives on the filled
     Enter 42 action. Was: toContain('var(--oi-red)'). */
  expect(gateCss).toContain('background: var(--accent)');
  expect(gateCss).toContain('grid-template-columns: minmax(0, 2fr) minmax(320px, 1fr)');
  expect(gateCss).toContain('transform: scaleX(var(--gate-entry-progress))');
  expect(gateCss).toContain('@media (max-width: 760px)');
  expect(gateCss).not.toMatch(/\.(?:ld|es)-|\[data-motion|@keyframes|loader|gradient|backdrop-filter|box-shadow/i);
  expect(await emptyFile.exists()).toBe(true);

  const emptyCss = await emptyFile.text();
  for (const selector of ['.ld-rings', '.ld-drum', '.ld-orbit', '.ld-sweep', '.es-block', '.es-field']) {
    expect(emptyCss).toContain(selector);
  }
  expect(emptyCss).not.toMatch(
    /@keyframes|\banimation(?:-\w+)?\s*:|\binfinite\b|gradient|glow|blur/i,
  );
  /* The loaders stay still. The one transition in the sheet is the control
     move on the two empty state actions (round 4, task 26, P19), and the
     package's midnight ghost rule on the row move is outranked by the same
     rule, so both themes move at the control speed (round 5, task 27). */
  expect(emptyCss.match(/transition\s*:/g) || []).toHaveLength(1);
  const move = emptyCss.match(/\.es-act, \.es-act-ghost, html\[data-dir="midnight"\] \.es-act-ghost \{([^}]*)\}/);
  expect(move, 'one transition rule for both themes').not.toBeNull();
  expect(move[1]).toMatch(/transition: color var\(--motion-control\) var\(--motion-standard-easing\), border-color var\(--motion-control\) var\(--motion-standard-easing\), background-color var\(--motion-control\) var\(--motion-standard-easing\), box-shadow var\(--motion-control\) var\(--motion-standard-easing\);/);
  /* The rule carries the move and nothing else, so it restyles no theme. */
  expect(move[1].trim()).toMatch(/^transition: [^;]+;$/);
  expect(emptyCss).not.toMatch(/--motion-row/);

  expect(main).toContain("import('./app.css')");
  expect(main).toContain("import('./styles/empty-states.css')");
  expect(main).toContain("import('./styles/dossier.css')");
  expect(main).not.toContain("import('ogilvy-intelligence-design-system/style.css')");
  expect(main).not.toContain("import('./styles/ogilvy-intelligence.css')");
});

test('gate access action has one restrained state transition and no card effects', async () => {
  const css = await Bun.file(new URL('../../styles/gate.css', import.meta.url)).text();
  const reset = css.match(
    /\.gate-v4-submit,\s*\.gate-v4-submit:hover\s*\{([^}]*)\}/,
  );

  expect(reset).not.toBeNull();
  expect(reset[1]).toMatch(/transform:\s*none/);
  expect(css).not.toContain('gate-v3-card');
  expect(css).not.toMatch(/border-radius:\s*(?:12|18|26)px/);
});

/* Restated, design audit 2 October 2026: the red mono 01, 02, 03 folios were
   the numbered feature list idiom, so the dt is now the bold sans title in
   ink. Was: dt font 700 20px/1.4 var(--oi-mono), color var(--accent-text),
   contrast checked on --accent-text. */
test('gate system terms use accessible themed ink titles', async () => {
  const gateCss = await Bun.file(new URL('../../styles/gate.css', import.meta.url)).text();
  const hostCss = await Bun.file(new URL('../../app.css', import.meta.url)).text();
  expect(parsedCssValue(gateCss, '.gate-v4-system dt', 'font')).toBe('600 var(--type-3)/1.4 var(--oi-sans)');
  expect(parsedCssValue(gateCss, '.gate-v4', 'background')).toBe('var(--canvas)');
  expect(parsedCssValue(gateCss, '.gate-v4-system dt', 'color')).toBe('var(--ink)');
  for (const theme of ['midnight', 'daylight']) expect(gatePairContrast(hostCss, theme, '--ink')).toBeGreaterThanOrEqual(4.5);
});

test('Live Margin uses the installed Ogilvy display family and exact motion owners', async () => {
  const index = await Bun.file(new URL('../../../index.html', import.meta.url)).text();
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();
  const main = await Bun.file(new URL('../../main.jsx', import.meta.url)).text();

  const expected = {
    '--oi-ink': '#171412',
    '--oi-paper': '#f3efe7',
    '--oi-white': '#fffaf1',
    '--oi-red': '#d71920',
    '--oi-amber': '#aa6a00',
    '--oi-green': '#1f6a44',
    '--oi-rule': '#aaa198',
    '--oi-serif': '"Ogilvy Serif", OgilvySerif, Georgia, "Times New Roman", serif',
    '--oi-sans': '"Ogilvy Sans", OgilvySans, Arial, Helvetica, sans-serif',
    '--oi-mono': 'Consolas, "Courier New", monospace',
  };
  const declarations = [...css.matchAll(/(--oi-[\w-]+)\s*:\s*([^;]+);/g)]
    .map(([, name, value]) => [name, value.trim()]);
  const names = declarations.map(([name]) => name);

  expect(new Set(names).size).toBe(10);
  expect(declarations).toHaveLength(10);
  for (const [name, value] of Object.entries(expected)) {
    expect(declarations.filter(([actualName, actualValue]) => (
      actualName === name && actualValue === value
    ))).toHaveLength(1);
  }

  const typeClasses = [...css.matchAll(/\.(oi-[\w-]+)\s*\{/g)]
    .map(([, name]) => name);
  const expectedTypeClasses = [
    'oi-folio',
    'oi-display',
    'oi-heading',
    'oi-body',
    'oi-small',
    'oi-navigation',
    'oi-metadata',
    'oi-provenance',
  ];
  expect(typeClasses.filter((name) => expectedTypeClasses.includes(name)).sort()).toEqual(
    [...expectedTypeClasses].sort(),
  );
  expect(typeClasses.filter((name) => (
    css.match(new RegExp(`\\.${name}\\s*\\{([^}]*)\\}`))?.[1].includes('font-family')
  ))).toHaveLength(8);
  expect(parsedCssValue(css, '.oi-display', 'font-family')).toBe('var(--oi-serif)');
  expect(parsedCssValue(css, '.oi-heading', 'font-family')).toBe('var(--oi-serif)');

  const reducedMotion = css.match(
    /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{[\s\S]*?\n\s*\}\s*\n\}/,
  );
  expect(reducedMotion).not.toBeNull();
  const nonReducedCss = css.replace(reducedMotion[0], '');
  const nonReducedDurations = [...new Set(nonReducedCss.match(/\b\d+(?:\.\d+)?ms\b/g) ?? [])]
    .sort();
  /* The motion law: no duration is written in the sheet. */
  expect(nonReducedDurations).toEqual([]);
  const nonReducedRules = [...nonReducedCss.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .map(([, selectors, body]) => ({
      selectors: selectors.split(',').map((selector) => selector.trim()),
      body,
    }));
  const transitionOwners = nonReducedRules
    .filter(({body}) => /(?:^|;)\s*transition(?:-duration)?\s*:/.test(body))
    .flatMap(({selectors}) => selectors);
  expect([...new Set(transitionOwners)].sort()).toEqual([
    '.oi-briefing__chapter-nav button::after',
    '.oi-briefing__chapter-nav button::before',
    '.oi-briefing__chapter-nav::after',
    '.oi-briefing__receipt-link::after',
    '.oi-receipt',
    '.oi-receipt::before',
    '.oi-shell__action',
    '.oi-shell__market',
  ]);
  const expectedTransitions = {
    '.oi-briefing__chapter-nav::after': 'transform var(--motion-handoff) var(--motion-handoff-easing)',
    '.oi-briefing__chapter-nav button::before': 'transform var(--motion-control) var(--motion-standard-easing)',
    '.oi-briefing__chapter-nav button::after': 'transform var(--motion-control) var(--motion-standard-easing)',
    /* The receipt is a row: its plane steps up and its leading rule goes red
       on the row move. Nothing moves geometry (round 5, task 27). */
    '.oi-receipt': 'background-color var(--motion-row) var(--motion-standard-easing)',
    '.oi-receipt::before': 'background-color var(--motion-row) var(--motion-standard-easing)',
    /* The shell action and market chip answer the pointer with colour on the
       control move (round 5, task 27). */
    '.oi-shell__action': 'color var(--motion-control) var(--motion-standard-easing)',
    '.oi-shell__market': 'color var(--motion-control) var(--motion-standard-easing), background-color var(--motion-control) var(--motion-standard-easing)',
    /* The title and receipt underlines wipe from the leading edge on hover and
       focus alike: the same deliberate duration as the chapter title rule. */
    '.oi-briefing__receipt-link::after': 'transform var(--motion-control) var(--motion-standard-easing)',
  };
  for (const [selector, expectedTransition] of Object.entries(expectedTransitions)) {
    const ownerRules = nonReducedRules.filter(({selectors}) => selectors.includes(selector));
    expect(ownerRules).toHaveLength(1);
    const transitionDeclarations = [
      ...ownerRules[0].body.matchAll(/\b(transition(?:-duration)?)\s*:\s*([^;]+);/g),
    ].map(([, property, value]) => [property, value.trim()]);
    expect(transitionDeclarations).toEqual([['transition', expectedTransition]]);
  }
  expect(reducedMotion[0].match(/\b\d+(?:\.\d+)?ms\b/g) ?? []).toEqual([
    '0.001ms',
    '0.001ms',
  ]);

  const ruleBody = (selector) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`))?.[1] ?? '';
  };
  expect(ruleBody('.oi-briefing__chapter-nav::after')).toMatch(
    /transition:\s*transform var\(--motion-handoff\) var\(--motion-handoff-easing\)/,
  );
  expect(ruleBody('.oi-briefing__chapter-nav::after')).toMatch(
    /transform:\s*scaleY\(var\(--live-margin-progress,\s*0\.25\)\)/,
  );
  expect(reducedMotion[0]).toContain('transition-duration: 0.001ms !important');
  expect(reducedMotion[0]).toContain('animation-duration: 0.001ms !important');

  expect(css).not.toMatch(/@import\b/i);

  for (const value of [
    'fonts.googleapis.com',
    'fonts.gstatic.com',
    '@import',
    'Newsreader',
    'Hanken Grotesk',
    'IBM Plex Mono',
  ]) {
    expect(index).not.toContain(value);
  }

  const packageStyle = main.indexOf("import 'ogilvy-intelligence-design-system/style.critical.css';");
  const bootStyle = main.indexOf("import './styles/boot.css';");
  const app = main.indexOf("import App from './App.jsx';");
  expect(packageStyle).toBeGreaterThanOrEqual(0);
  expect(main).toContain("import('ogilvy-intelligence-design-system/style.deferred.css')");
  expect(main).not.toContain("import 'ogilvy-intelligence-design-system/style.css';");
  expect(bootStyle).toBeGreaterThan(packageStyle);
  expect(app).toBeGreaterThan(bootStyle);
  expect(main).not.toContain("import './tokens.css';");
  expect(main).not.toContain("import './styles/ogilvy-intelligence.css';");
  expect(main).not.toContain("import './ui/ui.css';");
  expect(main).not.toContain("import './app.css';");
  expect(main).not.toContain('fontSize');
  expect(main).not.toContain('font-size');
});

test('Live Margin keeps the lead sticky at 800 pixels and releases it at 799 pixels', async () => {
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();

  expect(parsedCssValue(css, '.oi-briefing__lead-margin', 'position', '(min-width: 800px)')).toBe('sticky');
  expect(parsedCssValue(css, '.oi-briefing__lead-margin', 'align-self', '(min-width: 800px)')).toBe('start');
  expect(parsedCssValue(css, '.oi-briefing__lead-margin', 'position', '(max-width: 799px)')).toBe('static');
  expect(parsedCssValue(css, '.oi-briefing__decisions', 'grid-template-columns')).toBe('1fr');
  expect(parsedCssValue(css, '.oi-product', 'scroll-behavior')).toBe('smooth');
});

test('Live Margin title, chapter, receipt and progress states preserve hover focus and touch parity', async () => {
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();
  const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selectorText, body]) => ({
    selectors: selectorText.split(',').map((selector) => selector.trim()),
    body,
  }));
  const bodyFor = (selector) => rules.find(({selectors}) => selectors.includes(selector))?.body ?? '';
  const sharedState = rules.find(({selectors}) => (
    selectors.includes('.oi-briefing__chapter-nav button:hover::after')
    && selectors.includes('.oi-briefing__chapter-nav button:focus-visible::after')
    && selectors.includes('.oi-briefing__chapter-nav button[data-active="true"]::after')
  ));
  const receiptState = rules.find(({selectors}) => (
    selectors.includes('.oi-receipt:hover') && selectors.includes('.oi-receipt:focus-within')
  ));

  expect(bodyFor('.oi-briefing__chapter-nav button')).toMatch(/min-height:\s*48px/);
  expect(bodyFor('.oi-briefing__chapter-nav button::after')).toMatch(/transform:\s*scaleX\(0\)/);
  expect(bodyFor('.oi-briefing__chapter-nav button::after')).toMatch(/background:\s*var\(--oi-red\)/);
  expect(sharedState?.body ?? '').toMatch(/transform:\s*scaleX\(1\)/);
  expect(bodyFor('.oi-briefing__chapter-nav button::before')).toMatch(/counter-increment:\s*live-margin/);
  expect(bodyFor('.oi-briefing__chapter-nav::after')).toMatch(/transform-origin:\s*top/);
  expect(receiptState?.body ?? '').toMatch(/background-color:\s*var\(--oi-white\)/);
  expect(receiptState?.body ?? '').not.toMatch(/transform/);
  expect(bodyFor('.oi-receipt')).not.toMatch(/transform/);
  expect(bodyFor('.oi-receipt')).toMatch(/transition:\s*background-color var\(--motion-row\) var\(--motion-standard-easing\)/);
  const receiptRuleState = rules.find(({selectors}) => (
    selectors.includes('.oi-receipt:hover::before') && selectors.includes('.oi-receipt:focus-within::before')
  ));
  expect(receiptRuleState?.body ?? '').toMatch(/background:\s*var\(--oi-red\)/);
  expect(css).not.toMatch(/\.oi-receipt[^{]*\{[^}]*translateX/);
});

test('Live Margin CSS excludes every rejected dashboard motion and depth effect', async () => {
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();

  expect(css).not.toMatch(
    /(?:linear|radial|conic)-gradient|@keyframes|\binfinite\b|\bglow\b|backdrop-filter|filter\s*:\s*blur|background-attachment\s*:\s*fixed|transform\s*:\s*translateY|transform\s*:\s*scale\(|animation\s*:|box-shadow\s*:\s*(?!none)/i,
  );
});

test('a legacy topic row is labelled curated rather than dynamic', () => {
  const model = buildBriefingModel({id: 'legacy_1', topic: 'Street football'});
  expect(model.identity).toEqual({kind: 'curated_category', label: 'Curated category'});
});

test('a dynamic signal requires identity, approved mode and evidence state', () => {
  const model = buildBriefingModel({
    signal_id: 'signal_1',
    discovery_mode: 'phrase',
    evidence_state: 'ready',
    label: 'Street football culture',
  });
  expect(model.identity).toEqual({kind: 'dynamic_signal', label: 'Dynamic signal'});
});

const dynamicIdentity = {
  signal_id: 'signal_1',
  discovery_mode: 'phrase',
  evidence_state: 'ready',
};

test.each([
  ['signal_id', {discovery_mode: 'phrase', evidence_state: 'ready'}],
  ['discovery_mode', {signal_id: 'signal_1', evidence_state: 'ready'}],
  ['evidence_state', {signal_id: 'signal_1', discovery_mode: 'phrase'}],
])('dynamic identity is withheld when %s is absent', (_field, topic) => {
  expect(buildBriefingModel(topic).identity).toEqual({
    kind: 'curated_category',
    label: 'Curated category',
  });
});

test.each([
  ['signal_id', {...dynamicIdentity, signal_id: ' '}],
  ['discovery_mode', {...dynamicIdentity, discovery_mode: 'manual'}],
  ['evidence_state', {...dynamicIdentity, evidence_state: 'complete'}],
])('dynamic identity is withheld when %s is invalid', (_field, topic) => {
  expect(buildBriefingModel(topic).identity).toEqual({
    kind: 'curated_category',
    label: 'Curated category',
  });
});

test.each([
  ['what_changed', {what_changed: 'Raw change'}],
  ['why', {why: 'Legacy explanation'}],
  ['brief.why_now', {brief: {why_now: 'Brief fallback'}}],
])('why now stays unavailable when only %s is present', (_field, extra) => {
  expect(buildBriefingModel({...dynamicIdentity, ...extra}).whyNow).toEqual({
    state: 'unavailable',
    text: 'No completed-run why-now interpretation is available.',
  });
});

test('why now accepts only explicit top-level or nested signal why_now', () => {
  expect(buildBriefingModel({
    ...dynamicIdentity,
    why_now: 'Explicit top-level interpretation',
  }).whyNow).toEqual({state: 'ready', text: 'Explicit top-level interpretation'});
  expect(buildBriefingModel({
    signal: {...dynamicIdentity, why_now: 'Explicit nested interpretation'},
  }).whyNow).toEqual({state: 'ready', text: 'Explicit nested interpretation'});
});

test('duplicate receipt IDs collapse while direct URLs are preserved', () => {
  const model = buildBriefingModel({
    signal_id: 'signal_1',
    discovery_mode: 'phrase',
    evidence_state: 'ready',
    label: 'Street football culture',
    receipts: [
      {id: 'receipt_1', url: 'https://example.com/first'},
      {id: 'receipt_1', url: 'https://example.com/duplicate'},
      {id: 'receipt_2', url: 'https://example.com/second'},
    ],
  });

  expect(model.proof.receipts).toHaveLength(2);
  expect(model.proof.receipts[0].url).toBe('https://example.com/first');
  expect(model.proof.receipts[1].url).toBe('https://example.com/second');
});

test('empty receipts force the proof and readiness state to unchecked', () => {
  const model = buildBriefingModel({
    signal_id: 'signal_1',
    discovery_mode: 'phrase',
    evidence_state: 'ready',
  });

  expect(model.proof).toEqual({state: 'unchecked', receipts: []});
  expect(model.readiness).toBe('unchecked');
});

test('contradictory evidence remains contradictory when receipts exist', () => {
  const model = buildBriefingModel({
    signal_id: 'signal_1',
    discovery_mode: 'phrase',
    evidence_state: 'contradictory',
    receipts: [{id: 'receipt_1', url: 'https://example.com/first'}],
  });

  expect(model.proof.state).toBe('contradictory');
  expect(model.readiness).toBe('contradictory');
});

test('missing precedent and response stay explicitly unavailable', () => {
  const model = buildBriefingModel({id: 'legacy_1', topic: 'Street football'});

  expect(model.precedent).toEqual({
    state: 'unavailable',
    text: 'No qualifying historical analogue is available.',
  });
  expect(model.response).toEqual({
    state: 'unavailable',
    text: 'No evidence-backed response is available.',
  });
});

test('each model receives fresh unavailable section objects', () => {
  const first = buildBriefingModel({id: 'legacy_1'});
  const second = buildBriefingModel({id: 'legacy_2'});

  for (const section of ['whyNow', 'precedent', 'response']){
    expect(first[section]).not.toBe(second[section]);
  }
  first.whyNow.text = 'mutated why now';
  first.precedent.text = 'mutated precedent';
  first.response.text = 'mutated response';
  expect(buildBriefingModel({id: 'legacy_3'})).toMatchObject({
    whyNow: {
      state: 'unavailable',
      text: 'No completed-run why-now interpretation is available.',
    },
    precedent: {
      state: 'unavailable',
      text: 'No qualifying historical analogue is available.',
    },
    response: {
      state: 'unavailable',
      text: 'No evidence-backed response is available.',
    },
  });
});

test('briefing queue excludes the lead and duplicate signal IDs', () => {
  const topics = [
    {id: 'lead', signal_id: 'signal_lead', label: 'Lead'},
    {id: 'other_1', signal_id: 'signal_other', label: 'Other one'},
    {id: 'other_2', signal_id: 'signal_other', label: 'Duplicate other'},
    {id: 'other_3', signal_id: 'signal_last', label: 'Other last'},
  ];

  const queue = buildBriefingQueue(topics, 'lead', 4);

  expect(queue.map((item) => item.title)).toEqual(['Other one', 'Other last']);
  expect(queue.every((item) => item.signalId !== 'signal_lead')).toBe(true);
});

test('empty topics return no-discovery state', () => {
  expect(buildBriefingState({topics: [], freshness: {age_hours: 1}})).toEqual({
    state: 'no_discovery',
  });
});

test('explicit API errors return error state without fallback content', () => {
  const error = {code: 'api_unavailable', message: 'Desk unavailable'};

  expect(buildBriefingState({topics: [{id: 'topic_1'}], error})).toEqual({
    state: 'error',
    error,
  });
});

test('loading is explicit and cannot present as a completed no-discovery run', () => {
  expect(buildBriefingState({topics: [], loading: true})).toEqual({state: 'loading'});
  expect(buildBriefingState({topics: [], loading: true, error: {code: 'api_unavailable'}})).toEqual({
    state: 'error',
    error: {code: 'api_unavailable'},
  });

  const markup = renderToStaticMarkup(<RedThreadBriefing topics={[]} loading />);
  expect(markup).toContain('data-briefing-state="loading"');
  expect(markup).toContain('Preparing the evidence window');
  expect(markup).not.toContain('No discovery');
});

test('Today preserves the engine decision order rather than re-ranking by reach', () => {
  const first = {...freshFixture(ROOT_SIGNAL), signal_id: 'signal_first', label: 'Engine first', reach: 1};
  const second = {...freshFixture(ROOT_SIGNAL), signal_id: 'signal_second', label: 'High reach second', reach: 999};
  const markup = renderToStaticMarkup(
    <TodayPage
      topics={[first, second]}
      freshness={{status: 'green'}}
    />,
  );
  expect(markup.indexOf('Engine first')).toBeLessThan(markup.indexOf('High reach second'));
});

// Today became TodayPage42 in task 1.15: it reads /api/today itself and owns its loading and error
// states (today42.test.jsx covers them), so App must mount it on the pulse route and never hold it
// behind the old desk read.
test('App routes Briefing to TodayPage42, which owns its own loading and API error state', async () => {
  const source = await Bun.file(new URL('../../App.jsx', import.meta.url)).text();
  expect(source).toContain("route === 'pulse' && (");
  const todayRoute = source.match(/route === 'pulse' && \([^]*?<TodayPage42\b([^>]*)>/);
  expect(todayRoute).not.toBeNull();
  expect(todayRoute[1]).toMatch(/(?:^|\s)region=\{region\}/);
  expect(todayRoute[1]).toMatch(/(?:^|\s)onAuth=\{onAuth\}/);
  expect(todayRoute[1]).toMatch(/(?:^|\s)onRegionChange=\{setRegion\}/);
  expect(source).not.toMatch(/route === 'pulse' && \([^]*?<TodayPage (?!42)/);
  // Never behind the desk: the line that opens the pulse route carries no desk condition, and the
  // desk error screen is kept off pulse.
  const pulseLine = source.split('\n').find((line) => line.includes("route === 'pulse' && ("));
  expect(pulseLine).not.toMatch(/desk\./);
  expect(source).toMatch(/desk\.state === 'error' && route !== 'pulse' && [^\n]*<DeskError/);
});

test('error state outranks empty topics', () => {
  const error = {code: 'api_unavailable', message: 'Desk unavailable'};

  expect(buildBriefingState({topics: [], freshness: {status: 'green'}, error})).toEqual({
    state: 'error',
    error,
  });
});

test('error state outranks stale freshness', () => {
  const error = {code: 'api_unavailable', message: 'Desk unavailable'};

  expect(buildBriefingState({
    topics: [{id: 'topic_1'}],
    freshness: {status: 'amber'},
    error,
  })).toEqual({state: 'error', error});
});

test('stale freshness outranks no discovery and retains its timestamp', () => {
  const checkedAt = '2026-08-27T06:30:00Z';

  expect(buildBriefingState({
    topics: [],
    freshness: {status: 'amber', stamp_utc: checkedAt},
  })).toEqual({state: 'stale', checkedAt});
});

test('no discovery outranks ready when a fresh run has no topics', () => {
  expect(buildBriefingState({topics: [], freshness: {status: 'green'}})).toEqual({
    state: 'no_discovery',
  });
});

test('a fresh run with topics returns ready state', () => {
  expect(buildBriefingState({
    topics: [{id: 'topic_1'}],
    freshness: {status: 'green'},
  })).toEqual({state: 'ready'});
});

/* The ingest stamp holds a run that carries nothing and dates the wait. It
   does not outrank a released run: the run's own status decides, and the shell
   keeps showing the stamp in the utility strip either way. */
test('an amber stamp holds an empty run and never outranks one that carries signals', () => {
  const checkedAt = '2026-08-27T06:30:00Z';
  const amber = {status: 'amber', age_hours: 1, stamp_utc: checkedAt};

  expect(buildBriefingState({topics: [], freshness: amber})).toEqual({state: 'stale', checkedAt});
  expect(buildBriefingState({topics: [{id: 'topic_1'}], freshness: amber})).toEqual({state: 'ready'});
});

test('backend green status remains ready regardless of frontend age arithmetic', () => {
  expect(buildBriefingState({
    topics: [{id: 'topic_1'}],
    freshness: {status: 'green', age_hours: 99},
  })).toEqual({state: 'ready'});
});

const completeLead = {
  id: 'lead',
  signal_id: 'signal_lead',
  discovery_mode: 'phrase',
  evidence_state: 'ready',
  signal_name: 'Street football owns the evening',
  window_label: '28 July to 26 August 2026',
  why_now: 'Local tournament clips moved from isolated posts into a shared format.',
  receipts: [
    ['TikTok', 'The block final', '@touchline', '18k views', '2d', 'https://example.com/block-final'],
    ['Instagram', 'Five a side after work', '@citypitch', '9k views', '3d', 'https://example.com/five-a-side'],
  ],
  precedent: {
    match: 'The format repeats the neighbourhood tournament cycle seen in 2024.',
    difference: 'This run crosses two platforms and centres evening play.',
  },
  possible_response: 'Brief one local pitch as a recurring cultural stage.',
};

const comparisonTopics = [
  completeLead,
  {
    id: 'queue_1',
    signal_id: 'signal_queue_1',
    discovery_mode: 'creator',
    evidence_state: 'thin',
    signal_name: 'Sunday cycling clubs',
    why_now: 'Club ride receipts are beginning to repeat.',
    receipts: [{id: 'cycle_1', url: 'https://example.com/cycling'}],
  },
  {
    id: 'queue_2',
    signal_id: 'signal_queue_1',
    discovery_mode: 'creator',
    evidence_state: 'thin',
    signal_name: 'Duplicate cycling row',
    receipts: [{id: 'cycle_2', url: 'https://example.com/duplicate-cycling'}],
  },
  {
    id: 'queue_3',
    topic: 'Neighbourhood food tables',
    receipts: [{id: 'food_1', url: 'https://example.com/food'}],
  },
];

function renderBriefing(props={}){
  return renderToStaticMarkup(
    <RedThreadBriefing
      topics={comparisonTopics}
      freshness={{status: 'green', stamp_utc: '2026-08-27T06:30:00Z'}}
      onOpen={() => {}}
      {...props}
    />,
  );
}

test('ready briefing renders the approved editorial order from the reviewed adapter', () => {
  const markup = renderBriefing();
  const orderedText = [
    'Dynamic signal',
    'Closed window',
    completeLead.window_label,
    completeLead.signal_name,
    'id="why-now"',
    completeLead.why_now,
    'id="proof"',
    'The block final',
    'Five a side after work',
    'id="precedent"',
    'Match',
    completeLead.precedent.match,
    'Difference',
    completeLead.precedent.difference,
    'id="response"',
    'Compare next',
    'Sunday cycling clubs',
    'Neighbourhood food tables',
  ];
  const positions = orderedText.map((text) => markup.indexOf(text));

  expect(positions.every((position) => position >= 0)).toBe(true);
  expect(positions).toEqual([...positions].sort((a, b) => a - b));
  /* The response text appears twice by design: the mobile cue inside the lead
     and the response chapter itself. Both placements are asserted. */
  const responseChapter = markup.slice(markup.indexOf('id="response"'));
  expect(responseChapter).toContain(completeLead.possible_response);
  expect(markup.indexOf(completeLead.possible_response)).toBeLessThan(markup.indexOf('id="why-now"'));
  expect(markup).toMatch(/<article[^>]*class="[^"]*oi-briefing/);
  expect(markup).toMatch(/<aside[^>]*aria-label="Compare next"/);
  expect(markup).toMatch(/<ol[^>]*class="[^"]*oi-briefing__receipts/);
  expect(markup.match(/<li/g)).toHaveLength(2);
  expect(markup).toContain('href="https://example.com/block-final"');
  expect(markup).toContain('href="https://example.com/five-a-side"');
  expect(markup).toContain('target="_blank"');
  expect(markup).toContain('rel="noreferrer"');
  expect(markup).not.toContain('Duplicate cycling row');
});

test('Live Margin renders the reviewed chapter structure without changing briefing evidence', () => {
  const markup = renderBriefing();

  expect(markup).toMatch(/<article[^>]*style="--live-margin-progress:0\.25"/);
  expect(markup).toMatch(/<nav[^>]*aria-label="Live Margin chapters"/);
  expect(markup).toMatch(/<button(?=[^>]*aria-current="step")(?=[^>]*data-active="true")(?=[^>]*data-live-margin-chapter="why-now")[^>]*>Why now<\/button>/);
  for (const [chapterId, label] of [
    ['why-now', 'Why now'],
    ['proof', 'Direct receipts'],
    ['precedent', 'Precedent'],
    ['response', 'Possible response'],
  ]) {
    expect(markup).toMatch(new RegExp(`<section[^>]*id="${chapterId}"[^>]*data-live-margin-chapter="${chapterId}"`));
    expect(markup).toMatch(new RegExp(`<button[^>]*data-live-margin-chapter="${chapterId}"[^>]*>${label}</button>`));
  }
  expect(markup).toContain('href="https://example.com/block-final"');
  expect(markup).toContain('href="https://example.com/five-a-side"');
  expect(markup.indexOf('The block final')).toBeLessThan(markup.indexOf('Five a side after work'));
  expect(markup).toContain('rel="noreferrer"');
  expect(markup).not.toContain('Duplicate cycling row');
});

test('rendered precedent and response chapters occupy sequential decision rows', async () => {
  const markup = renderBriefing();
  const decisionStart = markup.indexOf('<div class="oi-briefing__decisions');
  const decisionPath = markup.slice(decisionStart, markup.indexOf('<aside', decisionStart));
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();

  expect(decisionStart).toBeGreaterThan(-1);
  expect(decisionPath.match(/id="precedent"/g) ?? []).toHaveLength(1);
  expect(decisionPath.match(/id="response"/g) ?? []).toHaveLength(1);
  expect(decisionPath.indexOf('id="precedent"')).toBeLessThan(decisionPath.indexOf('id="response"'));
  expect(parsedCssValue(css, '.oi-briefing__decisions', 'grid-template-columns')).toBe('1fr');
});

test('curated input is labelled Curated category and never promoted to dynamic', () => {
  const curated = {
    ...completeLead,
    id: 'curated',
    signal_id: undefined,
    discovery_mode: undefined,
    evidence_state: undefined,
  };
  const markup = renderBriefing({topics: [curated]});

  expect(markup).toContain('Curated category');
  expect(markup).not.toContain('Dynamic signal');
});

test.each([
  ['ready', 'Ready'],
  ['thin', 'Thin evidence'],
  ['contradictory', 'Contradictory evidence'],
])('%s evidence state remains explicit in the rendered briefing', (evidenceState, label) => {
  const topic = {...completeLead, evidence_state: evidenceState};
  const markup = renderBriefing({topics: [topic]});

  expect(markup).toContain(`data-readiness="${evidenceState}"`);
  expect(markup).toContain(label);
});

test('unchecked and no receipt states render without invented proof', () => {
  const topic = {...completeLead, receipts: []};
  const markup = renderBriefing({topics: [topic]});

  expect(markup).toContain('data-readiness="unchecked"');
  expect(markup).toContain('Unchecked');
  expect(markup).toContain('No direct receipts are available.');
  expect(markup).not.toMatch(/<ol[^>]*oi-briefing__receipts/);
});

test('missing precedent stays explicitly unavailable in the decision path', () => {
  const topic = {...completeLead, precedent: undefined};
  const markup = renderBriefing({topics: [topic]});

  expect(markup).toContain('No qualifying historical analogue is available.');
  expect(markup).not.toContain(completeLead.precedent.match);
});

test('missing evidence backed response stays explicitly unavailable', () => {
  const topic = {...completeLead, possible_response: undefined};
  const markup = renderBriefing({topics: [topic]});

  expect(markup).toContain('No evidence-backed response is available.');
  expect(markup).not.toContain(completeLead.possible_response);
});

test('an amber stamp holds an empty run and never hides an observed lead', () => {
  const amber = {status: 'amber', stamp_utc: '2026-08-27T06:30:00Z'};
  const held = renderBriefing({topics: [], freshness: amber});

  expect(held).toContain('data-briefing-state="stale"');
  expect(held).toContain('2026-08-27T06:30:00Z');
  expect(held).not.toContain(completeLead.signal_name);

  const observed = renderBriefing({freshness: amber});

  expect(observed).toContain('data-briefing-state="ready"');
  expect(observed).toContain(completeLead.signal_name);
});

test('API error renders the supplied error and no fabricated briefing content', () => {
  const markup = renderBriefing({
    error: {code: 'api_unavailable', message: 'Desk unavailable'},
  });

  expect(markup).toContain('role="alert"');
  expect(markup).toContain('Desk unavailable');
  expect(markup).not.toContain(completeLead.signal_name);
  expect(markup).not.toContain('Why now');
  expect(markup).not.toContain('Compare next');
});

test('empty completed run renders no discovery without fallback claims', () => {
  const markup = renderBriefing({topics: []});

  expect(markup).toContain('data-briefing-state="no_discovery"');
  expect(markup).toContain('No signals were discovered in the completed run.');
  expect(markup).not.toContain('Why now');
  expect(markup).not.toContain('Possible response');
  expect(markup).not.toMatch(/<article/);
});

test('Today begins with one Red Thread briefing and no retired dashboard surfaces', () => {
  const lead = freshFixture(ROOT_SIGNAL);
  const second = {...freshFixture(ROOT_SIGNAL), signal_id: 'signal_second', label: 'Sunday cycling clubs'};
  const markup = renderToStaticMarkup(
    <TodayPage
      topics={[lead, second]}
      freshness={{status: 'green', stamp_utc: '2026-08-27T06:30:00Z'}}
      onOpen={() => {}}
      onAsk={() => {}}
      metrics={{total_topics: 41}}
      digest={{}}
    />,
  );

  expect(markup.match(/data-instrument-briefing/g)).toHaveLength(1);
  expect(markup.match(/data-briefing-block="compare-next"/g)).toHaveLength(1);
  expect(markup.match(new RegExp(`<h1[^>]*>${lead.label}<\\/h1>`, 'g'))).toHaveLength(1);
  expect(markup).toContain('Sunday cycling clubs');
  expect(markup).not.toMatch(/<form|<input/);
  for (const retired of ['Try', 'Rising signals', 'Weakening signals', 'Emerging this week', 'The board', 'Method summary']){
    expect(markup).not.toContain(retired);
  }
  expect(markup).not.toMatch(/<nav[^>]*(?:sticky|fixed|bottom)/i);
});

test('mobile briefing layout fixes the folio, proof order and stacked decisions', async () => {
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();
  const marker = '@media (max-width: 560px)';
  const blocks = [];
  let cursor = 0;
  while ((cursor = css.indexOf(marker, cursor)) !== -1){
    const open = css.indexOf('{', cursor + marker.length);
    let depth = 1;
    let close = open + 1;
    while (depth && close < css.length){
      if (css[close] === '{') depth += 1;
      if (css[close] === '}') depth -= 1;
      close += 1;
    }
    blocks.push(css.slice(open + 1, close - 1));
    cursor = close;
  }
  const mobile = blocks.join('\n');

  expect(blocks.length).toBeGreaterThan(0);
  expect(mobile).toMatch(/\.oi-briefing\s*\{[\s\S]*grid-template-columns:\s*58px minmax\(0,\s*1fr\)/);
  expect(mobile).toMatch(/grid-template-areas:\s*"folio lead"\s*"folio proof"\s*"folio decision"\s*"folio queue"/);
  expect(mobile).toMatch(/\.oi-briefing__decisions[\s\S]*grid-template-columns:\s*1fr/);
  expect(mobile).not.toMatch(/position:\s*(?:fixed|sticky)/);
});

test('mobile briefing surfaces the recommendation cue inside the first 720 pixels', async () => {
  const markup = renderBriefing();
  expect(markup).toContain('oi-briefing__response-cue');
  const cueAt = markup.indexOf('oi-briefing__response-cue');
  const whyNowAt = markup.indexOf('id="why-now"');
  expect(cueAt).toBeGreaterThan(-1);
  expect(cueAt).toBeLessThan(whyNowAt);
  expect(markup.slice(cueAt, cueAt + 400)).toContain(completeLead.possible_response);

  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();
  expect(css).toMatch(/\.oi-briefing__response-cue\s*\{[^}]*display:\s*none/);
  const mobileBlock = css.slice(css.indexOf('@media (max-width: 560px)'));
  expect(mobileBlock).toMatch(/\.oi-briefing__response-cue\s*\{[^}]*display:\s*block/);
});

test('the recommendation cue never renders when no evidence-backed response exists', () => {
  const bare = comparisonTopics.map((topic, index) => index === 0
    ? {...topic, possible_response: undefined, brief: undefined, response: undefined, recommendation: undefined, action: undefined}
    : topic);
  const markup = renderBriefing({topics: bare});
  expect(markup).not.toContain('oi-briefing__response-cue');
});

test('stale empty run renders only its checked state without constructing a lead', () => {
  const checkedAt = '2026-08-27T06:30:00Z';
  const markup = renderBriefing({
    topics: [],
    freshness: {status: 'amber', stamp_utc: checkedAt},
  });

  expect(markup).toContain('data-briefing-state="stale"');
  expect(markup).toContain('Evidence window expired');
  expect(markup).toContain('Briefing held');
  expect(markup).toContain('42 is withholding a recommendation until a fresh evidence window closes.');
  expect(markup).toContain('Last checked 27 Aug 2026 at 06:30 UTC.');
  expect(markup).toContain('No recommendation, proof state, or possible response is inferred from stale data.');
  expect(markup).toContain(checkedAt);
  expect(markup).not.toMatch(/<article/);
  for (const fabricated of [
    completeLead.signal_name,
    'Curated category',
    'Untitled signal',
    'Not supplied',
    'Why now',
    'Direct receipts',
  ]){
    expect(markup).not.toContain(fabricated);
  }
});

/* Round 5, task 27. The Briefing state frame is the package frame alone: no
   host card around it and no stale composition of its own, since the route
   never carried the modifier. */
test('the Briefing state wrapper draws no card and keeps no stale composition', async () => {
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();
  expect(css).not.toMatch(/\.oi-briefing-state\s*\{/);
  expect(css).not.toMatch(/\.oi-briefing-state--stale/);
  expect(css).not.toMatch(/\.oi-briefing-state__mark/);
  const today = await Bun.file(new URL('../../today.jsx', import.meta.url)).text();
  expect(today).not.toMatch(/oi-briefing-state/);
  expect(today).not.toMatch(/role=\{alert/);
});

test('global utility navigation styles load before any lazy workspace chunk', async () => {
  const productCss = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();
  const workspaceCss = await Bun.file(new URL('../../styles/workspaces.css', import.meta.url)).text();
  expect(productCss).toMatch(/\.oi-shell__utility\s*\{[^}]*display:\s*flex/s);
  expect(productCss).toMatch(/\.oi-shell__utility a\s*\{[^}]*min-height:\s*48px/s);
  expect(workspaceCss).not.toContain('.oi-shell__utility');
});

test('completed run date metadata does not invent a closed evidence window', () => {
  const topic = {...completeLead, window_label: undefined};
  const markup = renderToStaticMarkup(
    <TodayPage
      topics={[topic]}
      deskDate="2026-08-27"
      freshness={{status: 'green'}}
      onOpen={() => {}}
    />,
  );

  expect(markup).toContain('data-run-date="2026-08-27"');
  expect(markup).toContain('data-briefing-state="unavailable"');
  expect(markup).not.toContain('data-briefing-state="unchecked"');
  expect(markup).not.toContain('2026-08-27 to');
});

test('receipt links allow only absolute http and https while preserving valid URLs', () => {
  const urls = {
    http: 'http://example.com/path?source=direct#proof',
    https: 'https://example.com/%7Esource?x=1',
    javascript: 'javascript:alert(1)',
    data: 'data:text/html,unsafe',
    ftp: 'ftp://example.com/file',
    relative: '/relative/path',
    malformed: 'not a url',
  };
  const topic = {
    ...completeLead,
    receipts: Object.entries(urls).map(([label, url]) => ({
      id: label,
      title: `${label} receipt`,
      url,
    })),
  };
  const markup = renderBriefing({topics: [topic]});

  expect(markup).toContain(`href="${urls.http}"`);
  expect(markup).toContain(`href="${urls.https}"`);
  expect(markup.match(/<a /g)).toHaveLength(2);
  for (const label of Object.keys(urls)) expect(markup).toContain(`${label} receipt`);
  for (const unsafe of [urls.javascript, urls.data, urls.ftp, urls.relative, urls.malformed]){
    expect(markup).not.toContain(`href="${unsafe}"`);
  }
});

test('receipt anchors preserve their destination and expose a 48 pixel keyboard target', async () => {
  const markup = renderBriefing();
  expect(markup).toMatch(
    /<a class="oi-briefing__receipt-link" href="https:\/\/example\.com\/block-final" target="_blank" rel="noreferrer">The block final<\/a>/,
  );

  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();
  const link = parsedCssRule(css, '.oi-briefing__receipt-link');
  const focus = css.match(/\.oi-briefing__receipt-link:focus-visible\s*\{([^}]*)\}/);

  expect(parsedCssValue(css, '.oi-briefing__receipt-link', 'display')).toBe('inline-flex');
  expect(parsedCssValue(css, '.oi-briefing__receipt-link', 'align-items')).toBe('center');
  expect(parsedCssValue(css, '.oi-briefing__receipt-link', 'min-width')).toBe('48px');
  expect(parsedCssValue(css, '.oi-briefing__receipt-link', 'min-height')).toBe('48px');
  expect(link.nodes.filter((node) => node.type === 'decl').map((node) => node.prop)).not.toContainAnyValues([
    'font-size', 'border-radius', 'box-shadow', 'transition',
  ]);
  expect(focus).not.toBeNull();
  expect(focus[1]).toMatch(/outline:\s*3px solid var\(--oi-red\)/);
  expect(focus[1]).toMatch(/outline-offset:\s*var\(--focus-offset\)/);
  expect(focus[1]).not.toMatch(/font-size|border-radius|box-shadow|transition/);
});

test('mobile folio is a 58 pixel border box inside the 58 pixel track', async () => {
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();
  const mobileRule = css.match(
    /@media\s*\(max-width:\s*560px\)[\s\S]*?\.oi-briefing__folio\s*\{([^}]*)\}/,
  );

  expect(mobileRule).not.toBeNull();
  expect(mobileRule[1]).toMatch(/width:\s*58px/);
  expect(mobileRule[1]).toMatch(/box-sizing:\s*border-box/);
});

test('receipt without a supplied label renders an explicit unavailable label', () => {
  const topic = {
    ...completeLead,
    receipts: [{url: 'https://example.com/unlabelled'}],
  };
  const markup = renderBriefing({topics: [topic]});

  expect(markup).toContain('Receipt label unavailable');
  expect(markup).not.toContain('Untitled receipt');
  expect(markup).toContain('href="https://example.com/unlabelled"');
});

test('queue renders noninteractive comparison rows when onOpen is absent', () => {
  const markup = renderBriefing({onOpen: undefined});
  const queue = markup.slice(markup.indexOf('<aside'));

  expect(queue).toContain('Compare next');
  expect(queue).toContain('dossier-comparison');
  expect(queue).toContain('Sunday cycling clubs');
  expect(queue).not.toMatch(/<button/);
});

test('queue items use the comparison strip grammar with movement and receipt proof', () => {
  const movementTopics = comparisonTopics.map((topic) => (
    topic.id === 'queue_1' ? {...topic, momentum: 'rising'} : topic
  ));
  const markup = renderBriefing({topics: movementTopics});
  const queue = markup.slice(markup.indexOf('<aside'));

  expect(queue).toContain('dossier-comparison');
  expect(queue).not.toContain('oi-briefing__queue-row');
  expect(queue).toContain('Movement: rising');
  expect(queue).toContain('Club ride receipts are beginning to repeat.');
  expect(queue).toContain('1 direct receipt');
  expect(queue).toContain('Thin');
  expect(queue).toContain('Inspect evidence');
});

const fixtureStates = ['ready', 'thin', 'contradictory', 'unchecked', 'stale', 'error', 'no-discovery'];

function renderFixture(directory, {mutation, state}={}){
  const output = join(directory, 'fixtures.html');
  const args = [
    fileURLToPath(new URL('../../../../scripts/test_ogilvy_intelligence_ui.mjs', import.meta.url)),
    '--render',
    output,
  ];
  if (state) args.push('--state', state);
  if (mutation) args.push('--fixture-mutation', mutation);
  const result = spawnSync('node', args, {encoding: 'utf8'});
  return {
    html: result.status === 0 ? readFileSync(output, 'utf8') : '',
    output,
    result,
  };
}

function preflightFixture(output){
  return spawnSync('node', [
    fileURLToPath(new URL('../../../../scripts/test_ogilvy_intelligence_ui.mjs', import.meta.url)),
    '--preflight',
    output,
  ], {encoding: 'utf8'});
}

function fixtureIds(html){
  return [...html.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1]);
}

function fixtureModuleInputs(html){
  const match = html.match(/<script id="oi-fixture-module-inputs" type="application\/json">([^<]+)<\/script>/);
  expect(match).not.toBeNull();
  return JSON.parse(match[1]);
}

function replaceFixtureManifest(html, mutate){
  const pattern = /(<script id="oi-fixture-module-inputs" type="application\/json">)([^<]+)(<\/script>)/;
  const match = html.match(pattern);
  expect(match).not.toBeNull();
  const manifest = JSON.parse(match[2]);
  mutate(manifest);
  return html.replace(pattern, `$1${JSON.stringify(manifest)}$3`);
}

function staleFixtureSourceHash(html, productInput){
  return replaceFixtureManifest(html, (manifest) => {
    manifest.productSourceHashes ??= {browser: {}, server: {}};
    for (const bundle of ['browser', 'server']){
      manifest.productSourceHashes[bundle] ??= {};
      if (manifest[bundle].includes(productInput)){
        manifest.productSourceHashes[bundle][productInput] = '0'.repeat(64);
      }
    }
  });
}

function mutateFixtureClientBundle(html){
  const manifestEnd = html.indexOf('</script>', html.indexOf('id="oi-fixture-module-inputs"'));
  const scriptStart = html.indexOf('<script', manifestEnd + 9);
  const contentStart = html.indexOf('>', scriptStart) + 1;
  expect(manifestEnd).toBeGreaterThan(-1);
  expect(scriptStart).toBeGreaterThan(-1);
  return `${html.slice(0, contentStart)}void 0;${html.slice(contentStart)}`;
}

test('fixture renderer rejects static SSR markers and copied component mutations', () => {
  const directory = mkdtempSync(join(tmpdir(), 'oi-task-4-mutations-'));
  try {
    for (const mutation of ['static-ssr', 'copied-components']){
      const {result} = renderFixture(directory, {mutation});
      expect(result.status).not.toBe(0);
      expect(`${result.stderr}\n${result.stdout}`).toMatch(/fixture bundle provenance/i);
    }
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});

test('fixture renderer manifests both actual product modules in both bundles', () => {
  const directory = mkdtempSync(join(tmpdir(), 'oi-task-4-provenance-'));
  try {
    const {html, result} = renderFixture(directory);
    if (result.status !== 0) throw new Error(result.stderr || result.stdout);
    expect(result.status).toBe(0);
    const inputs = fixtureModuleInputs(html);
    const productInputs = [
      'frontend/src/ui/OgilvyShell.jsx',
      'frontend/src/ui/RedThreadBriefing.jsx',
    ];
    for (const bundle of ['server', 'browser']){
      for (const productInput of productInputs) expect(inputs[bundle]).toContain(productInput);
      expect(Object.keys(inputs.productSourceHashes?.[bundle] ?? {}).sort()).toEqual(
        inputs[bundle].filter((input) => (
          input.startsWith('frontend/src/') && /\.(?:js|jsx)$/.test(input)
        )).sort(),
      );
    }
    expect(inputs.browser.some((input) => input.includes('react-dom/client'))).toBe(true);
    expect(inputs.clientBundleSha256).toMatch(/^[a-f0-9]{64}$/);
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});

test('fixture preflight rejects stale JavaScript and JSX product sources', () => {
  const directory = mkdtempSync(join(tmpdir(), 'oi-final-source-preflight-'));
  try {
    const {html, output, result} = renderFixture(directory);
    if (result.status !== 0) throw new Error(result.stderr || result.stdout);
    for (const productInput of [
      'frontend/src/ui/useLiveMargin.js',
      'frontend/src/ui/RedThreadBriefing.jsx',
    ]){
      writeFileSync(output, staleFixtureSourceHash(html, productInput), 'utf8');
      const preflight = preflightFixture(output);
      expect(preflight.status).not.toBe(0);
      expect(`${preflight.stderr}\n${preflight.stdout}`).toMatch(
        /Fixture product source hash mismatch/,
      );
    }
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});

test('fixture preflight rejects client bundle content drift', () => {
  const directory = mkdtempSync(join(tmpdir(), 'oi-final-bundle-preflight-'));
  try {
    const {html, output, result} = renderFixture(directory);
    if (result.status !== 0) throw new Error(result.stderr || result.stdout);
    writeFileSync(output, mutateFixtureClientBundle(html), 'utf8');
    const preflight = preflightFixture(output);
    expect(preflight.status).not.toBe(0);
    expect(`${preflight.stderr}\n${preflight.stdout}`).toMatch(/Fixture client bundle.*mismatch/);
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});

test('fixture preflight rejects stale product CSS and accepts regenerated output', () => {
  const directory = mkdtempSync(join(tmpdir(), 'oi-task-5-preflight-'));
  try {
    const {html, output, result} = renderFixture(directory);
    if (result.status !== 0) throw new Error(result.stderr || result.stdout);
    const stale = html.replace(
      '.oi-briefing__decisions {\n  display: grid;\n  grid-area: decision;\n  grid-template-columns: 1fr;',
      '.oi-briefing__decisions {\n  display: grid;\n  grid-area: decision;\n  grid-template-columns: repeat(2, minmax(0, 1fr));',
    );
    expect(stale).not.toBe(html);
    writeFileSync(output, stale, 'utf8');

    const stalePreflight = preflightFixture(output);
    expect(stalePreflight.status).not.toBe(0);
    expect(`${stalePreflight.stderr}\n${stalePreflight.stdout}`).toMatch(
      /fixture product CSS.*mismatch/i,
    );

    const regenerated = renderFixture(directory);
    if (regenerated.result.status !== 0){
      throw new Error(regenerated.result.stderr || regenerated.result.stdout);
    }
    const freshPreflight = preflightFixture(regenerated.output);
    expect(freshPreflight.status).toBe(0);
    expect(freshPreflight.stdout).toMatch(/Fixture product preflight passed/);
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});

test('fixture renderer mounts one selected state with unique IDs in every mode', () => {
  const directory = mkdtempSync(join(tmpdir(), 'oi-task-4-states-'));
  try {
    for (const state of fixtureStates){
      const {html, result} = renderFixture(directory, {state});
      if (result.status !== 0) throw new Error(result.stderr || result.stdout);
      expect(result.status).toBe(0);
      expect(html.match(/data-fixture-state="/g) ?? []).toHaveLength(1);
      expect(html).toContain(`data-fixture-state="${state}"`);
      const ids = fixtureIds(html);
      expect(new Set(ids).size).toBe(ids.length);
    }

    const {html} = renderFixture(directory);
    expect(html).toMatch(/^<!doctype html>/i);
    expect(html).toContain('id="oi-fixture-root"');
    expect(html).toContain('data-fixture-client="pending"');
    expect(html).toContain('data-fixture-state="ready"');
    expect(html).toContain('class="oi-product oi-shell"');
    expect(html).toContain('aria-label="42 Ogilvy Intelligence, open Briefing"');
    expect(html).toContain('aria-label="Primary jobs"');
    expect(html).toContain('class="oi-briefing"');
    for (const chapter of ['why-now', 'proof', 'precedent', 'response']){
      expect(html).toContain(`id="${chapter}"`);
      expect(html).toContain(`data-live-margin-chapter="${chapter}"`);
    }
    expect(html).toContain('>Q</a>');
    expect(html).toContain('.oi-shell__masthead');
    expect(html).toContain('.es-block');
    expect(html).toContain('.gate-v4');
    expect(html).toContain('</script>');
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}, 30000);  // The renderer spawns node, builds two esbuild bundles and runs React SSR. 0.3s warm on this desk, 5007ms on a cold CI runner, so bun's 5000ms default fails it there and nowhere else. Timeout only; no assertion changes.


test('premium underlines wipe from the leading edge and answer focus as well as hover', async () => {
  const css = await Bun.file(new URL('../../styles/ogilvy-intelligence.css', import.meta.url)).text();

  const exploreCss = await Bun.file(new URL('../../ogilvy-intelligence.css', import.meta.url)).text();
  for (const [sheet, owner] of [[css, '.oi-briefing__receipt-link'], [exploreCss, '.explore-index__select']]){
    expect(sheet).toContain(owner + '::after');
    expect(sheet).toContain(owner + ':is(:hover, :focus-visible)::after');
  }

  /* Scale belongs to the wipe, never to the element: nothing grows under a
     cursor, and the reveal starts from the leading edge. */
  const wipes = [...(css + exploreCss).matchAll(/::after\s*\{[^}]*transform:\s*scaleX\(0\)[^}]*\}/g)];
  expect(wipes.length).toBeGreaterThanOrEqual(2);
  expect(exploreCss).toMatch(/prefers-reduced-motion[\s\S]*explore-index__select::after[^}]*transition-duration:\s*0\.001ms/);
  expect(css).not.toMatch(/:hover[^{]*\{[^}]*transform:\s*scale\(/);
  expect(css).not.toMatch(/:hover[^{]*\{[^}]*translateY\(-/);
});

/* Only routes that can show desk freshness reserve its second row. Measure
   pending and widest-stamp geometry together, and keep routes without freshness
   at their natural height. The stamp comes from the consumer's own formatter. */
test('freshness routes reserve their loading height without adding a blank row elsewhere', async () => {
  const chrome = [
    process.env.CHROME_PATH,
    String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
    String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
    '/usr/bin/google-chrome',
  ].find((candidate) => candidate && existsSync(candidate));
  expect(chrome).toBeTruthy();

  let widest = '';
  for (let ageHours = 0; ageHours <= 24 * 400; ageHours += 0.25){
    const label = updatedLabel({age_hours: ageHours}).replace(/^Updated\s+/, '');
    if (label.length > widest.length) widest = label;
  }
  expect(widest.length).toBeGreaterThan(0);

  const packageCss = readFileSync(join(
    fileURLToPath(new URL('../../../', import.meta.url)),
    'node_modules', 'ogilvy-intelligence-design-system', 'dist', 'style.css',
  ), 'utf8');
  const {critical} = splitInstrumentStylesheet(packageCss);
  const bootCss = readFileSync(new URL('../../styles/boot.css', import.meta.url), 'utf8');
  const shell = renderToStaticMarkup(<>
    {[
      {name: 'reserved-ready', reserve: true, checkedAt: widest},
      {name: 'reserved-pending', reserve: true},
      {name: 'unreserved-empty'},
      {name: 'unreserved-ready', checkedAt: widest},
    ].map(({name, reserve, checkedAt}) => (
      <div key={name} className="app has-rail42" data-strip-state={name}
        data-reserve-freshness={reserve ? 'true' : undefined}>
        <InstrumentShell
          route="briefing"
          marketScope={['ZA', 'NG', 'KE']}
          checkedAt={checkedAt}
          theme="midnight"
          onThemeChange={() => {}}
        ><div /></InstrumentShell>
      </div>
    ))}
  </>);

  const directory = mkdtempSync(join(tmpdir(), 'lp-strip-reserve-'));
  try {
    /* Windows will not open a browser window narrower than about 500 pixels, so
       the 390 viewport is an iframe, the same way gate-geometry.test.jsx does it. */
    const framePath = join(directory, 'frame.html');
    const pagePath = join(directory, 'strip.html');
    writeFileSync(framePath, `<!doctype html><html data-dir="midnight"><head><meta charset="utf-8">`
      + `<style>html,body{margin:0}${critical.replace(/@font-face\{[^}]*\}/g, '')}${bootCss}</style>`
      + `</head><body>${shell}</body></html>`, 'utf8');
    writeFileSync(pagePath, '<!doctype html><html><head><meta charset="utf-8"></head><body>'
      + '<iframe src="./frame.html" style="width:390px;height:844px;border:0"></iframe>'
      + '<iframe src="./frame.html" style="width:1280px;height:900px;border:0"></iframe>'
      + '<pre id="measured">pending</pre><script>'
      + 'const frames=Array.from(document.querySelectorAll("iframe"));'
      + 'const out=document.getElementById("measured");let tries=0;'
      + 'const read=()=>{tries+=1;try{'
      + 'const layouts=frames.map(frame=>{const view=frame.contentWindow;'
      + 'const roots=Array.from(view.document.querySelectorAll("[data-strip-state]"));'
      + 'if(roots.length!==4)throw new Error("strips never rendered");'
      + 'const states=Object.fromEntries(roots.map(root=>{'
      + 'const strip=root.querySelector(".instrument-utility-strip");'
      + 'const style=view.getComputedStyle(strip);const box=strip.getBoundingClientRect();'
      + 'const children=Array.from(strip.children).filter(child=>{'
      + 'const rect=child.getBoundingClientRect();return rect.width>0&&rect.height>0});'
      + 'const boxes=children.map(child=>child.getBoundingClientRect());'
      + 'const edges=[style.paddingTop,style.paddingBottom,style.borderTopWidth,style.borderBottomWidth]'
      + '.reduce((total,value)=>total+parseFloat(value),0);'
      + 'const maxChildHeight=Math.max(...boxes.map(rect=>rect.height));'
      + 'const clipped=children.filter(child=>{const rect=child.getBoundingClientRect();'
      + 'return rect.left<box.left-1||rect.right>box.right+1||rect.top<box.top-1||rect.bottom>box.bottom+1'
      + '||(child.matches("button")&&child.scrollWidth>child.clientWidth+1)}).length;'
      + 'return [root.dataset.stripState,{height:box.height,reserved:parseFloat(style.minBlockSize),'
      + 'natural:Math.max(parseFloat(style.minBlockSize),maxChildHeight+edges),maxChildHeight,'
      + 'rowsHeight:Math.max(...boxes.map(rect=>rect.bottom))-Math.min(...boxes.map(rect=>rect.top)),'
      + 'checkedAt:strip.querySelector(".instrument-checked-at")?.textContent,clipped}]'
      + '}));return {width:view.innerWidth,states}});out.textContent=JSON.stringify({layouts});'
      + '}catch(error){if(tries<100){setTimeout(read,25);return}out.textContent=JSON.stringify({error:String(error)})}};'
      + 'read();'
      + '</script></body></html>', 'utf8');
    const result = spawnSync(chrome, [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      /* The measuring page reads into its own file:// iframe, which Chrome
         treats as a separate origin without this. */
      '--allow-file-access-from-files',
      '--hide-scrollbars', '--window-size=900,900', '--virtual-time-budget=4000', '--dump-dom',
      `--user-data-dir=${join(directory, 'profile')}`,
      pathToFileURL(pagePath).href,
    ], {encoding: 'utf8', timeout: 30000, windowsHide: true});
    expect(result.status, result.stderr).toBe(0);
    const measured = JSON.parse(result.stdout.match(/<pre id="measured">([^<]+)<\/pre>/)[1]);

    expect(measured.error).toBeUndefined();
    expect(measured.layouts.map(({width}) => width)).toEqual([390, 1280]);
    for (const {width, states} of measured.layouts){
      const ready = states['reserved-ready'];
      const pending = states['reserved-pending'];
      const empty = states['unreserved-empty'];
      const unreservedReady = states['unreserved-ready'];
      for (const state of Object.values(states)) expect(state.clipped).toBe(0);
      expect(ready.checkedAt).toContain(widest);
      expect(pending.checkedAt).toBeUndefined();
      expect(empty.checkedAt).toBeUndefined();
      expect(Math.abs(pending.height - ready.height)).toBeLessThanOrEqual(1);
      expect(Math.abs(empty.height - empty.natural)).toBeLessThanOrEqual(1);
      expect(empty.rowsHeight).toBeLessThanOrEqual(empty.maxChildHeight + 1);
      if (width === 390){
        expect(pending.height).toBeGreaterThan(empty.height);
        expect(ready.height, `strip wrapped past its reservation with "${widest}"`)
          .toBeLessThanOrEqual(ready.reserved + 1);
      } else {
        expect(pending.reserved).toBe(empty.reserved);
        expect(Math.abs(pending.height - empty.height)).toBeLessThanOrEqual(1);
        expect(Math.abs(ready.height - unreservedReady.height)).toBeLessThanOrEqual(1);
      }
    }
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}, 60000);
