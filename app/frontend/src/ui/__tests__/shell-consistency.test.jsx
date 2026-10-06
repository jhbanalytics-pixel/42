/* Shell consistency, 2 October 2026. The More pages read as the 42 pages do:
   one gutter, one title system (the menu's own noun as the h1 with a muted
   sentence under it), one tab look, one empty state, one secondary button and
   a plain stage line instead of a boxed wizard. Each test holds one of those. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import React from 'react';
import postcss from 'postcss';

GlobalRegistrator.register();

const {renderToStaticMarkup} = await import('react-dom/server');
const {RAIL_MORE} = await import('../../rail42.jsx');
const {SeedsPage} = await import('../../seeds.jsx');
const {LexiconPage} = await import('../../lexicon.jsx');
const {ListenPage} = await import('../../listen.jsx');
const {NetworkGraph} = await import('../../network.jsx');
const {BoardPage} = await import('../../board.jsx');
const {BrowsePage, MethodPage} = await import('../../views.jsx');
const {SchedulesPage42} = await import('../../schedules42.jsx');
const {SkinsPage42} = await import('../../skins42.jsx');
const {LoopRail} = await import('../../loopRail.jsx');
const {InvestigationsPage} = await import('../../investigations42.jsx');

afterAll(() => { GlobalRegistrator.unregister(); });

const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');
const dom = (element) => {
  const box = document.createElement('div');
  box.innerHTML = renderToStaticMarkup(element);
  return box;
};
const rules = (sheet) => {
  const found = [];
  postcss.parse(read(sheet)).walkRules((rule) => {
    const media = rule.parent && rule.parent.type === 'atrule' ? rule.parent.params : '';
    found.push({selectors: rule.selectors.map((s) => s.trim()), media, decls: Object.fromEntries(rule.nodes.filter((n) => n.type === 'decl').map((n) => [n.prop, n.value]))});
  });
  return found;
};
/* Page port, 3 October 2026: Board, Browse, Listen and Network left More
   (their links open the 42 page that does the same job), so their own pages
   keep the noun the menu gave them. */
const LEFT_MORE = {board: 'Board', browse: 'Browse', listen: 'Listen', network: 'Network'};
const railLabel = (route) => LEFT_MORE[route] || RAIL_MORE.find((item) => item.routes.includes(route)).label;

/* Alignment: Today, Ask and History put their title 24px in from the
   workspace on a wide screen and add nothing on a phone. The More pages had
   five different gutters (30px, 32px, 18px, 16px and a centred 1000px
   column), so their titles started at five different lines. */
test('the More pages share one shell gutter, the one the 42 pages use', () => {
  const app = rules('app.css');
  const shell = app.find((rule) => !rule.media && ['.page-shell', '.network-page', '.listen-page', '.lexicon-page'].every((s) => rule.selectors.includes(s)));
  expect(shell, 'one shell rule names every More page container').toBeTruthy();
  expect(shell.decls.padding).toBe('var(--s-6) var(--s-5) var(--s-8)');
  expect(shell.decls['max-width']).toBe('var(--maxw)');
  expect(shell.decls.margin).toBe('0');
  const phone = app.find((rule) => /max-width:\s*479px/.test(rule.media) && rule.selectors.includes('.page-shell') && rule.selectors.includes('.listen-page'));
  expect(phone, 'the phone rule drops the page gutter, as .t42 does').toBeTruthy();
  expect(phone.decls['padding-inline']).toBe('0');
  /* No page sheet restates its own gutter or width on top of the shell. */
  const restated = [];
  for (const sheet of ['styles/graphs.css', 'styles/lexlisten.css']){
    for (const rule of rules(sheet)){
      for (const selector of rule.selectors){
        if (!['.network-page', '.listen-page', '.lexicon-page'].includes(selector)) continue;
        for (const prop of ['padding', 'padding-inline', 'max-width', 'margin']) if (prop in rule.decls) restated.push(`${sheet} ${selector} ${prop}`);
      }
    }
  }
  expect(restated).toEqual([]);
  expect(read('board.jsx')).not.toMatch(/padding: '32px 30px 90px'/);
  expect(read('views.jsx')).not.toMatch(/padding: '32px 30px 90px'/);
  expect(read('seeds.jsx')).not.toMatch(/paddingTop: '24px'/);
});

