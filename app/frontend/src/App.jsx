/* PULSE · app shell: routing, region, ask flow, brief history */
import {useState, useEffect, useLayoutEffect, useMemo, useRef, useCallback, lazy, Suspense} from 'react';
import {InstrumentShell, StateView} from 'ogilvy-intelligence-design-system';
import {PASS_KEY, REASON_KEY, credential, useApi, clearCache, storedValue} from './api.js';
import {createWatch, fetchAlerts, listInvestigations, listSchedules, sendFeedback} from './api42.js';
import {railSummaryForTopics} from './instrumentAdapters.js';
import {routeStateView} from './instrumentRouteModels.js';
import {decorateAll} from './model.js';
import {PasscodeScreen} from './passcode.jsx';
import {useHashRoute, go, buildTopicHash, buildScopedReadHash} from './router.js';
import {buildWorkbenchPath, parseWorkbenchRoute} from './workbenchRoute.js';
import {normalizeView} from './redesignContract.js';
import {RouteErrorBoundary} from './routeError.jsx';

/* The gate's words for a key that is not accepted (C5 v2 section 13.2). */
const STALE_WORDS = 'Your saved access key is no longer valid. Enter the current key.';
const WRONG_WORDS = 'Access not recognised. Check the key and try again.';
const CHECK_WORDS = 'Sign-in could not be checked just now. Try again shortly.';
const SAVE_WORDS = 'This browser would not save the key, so you will be asked again after a reload.';

const TodayPage42 = lazy(() => import('./today42.jsx').then((m) => ({default: m.TodayPage42})));
const AskPage = lazy(() => import('./ask42.jsx').then((m) => ({default: m.AskPage})));
const TopicDetail = lazy(() => import('./views.jsx').then((m) => ({default: m.TopicDetail})));
const BrowsePage = lazy(() => import('./views.jsx').then((m) => ({default: m.BrowsePage})));
const MethodPage = lazy(() => import('./views.jsx').then((m) => ({default: m.MethodPage})));
const AskLoadingPanel = lazy(() => import('./ask.jsx').then((m) => ({default: m.AskLoadingPanel})));
const ConsoleWorkbench = lazy(() => import('./ConsoleWorkbench.jsx').then((m) => ({default: m.ConsoleWorkbench})));
const Build42 = lazy(() => import('./build42.jsx').then((m) => ({default: m.Build42})));
const OlderLink42 = lazy(() => import('./olderLink42.jsx').then((m) => ({default: m.OlderLink42})));
const SeedsPage = lazy(() => import('./seeds.jsx').then((m) => ({default: m.SeedsPage})));
const SeedPathPage = lazy(() => import('./seedpath.jsx').then((m) => ({default: m.SeedPathPage})));
const Discover42 = lazy(() => import('./discover42.jsx').then((m) => ({default: m.Discover42})));
const TopicPage42 = lazy(() => import('./topic42.jsx').then((m) => ({default: m.TopicPage42})));
const CoveragePage42 = lazy(() => import('./coverage42.jsx').then((m) => ({default: m.CoveragePage42})));
const AlertsPage42 = lazy(() => import('./alerts42.jsx').then((m) => ({default: m.AlertsPage42})));
const LexiconPage = lazy(() => import('./lexicon.jsx').then((m) => ({default: m.LexiconPage})));
const MapPage = lazy(() => import('./map.jsx').then((m) => ({default: m.MapPage})));
const BoardPage = lazy(() => import('./board.jsx').then((m) => ({default: m.BoardPage})));
const Compare42 = lazy(() => import('./compare42.jsx').then((m) => ({default: m.Compare42})));
const DossiersPage = lazy(() => import('./dossiers42.jsx').then((m) => ({default: m.DossiersPage})));
const DossierPage = lazy(() => import('./dossiers42.jsx').then((m) => ({default: m.DossierPage})));
const SharedDossier = lazy(() => import('./dossiers42.jsx').then((m) => ({default: m.SharedDossier})));
const CreatorPage42 = lazy(() => import('./people42.jsx').then((m) => ({default: m.CreatorPage42})));
const CommunitiesPage42 = lazy(() => import('./people42.jsx').then((m) => ({default: m.CommunitiesPage42})));
const CommunityPage42 = lazy(() => import('./people42.jsx').then((m) => ({default: m.CommunityPage42})));
const HiddenPeoplePage42 = lazy(() => import('./people42.jsx').then((m) => ({default: m.HiddenPeoplePage42})));
const HistoryPage42 = lazy(() => import('./history42.jsx').then((m) => ({default: m.HistoryPage42})));
const HistoryItemPage42 = lazy(() => import('./history42.jsx').then((m) => ({default: m.HistoryItemPage42})));
const InvestigationsPage42 = lazy(() => import('./investigations42.jsx').then((m) => ({default: m.InvestigationsPage})));
const InvestigationPage42 = lazy(() => import('./investigations42.jsx').then((m) => ({default: m.InvestigationPage})));
const SchedulesPage42 = lazy(() => import('./schedules42.jsx').then((m) => ({default: m.SchedulesPage42})));
const SkinsPage42 = lazy(() => import('./skins42.jsx').then((m) => ({default: m.SkinsPage42})));
const SkinPage42 = lazy(() => import('./skins42.jsx').then((m) => ({default: m.SkinPage42})));
const ListenPage = lazy(() => import('./listen.jsx').then((m) => ({default: m.ListenPage})));
const NetworkGraph = lazy(() => import('./network.jsx').then((m) => ({default: m.NetworkGraph})));
const SourceLabWorkspace = lazy(() => import('./sourceLab.jsx').then((m) => ({default: m.SourceLabWorkspace})));
const FieldworkWorkspace = lazy(() => import('./fieldwork.jsx').then((m) => ({default: m.FieldworkWorkspace})));
const HistoricalWorkspace = lazy(() => import('./historicalWorkspace.jsx').then((m) => ({default: m.HistoricalWorkspace})));

/* The 42 rail (rail42.jsx). Its chunk is asked for as this module loads,
   while the fonts the first render waits on are still arriving, so its
   stylesheet and the column it takes are normally in place by the first
   paint. The stylesheet also hides the shell's own rail, so a chunk that
   fails to arrive leaves that rail and every hash working, never a broken
   page. */
const loadRail = () => import('./rail42.jsx').then((m) => ({default: m.Rail42}), () => ({default: () => null}));
const railChunk = typeof document !== 'undefined' ? loadRail() : null;
const Rail42 = lazy(() => railChunk || loadRail());

/* The chunk wait is the package loading frame with a static reserved band,
   the same frame every route state renders, so nothing loops while a route's
   code is on its way. */
const ROUTE_WAIT = Object.freeze({
  state: 'loading',
  title: 'Preparing the workspace',
  task: 'Loading this route',
  body: 'The workspace opens once its code and its reading have arrived.',
  reservedRows: 3,
});

function RouteWait(){
  return (
    <div className="page"><StateView {...routeStateView(ROUTE_WAIT)} /></div>
  );
}

/* Round three, 24 Sept 2026: the sign-in wait is the same frame as the chunk
   wait, but it paints before the shell and its route layer exist, and before
   app.css, so it takes the route layer's entrance from boot.css, the same
   fade the chunk wait takes inside that layer. A quick check then shows no
   flash of the wait, and Chrome, which never counts text first painted at
   opacity 0, does not count the wait's sentence as the largest paint over
   the page that follows it. */
