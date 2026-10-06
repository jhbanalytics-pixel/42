import test from 'node:test';
import assert from 'node:assert/strict';
import {cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import * as redesign from '../frontend/src/redesignContract.js';

import {
  PRIMARY_NAV,
  UTILITY_NAV,
  normalizeView,
  capacityFailure,
  apiFailureDetails,
  signalEvidence,
  splitMomentum,
  leadReading,
  todayMetricLabels,
  normalizeApiDetail,
} from '../frontend/src/redesignContract.js';

const uiCss = readFileSync(new URL('../frontend/src/ui/ui.css', import.meta.url), 'utf8');
const pageHeroSource = readFileSync(new URL('../frontend/src/ui/PageHero.jsx', import.meta.url), 'utf8');
const creatorSource = readFileSync(new URL('../frontend/src/creator.jsx', import.meta.url), 'utf8');
const voicesCss = readFileSync(new URL('../frontend/src/styles/voices.css', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../frontend/src/App.jsx', import.meta.url), 'utf8');
const passcodeSource = readFileSync(new URL('../frontend/src/passcode.jsx', import.meta.url), 'utf8');
const todaySource = readFileSync(new URL('../frontend/src/today.jsx', import.meta.url), 'utf8');
const askSource = readFileSync(new URL('../frontend/src/ask.jsx', import.meta.url), 'utf8');
const chatSource = readFileSync(new URL('../frontend/src/chat.jsx', import.meta.url), 'utf8');
const topicSource = readFileSync(new URL('../frontend/src/topic.jsx', import.meta.url), 'utf8');
const viewsSource = readFileSync(new URL('../frontend/src/views.jsx', import.meta.url), 'utf8');
const intelSource = readFileSync(new URL('../frontend/src/intel.jsx', import.meta.url), 'utf8');
const mapSource = readFileSync(new URL('../frontend/src/map.jsx', import.meta.url), 'utf8');
const boardSource = readFileSync(new URL('../frontend/src/board.jsx', import.meta.url), 'utf8');
const consoleSource = readFileSync(new URL('../frontend/src/ConsoleWorkbench.jsx', import.meta.url), 'utf8');
const researchDocSource = readFileSync(new URL('../frontend/src/ResearchDocPanel.jsx', import.meta.url), 'utf8');
const researchLibSource = readFileSync(new URL('../frontend/src/researchLib.jsx', import.meta.url), 'utf8');
const frontendHtml = readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
const voicesBoardSource = creatorSource.slice(
  creatorSource.indexOf('export function VoicesBoard'),
  creatorSource.indexOf('/* ==== Creator profile ==== */'),
);

function blockAfter(source, marker) {
  const markerIndex = source.indexOf(marker);
  assert.notEqual(markerIndex, -1, `Missing CSS marker: ${marker}`);
  const openIndex = source.indexOf('{', markerIndex);
  assert.notEqual(openIndex, -1, `Missing CSS block: ${marker}`);
  let depth = 0;
  for (let index = openIndex; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') depth -= 1;
    if (depth === 0) return source.slice(openIndex + 1, index);
  }
  assert.fail(`Unclosed CSS block: ${marker}`);
}

function rule(selector, source = uiCss) {
  return blockAfter(source, selector);
}

function assertPageHeroSemantics(source) {
  const markers = [
    '<header className="ui-page-hero">',
    '<h1 className="ui-page-hero-title serif">',
    '<p className="ui-page-hero-sub">',
    '<div className="ui-page-hero-actions">',
    '</header>',
  ];
  const positions = markers.map((marker) => source.indexOf(marker));
  for (const [index, position] of positions.entries()) {
    assert.notEqual(position, -1, `Missing PageHero semantic marker: ${markers[index]}`);
    if (index > 0) assert.ok(position > positions[index - 1], `PageHero marker out of order: ${markers[index]}`);
  }
}

function assertVoicesLadderSemantics(source) {
  assert.match(source, /<ol className="voices-ladder" aria-label="Ranked voices">/);
  assert.match(source, /<li key=\{c\.handle \|\| sourceIndex\} value=\{sourceIndex \+ 1\} className=\{'voices-entry voices-entry-' \+ variant\}>/);
  assert.match(source, /<\/li>/);
  assert.match(source, /<\/ol>/);
}

test('PageHero keeps its approved API and semantic order', () => {
  assert.match(pageHeroSource, /export function PageHero\(\{eyebrow, title, sub, actions\}\)/);
  assertPageHeroSemantics(pageHeroSource);
  assert.throws(
    () => assertPageHeroSemantics(pageHeroSource.replace('<header className="ui-page-hero">', '<div className="ui-page-hero">')),
    /Missing PageHero semantic marker/,
  );
});

test('The standing shell is branded 42 and audience neutral', () => {
  assert.match(frontendHtml, /<title>42 · Open Intelligence<\/title>/);
  assert.match(frontendHtml, /<meta name="description" content="42 Open Intelligence:/);
  assert.doesNotMatch(frontendHtml, /PULSE|Gen-Z|Gen Z/);
  assert.match(passcodeSource, /<main className="gate-v4 oi-product"/);
  assert.match(passcodeSource, /<p><strong>42<\/strong><span>Ogilvy Intelligence<\/span><\/p>/);
  assert.match(passcodeSource, /<h1 id="gate-title">Open cultural intelligence\.<\/h1>/);
  assert.doesNotMatch(passcodeSource, /four-dot Google|Google × Ogilvy|Google x Ogilvy/);
  assert.doesNotMatch(passcodeSource, /PULSE|Gen-Z|Gen Z/);
  assert.match(appSource, /<OgilvyShell/);
  assert.match(todaySource, /<RedThreadBriefing/);
});

test('Visible question and topic surfaces do not invent an audience', () => {
  assert.match(askSource, /Reading the live signal · /);
  assert.match(askSource, /Back to Today/);
  assert.doesNotMatch(askSource, /Back to PULSE/);
  assert.doesNotMatch(askSource, /Gen-Z|Gen Z/);
  assert.match(chatSource, /What is moving in ZA\?/);
  assert.doesNotMatch(chatSource, /Gen-Z|Gen Z/);
  assert.match(topicSource, /Cited voices from the feed/);
  assert.match(topicSource, /turn this signal into participation/);
  assert.doesNotMatch(topicSource, /what Gen Z is saying|get Gen Z creating/);
  assert.doesNotMatch(viewsSource, /WHAT GEN Z IS SAYING|get Gen Z creating|North star: get Gen Z/);
  assert.doesNotMatch(viewsSource, /Skew younger|\byouth\b|\b16\s*(?:-|to)\s*19\b/i);
});

test('Copied brief stays audience neutral at the executable boundary', async () => {
  assert.match(viewsSource, /import \{briefText\} from '\.\/briefText\.js';/);
  const {briefText} = await import('../frontend/src/briefText.js');
  const output = briefText({
    topic: 'Street football',
    regionName: 'South Africa',
    region: 'ZA',
    momentum: 'rising',
    score: 0.72,
    mentions: 84,
    creators: 9,
    sources: 3,
    voices: [['TikTok', 'Pickup games are taking over the block', '2h']],
    brief: {
      trend: 'Local pickup football clips are rising.',
      relevance: 'Three source channels carry the behaviour.',
      opportunity: 'Give the format a participatory mechanic.',
      idea: {tool: 'Lyria', text: 'Score a local match remix.'},
      prompt: {lyria: 'Percussive local football anthem.'},
    },
  });
  const normalized = output.normalize('NFKC').toLowerCase().replace(/[‐‑‒–—―]/g, '-');

  assert.match(output, /^42 BRIEF · STREET FOOTBALL · South Africa \(ZA\)/);
  assert.match(output, /ON THE GROUND · CITED VOICES/);
  assert.match(output, /turn this signal into participation/);
  assert.match(output, /· 42 · Ogilvy Intelligence$/);
  assert.doesNotMatch(output, /Google × Ogilvy|Google x Ogilvy/);
  assert.doesNotMatch(normalized, /\bgen(?:\s+|-)z\b|\byouth\b|\b18\s*(?:-|to)\s*24\b|\bcore audience\b/);
});

test('Topics fails closed when demographic measurement is not connected', () => {
  assert.match(intelSource, /Audience measurement not connected/);
  assert.match(intelSource, /42 will not infer age or gender from platform behaviour/);
  assert.match(intelSource, /source, window, sample, method and confidence/);
  assert.match(intelSource, /className="preveal m-stack" role="status"/);
  assert.doesNotMatch(intelSource, /Gen Z · ages|core PULSE serves|oldest Gen Z|young-audience block/);
  assert.doesNotMatch(intelSource, /<Audience dem=\{d\.demographics\}/);
});

test('Visible product identity uses the current shell while compatibility routes stay stable', () => {
  assert.match(appSource, /<OgilvyShell/);
  assert.doesNotMatch(appSource, /<CommandBar/);
  assert.match(mapSource, /\{label: 'Today', route: '#\/pulse'/);
  assert.doesNotMatch(mapSource, /\{label: 'Pulse', route: '#\/pulse'/);
  assert.match(mapSource, /Predictions are labelled where shown/);
  assert.doesNotMatch(mapSource, /Nothing is a forecast/);
  assert.match(boardSource, /anywhere in 42/);
  assert.doesNotMatch(boardSource, /anywhere in PULSE/);
  assert.match(chatSource, />42 analyst<\/div>/);
  assert.match(chatSource, /grounded in retrieved evidence/);
  assert.doesNotMatch(chatSource, />PULSE analyst<\/div>|grounded in the engine and/);
  assert.match(viewsSource, /data-screen-label="Method · how 42 reads"/);
  assert.match(viewsSource, /42 reads the latest completed pipeline run/);
  assert.match(viewsSource, /Predictions and inferred states are labelled when they appear/);
  assert.doesNotMatch(viewsSource, /how PULSE listens|PULSE reads the Trends Engine|Nothing is a forecast/);
});

test('Research and topic presentation hides retired vendor branding', () => {
  assert.match(topicSource, /<span>Signal brief<\/span>/);
  assert.match(topicSource, /Social mood · monitored source/);
  assert.doesNotMatch(topicSource, /<span>Prompt Pulse<\/span>|audience trend from/);
  assert.match(consoleSource, /Fast read across retrieved evidence/);
  assert.match(consoleSource, /Back to Today/);
  assert.doesNotMatch(consoleSource, /Back to Pulse/);
  assert.match(researchDocSource, /connected evidence sources and search behaviour/);
  assert.match(researchDocSource, /A monitored source was unavailable/);
  assert.doesNotMatch(researchDocSource, /timed out/);
  assert.doesNotMatch(researchLibSource, /Independent corroboration/);
  assert.match(researchLibSource, /a\.download = '42-brief-'/);
  assert.doesNotMatch(researchLibSource, /corroboration|a\.download = 'pulse-brief-'/);
});

test('PageHero is a flat editorial lead rail', () => {
  const hero = rule('.ui-page-hero');
  assert.match(hero, /display:\s*grid;/);
  assert.match(hero, /grid-template-columns:\s*minmax\(0,\s*1fr\)\s+max-content;/);
  assert.match(hero, /border-(?:top|bottom):\s*1px\s+solid\s+var\(--(?:line|hairline)\);/);
  assert.doesNotMatch(hero, /background|border-radius|box-shadow|text-shadow|filter|gradient/i);

  const heroRules = uiCss.slice(uiCss.indexOf('.ui-page-hero {'), uiCss.indexOf('.ui-page-aside {'));
  assert.doesNotMatch(heroRules, /#[0-9a-f]{3,8}\b|\b(?:rgb|hsl|oklch)\(/i);
  assert.match(rule('.ui-page-hero-eyebrow > span:empty'), /display:\s*none\s*!important;/);
});

test('PageHero uses the admitted editorial measures', () => {
  const title = rule('.ui-page-hero-title');
  assert.match(title, /font-size:\s*clamp\(40px,\s*[^,]+,\s*72px\);/);
  assert.match(title, /max-width:\s*18ch;/);
  assert.match(rule('.ui-page-hero-sub'), /max-width:\s*min\(68ch,\s*100%\);/);
});

test('PageHero actions meet the target size and follow copy on mobile', () => {
  const targets = rule('.ui-page-hero-actions :is(a, button, input, select)');
  assert.match(targets, /min-width:\s*48px;/);
  assert.match(targets, /min-height:\s*48px;/);
  const inlineTargets = rule('.ui-page-hero-sub :is(a, button, input, select)');
  assert.match(inlineTargets, /min-width:\s*48px;/);
  assert.match(inlineTargets, /min-height:\s*48px;/);
  assert.match(rule('.ui-page-hero-sub a'), /display:\s*inline-flex;/);

  const mobile = blockAfter(uiCss, '@media (max-width: 640px)');
  assert.match(rule('.ui-page-hero', mobile), /grid-template-columns:\s*minmax\(0,\s*1fr\);/);
  assert.match(rule('.ui-page-hero-actions', mobile), /grid-row:\s*2;/);
});

test('Voices is one source-ordered semantic ladder with admitted editorial roles', () => {
  assert.match(voicesBoardSource, /const rankedRows = all\.map\(\(creator, sourceIndex\) => \(\{creator, sourceIndex\}\)\);/);
  assert.match(voicesBoardSource, /const rows = needle\s*\? rankedRows\.filter/);
  assert.doesNotMatch(voicesBoardSource, /\.sort\(/);
  assert.match(voicesBoardSource, /const variant = sourceIndex === 0 \? 'lead' : sourceIndex < 3 \? 'supporting' : 'ledger';/);
  assertVoicesLadderSemantics(voicesBoardSource);
  assert.doesNotMatch(voicesBoardSource, /tierBreak|11 to 25|26 to/);
  assert.throws(
    () => assertVoicesLadderSemantics(voicesBoardSource.replace('<ol className="voices-ladder"', '<div className="voices-ladder"')),
    /voices-ladder/,
  );
});

test('Voices keeps source facts and actions while each missing value hides locally', () => {
  assert.match(voicesBoardSource, /c\.handle && \(/);
  assert.match(voicesBoardSource, /href=\{creatorHref\(c\.handle\)\}/);
  assert.match(voicesBoardSource, /aria-label=\{'Open @' \+ c\.handle \+ ', ranked ' \+ rank\}/);
  assert.match(voicesBoardSource, /aria-label=\{'Open @' \+ c\.handle\}/);
  assert.match(voicesBoardSource, />@\{c\.handle\}<\/span>/);
  assert.match(voicesBoardSource, /c\.platform && <PlatformGlyph platform=\{c\.platform\} \/>/);
  assert.match(voicesBoardSource, /c\.market && <MarketChip market=\{c\.market\} \/>/);
  assert.equal((voicesBoardSource.match(/<MarketChip market=\{c\.market\} \/>/g) || []).length, 1);
  assert.match(
    voicesBoardSource,
    /<span className="voices-identity-line">\s*\{c\.platform && <PlatformGlyph platform=\{c\.platform\} \/>\}\s*\{c\.handle && \(\s*<a[^>]+>\s*<span className="voices-handle">@\{c\.handle\}<\/span>\s*<\/a>\s*\)\}\s*\{c\.market && <MarketChip market=\{c\.market\} \/>\}/,
  );
  assert.match(voicesBoardSource, /c\.posts != null && <span className="voices-activity">\{c\.posts\} posts · activity<\/span>/);
  assert.match(voicesBoardSource, /c\.reach != null && \(/);
  assert.match(voicesBoardSource, /\{human\(c\.reach\)\} <span>engagement · 30d<\/span>/);
  assert.match(voicesBoardSource, /!!\(c\.topics \|\| \[\]\)\.length && \(/);
  assert.match(voicesBoardSource, /\{sp && \(\s*<div className="voices-sparkline" aria-hidden="true">/);
  assert.match(voicesBoardSource, /aria-label=\{\(on \? 'Remove ' : 'Track '\) \+ '@' \+ c\.handle\}/);
  assert.match(voicesBoardSource, /aria-pressed=\{on\}/);
});

test('Voices hierarchy is full-width lead, paired support, then compact ledger', () => {
  assert.match(rule('.voices-ladder', voicesCss), /display:\s*grid;/);
  assert.match(rule('.voices-ladder', voicesCss), /grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\);/);
  assert.match(rule('.voices-entry-lead', voicesCss), /grid-column:\s*1\s*\/\s*-1;/);
  assert.match(rule('.voices-entry-ledger', voicesCss), /grid-column:\s*1\s*\/\s*-1;/);
  assert.match(rule('.voices-entry-lead .voices-handle', voicesCss), /font-size:\s*clamp\(32px,/);
  assert.match(rule('.voices-entry-supporting .voices-handle', voicesCss), /font-size:\s*clamp\(23px,/);
  assert.match(rule('.voices-entry-ledger', voicesCss), /padding:\s*10px\s+14px;/);
});

test('Voices fits 390px with 48px targets and no hover-only content', () => {
  assert.doesNotMatch(voicesCss, /44px/);
  assert.match(voicesBoardSource, /<div className="voices-page" style=/);
  const targets = rule('.voices-page :is(a, button, input)', voicesCss);
  assert.match(targets, /min-width:\s*48px;/);
  assert.match(targets, /min-height:\s*48px;/);
  assert.match(rule('.voices-page', voicesCss), /min-width:\s*0;/);
  assert.match(rule('.voices-page', voicesCss), /max-width:\s*100%;/);
  assert.match(rule('.app:has(.voices-page)', voicesCss), /overflow-x:\s*clip;/);
  assert.match(rule('.voices-handle', voicesCss), /overflow-wrap:\s*anywhere;/);
  assert.match(rule('.voices-entry', voicesCss), /min-width:\s*0;/);
  assert.match(rule('.voices-entry', voicesCss), /max-width:\s*100%;/);

  const mobile = blockAfter(voicesCss, '@media (max-width: 640px)');
  assert.match(rule('.voices-ladder', mobile), /grid-template-columns:\s*minmax\(0,\s*1fr\);/);
  assert.match(rule('.voices-entry', mobile), /grid-column:\s*1;/);
  assert.match(rule('.voices-filter', mobile), /min-width:\s*0\s*!important;/);
  assert.match(rule('.voices-filter', mobile), /width:\s*100%;/);
  assert.throws(
    () => assert.match(targets.replace('min-height: 48px;', 'min-height: 44px;'), /min-height:\s*48px;/),
    /input did not match/,
  );
  assert.doesNotMatch(voicesBoardSource, /onMouseEnter|onMouseLeave/);
});

test('primary jobs lead to the approved routes', () => {
  assert.deepEqual(PRIMARY_NAV, [
    {label: 'Briefing', path: '/pulse'},
    {label: 'Discover', path: '/explore'},
    {label: 'Compare', path: '/compare'},
    {label: 'Build', path: '/console'},
    {label: 'Fieldwork', path: '/fieldwork'},
  ]);
  // Fieldwork appears once. Repeating it in utility navigation would make the
  // same workspace look like two different destinations.
  assert.equal(PRIMARY_NAV.filter((item) => item.label === 'Fieldwork').length, 1);
  assert.ok(!UTILITY_NAV.some((item) => item.label === 'Fieldwork'));
});

test('secondary destinations live in one utility collection', () => {
  assert.deepEqual(UTILITY_NAV.map(({label}) => label), [
    'Method', 'Source Lab', 'Historical', 'Network', 'Lexicon', 'My board', 'Browse', 'Journey map',
  ]);
});

test('Discover is owned by the canonical Explore route', () => {
  assert.match(appSource, /route === 'explore' && <ExplorePage/);
  assert.doesNotMatch(appSource, /DiscoverPage|route === 'discover'/);
});

test('Today keeps the footer out of the loading geometry', () => {
  assert.equal(redesign.showFooter('pulse', 'loading'), false);
  assert.equal(redesign.showFooter('pulse', 'ready'), true);
  assert.equal(redesign.showFooter('pulse', 'error'), true);
  assert.equal(redesign.showFooter('explore', 'loading'), true);
});

test('saved route aliases still resolve to a supported view', () => {
  const aliases = ['pulse', 'explore', 'compare', 'console', 'method', 'network', 'lexicon', 'board', 'browse', 'map', 'seeds', 'seedpath', 'topics', 'voices', 'listen'];
  for (const route of aliases) assert.equal(normalizeView(route), route);
  assert.equal(normalizeView('discover'), 'pulse');
  assert.equal(normalizeView(''), 'pulse');
  assert.equal(normalizeView('unknown'), 'pulse');
});

test('capacity overload is identified without replacing the user input', () => {
  const error = Object.assign(new Error('The desk is at capacity. Try again shortly.'), {
    code: 'capacity_busy', retryAfterSeconds: 30,
  });
  assert.deepEqual(capacityFailure(error, 'amapiano'), {
    busy: true,
    message: 'The desk is at capacity. Try again shortly.',
    retryAfterSeconds: 30,
    input: 'amapiano',
  });
  assert.equal(capacityFailure(new Error('offline'), 'amapiano'), null);
});

test('capacity API payload becomes a retryable application error', () => {
  assert.deepEqual(apiFailureDetails(429, {
    error: 'capacity_busy',
    message: 'The desk is at capacity. Try again shortly.',
    retry_after_seconds: 30,
  }, '30'), {
    message: 'The desk is at capacity. Try again shortly.',
    code: 'capacity_busy',
    retryAfterSeconds: 30,
  });
});

test('signal evidence states cited posts and source channels or honest absence', () => {
  assert.equal(signalEvidence({social_refs: ['a', 'b'], platforms: ['TikTok', 'News']}), '2 cited posts · 2 source channels');
  assert.equal(signalEvidence({}), 'Cited-post and source-channel counts unavailable');
});

test('signal evidence counts unique live desk receipts', () => {
  assert.equal(signalEvidence({
    receipts: [
      {id: 'post-1', url: 'https://example.test/1'},
      {id: 'post-1', url: 'https://example.test/duplicate'},
      {url: 'https://example.test/2'},
      'https://example.test/2',
      ['TikTok', '@creator', 'quoted text', '9k engagement', '3h', 'https://example.test/3'],
      ['TikTok', '@creator', 'quoted text', '9k engagement', '3h', 'https://example.test/3'],
    ],
    platforms: ['TikTok', 'TikTok', 'News'],
  }), '3 cited posts · 2 source channels');
});

test('lead reading maps current desk relevance and opportunity fields', () => {
  assert.deepEqual(leadReading({
    why: 'Volume rose across the completed run.',
    brief: {relevance: 'The behaviour is entering daily routines.', opportunity: 'Test it in a creator brief.'},
  }), {
    changed: 'Volume rose across the completed run.',
    matters: 'The behaviour is entering daily routines.',
    nextMove: 'Test it in a creator brief.',
  });
  assert.equal(leadReading({brief: {idea: {text: 'Build from the observed format.'}}}).nextMove, 'Build from the observed format.');
});

test('Today metric labels name summed engagement and signal-index delta units', () => {
  assert.deepEqual(todayMetricLabels(0.18), {
    currentSignal: 'Current signal',
    engagement30d: 'Summed engagement · 30d',
    boardEngagement30d: 'Summed engagement · 30d',
    delta: '▲ 0.180 index change · 0 to 1',
  });
});

test('validation detail remains structured for API normalization', () => {
  const detail = [{loc: ['body', 'market'], msg: 'Field required'}, {loc: ['body', 'query'], msg: 'Too short'}];
  assert.deepEqual(apiFailureDetails(422, {detail}), {detail});
  assert.equal(normalizeApiDetail(detail), 'Field required; Too short');
});

test('rising and weakening signals come from observed direction only', () => {
  const topics = [
    {id: 'a', momentum: 'Rising'},
    {id: 'b', delta: 0.2},
    {id: 'c', momentum: 'Cooling'},
    {id: 'd', delta: -0.1},
    {id: 'e', momentum: 'Steady'},
  ];
  assert.deepEqual(splitMomentum(topics, 3), {rising: topics.slice(0, 2), weakening: topics.slice(2, 4)});
});

const MUTATIONS = [
  {
    name: 'admit_id_alone', marker: 'curated, seed, and incomplete identities fail closed', file: 'frontend/src/explore.jsx', replacements: [
      ["const SIGNAL_ID = /^sig_[0-9a-f]{64}$/;", "const SIGNAL_ID = /^.+$/;"],
      ["  const signalId = boundedText(sourceField(row, 'signal_id'), 68);", "  const signalId = boundedText(sourceField(row, 'signal_id') || sourceField(row, 'id'), 68);"],
    ],
  },
  {
    name: 'admit_curated_category', marker: 'curated, seed, and incomplete identities fail closed', file: 'frontend/src/explore.jsx', replacements: [
      ["  const signalId = boundedText(sourceField(row, 'signal_id'), 68);", "  const signalId = boundedText(sourceField(row, 'signal_id'), 68) || 'sig_' + '0'.repeat(64);"],
      ["  const discoveryMode = boundedText(sourceField(row, 'discovery_mode'), 32);", "  const discoveryMode = boundedText(sourceField(row, 'discovery_mode'), 32) || 'phrase';"],
      ["  const declaredReadiness = boundedText(sourceField(row, 'evidence_state'), 32);", "  const declaredReadiness = boundedText(sourceField(row, 'evidence_state'), 32) || 'unchecked';"],
    ],
  },
  {
    name: 'accept_manual_mode', marker: 'every approved mode and readiness is admitted from root or nested signal', file: 'frontend/src/explore.jsx', replacements: [
      ["  'cooccurrence', 'embedding', 'cross_platform',\n]);", "  'cooccurrence', 'embedding', 'cross_platform', 'manual',\n]);"],
    ],
  },
  {
    name: 'accept_complete_readiness', marker: 'every approved mode and readiness is admitted from root or nested signal', file: 'frontend/src/explore.jsx', replacements: [
      ["export const READINESS_STATES = Object.freeze(['ready', 'thin', 'contradictory', 'unchecked']);", "export const READINESS_STATES = Object.freeze(['ready', 'thin', 'contradictory', 'unchecked', 'complete']);"],
    ],
  },
  {
    name: 'sort_engine_order', marker: 'duplicate IDs collapse to the first engine row without sorting', file: 'frontend/src/explore.jsx', replacements: [
      ["  return {signals, diagnostic: {duplicateCount}};", "  signals.sort((a, b) => b.title.localeCompare(a.title));\n  return {signals, diagnostic: {duplicateCount}};"],
    ],
  },
  {
    name: 'show_stale_rows', marker: 'state precedence is error, loading, stale, no discovery, ready', file: 'frontend/src/explore.jsx', replacements: [
      ["  if (freshness && freshness.status === 'amber'){", "  if (false && freshness && freshness.status === 'amber'){"],
    ],
  },
  {
    name: 'declared_ready_without_receipts_model', marker: 'no normalized receipt forces unchecked proof', file: 'frontend/src/explore.jsx', replacements: [
      ["    readiness: receipts.length ? declaredReadiness : 'unchecked',", "    readiness: declaredReadiness,"],
    ],
  },
  {
    name: 'fallback_seed_fixture_archive', marker: 'loading and a complete current desk-shaped row render honest non-ready states', file: 'frontend/src/explore.jsx', replacements: [
      ["  if (!admitted.signals.length) return {state: 'no_discovery', diagnostic: admitted.diagnostic};", "  if (!admitted.signals.length) return {state: 'ready', signals: [{signalId: 'sig_' + 'f'.repeat(64), title: 'Archive fallback', discoveryMode: 'phrase', whyNow: 'Unavailable', response: 'Unavailable', receipts: [], receiptCount: 0, readiness: 'unchecked', observation: null}], diagnostic: admitted.diagnostic};"],
    ],
  },
  {
    name: 'render_internal_score_copy', marker: 'ready lead contains only admitted editorial fields and no retired copy', file: 'frontend/src/explore.jsx', replacements: [
      ["            proof={proof}", "            proof={`Graph score ${proof}`}"],
    ],
  },
  {
    name: 'render_retired_discover_route', marker: 'legacy direct hash canonicalizes and Browse remains its search utility', file: 'frontend/src/router.js', replacements: [
      ["  return /^#\\/?discover(?:[/?]|$)/.test(source) ? '#/explore' : source;", "  return source;"],
    ],
  },
  {
    name: 'count_empty_receipt', marker: 'receipt normalizer rejects malformed values and freezes exact six-string tuples', file: 'frontend/src/explore.jsx', replacements: [
      ["  if (!id) return null;", "  if (false && !id) return null;"],
    ],
  },
  {
    name: 'declared_ready_without_receipts_render', marker: 'missing Why Now, response, receipts, and observation window stay unavailable', file: 'frontend/src/explore.jsx', replacements: [
      ["            response={selected.response}\n            readiness={selected.readiness}", "            response={selected.response}\n            readiness=\"ready\""],
    ],
  },
  {
    name: 'invent_observation_start', marker: 'missing Why Now, response, receipts, and observation window stay unavailable', file: 'frontend/src/explore.jsx', replacements: [
      ["  const start = boundedText(sourceField(row, 'observation_start'));", "  const start = boundedText(sourceField(row, 'observation_start') || sourceField(row, 'date'));"],
    ],
  },
  {
    name: 'move_selection_without_focus', marker: 'selection preserves URL, focuses the lead, and market reset returns to first', file: 'frontend/src/explore.jsx', replacements: [
      ["    try { heading.focus({preventScroll: true}); } catch { heading.focus?.(); }", "    return heading;"],
    ],
  },
  {
    name: 'remove_drawer_focus_restoration', marker: 'mounted Escape close restores the exact Proof invoker after drawer autoFocus', file: 'frontend/src/explore.jsx', replacements: [
      ["      if (invoker?.isConnected && typeof invoker.focus === 'function') invoker.focus();", "      return invoker;"],
    ],
  },
  {
    name: 'mobile_target_below_48', marker: 'route CSS has zero overflow, 48 pixel controls, and reduced-motion overrides', file: 'frontend/src/ogilvy-intelligence.css', replacements: [
      [".explore-page :is(a, button) {\n  min-width: 48px;\n  min-height: 48px;\n}", ".explore-page :is(a, button) {\n  min-width: 48px;\n  min-height: 44px;\n}"],
    ],
  },
  {
    name: 'retain_reduced_motion', marker: 'route CSS has zero overflow, 48 pixel controls, and reduced-motion overrides', file: 'frontend/src/ogilvy-intelligence.css', replacements: [
      ["  .explore-selection {\n    transition: none;\n  }", "  .explore-selection {\n    transition: transform 180ms var(--oi-ease);\n  }"],
    ],
  },
  {
    name: 'shadow_valid_receipt_identity', marker: 'receipt identity and URL candidates fall through shadowing invalid values', file: 'frontend/src/explore.jsx', replacements: [
      ["  const id = firstBoundedText(256, item.id, item.evidence_id, item.receipt_id, item.row_id) || url;", "  const id = firstText(item.id, item.evidence_id, item.receipt_id, item.row_id) || url;"],
      ["  if (!id) return null;", "  if (!id || id.length > 256) return null;"],
    ],
  },
  {
    name: 'shadow_valid_receipt_url', marker: 'receipt identity and URL candidates fall through shadowing invalid values', file: 'frontend/src/explore.jsx', replacements: [
      ["  const url = firstAbsoluteHttpUrl(item.url, item.source_url, item.uri);", "  const url = absoluteHttpUrl(item.url || item.source_url || item.uri);"],
    ],
  },
];

function applyMutation(source, mutation){
  let mutated = source;
  for (const [before, after] of mutation.replacements){
    const first = mutated.indexOf(before);
    assert.notEqual(first, -1, `${mutation.name}: mutation target missing`);
    assert.equal(mutated.indexOf(before, first + before.length), -1, `${mutation.name}: mutation target is ambiguous`);
    mutated = mutated.slice(0, first) + after + mutated.slice(first + before.length);
  }
  assert.notEqual(mutated, source, `${mutation.name}: mutation made no change`);
  return mutated;
}

function mutationOutput(result){
  return `${result.stdout || ''}\n${result.stderr || ''}`;
}

function assertSpawnHealthy(result, label){
  if (result.error) throw result.error;
  assert.notEqual(result.status, null, `${label} returned null status`);
}

function assertHealthyBaseline(result){
  assertSpawnHealthy(result, 'mutation baseline');
  assert.equal(result.status, 0, `mutation baseline failed\n${mutationOutput(result)}`);
}

function assertMutationKilled(result, mutation){
  assertSpawnHealthy(result, mutation.name);
  assert.notEqual(result.status, 0, `${mutation.name} survived\n${mutationOutput(result)}`);
  const escapedMarker = mutation.marker.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  assert.match(
    mutationOutput(result),
    new RegExp(`\\(fail\\)[^\\r\\n]*${escapedMarker}`),
    `${mutation.name} failed outside its named test marker: ${mutation.marker}`,
  );
}

test('mutation infrastructure rejects spawn errors, null status, and missing named failures', () => {
  assert.throws(
    () => assertMutationKilled({error: new Error('spawn failed'), status: null, stdout: '', stderr: ''}, {name: 'broken', marker: 'named test'}),
    /spawn failed/,
  );
  assert.throws(
    () => assertMutationKilled({status: null, stdout: '', stderr: ''}, {name: 'null-status', marker: 'named test'}),
    /null status/,
  );
  assert.throws(
    () => assertMutationKilled({status: 1, stdout: 'unrelated failure', stderr: ''}, {name: 'wrong-failure', marker: 'named test'}),
    /named test/,
  );
  assert.throws(
    () => assertHealthyBaseline({status: 1, stdout: 'baseline failure', stderr: ''}),
    /mutation baseline failed/,
  );
  assert.doesNotThrow(() => assertHealthyBaseline({status: 0, stdout: 'healthy', stderr: ''}));
  assert.doesNotThrow(() => assertMutationKilled(
    {status: 1, stdout: '(fail) suite > named test', stderr: ''},
    {name: 'expected', marker: 'named test'},
  ));
});

test('every Open Discover mutant names the focused test that must kill it', () => {
  for (const mutation of MUTATIONS) assert.ok(mutation.marker, `${mutation.name} has no failing test marker`);
});

test('Open Discover mutation matrix kills every approved contract mutation', {timeout: 120000}, () => {
  const root = fileURLToPath(new URL('..', import.meta.url));
  const temp = mkdtempSync(join(tmpdir(), 'lp-open-discover-mutations-'));
  const tempFrontend = join(temp, 'frontend');
  const sourceFrontend = join(root, 'frontend');
  const bunCandidate = join(process.env.USERPROFILE || '', '.bun', 'bin', 'bun.exe');
  const bun = existsSync(bunCandidate) ? bunCandidate : 'bun';
  mkdirSync(tempFrontend, {recursive: true});
  cpSync(join(sourceFrontend, 'src'), join(tempFrontend, 'src'), {recursive: true});
  symlinkSync(join(sourceFrontend, 'node_modules'), join(tempFrontend, 'node_modules'), 'junction');
  try {
    const runFocused = () => spawnSync(bun, ['test', 'frontend/src/ui/__tests__/explore-contract.test.jsx'], {
      cwd: temp,
      encoding: 'utf8',
      timeout: 30000,
      windowsHide: true,
    });
    const baseline = runFocused();
    assertHealthyBaseline(baseline);
    process.stdout.write('MUTATION BASELINE HEALTHY\n');
    for (const mutation of MUTATIONS){
      const target = join(temp, mutation.file);
      const original = readFileSync(target, 'utf8');
      try {
        writeFileSync(target, applyMutation(original, mutation), 'utf8');
        const result = runFocused();
        assertMutationKilled(result, mutation);
        process.stdout.write(`MUTATION KILLED ${mutation.name}\n`);
      } finally {
        writeFileSync(target, original, 'utf8');
      }
    }
  } finally {
    rmSync(temp, {recursive: true, force: true});
  }
});