/* The masthead already draws the line under the header; a second full width
   hairline 40px lower, with nothing above it, read as a stray rule. */
test('the page hero draws no rule of its own and adds no top padding', () => {
  const hero = rules('ui/ui.css').find((rule) => rule.selectors.includes('.ui-page-hero') && !rule.media);
  expect(hero.decls['border-top']).toBe('0');
  expect(hero.decls.padding).toBe('0 0 var(--s-5)');
});

/* Consistency: the page names itself with the word the menu uses, and the
   old headline becomes the muted sentence under it. No eyebrow repeats it. */
const PAGES = [
  /* Page port, 3 October 2026: Seeds now reads 42's own search queue, so its
     lead sentence is what the page now says; the title is still the menu's
     noun and the sentence under it is still checked. */
  /* UX pass, 3 October 2026: each page's line under its title leads with the question the page answers (pageQuestions.js). */
  ['seeds', <SeedsPage session={0} onAuth={() => {}} />, 'What will 42 search for next'],
  /* Page port, 3 October 2026: Lexicon now lists 42's own words and
     hashtags, and its sentence says so. */
  ['lexicon', <LexiconPage region="ZA" session={0} onAuth={() => {}} />, 'Which words and hashtags are people using?'],
  ['method', <MethodPage />, 'How 42 reads the signal'],
  ['board', <BoardPage session={0} onAuth={() => {}} />, 'What you are tracking'],
  ['browse', <BrowsePage region="ZA" />, 'Search what people actually said'],
  ['listen', <ListenPage region="ZA" setRegion={() => {}} session={0} onAuth={() => {}} />, 'Hear it in their words'],
  ['network', <NetworkGraph region="ZA" setRegion={() => {}} session={0} onAuth={() => {}} />, 'Topics and voices'],
  ['schedules', <SchedulesPage42 search="" onAuth={() => {}} />, 'Save a question'],
  ['skins', <SkinsPage42 onAuth={() => {}} />, 'A skin is'],
];

for (const [route, element, sentence] of PAGES){
  test(`${route} is titled with the menu's noun, with its old headline as the sentence under it`, () => {
    const page = dom(element);
    const h1 = page.querySelector('h1');
    expect(h1.textContent).toBe(railLabel(route));
    expect(page.querySelector('.eyebrow, .ui-page-hero-eyebrow')).toBeNull();
    const sub = h1.nextElementSibling;
    expect(sub.tagName).toBe('P');
    expect(sub.textContent.startsWith(sentence)).toBe(true);
  });
}

/* Page subtitles keep a reading measure. Restated 4 October 2026: the cap
   moved from 66ch to the shared --measure (100ch) because at 66ch pages left
   half of a wide screen empty (Albert). It is still a cap, never full width. */
test('every page subtitle on these pages is capped at a reading measure', () => {
  const cap = (sheet, selector) => rules(sheet).filter((rule) => rule.selectors.includes(selector)).map((rule) => rule.decls['max-width']).filter(Boolean).pop();
  expect(cap('app.css', '.page-sub')).toBe('var(--measure)');
  expect(cap('ui/ui.css', '.ui-page-hero-sub')).toBe('var(--measure)');
  expect(cap('styles/history42.css', '.hi42-sub')).toBe('var(--measure)');
  expect(cap('styles/alerts42.css', '.a42 .t42-head > .t42-status')).toBe('var(--measure)');
  for (const [route, element] of PAGES){
    const sub = dom(element).querySelector('h1').nextElementSibling;
    expect(sub.classList.contains('page-sub') || sub.classList.contains('ui-page-hero-sub') || sub.classList.contains('t42-status') || sub.classList.contains('net-lead'), `${route} subtitle carries a capped class`).toBe(true);
  }
});

/* The stage line is three links in one line of words: the page the reader
   is on is set at 600 and carries aria-current, with no boxes and no arrows
   standing in for a wizard. */
