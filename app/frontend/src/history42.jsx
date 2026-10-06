/* History on the 42 API (contract.md section 12.4; EXPERIENCE.md, History:
   past asks, briefs, findings and analogues). #/history lists what 42 has
   done, newest first, a page at a time; #/history/items/<id>?market= is one
   item's earlier waves and the items whose waves it most resembles. Every
   number is a Figure carrying its query. A part the API has not filled yet
   says so in words instead of hiding. */
import {useEffect, useRef, useState} from 'react';
import {fetchHistoryAsks, fetchHistoryBriefs, fetchHistoryFindings, fetchHistoryItem, searchHistory} from './api42.js';
import {readerFigure, sentenceCase} from './api.js';
import {safeUrl} from './safeUrl.js';
import {longDate, platformWord} from './ui/TrendCard.jsx';
import {RangeTimeline} from './ui/Charts42.jsx';
import './styles/history42.css';

const MARKET_WORDS = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya'};
const BRIEF_WORDS = {published: 'published', partial: 'published, some trends not yet explained', data_issue: 'data issue', not_ready: 'not ready', failed: 'failed'};
const FLAG_WORDS = {
  paid: 'Paid', paid_led: 'Paid-led', near_duplicate: 'Near duplicate', new_account: 'New account',
  likely_coordinated: 'Likely coordinated', check_pattern: 'Check pattern',
};
const CONFIDENCE_WORDS = {observed: 'Observed', corroborated: 'Corroborated', single_source: 'Single source', inferred: 'Inferred'};
const NO_MAP = "Analogues need 42's cultural map";
const NOT_CHECKED = 'Whether a finding still holds is not checked yet';
/* The server's note on Findings, in the words a reader needs. */
const FINDINGS_NOTE = 'Saved findings show what was true when they were saved.';
const ASKS_PAGE = 20;
const BRIEFS_PAGE = 30;
const FINDINGS_PAGE = 20;
const TABS = [
  {id: 'asks', label: 'Asks'},
  {id: 'briefs', label: 'Briefs'},
  {id: 'findings', label: 'Findings'},
  {id: 'search', label: 'Search items'},
];
/* What each tab holds, for the column beside the list: where else to look,
   in one line each, with the way in. */
const TAB_GUIDE = {
  asks: {go: 'Read past asks', what: 'Every question asked, with its answer when it finished.'},
  briefs: {go: 'Read past briefs', what: 'Each morning brief by date. Open one to read Today as it stood.'},
  findings: {go: 'Read saved findings', what: 'Answers someone saved, with the claims and posts behind them.'},
  search: {go: 'Search the archive', what: 'Find a hashtag, sound or creator and see how it rose and fell.'},
};

