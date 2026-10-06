import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import {isAbsolute, join, relative, resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath, pathToFileURL} from 'node:url';

import {observeLiveMarginChapters} from '../frontend/src/ui/useLiveMargin.js';

const root = fileURLToPath(new URL('..', import.meta.url));
const sourcePath = (relativePath) => join(root, ...relativePath.split('/'));
const read = (relativePath) => readFileSync(sourcePath(relativePath), 'utf8');
const sha256 = (value) => createHash('sha256').update(value, 'utf8').digest('hex');
const frontendRequire = createRequire(pathToFileURL(sourcePath('frontend/package.json')));
const postcss = frontendRequire('postcss');

const paths = {
  app: 'frontend/src/App.jsx',
  briefingContract: 'frontend/src/briefingContract.js',
  briefing: 'frontend/src/ui/RedThreadBriefing.jsx',
  briefText: 'frontend/src/briefText.js',
  emptyCss: 'frontend/src/styles/empty-states.css',
  gateCss: 'frontend/src/styles/gate.css',
  html: 'frontend/index.html',
  main: 'frontend/src/main.jsx',
  masthead: 'frontend/src/ui/OgilvyShell.jsx',
  passcode: 'frontend/src/passcode.jsx',
  productCss: 'frontend/src/styles/ogilvy-intelligence.css',
  today: 'frontend/src/today.jsx',
};

const sources = Object.fromEntries(
  Object.entries(paths).map(([name, relativePath]) => [name, read(relativePath)]),
);

function cssRoot(cssOrNode){
  return typeof cssOrNode === 'string' ? postcss.parse(cssOrNode) : cssOrNode;
}

function declarationNodes(rule, property){
  return rule.nodes.filter((node) => (
    node.type === 'decl' && (!property || node.prop === property)
  ));
}

function cssRules(cssOrNode){
  const rules = [];
  cssRoot(cssOrNode).walkRules((rule) => {
    const declarations = declarationNodes(rule);
    rules.push({
      body: declarations.map((decl) => decl.toString()).join('; ') + (declarations.length ? ';' : ''),
      node: rule,
      selectors: rule.selectors.map((selector) => selector.trim()),
    });
  });
  return rules;
}

function mediaBlock(cssOrNode, params){
  const matches = [];
  cssRoot(cssOrNode).walkAtRules('media', (atRule) => {
    if (atRule.params === params) matches.push(atRule);
  });
  assert.equal(matches.length, 1, `Expected one media rule: ${params}`);
  return matches[0];
}

function reducedMotionBlock(cssOrNode){
  return mediaBlock(cssOrNode, '(prefers-reduced-motion: reduce)');
}

function assertOnlyNoneShadows(css){
  cssRoot(css).walkDecls((decl) => {
    if (decl.prop === 'box-shadow' || decl.prop === 'text-shadow'){
      assert.equal(decl.value.trim().toLowerCase(), 'none', `${decl.prop} must be none`);
    }
  });
}

function cssRule(cssOrNode, selector){
  const matches = cssRules(cssOrNode).filter(({selectors}) => selectors.includes(selector));
  assert.equal(matches.length, 1, `Expected one rule owned by ${selector}`);
  return matches[0];
}

function declaration(cssOrNode, selector, property){
  const rule = cssRule(cssOrNode, selector);
  const matches = declarationNodes(rule.node, property);
  assert.equal(matches.length, 1, `Expected one ${property} declaration on ${selector}`);
  return matches[0];
}

function assertDeclaration(cssOrNode, selector, property, value, important=false){
  const actual = declaration(cssOrNode, selector, property);
  assert.equal(actual.value, value, `${selector} ${property}`);
  assert.equal(Boolean(actual.important), important, `${selector} ${property} importance`);
}

function assertStateParity(css, selectors, expectedValue, property = 'transform'){
  const match = cssRules(css).find(({selectors: actual}) => (
    selectors.every((selector) => actual.includes(selector))
  ));
  assert.ok(match, `Missing shared state rule for ${selectors.join(', ')}`);
  const values = declarationNodes(match.node, property);
  assert.equal(values.length, 1, `Expected one shared ${property} for ${selectors.join(', ')}`);
  assert.equal(values[0].value, expectedValue);
}

/* The motion law. A transition in the product or gate sheet runs at one of
   the package moves the addendum names, expressed through the package
   variable and paired with the easing the table gives it: row hover and
   control on the standard easing, layer open on the standard easing, reveal
   on the handoff easing. No duration or curve is written in the sheet. */
const MOTION_MOVES = new Map([
  ['--motion-control', '--motion-standard-easing'],
  ['--motion-row', '--motion-standard-easing'],
  ['--motion-layer', '--motion-standard-easing'],
  ['--motion-handoff', '--motion-handoff-easing'],
]);