test('the Discover, Seed path, Seeds line is three plain links with the current one marked', () => {
  const rail = dom(<LoopRail active="seeds" />);
  const links = [...rail.querySelectorAll('a.loop-stage')];
  expect(links.map((a) => a.getAttribute('href'))).toEqual(['#/explore', '#/seedpath', '#/seeds']);
  expect(rail.querySelector('.loop-rail-arrow, svg, .loop-stage-desc')).toBeNull();
  expect(rail.querySelector('nav.loop-rail').getAttribute('aria-label')).toBeTruthy();
  const css = rules('styles/loop.css');
  const stage = css.find((rule) => rule.selectors.includes('.loop-stage') && !rule.media);
  expect(stage.decls.border).toBe('0');
  expect(stage.decls.background).toBe('none');
  const active = css.find((rule) => rule.selectors.includes('.loop-stage[aria-current="step"]'));
  expect(active.decls['font-weight']).toBe('600');
});

/* Proximity: the seed key reads with the subtitle it explains, as a line of
   words, not a box beside the hero. */
/* Restated, design audit 2 October 2026: a legend for cards that are not on
   screen is noise, so the key now shows only once the seeds list renders.
   The loading render was the old subject and now must have no key; the
   ready render carries the key, still inline in the title column with no
   box. */
test('the seed key is an inline legend under the subtitle, not a box', async () => {
  const loading = dom(<SeedsPage session={0} onAuth={() => {}} />);
  expect(loading.querySelector('.seed-key') !== null, 'no key before the seeds arrive').toBe(false);

  const {createRoot} = await import('react-dom/client');
  const {flushSync} = await import('react-dom');
  const {clearCache} = await import('../../api.js');
  clearCache();
  const realFetch = globalThis.fetch;
  /* Page port, 3 October 2026: the ready render is now 42's search queue
     (GET /api/seeds), and the key names its three ways of picking a search
     in place of the old strength labels. Same checks on the same element. */
  const f = (value) => ({value, unit: 'searches queued', query_id: 'q_seeds_queue', run_id: null, result_hash: 'sha256:x'});
  const seed = {lane: 'expansion', lane_words: 'Following what is rising', kind: 'topic', label: 'Repair as a shared ritual', item_id: 'abc', href: '#/t/abc?market=ZA', platforms: ['TikTok'], priority: 1, credits: f(1)};
  const payload = {market: 'ZA', market_name: 'South Africa', status: 'ok', message: null, query_ids: ['q_seeds_queue', 'q_seeds_results'], lanes_about: [], notes: [], results: null,
    queue: {seed_date: '2026-10-01', seeds: [seed], lanes: [{lane: 'expansion', words: 'Following what is rising', seeds: f(1), credits: f(1)}], creator_accounts: null, seeds_total: f(1), credits_total: f(1), truncated: false}};
  globalThis.fetch = async () => new Response(JSON.stringify(payload), {status: 200, headers: {'Content-Type': 'application/json'}});
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  try {
    flushSync(() => root.render(<SeedsPage session={0} onAuth={() => {}} />));
    for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
    expect(host.textContent).toContain('Repair as a shared ritual');
    const key = host.querySelector('.seed-key');
    expect(key !== null, 'the ready render carries the key').toBe(true);
    expect(key.closest('.ui-page-hero-text, .seed-hero-text'), 'the key sits in the title column').not.toBeNull();
    expect(key.getAttribute('style') || '').not.toMatch(/border|background/);
  } finally {
    flushSync(() => root.unmount());
    host.remove();
    globalThis.fetch = realFetch;
    clearCache();
  }
});

/* One tab look: Investigations and Listen use the Today tab row, an ink
   underline under the chosen word, with their own ARIA kept. */