function historyTabFromHash(hash){
  const text = String(hash || '');
  const at = text.indexOf('?');
  const route = (at < 0 ? text : text.slice(0, at)).replace(/^#\/?/, '').replace(/\/+$/, '');
  if (route !== 'history') return 'asks';
  const requested = new URLSearchParams(at < 0 ? '' : text.slice(at + 1)).get('tab');
  return TABS.some((tab) => tab.id === requested) ? requested : 'asks';
}

function historyHashWithTab(hash, tab){
  const text = String(hash || '');
  const at = text.indexOf('?');
  const currentRoute = at < 0 ? text : text.slice(0, at);
  const routeName = currentRoute.replace(/^#\/?/, '').replace(/\/+$/, '');
  const route = routeName === 'history' ? currentRoute : '#/history';
  const query = new URLSearchParams(routeName === 'history' && at >= 0 ? text.slice(at + 1) : '');
  query.set('tab', tab);
  return route + '?' + query.toString();
}

const isFigure = (value) => Boolean(value && typeof value === 'object' && value.value !== undefined && value.value !== null);
const marketWord = (market) => MARKET_WORDS[market] || market;
const words = (code) => sentenceCase(String(code || '').replace(/_/g, ' '));
const flagWord = (flag) => FLAG_WORDS[flag] || words(flag);
const historyHref = (itemId, market) => '#/history/items/' + encodeURIComponent(itemId) + '?market=' + encodeURIComponent(market);
const topicHref = (itemId, market) => '#/t/' + encodeURIComponent(itemId) + '?market=' + encodeURIComponent(market);

/* A stamp as a SAST date and time. The API writes some stamps with a space
   instead of T (str of a database time); a stamp with no zone is already
   SAST, as the API reads it; a bare date stays a date. */
const SAST_PARTS = new Intl.DateTimeFormat('en-ZA', {
  timeZone: 'Africa/Johannesburg', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

function when(value){
  const text = String(value || '').trim();
  const stamp = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(:\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/i.exec(text);
  if (!stamp) return longDate(text);
  if (!stamp[4]) return longDate(stamp[1]) + ', ' + stamp[2];
  const at = new Date(stamp[1] + 'T' + stamp[2] + (stamp[3] || '') + stamp[4].toUpperCase().replace(/^([+-]\d{2})(\d{2})$/, '$1:$2'));
  if (Number.isNaN(at.getTime())) return longDate(text);
  const part = Object.fromEntries(SAST_PARTS.formatToParts(at).map((p) => [p.type, p.value]));
  return longDate(part.year + '-' + part.month + '-' + part.day) + ', ' + part.hour + ':' + part.minute;
}

/* "South Africa and Nigeria published; Kenya data issue": markets that
   share a status are named together, in the order the API lists them. */
function briefMarkets(markets){
  const groups = new Map();
  for (const m of Array.isArray(markets) ? markets : []){
    const status = BRIEF_WORDS[m.status] || words(m.status).toLowerCase();
    groups.set(status, [...(groups.get(status) || []), marketWord(m.market)]);
  }
  return [...groups].map(([status, names]) => {
    const named = names.length < 2 ? names.join('') : names.slice(0, -1).join(', ') + ' and ' + names.at(-1);
    return named + ' ' + status;
  }).join('; ');
}

/* "South Africa 0 trends shown, 12 held back; Nigeria 1 trend shown, 5 held
   back": what Today showed of each market's brief and what it held, from the
   API's cards and held Figures. A brief that held every item still says how
   many it held. Null when the API gives no counts. */
function briefCounts(markets){
  const parts = [];
  for (const m of Array.isArray(markets) ? markets : []){
    if (!isFigure(m.cards) || !isFigure(m.held)) continue;
    const shown = m.cards.value;
    parts.push(marketWord(m.market) + ' ' + shown + (shown === 1 ? ' trend' : ' trends') + ' shown, ' + m.held.value + ' held back');
  }
  return parts.length ? parts.join('; ') : null;
}

function askStatus(ask){
  if (ask.status === 'failed') return 'Did not finish';
  if (ask.status === 'running' || ask.status === 'queued') return 'Still running';
  if (ask.answer_status === 'complete') return 'Answered';
  if (ask.answer_status === 'partial') return 'Partly answered';
  if (ask.answer_status === 'refused') return 'Declined';
  return words(ask.answer_status || ask.status) || 'No status';
}

/* The same reading as askStatus, as a key the row's status mark is drawn from. */
function askStatusKey(ask){
  if (ask.status === 'failed') return 'failed';
  if (ask.status === 'running' || ask.status === 'queued') return 'running';
  if (ask.answer_status === 'complete') return 'answered';
  if (ask.answer_status === 'partial') return 'partial';
  if (ask.answer_status === 'refused') return 'declined';
  return 'other';
}

function asksNewestFirst(asks){
  return asks.map((ask, index) => ({ask, index, at: Date.parse(ask && ask.at)}))
    .sort((a, b) => {
      const aValid = Number.isFinite(a.at);
      const bValid = Number.isFinite(b.at);
      if (aValid && bValid) return b.at - a.at || a.index - b.index;
      if (aValid) return -1;
      if (bValid) return 1;
      return a.index - b.index;
    })
    .map(({ask}) => ask);
}

function Fig({figure, children}){
  return (
    <span className="hi42-fig" data-query-id={figure.query_id} title={'Query ' + figure.query_id + ', run ' + figure.run_id}>
      {children ?? readerFigure(figure.value)}
    </span>
  );
}

function failure(error, what){
  if (error && error.auth) return 'Enter the passcode to read ' + what + '.';
  return (error && error.status && error.message) || 'The 42 service did not answer.';
}

function useAuthHandover(onAuth){
  const ref = useRef(onAuth);
  ref.current = onAuth;
  return (error) => { if (error && error.auth && ref.current) ref.current(); };
}

/* ---------------- #/history ---------------- */

/* The header's market picker scopes every History list (Albert, 4 October
   2026: with Nigeria picked, History still listed South Africa's asks).
   All markets, or no pick, lists every market. */
const pickedMarket = (region) => (MARKET_WORDS[region] ? region : '');

export function HistoryPage42({onAuth, region}){
  const market = pickedMarket(region);
  const [tab, setTab] = useState(() => historyTabFromHash(typeof window === 'undefined' ? '' : window.location.hash));
  const refs = useRef({});
  const selectTab = (next) => {
    if (typeof window !== 'undefined') {
      window.history.replaceState(window.history.state, '', historyHashWithTab(window.location.hash, next));
    }
    setTab(next);
  };
  useEffect(() => {
    const syncTab = () => setTab(historyTabFromHash(window.location.hash));
    window.addEventListener('hashchange', syncTab);
    window.addEventListener('popstate', syncTab);
    return () => {
      window.removeEventListener('hashchange', syncTab);
      window.removeEventListener('popstate', syncTab);
    };
  }, []);
  const onKey = (event) => {
    const at = TABS.findIndex((t) => t.id === tab);
    const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = TABS[(at + step + TABS.length) % TABS.length].id;
    selectTab(next);
    if (refs.current[next]) refs.current[next].focus();
  };
  return (
    <section className="page hi42">
      <header className="hi42-head">
        <h1 className="hi42-heading">History</h1>
        <p className="hi42-sub">What did we see before, and what did we ask? Past asks, briefs and findings, and how any trend rose and fell.</p>
        <p className="hi42-scope" data-history-market={market || 'ALL'}>{market ? 'Showing ' + MARKET_WORDS[market] + ' only. Pick All markets at the top to see every market.' : 'Showing every market.'}</p>
      </header>
      {/* Layout pass, 4 October 2026: the list keeps the main column and a
          side column says what the other tabs hold, so a short list no
          longer leaves the right of a wide screen empty. */}
      <div className="hi42-body">
      <div className="hi42-main">
      <div className="hi42-tabs" role="tablist" aria-label="History" onKeyDown={onKey}>
        {TABS.map((t) => (
          <button
            key={t.id}
            ref={(node) => { refs.current[t.id] = node; }}
            type="button"
            role="tab"
            id={'hi42-tab-' + t.id}
            className="hi42-tab"
            aria-selected={tab === t.id ? 'true' : 'false'}
            aria-controls={'hi42-panel-' + t.id}
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => selectTab(t.id)}
          >{t.label}</button>
        ))}
      </div>
      <div className="hi42-panel" role="tabpanel" id={'hi42-panel-' + tab} aria-labelledby={'hi42-tab-' + tab}>
        {tab === 'asks' && <Asks key={market} market={market} onAuth={onAuth} />}
        {tab === 'briefs' && <Briefs key={market} market={market} onAuth={onAuth} />}
        {tab === 'findings' && <Findings key={market} market={market} onAuth={onAuth} />}
        {tab === 'search' && <Search key={market} market={market} onAuth={onAuth} />}
      </div>
      </div>
      <aside className="hi42-aside" aria-labelledby="hi42-aside-title">
        <h2 className="hi42-aside-title" id="hi42-aside-title">Also in History</h2>
        <ul className="hi42-guide">
          {TABS.filter((t) => t.id !== tab).map((t) => (
            <li key={t.id}>
              <button type="button" className="hi42-guide-go" onClick={() => selectTab(t.id)}>{TAB_GUIDE[t.id].go}</button>
              <span className="hi42-guide-what">{TAB_GUIDE[t.id].what}</span>
            </li>
          ))}
        </ul>
      </aside>
      </div>
    </section>
  );
}

/* A list read a page at a time with a before cursor, newest first. */
function usePages(read, key, onAuth){
  const [state, setState] = useState({phase: 'loading', rows: [], next: null, error: null, failedBefore: undefined, retrying: false});
  const handover = useAuthHandover(onAuth);
  const request = useRef(null);
  const load = (before) => {
    if (request.current && request.current.before === before && !request.current.controller.signal.aborted) return;
    if (request.current) request.current.controller.abort();
    const controller = new AbortController();
    const active = {before, controller};
    request.current = active;
    setState((current) => ({...current, phase: 'loading', error: null, retrying: current.phase === 'error' && current.failedBefore === before}));
    read(before, {signal: controller.signal})
      .then((page) => {
        if (controller.signal.aborted || request.current !== active) return;
        request.current = null;
        const found = Array.isArray(page && page[key]) ? page[key] : [];
        setState((current) => ({phase: 'ready', rows: before ? [...current.rows, ...found] : found, next: (page && page.next_before) || null, error: null, failedBefore: undefined, retrying: false}));
      })
      .catch((error) => {
        if (controller.signal.aborted || request.current !== active) return;
        request.current = null;
        setState((current) => ({...current, phase: 'error', error, failedBefore: before, retrying: false}));
        handover(error);
      });
  };
  useEffect(() => {
    load(null);
    return () => {
      if (!request.current) return;
      request.current.controller.abort();
      request.current = null;
    };
  }, []);
  return [state, load];
}

function Older({state, load}){
  if (!state.next || state.phase !== 'ready') return null;
  return <button type="button" className="hi42-button" onClick={() => load(state.next)}>Show older</button>;
}

function ListState({state, what, empty, onRetry}){
  if (state.phase === 'error') return (
    <>
      <p className="hi42-status" role="alert">{failure(state.error, what)}</p>
      {!state.error?.auth && typeof onRetry === 'function' && <button type="button" className="hi42-button" onClick={onRetry}>Try again</button>}
    </>
  );
  if (state.phase === 'loading') return (
    <>
      <p className="hi42-status" role="status">{state.rows.length ? 'Reading more ' + what : 'Reading ' + what}</p>
      {state.retrying && <button type="button" className="hi42-button" disabled>Try again</button>}
    </>
  );
  if (state.phase === 'ready' && state.rows.length === 0) return <p className="hi42-status" role="status">{empty}</p>;
  return null;
}

function Asks({onAuth, market = ''}){
  const [state, load] = usePages((before, options) => fetchHistoryAsks({limit: ASKS_PAGE, before, market: market || undefined}, options), 'asks', onAuth);
  const [showAll, setShowAll] = useState(false);
  const orderedRows = asksNewestFirst(state.rows);
  const rows = showAll ? orderedRows : orderedRows.filter((ask) => ask.answer_status === 'complete' || ask.answer_status === 'partial');
  return (
    <div data-section="asks">
      <ListState state={state} what="past asks" empty={market ? 'No questions about ' + MARKET_WORDS[market] + ' have been asked yet.' : 'No questions have been asked yet.'} onRetry={() => load(state.failedBefore)} />
      {state.phase === 'ready' && state.rows.length === 0 && <a className="hi42-button hi42-next" href="#/ask">Ask a question</a>}
      {state.rows.length > 0 && (
        <div className="hi42-toolbar">
          <p className="hi42-toolbar-note">{showAll ? 'Every question asked, newest first.' : 'Questions with an answer, newest first.'}</p>
          <button type="button" className="hi42-button" aria-pressed={showAll} onClick={() => setShowAll((current) => !current)}>Show all</button>
        </div>
      )}
      {state.phase === 'ready' && state.rows.length > 0 && rows.length === 0 && !showAll
        && <p className="hi42-status" role="status">No complete or partial answers in this page</p>}
      <div className="hi42-list-region" aria-busy={state.phase === 'loading'}>
        {rows.length > 0 && (
        <ol className="hi42-list" aria-label="Asks, newest first">
          {rows.map((ask) => (
            <li key={ask.ask_id} className="hi42-row hi42-row-compact">
              <div className="hi42-row-content">
                <span className="hi42-row-title" data-question="">{ask.question || 'A question with no text'}</span>
                <span className="hi42-muted hi42-when">
                  {!market && MARKET_WORDS[ask.market] && <><span data-ask-market="">{MARKET_WORDS[ask.market]}</span><span aria-hidden="true">{' · '}</span></>}
                  <time dateTime={ask.at || undefined}>{when(ask.at)}</time>
                  <span aria-hidden="true">{' · '}</span>
                  <span className="hi42-state" data-status={askStatusKey(ask)}>{askStatus(ask)}</span>
                </span>
              </div>
              {ask.ask_id && (ask.status === 'complete' || ask.status === 'ok')
                && (ask.answer_status === 'complete' || ask.answer_status === 'partial')
                && <a className="hi42-link" href={'#/ask?follow=' + encodeURIComponent(ask.ask_id)}>Read answer</a>}
            </li>
          ))}
        </ol>
        )}
      </div>
      <Older state={state} load={load} />
    </div>
  );
}

function Briefs({onAuth, market = ''}){
  const [state, load] = usePages((before, options) => fetchHistoryBriefs({limit: BRIEFS_PAGE, before, market: market || undefined}, options), 'dates', onAuth);
  return (
    <div data-section="briefs">
      <ListState state={state} what="published briefs" empty={market ? 'No ' + MARKET_WORDS[market] + ' brief has been published yet.' : 'No brief has been published yet.'} onRetry={() => load(state.failedBefore)} />
      <div className="hi42-list-region" aria-busy={state.phase === 'loading'}>
        {state.rows.length > 0 && (
        <ol className="hi42-list" aria-label="Briefs, newest first">
          {state.rows.map((d) => (
            <li key={d.date} className="hi42-row hi42-row-compact">
              <div className="hi42-row-content">
                <a className="hi42-row-title" href={'#/today?date=' + encodeURIComponent(d.date)}>{longDate(d.date)}</a>
                <span className="hi42-muted">
                  {briefMarkets(d.markets)}
                </span>
                {briefCounts(d.markets) && <span className="hi42-muted" data-counts="">{briefCounts(d.markets)}</span>}
              </div>
            </li>
          ))}
        </ol>
        )}
      </div>
      <Older state={state} load={load} />
    </div>
  );
}

function Findings({onAuth, market = ''}){
  const [state, setState] = useState({phase: 'loading', data: null, error: null});
  const [shown, setShown] = useState(FINDINGS_PAGE);
  const handover = useAuthHandover(onAuth);
  useEffect(() => {
    const ctrl = new AbortController();
    fetchHistoryFindings(market ? {market} : {}, {signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setState({phase: 'ready', data: data || {}, error: null}); })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        setState({phase: 'error', data: null, error});
        handover(error);
      });
    return () => ctrl.abort();
  }, []);
  if (state.phase !== 'ready'){
    return <div data-section="findings"><ListState state={{...state, rows: []}} what="saved findings" empty="" /><div className="hi42-list-region" aria-busy={state.phase === 'loading'} /></div>;
  }
  const findings = state.data.findings;
  const rows = Array.isArray(findings) ? findings : [];
  return (
    <div data-section="findings">
      {state.data.note && <p className="hi42-line">{state.data.note === NOT_CHECKED ? FINDINGS_NOTE : state.data.note}</p>}
      {!Array.isArray(findings) || rows.length === 0
        ? <p className="hi42-status" role="status">{market ? 'No findings about ' + MARKET_WORDS[market] + ' are saved yet.' : 'No findings are saved yet.'}</p>
        : <div className="hi42-list-region" aria-busy={state.phase === 'loading'}><ol className="hi42-list" aria-label="Findings, newest first">
            {rows.slice(0, shown).map((f) => <Finding key={f.finding_id} finding={f} />)}
          </ol></div>}
      {rows.length > shown && <button type="button" className="hi42-button" onClick={() => setShown((n) => n + FINDINGS_PAGE)}>Show older</button>}
    </div>
  );
}

function SavedContextValue({value, topLevel = false}){
  if (Array.isArray(value)){
    return value.length > 0
      ? <ul>{value.map((entry, index) => <li key={index}><SavedContextValue value={entry} /></li>)}</ul>
      : <span>None recorded</span>;
  }
  if (value && typeof value === 'object'){
    const entries = Object.entries(value);
    return entries.length > 0
      ? <dl>{entries.map(([key, entry]) => {
          const market = topLevel && key === 'market' && typeof entry === 'string'
            ? (MARKET_WORDS[entry] ? MARKET_WORDS[entry] + ' (' + entry + ')' : entry)
            : null;
          const date = ['from', 'to'].includes(key) && typeof entry === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(entry)
            ? longDate(entry)
            : null;
          const child = market || date || entry;
          return (
            <div key={key}>
              <dt>{topLevel && key === 'market' ? 'Ask market' : words(key)}</dt>
              <dd><SavedContextValue value={child} /></dd>
            </div>
          );
        })}</dl>
      : <span>None recorded</span>;
  }
  if (value === null || value === undefined) return <span>Not recorded</span>;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return <span>{String(value)}</span>;
  return <span>Not recorded</span>;
}

/* The saved context in one line: the market asked about and the posts'
   dates, e.g. "Asked about South Africa, 21 to 27 September 2026". */
function savedContextLine(context){
  if (!context || typeof context !== 'object') return '';
  const market = typeof context.market === 'string' ? MARKET_WORDS[context.market] || '' : '';
  const window = context.window && typeof context.window === 'object' ? context.window : {};
  const day = /^\d{4}-\d{2}-\d{2}/;
  let span = '';
  if (day.test(String(window.from || '')) && day.test(String(window.to || ''))){
    const from = String(window.from).slice(0, 10);
    const to = String(window.to).slice(0, 10);
    span = from.slice(0, 7) === to.slice(0, 7)
      ? Number(from.slice(8)) + ' to ' + longDate(to)
      : from.slice(0, 4) === to.slice(0, 4)
        ? longDate(from).replace(/ \d{4}$/, '') + ' to ' + longDate(to)
        : longDate(from) + ' to ' + longDate(to);
  }
  if (market && span) return 'Asked about ' + market + ', ' + span;
  if (market) return 'Asked about ' + market;
  return span ? 'Posts from ' + span : '';
}

/* A finding keeps its evidence flags: each is shown as words. */
function Finding({finding}){
  const claims = Array.isArray(finding.claims) ? finding.claims : [];
  const evidence = Array.isArray(finding.evidence) ? finding.evidence : [];
  const answer = typeof finding.answer === 'string' && finding.answer.trim() ? finding.answer : '';
  const meta = [
    finding.as_of ? 'Saved ' + when(finding.as_of) : null,
    finding.status ? words(finding.status) : null,
    finding.valid_from ? 'Valid from ' + when(finding.valid_from) : null,
    finding.valid_to ? 'Valid to ' + when(finding.valid_to) : null,
  ].filter(Boolean).join(' · ');
  const hasSavedContext = finding.saved_context !== null && finding.saved_context !== undefined;
  const contextLine = hasSavedContext ? savedContextLine(finding.saved_context) : '';
  const unavailableReason = typeof finding.unavailable_reason === 'string' && finding.unavailable_reason.trim()
    ? finding.unavailable_reason
    : null;
  return (
    <li className="hi42-row hi42-finding" data-finding={finding.finding_id}>
      {finding.question && <span className="hi42-row-title">{finding.question}</span>}
      {answer && <p className="hi42-answer">{answer}</p>}
      {!answer && <p className="hi42-muted" data-answer-unavailable role="note">{unavailableReason ? 'Saved answer unavailable: ' + unavailableReason : 'Saved answer unavailable.'}</p>}
      {meta && <span className="hi42-muted">{meta}</span>}
      {/* The saved context carries the schema version and raw run and ask ids,
          so the reader sees one line and the rest sits closed behind a quiet toggle. */}
      {contextLine && <span className="hi42-muted" data-saved-context-line>{contextLine}</span>}
      {hasSavedContext && (
        <details className="hi42-technical" data-saved-context>
          <summary className="hi42-muted">How this was checked</summary>
          <SavedContextValue value={finding.saved_context} topLevel />
        </details>
      )}
      {claims.length > 0 && (
        <ul className="hi42-claims">
          {claims.map((c, i) => (
            <li key={i}>
              {c.label && <span className="hi42-label">{CONFIDENCE_WORDS[c.label] || c.label}</span>}
              {' '}{c.text}
            </li>
          ))}
        </ul>
      )}
      {evidence.length > 0 && (
        <ul className="hi42-evidence">
          {evidence.map((e) => {
            const flags = Array.isArray(e.flags) ? e.flags : [];
            const url = safeUrl(e.url);
            const sourceMarket = typeof e.source_market === 'string' && e.source_market.trim()
              ? 'Recorded source market: ' + (MARKET_WORDS[e.source_market] || e.source_market)
              : null;
            const meta = [platformWord(e.platform), e.handle, e.posted_at ? longDate(e.posted_at) : null, sourceMarket].filter(Boolean).join(' · ');
            return (
              <li key={e.id} data-evidence={e.id}>
                <span className="hi42-muted">{meta}</span>
                {flags.map((flag) => <span key={flag} className="hi42-flag">{flagWord(flag)}</span>)}
                {e.text && <span className="hi42-post-text">{e.text}</span>}
                {url && <a className="hi42-link" href={url} target="_blank" rel="noopener noreferrer">Open the post</a>}
              </li>
            );
          })}
        </ul>
      )}
    </li>
  );
}

function Search({onAuth, market: picked = ''}){
  const [q, setQ] = useState('');
  const [market, setMarket] = useState(picked);
  const [state, setState] = useState({phase: 'idle', data: null, error: null, market: ''});
  const handover = useAuthHandover(onAuth);
  const ctrl = useRef(null);
  useEffect(() => () => { if (ctrl.current) ctrl.current.abort(); }, []);
  /* Results link to the market they were searched in, not to a market
     picked since and not yet searched. */
  const submit = (event) => {
    event.preventDefault();
    if (ctrl.current) ctrl.current.abort();
    const c = new AbortController();
    ctrl.current = c;
    const sent = market;
    setState({phase: 'loading', data: null, error: null, market: sent});
    searchHistory(q.trim(), sent || null, {signal: c.signal})
      .then((data) => { if (!c.signal.aborted) setState({phase: 'ready', data: data || {}, error: null, market: sent}); })
      .catch((error) => {
        if (c.signal.aborted) return;
        setState({phase: 'error', data: null, error, market: sent});
        handover(error);
      });
  };
  const items = state.data && Array.isArray(state.data.items) ? state.data.items : [];
  return (
    <div data-section="search">
      <form className="hi42-search" onSubmit={submit} role="search">
        <label className="hi42-field">
          <span>Item name or alias</span>
          <input type="search" value={q} onChange={(event) => setQ(event.target.value)} maxLength={100} />
        </label>
        <label className="hi42-field">
          <span>Market</span>
          <select value={market} onChange={(event) => setMarket(event.target.value)}>
            <option value="">All markets</option>
            {Object.entries(MARKET_WORDS).map(([code, name]) => <option key={code} value={code}>{name}</option>)}
          </select>
        </label>
        <button type="submit" className="hi42-button">Search</button>
      </form>
      {state.phase === 'loading' && <p className="hi42-status" role="status">Searching</p>}
      {state.phase === 'error' && <p className="hi42-status" role="alert">{failure(state.error, 'the search')}</p>}
      {state.phase === 'ready' && items.length === 0 && <p className="hi42-status">No item in 42's map matches that.</p>}
      {items.length > 0 && (
        <ul className="hi42-list">
          {items.map((item) => {
            const waves = Array.isArray(item.waves) ? item.waves : [];
            const where = state.market || (waves[0] && waves[0].market) || 'ZA';
            const aliases = Array.isArray(item.aliases) ? item.aliases : [];
            return (
              <li key={item.item_id} className="hi42-row" data-item={item.item_id}>
                <a className="hi42-row-title" href={historyHref(item.item_id, where)}>{item.label || item.item_id}</a>
                <span className="hi42-muted">
                  {[item.kind ? words(item.kind) : null, aliases.length ? 'also seen as ' + aliases.join(', ') : null].filter(Boolean).join(' · ')}
                </span>
                {item.waves === null
                  ? <span className="hi42-muted">Waves are not measured yet.</span>
                  : waves.length > 0
                    ? <ul className="hi42-rows">
                        {waves.map((w) => (
                          <li key={w.market + w.peak_date}>
                            Peaked {longDate(w.peak_date)} in {marketWord(w.market)}
                            {isFigure(w.peak_posts) && <> at <Fig figure={w.peak_posts} /> {w.peak_posts.unit}</>}
                          </li>
                        ))}
                      </ul>
                    : <span className="hi42-muted">No waves yet.</span>}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/* ---------------- #/history/items/<id>?market= ---------------- */

export function HistoryItemPage42({itemId, market, onAuth}){
  const [load, setLoad] = useState({state: 'loading'});
  const [tick, setTick] = useState(0);
  const handover = useAuthHandover(onAuth);
  useEffect(() => {
    const ctrl = new AbortController();
    setLoad({state: 'loading'});
    fetchHistoryItem(itemId, market, {signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setLoad({state: 'ready', data: data || {}}); })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){ setLoad({state: 'auth'}); handover(error); }
        else if (error && error.status === 404) setLoad({state: 'missing', message: error.message});
        else setLoad({state: 'error', message: failure(error, 'this item')});
      });
    return () => ctrl.abort();
  }, [itemId, market, tick]);

  if (load.state === 'loading'){
    return <section className="page hi42" aria-busy="true"><p className="hi42-status" role="status">Loading the item's history</p></section>;
  }
  if (load.state === 'auth'){
    return <section className="page hi42"><p className="hi42-status" role="status">Enter the passcode to read this item's history.</p></section>;
  }
  if (load.state === 'missing'){
    return (
      <section className="page hi42">
        <h1 className="hi42-heading">Item not found</h1>
        <p className="hi42-status">{load.message}</p>
      </section>
    );
  }
  if (load.state === 'error'){
    return (
      <section className="page hi42">
        <h1 className="hi42-heading">The item's history could not load</h1>
        <p className="hi42-status" role="alert">{load.message}</p>
        <button type="button" className="hi42-button" onClick={() => setTick((t) => t + 1)}>Try again</button>
      </section>
    );
  }
  const data = load.data;
  const where = data.market || market;
  const waves = data.waves;
  const analogues = data.analogues;
  const sub = [
    data.kind ? words(data.kind) : null,
    marketWord(where),
    data.as_of ? 'as of ' + longDate(data.as_of) : null,
  ].filter(Boolean).join(' · ');
  return (
    <section className="page hi42">
      <header className="hi42-head">
        <h1 className="hi42-heading">{data.label || itemId}</h1>
        <p className="hi42-sub">{sub}</p>
        {data.current_day > 0 && <p className="hi42-lead">Day {data.current_day} of its current wave</p>}
      </header>

      <WaveTimeline waves={waves} analogues={analogues} />

      <div className="hi42-grid">
        <section className="hi42-part" data-section="waves">
          <h2 className="hi42-part-title">Waves</h2>
          {waves === null || waves === undefined
            ? <p className="hi42-line">Waves are not measured yet.</p>
            : waves.length > 0
              ? <ol className="hi42-rows">
                  {waves.map((w) => (
                    <li key={w.wave_start}>
                      Peaked {longDate(w.peak_date)}
                      {isFigure(w.peak_posts) && <> at <Fig figure={w.peak_posts} /> {w.peak_posts.unit}</>}
                      {isFigure(w.above_half_days) && <>; <Fig figure={w.above_half_days} /> {w.above_half_days.unit}</>}
                      {'; ' + longDate(w.wave_start) + ' to ' + longDate(w.wave_end)}
                      {w.completed === false ? ', still going' : w.completed ? ', finished' : ''}
                    </li>
                  ))}
                </ol>
              : <p className="hi42-line">No waves in {marketWord(where)} yet.</p>}
        </section>
        {isFigure(data.recurrences) && (
          <section className="hi42-part" data-section="recurrences">
            <h2 className="hi42-part-title">Recurrences</h2>
            <p className="hi42-line"><Fig figure={data.recurrences} /> {data.recurrences.unit}</p>
          </section>
        )}
      </div>

      <section className="hi42-part" data-section="analogues">
        <h2 className="hi42-part-title">Items that moved like this</h2>
        {analogues === null || analogues === undefined
          ? <p className="hi42-line">{data.note || NO_MAP}</p>
          : analogues.length > 0
            ? <>
                <p className="hi42-line">Each is aligned on days since its wave began, read from day {data.current_day} onward.</p>
                <ul className="hi42-list">
                  {analogues.map((a) => <Analogue key={a.item_id} analogue={a} market={where} />)}
                </ul>
              </>
            : <p className="hi42-line">No similar item has a finished wave in {marketWord(where)} yet.</p>}
      </section>

      <p className="hi42-actions">
        <a className="hi42-link" href={topicHref(data.item_id || itemId, where)}>Open the topic</a>
      </p>
    </section>
  );
}

/* The item's waves, then its analogues' waves, on one time axis. The latest
   wave of the item is the one in focus; nothing is drawn without a wave. */
function WaveTimeline({waves, analogues}){
  if (!Array.isArray(waves) || waves.length === 0) return null;
  const latest = waves.reduce((a, w) => (String(w.wave_start) > String(a.wave_start) ? w : a));
  const peak = (f) => (isFigure(f) ? f.value : null);
  const rows = [
    ...waves.map((w) => ({
      key: 'w' + w.wave_start, label: longDate(w.wave_start).replace(/^\d+ /, ''),
      start: w.wave_start, peak: w.peak_date, end: w.completed ? w.wave_end : null,
      peakValue: peak(w.peak_posts), queryId: isFigure(w.peak_posts) ? w.peak_posts.query_id : undefined, focus: w === latest,
    })),
    ...(Array.isArray(analogues) ? analogues : []).filter((a) => a.wave && a.wave.start).map((a) => ({
      key: 'a' + a.item_id, label: a.label || a.item_id,
      start: a.wave.start, peak: a.wave.peak_date, end: a.wave.end || null, peakValue: peak(a.peak_posts),
      queryId: isFigure(a.peak_posts) ? a.peak_posts.query_id : undefined,
    })),
  ];
  const days = rows.flatMap((r) => [r.start, r.end || r.peak]).filter(Boolean).sort();
  return (
    <RangeTimeline title="Each wave, from first rise to fade" data="data-wave-timeline" unit="posts at peak" rows={rows}
      caption={'Waves from ' + longDate(days[0]) + ' to ' + longDate(days[days.length - 1]) + ', posts on each peak day'} />
  );
}

function Days({figure, later, earlier}){
  const n = figure.value;
  return n >= 0
    ? <>{later} <Fig figure={figure}>{readerFigure(n)}</Fig> {n === 1 ? 'day' : 'days'} later</>
    : <>{earlier} <Fig figure={figure}>{readerFigure(-n)}</Fig> {n === -1 ? 'day' : 'days'} earlier</>;
}

function Analogue({analogue: a, market}){
  return (
    <li className="hi42-row" data-analogue={a.item_id}>
      <a className="hi42-row-title" href={historyHref(a.item_id, market)}>{a.label || a.item_id}</a>
      {a.wave && a.wave.start && <span className="hi42-muted">Wave of {longDate(a.wave.start)} to {longDate(a.wave.end)}</span>}
      <span>
        {isFigure(a.days_to_peak) ? <Days figure={a.days_to_peak} later="It peaked" earlier="It had peaked" /> : 'Peak not measured'}
        {isFigure(a.peak_posts) && <>, at <Fig figure={a.peak_posts} /> {a.peak_posts.unit}</>}
        {'; '}
        {isFigure(a.days_to_fade) ? <Days figure={a.days_to_fade} later="it fell below half its peak" earlier="it had fallen below half its peak" /> : 'fade not measured'}
      </span>
    </li>
  );
}