function SignInWait(){
  return (
    <div className="page route-wait-enter"><StateView {...routeStateView(ROUTE_WAIT)} /></div>
  );
}

function SignInUnavailable(){
  return (
    <div className="page route-wait-enter">
      <div className="page-shell">
        <h1>Sign-in unavailable</h1>
        <p role="alert">Sign-in could not be confirmed. Reload to try again.</p>
        <button type="button" className="legacy-action" onClick={() => window.location.reload()}>Reload</button>
      </div>
    </div>
  );
}

/* The route layer, remounted on every route change. Round three, 24 Sept
   2026: the Ask page skips its entrance, so its head and suggestions paint
   from their first frame (Chrome never counts text first painted at opacity
   0 as the largest paint). That is settled once, when the layer mounts. A
   live selector let the entrance start over when the reader moved from Ask to
   Build brief, which keeps the console route and so keeps this layer. */
function RouteLayer({route, research, layerRef, children}){
  const [still] = useState(() => route === 'ask' || (route === 'console' && typeof window !== 'undefined' && consoleShowsAsk(window.location.hash)));
  return (
    <div id="main-content" ref={layerRef} className={'route-enter' + (still ? ' route-still' : '') + (research ? ' research-route' : '')}>
      <RouteErrorBoundary>{children}</RouteErrorBoundary>
    </div>
  );
}

/* Task 65, the geometry reservation. Method reads no desk data. Browse uses
   the desk only for optional search prompts, so it can still open when that
   legacy route is unavailable. Both routes mount while the desk is held and
   paint their own surface. */
const DESK_OPTIONAL = new Set(['browse', 'method']);

/* Routes that require the desk use this frame when its read fails. The routes
   hold no incident receipt, so the mapping renders the read as unavailable. */
function DeskError({code, message, onRetry}){
  return (
    <div className="page"><StateView {...routeStateView({
      state: 'error',
      title: 'The desk could not load',
      body: 'The desk read failed, so this route has nothing to show yet.',
      code,
      message,
      actions: [{id: 'retry-desk', label: 'Try again', onClick: onRetry}],
    })} /></div>
  );
}

/* human label per route, named in the polite live region on hash nav so a
   screen reader announces the view a keyboard user just moved to */
const ROUTE_LABELS = {
  pulse: "Today's desk", ask: 'Ask', browse: 'Browse signals', method: 'Method', console: 'Build',
  seeds: 'Seeds', seedpath: 'Seed Explorer', explore: 'Discover', topic: 'Topic story',
  lexicon: 'Lexicon', map: 'Journey map', board: 'My board', creator: 'Creator profile',
  compare: 'Compare', listen: 'Listen', network: 'Network',
  'source-lab': 'Source Lab', historical: 'Historical', topic42: 'Topic', coverage: 'Coverage', alerts: 'Alerts',
  dossiers: 'Dossiers', dossier: 'Dossier', 'dossier-share': 'Shared dossier',
  creator42: 'Creator', communities: 'Communities', community: 'Community', history: 'History', 'history-item': 'Item history',
  investigations: 'Investigations', investigation: 'Investigation', schedules: 'Scheduled questions',
  skins: 'Skins', skin: 'Skin', 'people-hidden': 'Hidden people',
};

const HOST_TO_INSTRUMENT_ROUTE = Object.freeze({
  pulse: 'briefing',
  explore: 'discover',
  compare: 'compare',
  console: 'build',
  fieldwork: 'fieldwork',
});
const INSTRUMENT_TO_HOST_ROUTE = Object.freeze({
  briefing: 'pulse',
  discover: 'explore',
  compare: 'compare',
  build: 'console',
  fieldwork: 'fieldwork',
});

/* A host route outside the five jobs is no job, so the rail marks nothing
   current on it rather than claiming Fieldwork. */
export function instrumentRouteForHostRoute(route){
  return HOST_TO_INSTRUMENT_ROUTE[route] || null;
}

/* Quiet register, 23 Sept 2026. The rail still links only the five jobs,
   but a reader on a page outside them must see where they are, so the rail
   marks that page: the shell's utility slot carries the one page the reader
   is on, marked current, and nothing else. Round 5 withdrew the utility list
   because sixteen entries overwhelmed the reader; a single "you are here"
   entry brings none of them back. Ask is the console's question page, so on
   Ask the rail marks Ask rather than the Build job. Each label is the page's
   own title. */
const CURRENT_PAGE_LINKS = Object.freeze({
  ask: Object.freeze({id: 'ask', label: 'Ask', href: '/ask'}),
  'source-lab': Object.freeze({id: 'source-lab', label: 'Source Lab', href: '/source-lab'}),
  listen: Object.freeze({id: 'listen', label: 'Listen', href: '/listen'}),
  network: Object.freeze({id: 'network', label: 'Network', href: '/network'}),
  historical: Object.freeze({id: 'historical', label: 'Historical', href: '/historical'}),
});

/* The console shows Ask on work=ask, on a question request, and on a
   question carried in the path, which opens straight onto Ask. */