test('Listen and Investigations use the Today tab row and keep their semantics', () => {
  const listen = dom(<ListenPage region="ZA" setRegion={() => {}} session={0} onAuth={() => {}} />);
  const chips = [...listen.querySelectorAll('.listen-filter-chip')];
  expect(chips.length).toBeGreaterThan(0);
  for (const chip of chips){
    expect(chip.classList.contains('t42-tab')).toBe(true);
    expect(chip.hasAttribute('aria-pressed')).toBe(true);
    expect(chip.parentElement.classList.contains('t42-tabs')).toBe(true);
  }
  expect(read('listen.jsx')).toContain("import './styles/today42.css'");
  const inv = dom(<InvestigationsPage onAuth={() => {}} />);
  const list = inv.querySelector('[role="tablist"]');
  expect(list.classList.contains('t42-tabs')).toBe(true);
  expect(list.classList.contains('inv42-segmented')).toBe(false);
  for (const tab of list.querySelectorAll('[role="tab"]')) expect(tab.classList.contains('t42-tab')).toBe(true);
  expect(read('investigations42.jsx')).toContain("import './styles/today42.css'");
  /* The pressed state takes the Today mark, ink and not red. */
  const pressed = rules('styles/lexlisten.css').find((rule) => rule.selectors.includes('.listen-filter-chip.t42-tab[aria-pressed="true"]'));
  expect(pressed.decls.color).toBe('var(--ink)');
  expect(pressed.decls['font-weight']).toBe('600');
});

/* One empty state: a short bold title, one sentence, one bordered action,
   and no arrow glyph doing the work of a word. */
test('Board empty state offers one bordered action with no arrow glyph', () => {
  const source = read('board.jsx');
  const block = source.slice(source.indexOf('title="Nothing pinned yet"'), source.indexOf('title="Nothing pinned yet"') + 600);
  expect(block).not.toMatch(/→|&rarr;/);
  expect((block.match(/label:/g) || []).length).toBe(1);
});

test('Lexicon states sit on the page edge and their action is the bordered button', () => {
  const css = rules('app.css');
  const frame = css.find((rule) => rule.selectors.includes('.lexicon-page .state-view'));
  expect(frame.decls.margin).toBe('0');
  // Restated 4 October 2026: the shared prose measure replaced 66ch.
  expect(frame.decls['max-width']).toBe('var(--measure)');
  const action = css.find((rule) => rule.selectors.includes('.lexicon-page .state-view__action'));
  expect(action.decls.border).toBe('1px solid var(--muted)');
  expect(action.decls['min-height']).toBe('var(--target-min)');
});

test('Dossiers empty state is the plain block: title, sentence, then its bordered action', () => {
  const css = rules('styles/dossiers42.css');
  const empty = css.find((rule) => rule.selectors.includes('.dossiers42-empty'));
  expect(empty.decls.display).toBe('block');
  expect(empty.decls['border-bottom']).toBeUndefined();
  const link = css.find((rule) => rule.selectors.includes('.dossiers42 .dossiers42-empty-link'));
  expect(link.decls['margin-left']).toBeUndefined();
  expect(link.decls.border).toBe('1px solid var(--muted)');
  expect(link.decls['font-size']).toBe('var(--type-3)');
});

/* One secondary button: 48px, 16px sides, a 1px muted rule and the 14px
   sans at 600, the legacy-action box the More pages already share. */
test('the secondary buttons on these pages share one box', () => {
  const box = (sheet, selector) => rules(sheet).filter((rule) => rule.selectors.includes(selector) && !rule.media).reduce((all, rule) => ({...all, ...rule.decls}), {});
  for (const [sheet, selector] of [['styles/history42.css', '.hi42-button'], ['styles/dossiers42.css', '.dossiers42-quiet'], ['styles/empty-states.css', '.es-act-ghost']]){
    const decls = box(sheet, selector);
    expect(decls['font-size'], `${selector} size`).toBe('var(--type-3)');
    expect(decls['font-weight'], `${selector} weight`).toBe('600');
    expect(/var\(--muted\)/.test(decls.border || decls['border-color'] || ''), `${selector} rule`).toBe(true);
  }
  const ghost = box('styles/empty-states.css', '.es-act-ghost');
  expect(ghost.color).toBe('var(--ink)');
});

/* Browse reserved a second masthead row for a freshness stamp that never
   comes on a fresh device, which left a 60px empty band on a phone. */
test('the masthead reserves the freshness row only when a stamp is there to show', () => {
  const app = read('App.jsx');
  expect(app).toMatch(/data-reserve-freshness=\{checkedStamp \? 'true' : undefined\}/);
});