function assertMotionLaw(cssOrNode, label){
  const parsed = cssRoot(cssOrNode);
  const normal = parsed.clone();
  normal.walkAtRules('media', (atRule) => {
    if (atRule.params === '(prefers-reduced-motion: reduce)') atRule.remove();
  });
  const used = new Set();
  normal.walkDecls((decl) => {
    if (!decl.prop.startsWith('transition')) return;
    assert.equal(decl.prop, 'transition', `${label}: ${decl.parent.selector} uses a transition longhand`);
    for (const layer of decl.value.split(/,(?![^(]*\))/).map((part) => part.trim())){
      if (layer === 'none') continue;
      const match = layer.match(/^([a-z-]+) var\((--motion-[a-z]+)\) var\((--motion-[a-z]+-easing)\)$/);
      assert.ok(match, `${label}: ${decl.parent.selector} transition "${layer}" is not a package move on its easing`);
      const [, , duration, easing] = match;
      assert.ok(MOTION_MOVES.has(duration), `${label}: ${decl.parent.selector} names ${duration}, which is not a host move`);
      assert.equal(easing, MOTION_MOVES.get(duration), `${label}: ${decl.parent.selector} pairs ${duration} with ${easing}`);
      used.add(duration);
    }
  });
  normal.walkDecls((decl) => {
    assert.doesNotMatch(decl.value, /\b\d+(?:\.\d+)?m?s\b/, `${label}: ${decl.parent.selector} ${decl.prop} writes a duration of its own`);
    assert.doesNotMatch(decl.value, /cubic-bezier\(|\b(?:ease|ease-in|ease-out|ease-in-out|linear)\b/, `${label}: ${decl.parent.selector} ${decl.prop} writes a curve of its own`);
  });
  return used;
}

function assertNoForbiddenLiveMarginEffects(css, briefing){
  const parsed = cssRoot(css);
  parsed.walkAtRules((atRule) => {
    assert.notEqual(atRule.name.toLowerCase(), 'keyframes', 'Keyframe animation is forbidden');
  });
  parsed.walkDecls((decl) => {
    const property = decl.prop.toLowerCase();
    const value = decl.value.toLowerCase();
    assert.doesNotMatch(value, /(?:linear|radial|conic)-gradient\(/, 'Gradient is forbidden');
    if (property === 'box-shadow' || property === 'text-shadow'){
      assert.equal(value, 'none', `${property} must be none`);
    }
    if (property === 'filter' || property === 'backdrop-filter'){
      assert.equal(value, 'none', `${property} effect is forbidden`);
    }
    if (property === 'scale' || property === 'translate'){
      assert.fail(`Individual transform property is forbidden: ${property}`);
    }
    if (/^-webkit-(?:filter|backdrop-filter|transform)$/.test(property)){
      assert.fail(`Prefixed effect alias is forbidden: ${property}`);
    }
    if (property === 'background-attachment'){
      assert.notEqual(value, 'fixed', 'Parallax background is forbidden');
    }
    if (property.startsWith('animation')){
      const reduced = decl.parent.parent;
      const allowed = reduced?.type === 'atrule'
        && reduced.name === 'media'
        && reduced.params === '(prefers-reduced-motion: reduce)'
        && property === 'animation-duration'
        && value === '0.001ms'
        && decl.important;
      assert.ok(allowed, `Unauthorized animation declaration: ${property}`);
    }
  });

  const key = (selectors) => [...selectors].sort().join(',');
  const allowedTransforms = new Map([
    [key(['.oi-briefing__chapter-nav::after']), 'scaleY(var(--live-margin-progress, 0.25))'],
    [key(['.oi-briefing__chapter-nav button::before']), 'translateX(0)'],
    [key(['.oi-briefing__chapter-nav button::after']), 'scaleX(0)'],
    [key([
      '.oi-briefing__chapter-nav button:hover::before',
      '.oi-briefing__chapter-nav button:focus-visible::before',
      '.oi-briefing__chapter-nav button[data-active="true"]::before',
    ]), 'translateX(4px)'],
    [key([
      '.oi-briefing__chapter-nav button:hover::after',
      '.oi-briefing__chapter-nav button:focus-visible::after',
      '.oi-briefing__chapter-nav button[data-active="true"]::after',
    ]), 'scaleX(1)'],
    /* The receipt-link underline wipe. The rule scales and the link does not.
       The receipt itself owns no transform: its hover is the row move, the
       plane stepping up and the leading rule going red. */
    [key(['.oi-briefing__receipt-link::after']), 'scaleX(0)'],
    [key(['.oi-briefing__receipt-link:is(:hover, :focus-visible)::after']), 'scaleX(1)'],
  ]);
  const seenTransforms = [];
  parsed.walkDecls('transform', (decl) => {
    const selectors = decl.parent.selectors.map((selector) => selector.trim());
    const selectorKey = key(selectors);
    assert.ok(allowedTransforms.has(selectorKey), `Unauthorized transform owner: ${selectors.join(', ')}`);
    assert.equal(decl.value, allowedTransforms.get(selectorKey), `Unauthorized transform value: ${selectors.join(', ')}`);
    seenTransforms.push(selectorKey);
  });
  assert.deepEqual([...seenTransforms].sort(), [...allowedTransforms.keys()].sort());

  assert.doesNotMatch(
    `${css}\n${briefing}`,
    /pointermove|mousemove|client[XY]|--cursor-[xy]/i,
    'Cursor-following motion is forbidden',
  );
}

function assertPremiumLiveMarginCss(css, briefing=sources.briefing){
  const parsed = cssRoot(css);
  const reduced = reducedMotionBlock(parsed);
  const normal = parsed.clone();
  reducedMotionBlock(normal).remove();
  assertDeclaration(normal, '.oi-product', '--oi-serif', '"Ogilvy Serif", OgilvySerif, Georgia, "Times New Roman", serif');
  assertDeclaration(normal, '.oi-product', 'scroll-behavior', 'smooth');
  assertDeclaration(normal, '.oi-display', 'font-family', 'var(--oi-serif)');
  assertDeclaration(normal, '.oi-heading', 'font-family', 'var(--oi-serif)');

  const moves = assertMotionLaw(normal, 'product');
  assert.deepEqual([...moves].sort(), ['--motion-control', '--motion-handoff', '--motion-row']);
  const owners = cssRules(normal)
    .filter(({node}) => declarationNodes(node).some((decl) => decl.prop === 'transition' || decl.prop.startsWith('transition-')))
    .flatMap(({node, selectors}) => declarationNodes(node)
      .filter((decl) => decl.prop === 'transition' || decl.prop.startsWith('transition-'))
      .flatMap(() => selectors))
    .sort();
  assert.deepEqual(owners, [
    '.oi-briefing__chapter-nav button::after',
    '.oi-briefing__chapter-nav button::before',
    '.oi-briefing__chapter-nav::after',
    '.oi-briefing__receipt-link::after',
    '.oi-receipt',
    '.oi-receipt::before',
    '.oi-shell__action',
    '.oi-shell__market',
  ]);
  const exactTransitions = new Map([
    /* The live margin fill is a reveal; the title and receipt underlines and
       the title nudge are controls; the receipt shift is a row hover. */
    ['.oi-briefing__chapter-nav::after', 'transform var(--motion-handoff) var(--motion-handoff-easing)'],
    ['.oi-briefing__chapter-nav button::before', 'transform var(--motion-control) var(--motion-standard-easing)'],
    ['.oi-briefing__chapter-nav button::after', 'transform var(--motion-control) var(--motion-standard-easing)'],
    ['.oi-receipt', 'background-color var(--motion-row) var(--motion-standard-easing)'],
    ['.oi-receipt::before', 'background-color var(--motion-row) var(--motion-standard-easing)'],
    ['.oi-briefing__receipt-link::after', 'transform var(--motion-control) var(--motion-standard-easing)'],
    /* The shell action and market chip answer the pointer with colour on the
       control move. */
    ['.oi-shell__action', 'color var(--motion-control) var(--motion-standard-easing)'],
    ['.oi-shell__market', 'color var(--motion-control) var(--motion-standard-easing), background-color var(--motion-control) var(--motion-standard-easing)'],
  ]);
  for (const [selector, value] of exactTransitions){
    const rule = cssRule(normal, selector);
    const transitions = declarationNodes(rule.node).filter((decl) => (
      decl.prop === 'transition' || decl.prop.startsWith('transition-')
    ));
    assert.deepEqual(transitions.map((decl) => [decl.prop, decl.value]), [['transition', value]]);
  }

  const desktop = mediaBlock(parsed, '(min-width: 800px)');
  const mobile = mediaBlock(parsed, '(max-width: 799px)');
  assertDeclaration(desktop, '.oi-briefing__lead-margin', 'position', 'sticky');
  assertDeclaration(desktop, '.oi-briefing__lead-margin', 'top', '96px');
  assertDeclaration(desktop, '.oi-briefing__lead-margin', 'align-self', 'start');
  assertDeclaration(mobile, '.oi-briefing__lead-margin', 'position', 'static');
  const decisionRules = cssRules(parsed).filter(({node, selectors}) => (
    node.parent === parsed && selectors.includes('.oi-briefing__decisions')
  ));
  assert.equal(decisionRules.length, 1, 'Expected one base decision flow rule');
  assert.deepEqual(
    declarationNodes(decisionRules[0].node, 'grid-template-columns').map((decl) => decl.value),
    ['1fr'],
  );

  assertDeclaration(parsed, '.oi-briefing__chapter-nav::after', 'transform', 'scaleY(var(--live-margin-progress, 0.25))');
  assertDeclaration(parsed, '.oi-briefing__chapter-nav::after', 'transform-origin', 'top');
  assertDeclaration(parsed, '.oi-briefing__chapter-nav button', 'min-height', '48px');
  assertDeclaration(parsed, '.oi-briefing__chapter-nav button::before', 'counter-increment', 'live-margin');
  assertDeclaration(parsed, '.oi-briefing__chapter-nav button::before', 'transform', 'translateX(0)');
  assertDeclaration(parsed, '.oi-briefing__chapter-nav button::after', 'background', 'var(--oi-red)');
  assertDeclaration(parsed, '.oi-briefing__chapter-nav button::after', 'transform', 'scaleX(0)');
  assertDeclaration(parsed, '.oi-briefing__chapter-nav button::after', 'transform-origin', 'left');
  assert.equal(declarationNodes(cssRule(parsed, '.oi-receipt').node, 'transform').length, 0, 'The receipt owns no transform');
  assertStateParity(css, [
    '.oi-briefing__chapter-nav button:hover::after',
    '.oi-briefing__chapter-nav button:focus-visible::after',
    '.oi-briefing__chapter-nav button[data-active="true"]::after',
  ], 'scaleX(1)');
  assertStateParity(css, [
    '.oi-briefing__chapter-nav button:hover::before',
    '.oi-briefing__chapter-nav button:focus-visible::before',
    '.oi-briefing__chapter-nav button[data-active="true"]::before',
  ], 'translateX(4px)');
  assertStateParity(css, ['.oi-receipt:hover', '.oi-receipt:focus-within'], 'var(--oi-white)', 'background-color');
  assertStateParity(css, ['.oi-receipt:hover::before', '.oi-receipt:focus-within::before'], 'var(--oi-red)', 'background');
  assertDeclaration(parsed, '.oi-briefing__receipt-link', 'min-width', '48px');
  assertDeclaration(parsed, '.oi-briefing__receipt-link', 'min-height', '48px');
  assertDeclaration(reduced, '.oi-product', 'scroll-behavior', 'auto', true);
  assertDeclaration(reduced, '.oi-product *', 'animation-duration', '0.001ms', true);
  assertDeclaration(reduced, '.oi-product *', 'transition-duration', '0.001ms', true);
  assertDeclaration(reduced, '.oi-product *', 'scroll-behavior', 'auto', true);
  assertNoForbiddenLiveMarginEffects(css, briefing);
}

function assertAccumulatedObserverProgress(observe){
  const originalWindow = globalThis.window;
  const hadWindow = Object.prototype.hasOwnProperty.call(globalThis, 'window');
  let observer;
  class TestObserver {
    constructor(callback){
      this.callback = callback;
      observer = this;
    }

    observe(){}
    disconnect(){}
  }
  const proof = {};
  const precedent = {};
  const response = {};
  const emitted = [];
  globalThis.window = {IntersectionObserver: TestObserver};
  try {
    const cleanup = observe(
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
    cleanup();
  } finally {
    if (hadWindow) globalThis.window = originalWindow;
    else delete globalThis.window;
  }
  assert.deepEqual(emitted, ['proof', 'precedent', 'response']);
}

test('Live Margin observer accumulates visible targets across deliveries', () => {
  assert.doesNotThrow(() => assertAccumulatedObserverProgress(observeLiveMarginChapters));
});

test('Live Margin observer gate rejects current delivery only state', () => {
  const currentDeliveryOnlyObserver = (chapterTargets, onActiveChapter) => {
    const chapterByTarget = new Map(
      [...chapterTargets].map(([chapterId, target]) => [target, chapterId]),
    );
    const observer = new window.IntersectionObserver((entries) => {
      const visible = entries.filter(({isIntersecting}) => isIntersecting);
      const nearest = visible.sort((left, right) => (
        Math.abs(left.boundingClientRect.top) - Math.abs(right.boundingClientRect.top)
      ))[0];
      if (nearest) onActiveChapter(chapterByTarget.get(nearest.target));
    });
    chapterTargets.forEach((target) => observer.observe(target));
    return () => observer.disconnect();
  };

  assert.throws(
    () => assertAccumulatedObserverProgress(currentDeliveryOnlyObserver),
    /Expected values to be strictly deep-equal/,
  );
});

function assertNaturalScrollProgress(observe){
  const originalWindow = globalThis.window;
  const hadWindow = Object.prototype.hasOwnProperty.call(globalThis, 'window');
  let scrollY = 0;
  let scrollListener;
  let frameCallback;
  let observer;
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
  const emitted = [];
  globalThis.window = {
    IntersectionObserver: TestObserver,
    innerHeight: 900,
    document: {documentElement: {scrollHeight: 1071}},
    get scrollY(){ return scrollY; },
    requestAnimationFrame(callback){
      frameCallback = callback;
      return 1;
    },
    cancelAnimationFrame(){ frameCallback = undefined; },
    addEventListener(type, listener){
      if (type === 'scroll') scrollListener = listener;
    },
    removeEventListener(){},
  };
  try {
    const proof = targetAt(180);
    const cleanup = observe(new Map([
      ['proof', proof],
      ['precedent', targetAt(600)],
      ['response', targetAt(760)],
    ]), (chapter) => {
      if (emitted.at(-1) !== chapter) emitted.push(chapter);
    });
    assert.equal(typeof scrollListener, 'function', 'Natural scroll listener missing');
    for (const nextScrollY of [0, 60, 120, 171]){
      scrollY = nextScrollY;
      scrollListener();
      const scheduled = frameCallback;
      frameCallback = undefined;
      assert.equal(typeof scheduled, 'function', 'Natural scroll frame missing');
      scheduled();
    }
    observer.callback([{isIntersecting: true, target: proof, boundingClientRect: {top: -28}}]);
    if (frameCallback){
      const scheduled = frameCallback;
      frameCallback = undefined;
      scheduled();
    }
    cleanup();
  } finally {
    if (hadWindow) globalThis.window = originalWindow;
    else delete globalThis.window;
  }
  assert.deepEqual(emitted, ['proof', 'precedent', 'response']);
}

test('Live Margin observer follows live rectangles across natural scroll', () => {
  assert.doesNotThrow(() => assertNaturalScrollProgress(observeLiveMarginChapters));
});

test('Live Margin observer gate rejects missing scroll evaluation', () => {
  const observerWithoutScroll = (chapterTargets) => {
    const observer = new window.IntersectionObserver(() => {});
    chapterTargets.forEach((target) => observer.observe(target));
    return () => observer.disconnect();
  };

  assert.throws(
    () => assertNaturalScrollProgress(observerWithoutScroll),
    /Natural scroll listener missing/,
  );
});

function assertNoRetiredCoBrand(text){
  assert.doesNotMatch(
    text,
    /Google\s*[×x]\s*Ogilvy|four[- ]dot Google|gate-v3-mark|gboot-stage-word|Google Sans|Newsreader|Hanken Grotesk|IBM Plex Mono/i,
  );
}

test('specific retired co-brand artifacts stay out while research subjects remain allowed', () => {
  const identitySources = [
    sources.briefText,
    sources.passcode,
    sources.masthead,
    sources.briefing,
  ].join('\n');
  assertNoRetiredCoBrand(identitySources);
  assert.match(sources.briefText, /· 42 · Ogilvy Intelligence'/);
  assert.match(sources.masthead, />Ogilvy Intelligence<\/span>/);
});

test('co-brand gate rejects retired identity mutations without blocking Google research subjects', () => {
  assert.doesNotThrow(() => assertNoRetiredCoBrand('Google AI Mode and Google Search are evidence subjects.'));
  assert.throws(() => assertNoRetiredCoBrand('42 · Google × Ogilvy'), /expected to not match/i);
  assert.throws(() => assertNoRetiredCoBrand('four-dot Google mark'), /expected to not match/i);
});

test('product sources use no external Google font transport', () => {
  const fontSources = [sources.html, sources.main, sources.productCss, sources.gateCss].join('\n');
  assert.doesNotMatch(fontSources, /fonts\.(?:googleapis|gstatic)\.com|@import\s+(?:url\()?[^;]*font/i);
});

test('new shell sources contain no prohibited effects or controls', () => {
  const shellJsx = [sources.masthead, sources.briefing, sources.passcode, sources.today].join('\n');
  const shellCss = [sources.productCss, sources.gateCss].join('\n');
  assert.doesNotMatch(
    shellCss,
    /(?:linear|radial|conic)-gradient|\bglow\b|\bglass\b|backdrop-filter|filter\s*:\s*blur|@keyframes|\binfinite\b/i,
  );
  assertOnlyNoneShadows(shellCss);
  assert.doesNotMatch(
    shellJsx,
    />\s*(?:Theme|Colour|Motion)\s*<|data-motion|motion controls?|route[- ]lanes?|bounc(?:e|ing)|className=["'][^"']*\bdot\b/i,
  );
});

test('shadow gate accepts resets and rejects visible shadow mutations', () => {
  assert.doesNotThrow(() => assertOnlyNoneShadows('box-shadow: none; text-shadow: none;'));
  assert.throws(() => assertOnlyNoneShadows('box-shadow: 0 2px 8px #000;'), /box-shadow must be none/);
  assert.throws(() => assertOnlyNoneShadows('text-shadow: 0 1px 2px #000;'), /text-shadow must be none/);
});

test('Ogilvy product owns exactly eight type roles and JSX owns no raw font size', () => {
  const typeOwners = cssRules(sources.productCss)
    .filter(({body}) => /(?:^|;)\s*font-family\s*:/.test(body))
    .flatMap(({selectors}) => selectors)
    .filter((selector) => /^\.oi-[\w-]+$/.test(selector))
    .sort();
  assert.deepEqual(typeOwners, [
    '.oi-body',
    '.oi-display',
    '.oi-folio',
    '.oi-heading',
    '.oi-metadata',
    '.oi-navigation',
    '.oi-provenance',
    '.oi-small',
  ]);

  const shellJsx = [sources.masthead, sources.briefing, sources.passcode, sources.today].join('\n');
  assert.doesNotMatch(shellJsx, /\bfontSize\s*:|\bfont-size\s*:/);
});

test('Ogilvy product radii never exceed two pixels', () => {
  const radii = [...sources.productCss.matchAll(/border-radius\s*:\s*([^;]+);/g)]
    .map(([, value]) => value.trim());
  assert.ok(radii.length > 0, 'Expected explicit product radius declarations');
  for (const value of radii){
    if (/^0(?:\s+0)*$/.test(value)) continue;
    const parts = value.split(/\s+/);
    for (const part of parts){
      const match = part.match(/^(\d+(?:\.\d+)?)px$/);
      assert.ok(match, `Unsupported product radius value: ${value}`);
      assert.ok(Number(match[1]) <= 2, `Product radius exceeds 2px: ${value}`);
    }
  }
});

test('premium Live Margin source contract is exact', () => {
  assert.doesNotThrow(() => assertPremiumLiveMarginCss(sources.productCss));
});

test('premium Live Margin gate kills every required source mutation', async (t) => {
  const css = sources.productCss;
  const replaceRequired = (source, before, after) => {
    assert.notEqual(source.indexOf(before), -1, `Mutation source missing: ${before}`);
    return source.replace(before, after);
  };
  const replaceInRule = (source, selector, before, after) => {
    const start = source.indexOf(`${selector} {`);
    assert.notEqual(start, -1, `Mutation rule missing: ${selector}`);
    const end = source.indexOf('}', start) + 1;
    const rule = source.slice(start, end);
    const mutatedRule = replaceRequired(rule, before, after);
    return source.slice(0, start) + mutatedRule + source.slice(end);
  };
  const receiptRule = `.oi-receipt {
  position: relative;
  transition: background-color var(--motion-row) var(--motion-standard-easing);
}`;
  const mutations = [
    ['missing installed font', () => replaceRequired(css, '"Ogilvy Serif", OgilvySerif, Georgia, "Times New Roman", serif', 'OgilvySerif, Georgia, "Times New Roman", serif')],
    ['missing OgilvySerif fallback', () => replaceRequired(css, '"Ogilvy Serif", OgilvySerif, Georgia, "Times New Roman", serif', '"Ogilvy Serif", Georgia, "Times New Roman", serif')],
    ['reordered OgilvySerif fallback', () => replaceRequired(css, '"Ogilvy Serif", OgilvySerif, Georgia, "Times New Roman", serif', '"Ogilvy Serif", Georgia, OgilvySerif, "Times New Roman", serif')],
    ['wrong display family', () => replaceRequired(css, '.oi-display {\n  font-family: var(--oi-serif);', '.oi-display {\n  font-family: var(--oi-sans);')],
    ['wrong heading family', () => replaceRequired(css, '.oi-heading {\n  font-family: var(--oi-serif);', '.oi-heading {\n  font-family: var(--oi-sans);')],
    ['wrong 800 pixel sticky boundary', () => replaceRequired(css, '@media (min-width: 800px)', '@media (min-width: 801px)')],
    ['wrong 799 pixel release boundary', () => replaceRequired(css, '@media (max-width: 799px)', '@media (max-width: 800px)')],
    ['two-column decisions', () => replaceInRule(css, '.oi-briefing__decisions', '  grid-template-columns: 1fr;', '  grid-template-columns: repeat(2, minmax(0, 1fr));')],
    ['sticky mobile lead', () => replaceRequired(css, '@media (max-width: 799px) {\n  .oi-briefing__lead-margin {\n    position: static;', '@media (max-width: 799px) {\n  .oi-briefing__lead-margin {\n    position: sticky;')],
    ['commented sticky declaration', () => replaceRequired(css, '    position: sticky;', '    /* position: sticky; */')],
    ['invalid sticky value', () => replaceRequired(css, '    position: sticky;', '    position: sticky invalid;')],
    ['missing receipt target width', () => replaceInRule(css, '.oi-briefing__receipt-link', '  min-width: 48px;\n  min-height: 48px;', '  min-height: 48px;')],
    ['missing receipt target height', () => replaceInRule(css, '.oi-briefing__receipt-link', '  min-width: 48px;\n  min-height: 48px;', '  min-width: 48px;')],
    ['commented receipt target width', () => replaceInRule(css, '.oi-briefing__receipt-link', '  min-width: 48px;', '  /* min-width: 48px; */')],
    ['commented receipt target height', () => replaceInRule(css, '.oi-briefing__receipt-link', '  min-height: 48px;', '  /* min-height: 48px; */')],
    ['invalid receipt target width', () => replaceInRule(css, '.oi-briefing__receipt-link', '  min-width: 48px;', '  min-width: 48px invalid;')],
    ['missing title focus parity', () => replaceRequired(css, '.oi-briefing__chapter-nav button:focus-visible::after,\n', '')],
    ['missing selected touch title state', () => replaceRequired(css, ',\n.oi-briefing__chapter-nav button[data-active="true"]::after', '')],
    ['missing receipt focus parity', () => replaceRequired(css, ',\n.oi-receipt:focus-within', '')],
    ['unauthorized transition owner', () => `${css}\n.oi-briefing__proof { transition: opacity 300ms; }`],
    ['transition property all', () => replaceRequired(css, receiptRule, `${receiptRule.slice(0, -2)}  transition-property: all;\n}`)],
    ['transition shorthand all', () => replaceRequired(css, receiptRule, `${receiptRule.slice(0, -2)}  transition: all var(--motion-row) var(--motion-standard-easing);\n}`)],
    ['literal duration', () => replaceInRule(css, '.oi-receipt', 'var(--motion-row) var(--motion-standard-easing)', '140ms var(--motion-standard-easing)')],
    ['literal curve', () => replaceInRule(css, '.oi-receipt', 'var(--motion-standard-easing)', 'cubic-bezier(.2, .8, .2, 1)')],
    ['unpaired easing', () => replaceInRule(css, '.oi-receipt', 'var(--motion-standard-easing)', 'var(--motion-handoff-easing)')],
    ['ribbon draw in the host', () => replaceInRule(css, '.oi-receipt', 'var(--motion-row)', 'var(--motion-draw)')],
    ['transition duration override', () => replaceRequired(css, receiptRule, `${receiptRule.slice(0, -2)}  transition-duration: 600ms;\n}`)],
    ['transition delay override', () => replaceRequired(css, receiptRule, `${receiptRule.slice(0, -2)}  transition-delay: 100ms;\n}`)],
    ['transition timing override', () => replaceRequired(css, receiptRule, `${receiptRule.slice(0, -2)}  transition-timing-function: linear;\n}`)],
    ['transition behavior override', () => replaceRequired(css, receiptRule, `${receiptRule.slice(0, -2)}  transition-behavior: allow-discrete;\n}`)],
    ['gradient', () => `${css}\n.oi-briefing { background: linear-gradient(#fff, #000); }`],
    ['idle animation', () => `${css}\n.oi-briefing { animation: drift 400ms infinite; }`],
    ['glow', () => `${css}\n.oi-briefing { box-shadow: 0 0 20px #fff; }`],
    ['drop shadow', () => `${css}\n.oi-briefing__proof { filter: drop-shadow(0 0 4px #000); }`],
    ['glass blur', () => `${css}\n.oi-briefing { backdrop-filter: blur(8px); }`],
    ['parallax', () => `${css}\n.oi-briefing { background-attachment: fixed; }`],
    ['floating movement', () => `${css}\n.oi-briefing__proof { transform: translateY(4px); }`],
    ['element scaling', () => `${css}\n.oi-receipt { transform: scale(1.02); }`],
    ['receipt shift restored', () => `${css}\n.oi-receipt:hover { transform: translateX(4px); }`],
    ['receipt hover without focus parity', () => replaceRequired(css, '.oi-receipt:hover,\n.oi-receipt:focus-within {\n  background-color: var(--oi-white);', '.oi-receipt:hover,\n.oi-receipt:focus-within {\n  background-color: var(--oi-paper);')],
    ['unauthorized scaleX', () => `${css}\n.oi-briefing__proof { transform: scaleX(1.02); }`],
    ['unauthorized scaleY', () => `${css}\n.oi-briefing__proof { transform: scaleY(1.02); }`],
    ['individual scale property', () => `${css}\n.oi-briefing__proof { scale: 1.02; }`],
    ['individual translate property', () => `${css}\n.oi-briefing__proof { translate: 0 4px; }`],
    ['WebKit drop shadow alias', () => `${css}\n.oi-briefing__proof { -webkit-filter: drop-shadow(0 0 4px #000); }`],
    ['WebKit transform alias', () => `${css}\n.oi-briefing__proof { -webkit-transform: scaleX(1.02); }`],
  ];
  for (const [name, mutate] of mutations){
    await t.test(name, () => {
      const mutatedCss = mutate();
      assert.notEqual(mutatedCss, css, `Mutation did not change source: ${name}`);
      assert.throws(() => assertPremiumLiveMarginCss(mutatedCss), {name: 'AssertionError'});
    });
  }
  await t.test('cursor following', () => {
    const mutatedBriefing = `${sources.briefing}\nwindow.addEventListener('pointermove', followCursor);`;
    assert.throws(
      () => assertPremiumLiveMarginCss(css, mutatedBriefing),
      /Cursor-following motion is forbidden/,
    );
  });
});

test('gate transitions run on the package motion tokens', () => {
  const moves = assertMotionLaw(sources.gateCss, 'gate');
  assert.deepEqual([...moves].sort(), ['--motion-control', '--motion-handoff']);
});

test('every approved briefing and loading state has a committed source hook', () => {
  const briefingStateSources = [sources.briefingContract, sources.briefing].join('\n');
  for (const state of ['ready', 'thin', 'contradictory', 'unchecked', 'stale', 'error', 'no_discovery']){
    assert.match(briefingStateSources, new RegExp(`['"]${state}['"]`), `Missing state hook: ${state}`);
  }
  assert.match(sources.app, /['"]loading['"]/, 'Missing loading state hook');
  assert.match(sources.briefing, /data-briefing-state=/);
  assert.match(sources.briefing, /data-readiness=/);
});

const controlledFixtureStates = ['ready', 'thin', 'contradictory', 'unchecked', 'stale', 'error', 'no-discovery'];

const fixtureEntry = String.raw`
import React from 'react';
import {OgilvyShell} from './frontend/src/ui/OgilvyShell.jsx';
import {RedThreadBriefing} from './frontend/src/ui/RedThreadBriefing.jsx';

const baseTopic = {
  id: 'lead',
  signal_id: 'signal_lead',
  discovery_mode: 'cross_platform',
  evidence_state: 'ready',
  signal_name: 'Street football owns the evening',
  window_label: '28 July to 26 August 2026',
  why_now: 'Local tournament clips moved from isolated posts into a shared format.',
  receipts: [
    ['TikTok', 'Q', '@touchline', '18k views', '2d', 'https://example.com/block-final'],
    ['Instagram', 'Five a side after work', '@citypitch', '9k views', '3d', 'https://example.com/five-a-side'],
  ],
  precedent: {
    match: 'The format repeats the neighbourhood tournament cycle seen in 2024.',
    difference: 'This run crosses two platforms and centres evening play.',
  },
  possible_response: 'Brief one local pitch as a recurring cultural stage.',
};

const fixtures = [
  ['ready', {topics: [{...baseTopic, evidence_state: 'ready'}], freshness: {status: 'green'}}],
  ['thin', {topics: [{...baseTopic, evidence_state: 'thin'}], freshness: {status: 'green'}}],
  ['contradictory', {topics: [{...baseTopic, evidence_state: 'contradictory'}], freshness: {status: 'green'}}],
  ['unchecked', {topics: [{...baseTopic, receipts: []}], freshness: {status: 'green'}}],
  ['stale', {topics: [baseTopic], freshness: {status: 'amber', stamp_utc: '2026-08-27T06:30:00Z'}}],
  ['error', {topics: [], error: {code: 'api_unavailable', message: 'Controlled API error'}}],
  ['no-discovery', {topics: [], freshness: {status: 'green'}}],
];

export function FixtureApp({fixtureState='ready'}){
  const [route, setRoute] = React.useState('pulse');
  const [region, setRegion] = React.useState('ZA');
  const [name, props] = fixtures.find(([candidate]) => candidate === fixtureState) ?? fixtures[0];
  const section = React.createElement(
    'section',
    {className: 'oi-fixture__section', 'data-fixture-state': name},
    React.createElement('p', {className: 'oi-fixture__label'}, name),
    React.createElement(RedThreadBriefing, {...props, onOpen: () => {}, runDate: '2026-08-27'}),
  );
  return React.createElement(
    OgilvyShell,
    {route, setRoute, region, setRegion, freshness: 'Updated 8m ago'},
    React.createElement('main', {className: 'oi-fixture oi-fixture__states'}, section),
  );
}
`;

function copiedComponentEntry(){
  return fixtureEntry
    .replace(
      "import {OgilvyShell} from './frontend/src/ui/OgilvyShell.jsx';",
      "function OgilvyShell({children}){ return React.createElement('div', {className: 'oi-product oi-shell'}, children); }",
    )
    .replace(
      "import {RedThreadBriefing} from './frontend/src/ui/RedThreadBriefing.jsx';",
      "function RedThreadBriefing(){ return React.createElement('article', {className: 'oi-briefing'}); }",
    );
}

function serverEntry(source, fixtureState){
  return String.raw`
import {renderToStaticMarkup} from 'react-dom/server';
${source}
export const markup = renderToStaticMarkup(React.createElement(FixtureApp, {fixtureState: ${JSON.stringify(fixtureState)}}));
`;
}

function browserEntry(source, mutation){
  if (mutation === 'static-ssr'){
    return String.raw`
${source}
const mount = document.getElementById('oi-fixture-root');
if (!mount) throw new Error('Live Margin fixture mount is missing');
window.__retainedFixtureMarker = 'createRoot data-fixture-client';
window.__unmountedFixtureElement = React.createElement(FixtureApp, {fixtureState: 'ready'});
`;
  }
  return String.raw`
import {createRoot} from 'react-dom/client';
${source}
const mount = document.getElementById('oi-fixture-root');
if (!mount) throw new Error('Live Margin fixture mount is missing');
const requestedState = new URLSearchParams(window.location.search).get('state');
const fixtureState = fixtures.some(([name]) => name === requestedState)
  ? requestedState
  : mount.dataset.fixtureStateDefault || 'ready';
mount.replaceChildren();
createRoot(mount).render(React.createElement(FixtureApp, {fixtureState}));
window.requestAnimationFrame(() => {
  mount.dataset.fixtureClient = 'mounted';
});
`;
}

function bundleInputs(metafile){
  return Object.keys(metafile.inputs)
    .map((input) => relative(root, isAbsolute(input) ? input : resolve(process.cwd(), input)).replaceAll('\\', '/'))
    .sort();
}

function productScriptInputs(inputs){
  return inputs.filter((input) => (
    input.startsWith('frontend/src/') && /\.(?:js|jsx)$/.test(input)
  )).sort();
}

function productSourceHashes(inputs){
  return Object.fromEntries(productScriptInputs(inputs).map((input) => [input, sha256(read(input))]));
}

function assertFixtureBundleProvenance(name, metafile, browser=false){
  const inputs = bundleInputs(metafile);
  for (const productInput of [paths.masthead, paths.briefing]){
    assert.ok(
      inputs.includes(productInput),
      `Fixture bundle provenance missing ${productInput} from ${name}`,
    );
  }
  if (browser){
    assert.ok(
      inputs.some((input) => input.includes('frontend/node_modules/react-dom/client')),
      'Fixture bundle provenance missing react-dom/client createRoot execution from browser',
    );
  }
  return inputs;
}

async function renderActualComponents({fixtureState='ready', mutation}={}){
  const esbuildPath = sourcePath('frontend/node_modules/esbuild/lib/main.js');
  const {build} = await import(pathToFileURL(esbuildPath).href);
  const source = mutation === 'copied-components' ? copiedComponentEntry() : fixtureEntry;
  const result = await build({
    stdin: {
      contents: serverEntry(source, fixtureState),
      loader: 'jsx',
      resolveDir: root,
      sourcefile: 'ogilvy-intelligence-fixtures.jsx',
    },
    bundle: true,
    define: {'process.env.NODE_ENV': '"production"'},
    format: 'cjs',
    metafile: true,
    nodePaths: [sourcePath('frontend/node_modules')],
    platform: 'node',
    write: false,
  });
  const serverInputs = assertFixtureBundleProvenance('server', result.metafile);

  const directory = mkdtempSync(join(tmpdir(), 'oi-component-renderer-'));
  const bundlePath = join(directory, 'renderer.cjs');
  try {
    writeFileSync(bundlePath, result.outputFiles[0].text, 'utf8');
    const require = createRequire(import.meta.url);
    const rendered = require(bundlePath);
    const client = await build({
      stdin: {
        contents: browserEntry(source, mutation),
        loader: 'jsx',
        resolveDir: root,
        sourcefile: 'ogilvy-intelligence-fixtures-client.jsx',
      },
      bundle: true,
      define: {'process.env.NODE_ENV': '"production"'},
      format: 'iife',
      metafile: true,
      nodePaths: [sourcePath('frontend/node_modules')],
      platform: 'browser',
      write: false,
    });
    const browserInputs = assertFixtureBundleProvenance('browser', client.metafile, true);
    return {
      clientScript: client.outputFiles[0].text,
      manifest: {
        browser: browserInputs,
        productSourceHashes: {
          browser: productSourceHashes(browserInputs),
          server: productSourceHashes(serverInputs),
        },
        server: serverInputs,
      },
      markup: rendered.markup,
    };
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}

function standaloneDocument(markup, clientScript, fixtureState, manifest){
  const fixtureCss = `
html, body { margin: 0; min-width: 0; background: #f3efe7; }
body { --maxw: 1280px; }
*, *::before, *::after { box-sizing: border-box; }
.oi-fixture { min-width: 0; color: #171412; background: #f3efe7; }
.oi-fixture__states { min-width: 0; }
.oi-fixture__section { min-width: 0; border-top: 1px solid #aaa198; }
.oi-fixture__label { max-width: var(--maxw); margin: 0 auto; padding: 16px 24px 0; font: 700 12px Arial, sans-serif; letter-spacing: .08em; text-transform: uppercase; }
`;
  const embeddedClientScript = clientScript.replaceAll('</script', '<\\/script');
  const clientBundleSha256 = sha256(embeddedClientScript);
  const fixtureManifest = {...manifest, clientBundleSha256};
  const productCssHash = sha256(sources.productCss);
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ogilvy Intelligence component fixtures</title>
<style>
${fixtureCss}
/* ${paths.gateCss} */
${sources.gateCss}
/* ${paths.emptyCss} */
${sources.emptyCss}
/* ${paths.productCss} sha256:${productCssHash} */
${sources.productCss}/* end ${paths.productCss} */
</style>
</head>
<body><div id="oi-fixture-root" data-fixture-client="pending" data-fixture-state-default="${fixtureState}">${markup}</div>
<script id="oi-fixture-module-inputs" type="application/json">${JSON.stringify(fixtureManifest)}</script>
<script id="oi-fixture-client" data-sha256="${clientBundleSha256}">${embeddedClientScript}</script></body>
</html>
`;
}

function fixtureManifest(html){
  const match = html.match(/<script id="oi-fixture-module-inputs" type="application\/json">([^<]+)<\/script>/);
  assert.ok(match, 'Fixture module input manifest missing');
  return JSON.parse(match[1]);
}

function assertFixtureProductScriptProvenance(html){
  const manifest = fixtureManifest(html);
  for (const bundle of ['browser', 'server']){
    const expectedInputs = productScriptInputs(manifest[bundle] ?? []);
    const declaredHashes = manifest.productSourceHashes?.[bundle] ?? {};
    assert.deepEqual(
      Object.keys(declaredHashes).sort(),
      expectedInputs,
      `Fixture product source manifest mismatch: ${bundle}`,
    );
    for (const input of expectedInputs){
      assert.equal(
        declaredHashes[input],
        sha256(read(input)),
        `Fixture product source hash mismatch: ${bundle} ${input}`,
      );
    }
  }

  const client = html.match(
    /<script id="oi-fixture-client" data-sha256="([a-f0-9]{64})">([\s\S]*?)<\/script><\/body>/,
  );
  assert.ok(client, 'Fixture client bundle marker missing');
  assert.equal(client[1], manifest.clientBundleSha256, 'Fixture client bundle marker mismatch');
  assert.equal(sha256(client[2]), manifest.clientBundleSha256, 'Fixture client bundle content mismatch');
  return {
    clientBundleSha256: manifest.clientBundleSha256,
    productSourceCount: new Set([
      ...Object.keys(manifest.productSourceHashes.browser),
      ...Object.keys(manifest.productSourceHashes.server),
    ]).size,
  };
}

function assertFixtureProductCssProvenance(html){
  const markerPrefix = `/* ${paths.productCss} sha256:`;
  const markerStart = html.indexOf(markerPrefix);
  assert.notEqual(markerStart, -1, 'Fixture product CSS marker missing');
  const hashStart = markerStart + markerPrefix.length;
  const markerEnd = html.indexOf(' */\n', hashStart);
  assert.notEqual(markerEnd, -1, 'Fixture product CSS marker is malformed');
  const declaredHash = html.slice(hashStart, markerEnd);
  assert.match(declaredHash, /^[a-f0-9]{64}$/, 'Fixture product CSS SHA256 marker is invalid');
  const cssStart = markerEnd + ' */\n'.length;
  const endMarker = `/* end ${paths.productCss} */`;
  const cssEnd = html.indexOf(endMarker, cssStart);
  assert.notEqual(cssEnd, -1, 'Fixture product CSS end marker missing');
  const embeddedCss = html.slice(cssStart, cssEnd);
  const currentHash = sha256(sources.productCss);
  const embeddedHash = sha256(embeddedCss);
  assert.equal(declaredHash, currentHash, 'Fixture product CSS marker mismatch with current source');
  assert.equal(embeddedHash, currentHash, 'Fixture product CSS content mismatch with current source');
  return currentHash;
}

test('fixture product CSS provenance rejects a retained two-column mutation', () => {
  const fresh = standaloneDocument('', '', 'ready', {});
  assert.doesNotThrow(() => assertFixtureProductCssProvenance(fresh));
  const stale = fresh.replace(
    '.oi-briefing__decisions {\n  display: grid;\n  grid-area: decision;\n  grid-template-columns: 1fr;',
    '.oi-briefing__decisions {\n  display: grid;\n  grid-area: decision;\n  grid-template-columns: repeat(2, minmax(0, 1fr));',
  );
  assert.notEqual(stale, fresh);
  assert.throws(
    () => assertFixtureProductCssProvenance(stale),
    /Fixture product CSS content mismatch/,
  );
});

const renderIndex = process.argv.indexOf('--render');
if (renderIndex !== -1){
  const output = process.argv[renderIndex + 1];
  const stateIndex = process.argv.indexOf('--state');
  const fixtureState = stateIndex === -1 ? 'ready' : process.argv[stateIndex + 1];
  const mutationIndex = process.argv.indexOf('--fixture-mutation');
  const mutation = mutationIndex === -1 ? undefined : process.argv[mutationIndex + 1];
  assert.ok(output, 'The --render option requires an output path');
  assert.ok(isAbsolute(output), 'The --render output path must be absolute');
  assert.ok(controlledFixtureStates.includes(fixtureState), `Unknown fixture state: ${fixtureState}`);
  assert.ok(
    mutation === undefined || ['static-ssr', 'copied-components'].includes(mutation),
    `Unknown fixture mutation: ${mutation}`,
  );
  const {clientScript, manifest, markup} = await renderActualComponents({fixtureState, mutation});
  writeFileSync(output, standaloneDocument(markup, clientScript, fixtureState, manifest), 'utf8');
  process.stdout.write(`Rendered actual component fixtures to ${output}\n`);
}

const preflightIndex = process.argv.indexOf('--preflight');
if (preflightIndex !== -1){
  const fixturePath = process.argv[preflightIndex + 1];
  assert.ok(fixturePath, 'The --preflight option requires a fixture path');
  assert.ok(isAbsolute(fixturePath), 'The --preflight fixture path must be absolute');
  const html = readFileSync(fixturePath, 'utf8');
  const cssHash = assertFixtureProductCssProvenance(html);
  const scripts = assertFixtureProductScriptProvenance(html);
  process.stdout.write(
    `Fixture product preflight passed: CSS ${cssHash}; ${scripts.productSourceCount} source hashes; client ${scripts.clientBundleSha256}; ${fixturePath}\n`,
  );
}