export function consoleShowsAsk(hashText){
  const text = String(hashText || '');
  const state = parseWorkbenchRoute(text || '#/console');
  if (state.work === 'ask') return true;
  const path = text.replace(/^#\/?/, '').split('?')[0].split('/');
  return state.work === 'landing' && path[0] === 'console' && Boolean(path[1]);
}

/* Build's own page answers the plain console. A question, a stored request
   or a brief link still opens the workbench, which is the page that reads it. */
export function consoleShowsBuild(hashText){
  const text = String(hashText || '') || '#/console';
  return !consoleShowsAsk(text) && parseWorkbenchRoute(text).work === 'landing';
}

/* Where the rail puts the reader: the job it marks, if any, and the one page
   outside the jobs it marks instead. */
export function railPlaceForHostRoute(route, {askOpen = false} = {}){
  if (route === 'ask' || (route === 'console' && askOpen)) return {job: null, currentPage: {...CURRENT_PAGE_LINKS.ask, current: true}};
  const page = Object.hasOwn(CURRENT_PAGE_LINKS, route) && route !== 'ask' ? CURRENT_PAGE_LINKS[route] : null;
  return {job: instrumentRouteForHostRoute(route), currentPage: page ? {...page, current: true} : null};
}

/* The console moves between Ask and Build brief by replacing its hash, which
   fires no hashchange, and announces the move with this event (the
   WORKBENCH_NAV_EVENT of researchLib.jsx), so the rail listens for it too. */
export const WORKBENCH_NAV_EVENT_NAME = 'pulse-workbench-nav';

/* The rail links the five jobs, so the shell only ever hands back a job path
   or the current page's own path. The routes outside the jobs open by hash. */
export function hostRouteForInstrumentRoute(route){
  const key = String(route || '').replace(/^\//, '');
  return INSTRUMENT_TO_HOST_ROUTE[key] || 'fieldwork';
}

/* Old route names still in localStorage or shared links keep working, and
   research is the Build job under its old name. */
const ROUTE_ALIASES = Object.freeze({today: 'pulse', chat: 'console', intel: 'explore', research: 'console'});

/* #/dossiers lists them, #/dossiers/<id> reviews one, and #/d/<id>/<version>
   is the share link to a frozen version. #/creators/<id> is a creator page
   on the 42 API (#/creator/<handle>, the older profile, now says it is
   from an older version),
   #/communities lists communities and #/communities/<id> opens one, and
   #/history/items/<id> is one item's history under #/history.
   #/investigations lists investigations and #/investigations/<id> opens one.
   #/schedules lists scheduled questions; ?q=<question>&market=ZA fills the form.
   #/skins lists client skins and #/skins/<id> opens one.
   #/people/hidden lists the people hidden from the app. */
export function resolveHostRoute(rawView, param){
  if (rawView === 'ask') return 'ask';
  if (rawView === 'people' && (!param || param === 'hidden')) return 'people-hidden';
  if (rawView === 'dossiers') return param ? 'dossier' : 'dossiers';
  if (rawView === 'creators' && param) return 'creator42';
  if (rawView === 'communities') return param ? 'community' : 'communities';
  if (rawView === 'history') return param === 'items' ? 'history-item' : 'history';
  if (rawView === 'investigations') return param ? 'investigation' : 'investigations';
  if (rawView === 'schedules') return 'schedules';
  if (rawView === 'skins') return param ? 'skin' : 'skins';
  if (rawView === 'd') return 'dossier-share';
  if (rawView === 't') return 'topic42';
  if (rawView === 'coverage') return 'coverage';
  if (rawView === 'alerts') return 'alerts';
  return normalizeView(ROUTE_ALIASES[rawView] || rawView);
}

/* The Ask route reads its question from the hash:
   #/ask?q=<question>&market=ZA&item=<item id>&date=YYYY-MM-DD, or follows
   an ask already started elsewhere (a spike, a scheduled answer) with
   #/ask?follow=<ask id>. #/ask?fit=<creator id>&market=NG opens creator fit
   for a creator page. */
export function parseAskQuery(hashText){
  const text = String(hashText || '');
  const at = text.indexOf('?');
  const query = new URLSearchParams(at >= 0 ? text.slice(at + 1) : '');
  const parsed = {q: query.get('q') || '', market: query.get('market') || null, item: query.get('item') || null, date: query.get('date') || null, follow: query.get('follow') || null, fit: query.get('fit') || null};
  /* A question in the address is always a draft, whatever else the address says.
     Nothing in a link can start a paid ask: only the cost confirm can, by
     granting a one-shot token in memory that the Ask page spends (askConsent.js). */
  if (parsed.q) parsed.draft = true;
  /* An investigation's follow-up names the answer it follows. */
  const parent = query.get('parent');
  parsed.parent = parent && /^[A-Za-z0-9_-]{1,128}$/.test(parent) ? parent : null;
  return parsed;
}

/* History opens a past brief at #/today?date=YYYY-MM-DD; anything else in
   date= is ignored and Today opens on its latest brief. */
export function todayDateFromHash(hashText){
  const text = String(hashText || '');
  const at = text.indexOf('?');
  const date = new URLSearchParams(at >= 0 ? text.slice(at + 1) : '').get('date') || '';
  return /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : null;
}

/* A 42 topic page is #/t/<item_id>?market=ZA. A market outside the three
   falls back to the reader's region, and All to South Africa, since a topic
   is read in one market. */
export function topic42Market(hashText, region){
  const market = hashMarket(hashText);
  if (market) return market;
  return ['ZA', 'NG', 'KE'].includes(region) ? region : 'ZA';
}

/* The market a hash names, or null. A community link may name none, and
   the API then finds the community in any market. */
export function hashMarket(hashText){
  const text = String(hashText || '');
  const at = text.indexOf('?');
  const market = String(new URLSearchParams(at >= 0 ? text.slice(at + 1) : '').get('market') || '').toUpperCase();
  return ['ZA', 'NG', 'KE'].includes(market) ? market : null;
}

/* #/console?work=ask was Ask's home before Ask had its own route. The alias
   keeps working and lands on #/ask. A stored question (request=<id>) is an
   older record; legacyRoutes.js sends its link to History. */
export function askAliasTarget(hashText){
  const state = parseWorkbenchRoute(String(hashText || '') || '#/console');
  return state.work === 'ask' && !state.requestId ? '/ask' : null;
}

/* How long a route move waits for its heading to mount before it stops
   looking. Longer than any chunk on the slow path, shorter than a reader's
   patience with a route that shows no heading at all. */
const HEADING_WAIT = 8000;

export function marketScopeForRegion(region){
  return ['ZA', 'NG', 'KE'].includes(region) ? [region] : ['ZA', 'NG', 'KE'];
}

/* The shell toggles one chip at a time and the desk answers one market or
   all three, so a two-market set is read as a move between those shapes.
   From one market a second chip returns the scope to ALL. From ALL every
   chip is checked, so a click unchecks one, and the two left cannot be
   served: the click narrows to the chip the reader touched. */
export function regionForMarketScope(currentRegion, nextScope){
  const ordered = ['ZA', 'NG', 'KE'].filter((market) => Array.isArray(nextScope) && nextScope.includes(market));
  if (ordered.length === 1) return ordered[0];
  if (ordered.length === 3) return 'ALL';
  if (ordered.length === 2){
    if (currentRegion !== 'ALL') return 'ALL';
    return ['ZA', 'NG', 'KE'].find((market) => !ordered.includes(market));
  }
  return currentRegion;
}

/* Communities shows one market, so the header holds one chip. The chip the
   reader touched is the market they want: a second chip moves the page to
   it, and unchecking the only chip leaves the page where it is. */
export function communitiesMarketForScope(currentMarket, nextScope){
  const scope = Array.isArray(nextScope) ? nextScope : [];
  const touched = ['ZA', 'NG', 'KE'].filter((market) => (market === currentMarket) !== scope.includes(market));
  return touched.length === 1 ? touched[0] : currentMarket;
}

/* real relative freshness from the desk payload: "Updated 9h ago", "Updated
   32m ago". Empty when the engine sent no stamp. Minutes and hours are whole
   elapsed units, never rounded up: the shell reads its amber state back out of
   this text from 3 hours, the desk's own edge (FRESHNESS_AMBER_HOURS in
   app/src/api/bq.py), so 2.5 hours must read "2h ago" and not "3h ago". */
export function updatedLabel(freshness){
  const h = freshness && typeof freshness.age_hours === 'number' ? freshness.age_hours : null;
  if (h === null) return '';
  if (h < 1) return 'Updated ' + Math.max(1, Math.floor(h * 60)) + 'm ago';
  if (h < 48) return 'Updated ' + Math.floor(h) + 'h ago';
  return 'Updated ' + Math.round(h / 24) + 'd ago';
}

/* The utility strip's checked time survives the desk load. The desk stamp is
   an age, so it is kept as the moment it names and read back as an age at
   render, which is what the strip shows while the next desk read is still
   on its way and what a reload shows before the first one lands. */
const CHECKED_KEY = 'pulse-checked-at';

export function checkedEpoch(freshness, now = Date.now()){
  const h = freshness && typeof freshness.age_hours === 'number' ? freshness.age_hours : null;
  return h === null ? null : now - h * 3600000;
}

export function checkedLabel(epoch, now = Date.now()){
  if (!Number.isFinite(epoch) || epoch === null) return '';
  return updatedLabel({age_hours: Math.max(0, now - epoch) / 3600000}).replace(/^Updated\s+/, '');
}

function storedCheckedEpoch(){
  const value = Number(storedValue(CHECKED_KEY, ''));
  return Number.isFinite(value) && value > 0 ? value : null;
}

export const DYNAMIC_DISCOVERY_CONTRACT_VERSION = 'desk_dynamic_signal_v2';
const DYNAMIC_DISCOVERY_STATES = new Set(['ready', 'no_discovery']);

/* Discover renders engine-owned dynamic signals and nothing else. Curated desk
   topics are a different contract, so a missing, unsupported or unavailable
   dynamic member becomes a visible error rather than a quiet fall back to the
   curated board, which would read as a completed run that found nothing. */
export function exploreDynamicProps(deskData, loading = false){
  if (loading) return {topics: [], error: null};
  const dynamic = deskData && deskData.dynamic_discovery;
  if (!dynamic || typeof dynamic !== 'object'){
    return {
      topics: [],
      error: {
        code: 'no_dynamic_discovery_member',
        message: 'Open discovery is unavailable from this service.',
      },
    };
  }
  if (dynamic.contract_version !== DYNAMIC_DISCOVERY_CONTRACT_VERSION){
    return {
      topics: [],
      error: {
        code: 'unsupported_contract_version',
        message: 'Open discovery is unavailable because this service reports an unsupported contract.',
      },
    };
  }
  if (!DYNAMIC_DISCOVERY_STATES.has(dynamic.status)){
    const reported = dynamic.error || {};
    return {
      topics: [],
      error: {
        code: reported.code || 'no_released_closed_run',
        message: reported.message || 'Open discovery is unavailable.',
      },
    };
  }
  /* Ready asserts the run completed and observed something, so a missing or
     malformed signal list contradicts the status it arrived with. Reporting
     that as an empty success would be the completed run that found nothing
     this function exists to prevent. no_discovery asserts the opposite, so an
     absent list agrees with it. */
  if (!Array.isArray(dynamic.signals)){
    if (dynamic.status === 'no_discovery') return {topics: [], error: null};
    return {
      topics: [],
      error: {
        code: 'malformed_dynamic_discovery',
        message: 'Open discovery is unavailable because this service reported a run without its signals.',
      },
    };
  }
  return {topics: dynamic.signals, error: null};
}

/* A released signal id (sig_ and 64 hex) names a signal of the released run,
   which the topic API does not hold. The Briefing lead and Discover open one
   at #/topic/<id>, so the topic route reads it from the same released run
   those two render instead of asking /api/topic for it. A malformed market
   selection keeps the topic route's own invalid link frame. */
const RELEASED_SIGNAL_ID = /^sig_[0-9a-f]{64}$/;

export function topicRouteReader(hash){
  return hash && !hash.error && RELEASED_SIGNAL_ID.test(String(hash.param || '')) ? 'released_signal' : 'topic';
}

/* Where the open handler takes a row. A curated topic carries its id at the
   top level; a released run row, which the Briefing's comparisons hand over,
   carries it under signal.signal_id. A generated brief opens in place. */
export function topicOpenPath(t){
  if (!t || typeof t !== 'object' || t.generated) return null;
  if (t.id) return '/topic/' + encodeURIComponent(t.id);
  const signalId = t.signal && typeof t.signal === 'object' ? t.signal.signal_id : null;
  return RELEASED_SIGNAL_ID.test(String(signalId || '')) ? '/topic/' + signalId : null;
}

/* The reduced-motion bridge.

   app.css carries a full [data-motion="off"] suppression and a comment saying
   this file sets the attribute. Nothing ever did, in source or in the built
   bundle, so every rule keyed on it was dead and the operating system
   preference reached only the routes that ship their own media query. This
   connects the two. It reads the preference rather than guessing, and follows
   it if the reader changes it while the page is open. */
function useReducedMotionBridge(){
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return undefined;
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const apply = () => {
      document.documentElement.setAttribute('data-motion', query.matches ? 'off' : 'on');
    };
    apply();
    if (query.addEventListener){
      query.addEventListener('change', apply);
      return () => query.removeEventListener('change', apply);
    }
    query.addListener(apply);
    return () => query.removeListener(apply);
  }, []);
}

/* Theme is the host's to own. The shell is controlled (it takes both `theme`
   and `onThemeChange`, and with only the first it renders its control disabled),
   so the stored preference, the write and the data-dir mirror all live here.
   The key is the package's own, so an uncontrolled shell would read the same
   value. The mirror runs once at module evaluation, before App renders and so
   before the first paint, and again on every change.

   EXPERIENCE.md, Navigation: Light (the default), Dark and Match system. The
   shell's control switches between Light and Dark; the rail offers all
   three. Match system is stored as system and read through the system's
   colour scheme, which App follows while the page is open. */
const THEME_KEY = 'oi-theme';
const THEMES = ['midnight', 'daylight'];
const THEME_PREFERENCES = ['daylight', 'midnight', 'system'];
const DARK_QUERY = '(prefers-color-scheme: dark)';

export function storedThemePreference(){
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    return THEME_PREFERENCES.includes(stored) ? stored : 'daylight';
  } catch (e){
    return 'daylight';
  }
}

export function resolveTheme(preference, systemDark){
  if (preference === 'system') return systemDark ? 'midnight' : 'daylight';
  return THEMES.includes(preference) ? preference : 'daylight';
}

function systemPrefersDark(){
  try { return Boolean(window.matchMedia && window.matchMedia(DARK_QUERY).matches); } catch (e){ return false; }
}

export function storedTheme(){
  return resolveTheme(storedThemePreference(), typeof window !== 'undefined' && systemPrefersDark());
}

export function mirrorTheme(theme){
  if (typeof document === 'undefined') return null;
  document.documentElement.setAttribute('data-dir', theme);
  return theme;
}

if (typeof document !== 'undefined') mirrorTheme(storedTheme());

export default function App(){
  useReducedMotionBridge();
  const [themePreference, setThemePreference] = useState(storedThemePreference);
  const [systemDark, setSystemDark] = useState(systemPrefersDark);
  useEffect(() => {
    if (themePreference !== 'system' || !window.matchMedia) return undefined;
    const query = window.matchMedia(DARK_QUERY);
    const follow = () => setSystemDark(query.matches);
    follow();
    query.addEventListener('change', follow);
    return () => query.removeEventListener('change', follow);
  }, [themePreference]);
  const theme = resolveTheme(themePreference, systemDark);
  useLayoutEffect(() => { mirrorTheme(theme); }, [theme]);
  const changeTheme = (next) => {
    if (!THEME_PREFERENCES.includes(next)) return;
    try { localStorage.setItem(THEME_KEY, next); } catch (e){}
    setThemePreference(next);
  };
  const hash = useHashRoute();
  const [, setWorkbenchMoves] = useState(0);
  useEffect(() => {
    const onMove = () => setWorkbenchMoves((count) => count + 1);
    window.addEventListener(WORKBENCH_NAV_EVENT_NAME, onMove);
    return () => window.removeEventListener(WORKBENCH_NAV_EVENT_NAME, onMove);
  }, []);
  /* old route names still in localStorage or shared links keep working */
  const rawView = hash.view || 'pulse';
  const route = resolveHostRoute(rawView, hash.param);
  const researchMode = route === 'console';
  const setRoute = (r) => go((r || '').startsWith('/') ? r : '/' + r);
  const [storedRegion, setRegionRaw] = useState(() => String(storedValue('pulse-region', 'ZA')).toUpperCase());
  const scopedReadRoute = ['listen', 'seedpath', 'lexicon'].includes(route);
  /* Communities reads one market, named in its hash or else the stored one
     (ALL reads South Africa), so the header shows that market, not ALL. */
  const communitiesRoute = route === 'communities';
  const explicitReadRegion = communitiesRoute ? hashMarket(window.location.hash) : !hash.error ? route === 'topic' ? hash.topicRegion : scopedReadRoute ? hash.readRegion : null : null;
  const region = explicitReadRegion || (communitiesRoute ? topic42Market('', storedRegion) : storedRegion);
  const setRegion = (r) => {
    const next = String(r || '').toUpperCase();
    if (!['ZA', 'NG', 'KE', 'ALL'].includes(next)) return;
    setRegionRaw(next);
    try { localStorage.setItem('pulse-region', next); } catch (e){}
    if (route === 'topic' && hash.param) go(buildTopicHash(hash.param, next).slice(1));
    else if (scopedReadRoute) go(buildScopedReadHash(route, hash.param || '', next).slice(1));
    else if (communitiesRoute && next !== 'ALL') go('/' + route + '?market=' + next);
  };
  useEffect(() => {
    if (!explicitReadRegion || explicitReadRegion === storedRegion) return;
    setRegionRaw(explicitReadRegion);
    try { localStorage.setItem('pulse-region', explicitReadRegion); } catch (e){}
  }, [explicitReadRegion, storedRegion]);
  const setMarketScope = (scope) => setRegion(communitiesRoute ? communitiesMarketForScope(region, scope) : regionForMarketScope(region, scope));

  /* mirror the active route to localStorage so a reload restores it. We do NOT
     redirect on an empty hash: a deep link can read an empty hash for one paint
     on first load, and redirecting then clobbers the shared URL. route already
     falls back to pulse when the hash is empty, without touching the URL. */
  useEffect(() => { try { localStorage.setItem('pulse-route', route); } catch (e){} }, [route]);

  /* A research link is a Build link: it lands on the console under its own
     canonical hash, carrying whatever persona, markets or artifact it named. */
  useEffect(() => {
    if (rawView !== 'research') return;
    const st = parseWorkbenchRoute(window.location.hash || '#/research');
    go(buildWorkbenchPath(st));
  }, [rawView]);
  useEffect(() => {
    if (rawView !== 'console') return;
    const target = askAliasTarget(window.location.hash);
    if (target) go(target);
  }, [rawView, hash]);
  const askQuery = useMemo(
    () => (route === 'ask' ? parseAskQuery(typeof window !== 'undefined' ? window.location.hash : '') : null),
    [route, hash],
  );
  /* Compare keeps its choice in #/compare?..., so a shared link opens the
     same comparison, and Schedules reads a question handed over from Ask.
     A new link remounts the screen on its own query. */
  const routeSearch = useMemo(() => {
    if ((route !== 'compare' && route !== 'schedules') || typeof window === 'undefined') return '';
    const text = String(window.location.hash || '');
    return text.indexOf('?') >= 0 ? text.slice(text.indexOf('?') + 1) : '';
  }, [route, hash]);
  const [active, setActive] = useState(null);
  const [asking, setAsking] = useState(null);
  const [askErr, setAskErr] = useState(false);
  /* one live ask at a time: a new ask or a closed panel aborts the in-flight
     poll so rapid repeat asks never leave overlapping polls running. */
  const askCtrl = useRef(null);
  const abortAsk = () => { if (askCtrl.current){ askCtrl.current.abort(); askCtrl.current = null; } };
  /* Brief history is versioned: a bump clears any briefs saved by an older
     build (or a local preview), so a stale-shaped or test brief never resurfaces
     in the rail. */
  const [briefs, setBriefs] = useState(() => {
    try {
      if (localStorage.getItem('pulse-briefs-ver') !== '2'){
        localStorage.removeItem('pulse-briefs');
        localStorage.setItem('pulse-briefs-ver', '2');
        return [];
      }
      return JSON.parse(localStorage.getItem('pulse-briefs') || '[]');
    } catch (e){ return []; }
  });
  /* A route change moves focus to the new view's heading so keyboard and
     screen-reader users land on the content, and a polite live region names
     the route. The first paint is skipped so we never steal focus on load.

     The heading is usually still behind a lazy chunk or a fetch when the hash
     moves, so the effect waits for it rather than settling for the container:
     focus on the whole workspace paints a ring around the page and announces
     nothing. While the wait is open, a heading that unmounts under focus (the
     loading frame's title giving way to the route's own) hands focus to the
     heading that replaces it. The wait is bounded, and a route that never
     shows a heading leaves focus where it was. */
  const mainRef = useRef(null);
  const firstRoute = useRef(true);
  const [routeMsg, setRouteMsg] = useState('');
  useEffect(() => {
    if (firstRoute.current){ firstRoute.current = false; return; }
    setRouteMsg('Now viewing ' + (ROUTE_LABELS[route] || route));
    const el = mainRef.current;
    if (!el) return;
    let observer = null;
    let timer = null;
    const stop = () => {
      if (observer) observer.disconnect();
      if (timer) clearTimeout(timer);
      observer = null;
      timer = null;
    };
    const land = () => {
      const node = el.querySelector('h1, h2');
      if (!node) return false;
      if (!node.hasAttribute('tabindex')) node.setAttribute('tabindex', '-1');
      try { node.focus({preventScroll: true}); } catch (e){ node.focus(); }
      return true;
    };
    const id = requestAnimationFrame(() => {
      land();
      observer = new MutationObserver(() => {
        const held = document.activeElement;
        if (held && held !== document.body && held.isConnected) return;
        land();
      });
      observer.observe(el, {childList: true, subtree: true});
      timer = setTimeout(stop, HEADING_WAIT);
    });
    return () => { cancelAnimationFrame(id); stop(); };
  }, [route]);

  const [needPass, setNeedPass] = useState(false);
  /* The gate's account of the last failed attempt (C5 v2 section 13.2, item
     11): a counter, so every failure is announced again, with the words and
     whether the typed value was refused. notice is a saved key the server no
     longer accepts. */
  const [gateFailure, setGateFailure] = useState(null);
  const [gateNotice, setGateNotice] = useState('');
  const [retrying, setRetrying] = useState(false);
  const [storageBlocked, setStorageBlocked] = useState(false);
  const attempts = useRef(0);
  const retryingNow = useRef(false);
  const [session, setSession] = useState(0);
  const [authMode, setAuthMode] = useState(null);
  const [authUnavailable, setAuthUnavailable] = useState(false);
  const [authReady, setAuthReady] = useState(null);
  const [health, setHealth] = useState(null);
  const onAuth = useCallback(() => {
    if (authMode === 'iap_readonly'){
      setAuthUnavailable(true);
      setNeedPass(false);
      setAuthReady(false);
      return;
    }
    setNeedPass(true);
    setAuthReady(false);
  }, [authMode]);

  const failWith = useCallback((kind, message, clear) => {
    attempts.current += 1;
    setGateNotice('');
    setGateFailure({n: attempts.current, kind, message, clear});
  }, []);
  /* What a check of the stored key means for the page. A turned-down key is
     handled by the gate's own subscriber below; a check that could not be
     completed keeps the key and offers Try again. */
  const applyStoredCheck = useCallback((result) => {
    if (result.outcome === 'ok'){
      setGateFailure(null);
      setGateNotice('');
      setNeedPass(false);
      setAuthReady(true);
      return;
    }
    setNeedPass(true);
    setAuthReady(false);
    if (result.outcome === 'wrong') credential.reject();
    else if (result.outcome === 'rate') failWith('rate', result.message, false);
    else failWith('check', CHECK_WORDS, false);
  }, [failWith]);

  useEffect(() => {
    let live = true;
    /* With a stored key the page waits, sends one verify, and starts no gated
       read until it answers 200. */
    const openWithStoredKey = () => {
      if (!credential.key()){ setNeedPass(true); setAuthReady(false); return; }
      credential.verifyStored().then((result) => { if (live) applyStoredCheck(result); });
    };
    fetch('/api/health')
      .then((r) => {
        if (r && r.ok === false) throw new Error('health_unavailable');
        return r.json();
      })
      .then((d) => {
        if (!live) return;
        setHealth(d && typeof d === 'object' ? d : null);
        if (d && Object.prototype.hasOwnProperty.call(d, 'auth_mode')){
          const mode = d.auth_mode;
          const authCheck = d.checks && d.checks.auth;
          if (mode === 'iap_readonly' && d.passcode === false && authCheck === 'ok'){
            credential.begin('iap');
            setAuthMode('iap_readonly');
            fetch('/api/auth/verify', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: '{}',
            })
              .then((r) => {
                if (r && r.ok === false) throw new Error('sign_in_unavailable');
                return r.json();
              })
              .then((identity) => {
                if (!live) return;
                if (!identity || identity.ok !== true) throw new Error('sign_in_unavailable');
                setAuthUnavailable(false);
                setNeedPass(false);
                setAuthReady(true);
              })
              .catch(() => {
                if (live) {
                  setAuthUnavailable(true);
                  setNeedPass(false);
                  setAuthReady(false);
                }
              });
            return;
          }
          if (mode === 'passcode' && d.passcode === true && authCheck === 'ok'){
            setAuthMode('passcode');
            setAuthUnavailable(false);
            credential.begin('passcode');
            openWithStoredKey();
            return;
          }
          setAuthMode('unavailable');
          setAuthUnavailable(true);
          setNeedPass(false);
          setAuthReady(false);
          return;
        }
        setAuthMode('legacy');
        if (!d || !d.passcode){
          credential.begin('open');
          setNeedPass(false);
          setAuthReady(true);
          return;
        }
        credential.begin('passcode');
        openWithStoredKey();
      })
      .catch(() => {
        if (live) {
          setAuthMode('unavailable');
          setAuthUnavailable(true);
          setNeedPass(false);
          setAuthReady(false);
        }
      });
    return () => { live = false; };
  }, [applyStoredCheck]);

  const retryStored = useCallback(() => {
    if (retryingNow.current) return;
    retryingNow.current = true;
    setRetrying(true);
    credential.verifyStored().then(applyStoredCheck).finally(() => {
      retryingNow.current = false;
      setRetrying(false);
    });
  }, [applyStoredCheck]);

  const apiEnabled = authReady === true && !needPass && route !== 'method';
  /* Demo polish, 2 October 2026: core/api serves no /api/desk, so only Browse
     and a released signal story, the two routes that still read the legacy
     desk, ask for it. Every other page fetches its own data, and the desk
     request there only logged a 404. */
  /* Page port, 3 October 2026: Browse's links now open Discover and an older
     topic link opens OlderLink42. A Browse route reached some other way
     keeps its read. */
  const routeUsesLegacyDesk = route === 'browse';

  /* Keep the legacy desk read for routes that still consume it. */
  const [desk, retryDesk] = useApi(
    '/api/desk?region=' + encodeURIComponent(String(region).toLowerCase()),
    session,
    onAuth,
    apiEnabled && routeUsesLegacyDesk,
    route === 'browse' ? 30_000 : 0,
  );
  /* The rail summary adapts every admitted signal, so it is memoised on the
     desk payload rather than recomputed on each render of the shell. */
  const railSummary = useMemo(
    () => (desk.state === 'ready' && desk.data
      ? railSummaryForTopics(exploreDynamicProps(desk.data).topics)
      : undefined),
    [desk.state, desk.data],
  );
  const deskFreshness = desk.state === 'ready' && desk.data ? desk.data.freshness : null;
  const [lastChecked, setLastChecked] = useState(storedCheckedEpoch);
  useEffect(() => {
    const epoch = checkedEpoch(deskFreshness);
    if (epoch === null) return;
    setLastChecked(epoch);
    try { localStorage.setItem(CHECKED_KEY, String(epoch)); } catch (e){}
  }, [deskFreshness]);

  useEffect(() => { document.body.style.overflow = (active || asking || askErr) ? 'hidden' : ''; }, [active, asking, askErr]);

  useEffect(() => {
    const h = (e) => {
      if (e.key === 'Escape'){ abortAsk(); setActive(null); setAsking(null); setAskErr(false); }
      if (e.key === '/' && !/INPUT|TEXTAREA/.test((document.activeElement || {}).tagName || '')){
        e.preventDefault();
        go('/pulse');
        requestAnimationFrame(() => { const inp = document.querySelector('.command input'); if (inp) inp.focus(); });
      }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, []);

  const handleAsk = async (query) => {
    abortAsk();
    const ctrl = new AbortController();
    askCtrl.current = ctrl;
    setActive(null); setAskErr(false); setAsking(query);
    try {
      const topic = await import('./ask.jsx').then((m) => m.generateBrief(query, region, ctrl.signal));
      if (ctrl.signal.aborted) return;
      askCtrl.current = null;
      setAsking(null);
      setActive(topic);
      const entry = {...topic, ts: Date.now()};
      setBriefs((prev) => {
        const next = [entry, ...prev.filter((p) => p.id !== topic.id)].slice(0, 8);
        try { localStorage.setItem('pulse-briefs', JSON.stringify(next)); } catch (e){}
        return next;
      });
    } catch (e){
      if (e && e.aborted) return;
      askCtrl.current = null;
      if (e && e.auth){ setAsking(null); onAuth(); return; }
      if (e && e.code === 'capacity_busy') setAskErr({code: e.code});
      else { setAsking(null); setAskErr(e && e.thin ? 'thin' : true); }
    }
  };

  /* stop any in-flight ask poll if the shell unmounts */
  useEffect(() => abortAsk, []);

  const removeBrief = (b) => {
    setBriefs((prev) => {
      const next = prev.filter((p) => !(p.id === b.id && p.ts === b.ts));
      try { localStorage.setItem('pulse-briefs', JSON.stringify(next)); } catch (e){}
      return next;
    });
  };

  const submitPasscode = async (code) => {
    if (authUnavailable || (authMode !== 'passcode' && authMode !== 'legacy')) return;
    const result = await credential.verify(code);
    if (result.outcome !== 'ok'){
      if (result.outcome === 'wrong') failWith('wrong', WRONG_WORDS, true);
      else if (result.outcome === 'rate') failWith('rate', result.message, false);
      else failWith('check', CHECK_WORDS, false);
      return;
    }
    /* A browser that will not save the key still opens the desk; the key is
       held for the life of the page and the reader is told. */
    setStorageBlocked(!credential.store(code));
    setGateFailure(null);
    setGateNotice('');
    setNeedPass(false);
    setAuthReady(true);
    clearCache();
    setSession((s) => s + 1);
  };

  /* Log out (6 October 2026). The passcode is the only credential this page
     holds and f42-api keeps no session for it, so logging out is local: the
     stored passcode, the saved answers and every cached read are forgotten,
     any reviewer session held in memory ends, and the gate returns. Theme and
     market stay, since they are preferences and not access. Only a passcode
     desk offers it: IAP access is the browser's Google sign-in, not ours. */
  const canSignOut = authMode === 'passcode' || (authMode === 'legacy' && Boolean(health && health.passcode));
  const closeDesk = useCallback((options = {}) => {
    if (askCtrl.current){ askCtrl.current.abort(); askCtrl.current = null; }
    credential.drop();
    clearCache();
    import('./dossierResource.js').then((m) => m.endReviewSession(), () => {});
    setActive(null);
    setAsking(null);
    setAskErr(false);
    /* A key the server turned down is not a Log out: the saved answers and
       chat threads stay, in this tab's memory as well as in storage. */
    if (!options.keepSaved) setBriefs([]);
    setGateFailure(null);
    setGateNotice(options.notice || '');
    setStorageBlocked(false);
    setNeedPass(true);
    setAuthReady(false);
    setSession((s) => s + 1);
  }, []);
  /* The older chat page saves its threads as it unmounts, which is after the
     click, so the keys are removed again once the gate has replaced the
     pages. */
  const signedOut = useRef(false);
  /* The marker goes before the key, so a tab that sees the key go finds no
     stale mark and reads it as the Log out it is. */
  const forgetStored = () => {
    for (const key of [REASON_KEY, PASS_KEY, 'pulse-briefs', 'pulse-chat']){
      try { localStorage.removeItem(key); } catch (e){}
    }
  };
  const signOut = useCallback(() => {
    forgetStored();
    signedOut.current = true;
    closeDesk();
  }, [closeDesk]);
  useEffect(() => {
    /* Signing in drops any mark a tab already at the gate picked up. */
    if (!needPass){ signedOut.current = false; return; }
    if (!signedOut.current) return;
    signedOut.current = false;
    forgetStored();
  }, [needPass]);
  /* A Log out in another tab clears the shared passcode; this tab follows. */
  useEffect(() => {
    if (!canSignOut) return undefined;
    const onStorage = (event) => {
      if (event.key !== null && event.key !== PASS_KEY) return;
      if (storedValue(PASS_KEY)) return;
      /* Another tab found the key stale. That is not a Log out, so the saved
         answers and chat threads stay and this tab only closes the desk. */
      if (storedValue(REASON_KEY) === 'stale'){
        closeDesk({keepSaved: true, notice: STALE_WORDS});
        return;
      }
      /* This tab's chat page saves its threads as it unmounts; mark the
         sign-out so the gate clears them again, as a click here would. */
      signedOut.current = true;
      closeDesk();
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [canSignOut, closeDesk]);
  useEffect(() => () => credential.end(), []);
  /* The gate turned the key down on a request: close the desk to the gate. */
  useEffect(() => credential.subscribe(() => closeDesk({keepSaved: true, notice: STALE_WORDS})), [closeDesk]);

  if (authUnavailable || (authMode === 'iap_readonly' && needPass)) return <SignInUnavailable />;
  if (authReady === null) return <SignInWait />;
  if (needPass){
    const blocked = gateFailure && (gateFailure.kind === 'rate' || gateFailure.kind === 'check') && credential.key();
    return <PasscodeScreen onSubmit={submitPasscode} failure={gateFailure} notice={gateNotice} onRetry={blocked ? retryStored : null} retrying={retrying} />;
  }

  /* routes that bring their own data or render their own desk states skip the generic desk gate */
  const STANDALONE = new Set(['ask', 'topic42', 'coverage', 'alerts', 'console', 'source-lab', 'fieldwork', 'historical', 'topic', 'creator', 'lexicon', 'map', 'board', 'compare', 'network', 'seeds', 'seedpath', 'explore', 'listen', 'dossiers', 'dossier', 'dossier-share', 'creator42', 'communities', 'community', 'history', 'history-item', 'investigations', 'investigation', 'schedules', 'skins', 'skin', 'people-hidden', 'method']);
  const topics = desk.state === 'ready' && desk.data ? decorateAll(desk.data.topics) : [];
  const lexicon = desk.state === 'ready' && desk.data ? desk.data.lexicon : null;

  /* a refined brief replaces the open panel and its history entry in place */
  const updateBrief = (nt) => {
    setActive(nt);
    setBriefs((prev) => {
      const next = prev.map((p) => p.id === nt.id ? {...nt, ts: p.ts} : p);
      try { localStorage.setItem('pulse-briefs', JSON.stringify(next)); } catch (e){}
      return next;
    });
  };

  const askOpen = route === 'console' && typeof window !== 'undefined' && consoleShowsAsk(window.location.hash);
  const railPlace = railPlaceForHostRoute(route, {askOpen});
  /* Shell consistency, 2 October 2026: the masthead reserves its second row
     only when there is a freshness stamp to put in it. Browse reserved the row
     on every load, and on a device that has never seen a desk reading (core/api
     serves no /api/desk) the stamp never came, so a phone showed a 60px empty
     band between the masthead and the page. A stamp read from the last check
     paints at first paint, so the row it needs is there from the start. */
  const checkedStamp = routeUsesLegacyDesk && !STANDALONE.has(route)
    ? checkedLabel(desk.state === 'ready' ? checkedEpoch(deskFreshness) : lastChecked)
    : undefined;

  return (
    <div
      className={'app has-rail42' + (researchMode ? ' app-research' : '')}
      data-reserve-freshness={checkedStamp ? 'true' : undefined}
    >
      {/* The wide rail is the left column, so it comes before the shell and
          the page, behind its own skip link, and the Tab order follows what
          the reader sees. The phone bar stays after the workspace. */}
      <Suspense fallback={null}>
        <Rail42 only="wide" route={route} askOpen={askOpen} theme={themePreference} onThemeChange={changeTheme} onSignOut={canSignOut ? signOut : undefined} />
      </Suspense>
      <InstrumentShell
        route={railPlace.job}
        onRouteChange={(nextRoute) => {
          /* The current page's own entry is where the reader already is. */
          if (railPlace.currentPage && nextRoute === railPlace.currentPage.href) return;
          setRoute(hostRouteForInstrumentRoute(nextRoute));
        }}
        utilityLinks={railPlace.currentPage ? [railPlace.currentPage] : undefined}
        marketScope={marketScopeForRegion(region)}
        onMarketScopeChange={setMarketScope}
        checkedAt={checkedStamp}
        railSummary={railSummary}
        theme={theme}
        onThemeChange={changeTheme}
      >
        <div className="sr-only" role="status" aria-live="polite">{routeMsg}</div>
        {authMode === 'iap_readonly' && <p className="t42-status" role="status">Read-only access</p>}
        {storageBlocked && <p className="t42-status" role="status">{SAVE_WORDS}</p>}
        <RouteLayer key={route} route={route} research={researchMode} layerRef={mainRef}>
        {/* Task 65. The desk-loading route wait is gone. Browse and Method were
            the only routes it ever reached, and DESK_OPTIONAL says why neither
            needs it: the route paints its own surface while the desk is held,
            so the reader's origin does not move when the data lands. RouteWait
            itself stays, as the Suspense fallback for a route whose code is on
            its way, which is a wait with nothing behind it to paint. */}
        {desk.state === 'error' && route !== 'pulse' && route !== 'browse' && !STANDALONE.has(route) && <DeskError code={desk.code} message={desk.message} onRetry={retryDesk} />}
        {route === 'pulse' && (
          <Suspense fallback={<RouteWait />}>
            <TodayPage42 region={region} onRegionChange={setRegion} onAuth={onAuth} date={todayDateFromHash(window.location.hash)} loadAlerts={fetchAlerts} loadInvestigations={listInvestigations} loadSchedules={listSchedules} onCreateWatch={createWatch} onFeedback={sendFeedback} />
          </Suspense>
        )}
        {route === 'ask' && (
          <Suspense fallback={<RouteWait />}>
            <AskPage region={region} setRegion={setRegion} query={askQuery} onAuth={onAuth} health={health} />
          </Suspense>
        )}
        {route === 'browse' && (
          <Suspense fallback={<RouteWait />}>
            <BrowsePage topics={topics} region={region} onOpen={setActive} lexicon={lexicon} desk={desk.data} onAuth={onAuth} />
          </Suspense>
        )}
        {route === 'method' && (
          <Suspense fallback={<RouteWait />}>
            <MethodPage />
          </Suspense>
        )}
        <Suspense fallback={<RouteWait />}>
          {route === 'explore' && <Discover42 region={region} onRegionChange={setRegion} onAuth={onAuth} onCreateWatch={createWatch} />}
          {route === 'topic42' && <TopicPage42 key={hash.param + ':' + topic42Market(window.location.hash, region)} itemId={hash.param} market={topic42Market(window.location.hash, region)} onAuth={onAuth} onCreateWatch={createWatch} />}
          {route === 'coverage' && <CoveragePage42 onAuth={onAuth} />}
          {route === 'alerts' && <AlertsPage42 region={region} onAuth={onAuth} />}
          {route === 'console' && consoleShowsBuild(window.location.hash) && <Build42 region={region} onAuth={onAuth} />}
          {route === 'console' && !consoleShowsBuild(window.location.hash) && <ConsoleWorkbench region={region} setRegion={setRegion} session={session} onAuth={onAuth} initialQuery={hash.param} />}
          {route === 'seeds' && <SeedsPage session={session} onAuth={onAuth} region={region} setRegion={setRegion} />}
          {route === 'seedpath' && <SeedPathPage session={session} onAuth={onAuth} initialKeyword={hash.param} initialMarket={region} setRegion={setRegion} scopeError={hash.error} />}
          {/* Page port, 3 October 2026: the older topic and creator pages
              read /api/topic and /api/creator, which f42-api does not serve.
              A 42 item id is sent on to #/t/ (legacyRoutes.js); any other
              older link says it cannot open here. */}
          {route === 'topic' && <OlderLink42 kind="topic" />}
          {route === 'creator' && <OlderLink42 kind="creator" />}
          {['listen', 'lexicon'].includes(route) && hash.error && <section className="page"><div className="page-shell"><h1>Invalid market link</h1><p>Select a market in the header to repair this link.</p></div></section>}
          {route === 'lexicon' && !hash.error && <LexiconPage key={hash.param + ':' + region} region={region} term={hash.param} session={session} onAuth={onAuth} />}
          {route === 'map' && <MapPage />}
          {route === 'board' && <BoardPage session={session} onAuth={onAuth} />}
          {route === 'compare' && <Compare42 key={routeSearch} search={routeSearch} region={region} onAuth={onAuth} />}
          {route === 'dossiers' && <DossiersPage region={region} onAuth={onAuth} />}
          {route === 'dossier' && <DossierPage key={hash.param} dossierId={hash.param} onAuth={onAuth} />}
          {route === 'dossier-share' && <SharedDossier key={hash.param + '/' + hash.param2} dossierId={hash.param} version={hash.param2} onAuth={onAuth} />}
          {route === 'creator42' && <CreatorPage42 key={hash.param + ':' + topic42Market(window.location.hash, region)} creatorId={hash.param} market={topic42Market(window.location.hash, region)} onAuth={onAuth} />}
          {route === 'communities' && <CommunitiesPage42 key={topic42Market(window.location.hash, region)} market={topic42Market(window.location.hash, region)} onAuth={onAuth} />}
          {route === 'community' && <CommunityPage42 key={hash.param + ':' + (hashMarket(window.location.hash) || '')} communityId={hash.param} market={hashMarket(window.location.hash)} onAuth={onAuth} />}
          {route === 'history' && <HistoryPage42 region={region} onAuth={onAuth} />}
          {route === 'history-item' && (hash.param2
            ? <HistoryItemPage42 key={hash.param2 + ':' + topic42Market(window.location.hash, region)} itemId={hash.param2} market={topic42Market(window.location.hash, region)} onAuth={onAuth} />
            : <HistoryPage42 region={region} onAuth={onAuth} />)}
          {route === 'investigations' && <InvestigationsPage42 region={region} onAuth={onAuth} />}
          {route === 'investigation' && <InvestigationPage42 key={hash.param} investigationId={hash.param} onAuth={onAuth} />}
          {route === 'schedules' && <SchedulesPage42 key={routeSearch} search={routeSearch} region={region} onAuth={onAuth} />}
          {route === 'skins' && <SkinsPage42 onAuth={onAuth} />}
          {route === 'skin' && <SkinPage42 key={hash.param} skinId={hash.param} onAuth={onAuth} />}
          {route === 'people-hidden' && <HiddenPeoplePage42 onAuth={onAuth} />}
          {route === 'listen' && !hash.error && <ListenPage region={region} setRegion={setRegion} session={session} onAuth={onAuth} initialQuery={hash.param} />}
          {route === 'network' && <NetworkGraph region={region} setRegion={setRegion} session={session} onAuth={onAuth} />}
          {route === 'source-lab' && <SourceLabWorkspace onAuth={onAuth} />}
          {route === 'fieldwork' && <FieldworkWorkspace onAuth={onAuth} />}
          {route === 'historical' && <HistoricalWorkspace route={hash} onAuth={onAuth} />}
        </Suspense>
        </RouteLayer>
        <Suspense fallback={null}>
          <Rail42 only="compact" route={route} askOpen={askOpen} theme={themePreference} onThemeChange={changeTheme} onSignOut={canSignOut ? signOut : undefined} />
        </Suspense>
      </InstrumentShell>

      <Suspense fallback={null}>
        <TopicDetail t={active} onClose={() => setActive(null)} onUpdate={updateBrief} />
        <AskLoadingPanel query={asking} error={askErr} region={region} onRetry={() => handleAsk(asking)} onClose={() => { abortAsk(); setAsking(null); setAskErr(false); }} />
      </Suspense>
    </div>
  );
}
