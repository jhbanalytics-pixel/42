import {useEffect, useMemo, useRef, useState} from 'react';
import {
  DiscoverInstrument,
  EvidenceRoom,
  StateView,
} from 'ogilvy-intelligence-design-system';
import {buildTopicHash, go} from './router.js';
import {adaptSignalToInstrumentModel} from './instrumentAdapters.js';
import {HELD_FRAME_ROWS, RULED_COPY, packageBriefingModel, routeRunFacts, routeStateView, tryAgainAction} from './instrumentRouteModels.js';
import {releasedRunLabel} from './plainLabels.js';
import {regionLabel} from './model.js';
/* The package's route-surface rules travel with the route chunk, so the
   instrument cannot mount before its stylesheet exists. main.jsx still loads
   the same half after first paint for whatever else composes it. */
import './styles/instrument-route-surfaces.css';
import './ogilvy-intelligence.css';

export const DISCOVERY_MODES = Object.freeze([
  'phrase', 'hashtag', 'sound', 'creator', 'entity',
]);
export const READINESS_STATES = Object.freeze(['ready', 'thin', 'contradictory', 'unchecked']);

const DISCOVERY_MODE_SET = new Set(DISCOVERY_MODES);
const READINESS_STATE_SET = new Set(READINESS_STATES);
const SIGNAL_ID = /^sig_[0-9a-f]{64}$/;
export const QUALITY_FIELDS = Object.freeze(['velocity', 'novelty', 'breadth', 'independence', 'history', 'geo_confidence']);
export const QUALITY_LABELS = Object.freeze({
  velocity: 'Velocity',
  novelty: 'Novelty',
  breadth: 'Breadth',
  independence: 'Source independence',
  history: 'History',
  geo_confidence: 'Geographic confidence',
});
const QUALITY_WORDS = new Set(['low', 'moderate', 'high', 'unmeasured']);
const UNAVAILABLE = 'Unavailable';

function sourceField(row, key){
  if (!row || typeof row !== 'object') return undefined;
  if (row[key] !== undefined && row[key] !== null) return row[key];
  return row.signal && typeof row.signal === 'object' ? row.signal[key] : undefined;
}

function boundedText(value, limit=2000){
  if (typeof value !== 'string') return '';
  const text = value.trim();
  if (!text) return '';
  // The engine bounds its text in Unicode code points. String.length counts
  // UTF-16 code units, so measuring it here would silently drop a conformant
  // value the moment it carried an emoji and show Unavailable in its place.
  return [...text].length <= limit ? text : '';
}

function firstText(...values){
  for (const value of values){
    const text = boundedText(value);
    if (text) return text;
  }
  return '';
}

function firstBoundedText(limit, ...values){
  for (const value of values){
    const text = boundedText(value, limit);
    if (text) return text;
  }
  return '';
}

function absoluteHttpUrl(value){
  const text = boundedText(value, 2048);
  if (!text) return '';
  try {
    const url = new URL(text);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
  } catch {
    return '';
  }
}

function firstAbsoluteHttpUrl(...values){
  for (const value of values){
    const url = absoluteHttpUrl(value);
    if (url) return url;
  }
  return '';
}

function objectReceipt(item){
  const url = firstAbsoluteHttpUrl(item.url, item.source_url, item.uri);
  const id = firstBoundedText(256, item.id, item.evidence_id, item.receipt_id, item.row_id) || url;
  if (!id) return null;
  return Object.freeze({
    id,
    url,
    platform: firstText(item.platform, item.source_family),
    author: firstText(item.author, item.author_label, item.handle),
    snippet: firstText(item.snippet, item.excerpt, item.text),
    metric: firstText(item.metric, item.metric_label),
    age: firstText(item.age, item.published_at),
  });
}

function tupleReceipt(item){
  if (item.length !== 6 || item.some((value) => typeof value !== 'string')) return null;
  const url = absoluteHttpUrl(item[5]);
  if (!url) return null;
  return Object.freeze({
    id: url,
    url,
    platform: item[0].trim(),
    author: item[1].trim(),
    snippet: item[2].trim(),
    metric: item[3].trim(),
    age: item[4].trim(),
  });
}

export function normalizeExploreReceipts(value){
  const receipts = [];
  const seen = new Set();
  for (const item of Array.isArray(value) ? value : []){
    const receipt = Array.isArray(item)
      ? tupleReceipt(item)
      : item && typeof item === 'object'
        ? objectReceipt(item)
        : null;
    if (!receipt || seen.has(receipt.id)) continue;
    seen.add(receipt.id);
    receipts.push(receipt);
  }
  return receipts;
}

function attachedReceipts(row){
  for (const key of ['receipts', 'evidence', 'references']){
    const value = sourceField(row, key);
    if (Array.isArray(value)) return value;
  }
  return [];
}

/* Qualities arrive as words. A number here would be an engine score that
   escaped its band, so it is refused and read as unmeasured: a measurement
   nobody can state is not a measurement. */
function qualitiesFor(row){
  const source = sourceField(row, 'qualities');
  const declared = source && typeof source === 'object' ? source : {};
  if (!source || typeof source !== 'object') return null;
  const qualities = {};
  for (const field of QUALITY_FIELDS){
    const word = declared[field];
    if (typeof word !== 'string' || !QUALITY_WORDS.has(word)) return null;
    qualities[field] = word;
  }
  return qualities;
}

function topicTagsFor(row){
  const source = sourceField(row, 'qualities');
  const tags = source && typeof source === 'object' ? source.topic_tags : null;
  if (!Array.isArray(tags)) return null;
  if (tags.some((tag) => typeof tag !== 'string' || !tag.trim() || tag !== tag.trim())) return null;
  if (new Set(tags).size !== tags.length) return null;
  return [...tags];
}

function observationFor(row){
  const start = boundedText(sourceField(row, 'observation_start'));
  const end = boundedText(sourceField(row, 'observation_end'));
  const method = boundedText(sourceField(row, 'observation_method'));
  return start && end && method ? Object.freeze({start, end, method}) : null;
}

function admittedSignal(row){
  if (sourceField(row, 'contract_version') !== 'desk_dynamic_signal_v2') return null;
  const signalId = boundedText(sourceField(row, 'signal_id'), 68);
  const discoveryMode = boundedText(sourceField(row, 'discovery_mode'), 32);
  const declaredReadiness = boundedText(sourceField(row, 'evidence_state'), 32);
  const title = firstText(
    sourceField(row, 'signal_name'),
    sourceField(row, 'title'),
    sourceField(row, 'label'),
  );
  if (
    !SIGNAL_ID.test(signalId)
    || !DISCOVERY_MODE_SET.has(discoveryMode)
    || !READINESS_STATE_SET.has(declaredReadiness)
    || !title
  ) return null;
  const qualities = qualitiesFor(row);
  const topicTags = topicTagsFor(row);
  if (!qualities || !topicTags) return null;
  const receipts = normalizeExploreReceipts(attachedReceipts(row));
  return Object.freeze({
    signalId,
    title,
    discoveryMode,
    whyNow: boundedText(sourceField(row, 'why_now')) || UNAVAILABLE,
    response: boundedText(sourceField(row, 'possible_response')) || UNAVAILABLE,
    receipts,
    receiptCount: receipts.length,
    readiness: receipts.length ? declaredReadiness : 'unchecked',
    observation: observationFor(row),
    market: boundedText(sourceField(row, 'market'), 8),
    qualities,
    topicTags,
  });
}

export function admitExploreSignals(rows){
  const signals = [];
  const seen = new Set();
  let duplicateCount = 0;
  for (const row of Array.isArray(rows) ? rows : []){
    const signal = admittedSignal(row);
    if (!signal) continue;
    if (seen.has(signal.signalId)){
      duplicateCount += 1;
      continue;
    }
    seen.add(signal.signalId);
    signals.push(signal);
  }
  return {signals, diagnostic: {duplicateCount}};
}

function checkedAt(freshness){
  return freshness && (freshness.checkedAt || freshness.checked_at || freshness.stamp_utc);
}

/* A failed read carries its code and nothing else: the package words the
   frame and the code sits inside Details. The code is the one the read
   recorded, the status where it recorded none, and unstated where the caller
   handed over neither; never a code no read produced. */
function errorCode(error){
  const code = error && typeof error === 'object' ? error.code : null;
  if (typeof code === 'string' && code.trim()) return code.trim();
  const status = error && typeof error === 'object' ? error.status : null;
  return Number.isInteger(status) ? 'http_' + status : 'unstated';
}

/* Open Discover readiness is the released run's own, decided by what the run
   admitted and nothing else. The desk freshness stamp measures the ingest
   pipeline that feeds the next run, not this one, and the shell shows it in
   the utility strip either way. A run that admitted nothing is the branch the
   stamp can still date, so the stale card stays there. */
export function buildExploreState({topics, freshness, error, loading}={}){
  if (error) return {state: 'error', code: errorCode(error)};
  if (loading) return {state: 'loading'};
  const admitted = admitExploreSignals(topics);
  if (admitted.signals.length) return {state: 'ready', ...admitted};
  if (freshness && freshness.status === 'amber'){
    const timestamp = checkedAt(freshness);
    return timestamp ? {state: 'stale', checkedAt: timestamp} : {state: 'stale'};
  }
  return {state: 'no_discovery', diagnostic: admitted.diagnostic};
}

export function filterExploreSignals(signals, {readiness='all', mode='all'}={}){
  const rows = Array.isArray(signals) ? signals : [];
  return rows.filter((signal) => (
    (readiness === 'all' || signal.readiness === readiness)
    && (mode === 'all' || signal.discoveryMode === mode)
  ));
}

export function initialExploreSelection(signals){
  return Array.isArray(signals) && signals[0] ? signals[0].signalId : '';
}

export function resetExploreSelection(signals, setSelectedId, setDrawerOpen){
  setSelectedId(initialExploreSelection(signals));
  setDrawerOpen(false);
}

export function selectExploreSignal(signalId, setSelectedId, leadRoot, schedule){
  setSelectedId(signalId);
  const run = typeof schedule === 'function'
    ? schedule
    : typeof requestAnimationFrame === 'function'
      ? requestAnimationFrame
      : (callback) => callback();
  run(() => {
    const heading = leadRoot && leadRoot.querySelector('.discover-instrument__proposition, .dossier-lead__title');
    if (!heading) return;
    if (!heading.hasAttribute || !heading.hasAttribute('tabindex')) heading.setAttribute?.('tabindex', '-1');
    try { heading.focus({preventScroll: true}); } catch { heading.focus?.(); }
  });
}

export function evidenceForSignal(signals, signalId){
  const selected = (Array.isArray(signals) ? signals : []).find((signal) => signal.signalId === signalId);
  return selected ? selected.receipts : [];
}

const BROWSE_EVIDENCE = Object.freeze({id: 'browse-evidence', label: 'Browse evidence', onClick: () => go('/browse')});
const OPEN_SOURCE_LAB = Object.freeze({id: 'open-source-lab', label: 'Open Source Lab', onClick: () => go('/source-lab')});

/* Every held, withheld, loading, error and empty branch of Discover renders the
   package state frame through the shared route mapping, and the package words
   the loading, stale, error and filtered-empty frames. The no discovery frame
   carries the ruled copy on the empty variant, because the desk sends no run
   authority. The wrapper keeps data-explore-state for the route assertions
   and the QA harness.

   The loading branch takes a class the other branches do not, the same one
   Briefing's loading wrapper takes. The width law in the boot sheet governs
   held cards, and a loading card holds the place of the selection rather than
   answering in its own right, so it takes the corner the settle starts from
   instead of the card's centring. The class is the hook the sheet undoes the
   law through, and it is a class rather than a second selector in the sheet
   because the host's share of the render blocking stylesheet has eight bytes
   of room in it and a route named there costs thirty. */
function ExploreState({state, view, facts}){
  return (
    <section
      className={`explore-state explore-state--${state}${state === 'loading' ? ' loading-card' : ''}`}
      data-explore-state={state}
    >
      <StateView {...routeStateView(view, facts)} />
    </section>
  );
}

function instrumentCandidates(topics, signals){
  const rows = Array.isArray(topics) ? topics : [];
  return signals.map((signal) => {
    const row = rows.find((candidate) => sourceField(candidate, 'signal_id') === signal.signalId);
    const adapted = adaptSignalToInstrumentModel(row);
    return adapted && adapted.state !== 'unavailable'
      ? {id: signal.signalId, model: packageBriefingModel(adapted), admission: signal}
      : null;
  }).filter(Boolean);
}

function filterInstrumentCandidates(candidates, filters){
  const query = filters.query.trim().toLowerCase();
  return candidates.filter(({model}) => (
    (!query || model.title.toLowerCase().includes(query))
    && (!filters.evidenceStates.length || filters.evidenceStates.includes(model.state))
  ));
}

/* The folio names a run the way a reader would say it, by the day it ran.
   The engine's run id stays under the summary for anyone who has to quote
   it back to support. Only a run id carrying a real calendar date as its
   own segment is renamed; any other label is shown as the engine gave it,
   because the run facts say nothing about a run being latest or complete. */
const RUN_DATE = /(?:^|_)(\d{4})(\d{2})(\d{2})(?:_|$)/;

export function runDisplayLabel(label){
  const text = String(label || '');
  if (!/^Run /.test(text)) return text;
  const match = RUN_DATE.exec(text.slice(4));
  if (!match) return text;
  const [year, month, day] = match.slice(1).map(Number);
  if (month < 1 || month > 12) return text;
  const date = new Date(Date.UTC(year, month - 1, day));
  if (Number.isNaN(date.getTime()) || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return text;
  return 'Run of ' + date.toLocaleDateString('en-ZA', {timeZone: 'UTC', day: 'numeric', month: 'short', year: 'numeric'});
}

/* A machine reference set in a narrow column: a line may break after each
   underscore or space, and never inside a part, so the id keeps every
   character a reader copies and wraps at its joints rather than mid token. */
function breakableReference(text){
  const parts = String(text).split(/(?<=[_ ])/);
  return parts.map((part, index) => (
    index < parts.length - 1 ? [part, <wbr key={index} />] : part
  ));
}

export function ExplorePage({topics, freshness, loading=false, error=null, region='ALL', onRetry}){
  const view = useMemo(
    () => buildExploreState({topics, freshness, loading, error}),
    [topics, freshness, loading, error],
  );
  const signals = view.state === 'ready' ? view.signals : [];
  const firstSignalId = initialExploreSelection(signals);
  const [selectedId, setSelectedId] = useState(firstSignalId);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [filters, setFilters] = useState({query: '', evidenceStates: []});
  const leadRoot = useRef(null);
  const evidenceInvoker = useRef(null);
  const restoreEvidenceInvoker = useRef(false);

  useEffect(() => {
    resetExploreSelection(signals, setSelectedId, setDrawerOpen);
  }, [region, firstSignalId]);

  useEffect(() => {
    if (drawerOpen || !restoreEvidenceInvoker.current) return undefined;
    const invoker = evidenceInvoker.current;
    restoreEvidenceInvoker.current = false;
    evidenceInvoker.current = null;
    const timer = setTimeout(() => {
      if (invoker?.isConnected && typeof invoker.focus === 'function') invoker.focus();
    }, 0);
    return () => clearTimeout(timer);
  }, [drawerOpen]);

  useEffect(() => {
    if (!drawerOpen) return undefined;
    const onKeyDown = (event) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      restoreEvidenceInvoker.current = true;
      setDrawerOpen(false);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [drawerOpen]);

  const openEvidence = (target) => {
    const signalId = target?.kind === 'signal' ? target.id : selectedId;
    evidenceInvoker.current = typeof document === 'undefined' ? null : document.activeElement;
    setSelectedId(signalId);
    setDrawerOpen(true);
  };

  const closeEvidence = () => {
    restoreEvidenceInvoker.current = true;
    setDrawerOpen(false);
  };

  const facts = routeRunFacts({topics, freshness});
  const runId = facts.method && facts.method.label.startsWith('Run ') ? facts.method.label.slice(4) : null;
  const market = regionLabel(String(region || 'ALL').toUpperCase());

  if (view.state === 'error'){
    return (
      <div className="page explore-page">
        <ExploreState state="error" facts={facts} view={{state: 'error', code: view.code, actions: [tryAgainAction(onRetry)]}} />
      </div>
    );
  }
  if (view.state === 'loading'){
    return (
      <div className="page explore-page">
        <ExploreState state="loading" facts={facts} view={{
          state: 'loading',
          task: 'Checking admitted dynamic signals',
          reservedRows: HELD_FRAME_ROWS.stale,
        }} />
      </div>
    );
  }
  if (view.state === 'stale'){
    return (
      <div className="page explore-page">
        <ExploreState state="stale" facts={facts} view={{state: 'stale', checkedAt: view.checkedAt, actions: [BROWSE_EVIDENCE]}} />
      </div>
    );
  }
  if (view.state === 'no_discovery'){
    return (
      <div className="page explore-page">
        <ExploreState state="no_discovery" facts={facts} view={{
          state: 'empty',
          ...RULED_COPY.noDiscovery,
          nextAction: BROWSE_EVIDENCE.label,
          actions: [BROWSE_EVIDENCE, OPEN_SOURCE_LAB],
        }} />
      </div>
    );
  }

  const candidates = instrumentCandidates(topics, signals);
  const filtered = filterInstrumentCandidates(candidates, filters);

  if (!filtered.length){
    return (
      <div className="page explore-page" data-explore-state="ready" data-duplicate-count={view.diagnostic.duplicateCount}>
        <div className="explore-layout">
          <p className="explore-state__kicker">Discover · {market}</p>
          <p role="status">{filtered.length} of {signals.length} signals shown</p>
          <ExploreState state="filtered-empty" facts={facts} view={{
            state: 'empty',
            actions: [{id: 'clear-filters', label: 'Clear the filter', onClick: () => setFilters({query: '', evidenceStates: []})}],
          }} />
        </div>
      </div>
    );
  }

  const selected = filtered.find((candidate) => candidate.id === selectedId) || filtered[0];
  /* A candidate whose engine sent an explicit evidence summary over a closed
     window with an unvalidated authority is held, and the hold is named with
     the engine's limitation. A legacy row with no summary at all, and a
     summary the package validator refused (its ribbon carries the token
     evidence_summary_invalid), keep the package's own unavailable cells. */
  const refusedSummary = Array.isArray(selected.model.ribbonModel?.limitations) && selected.model.ribbonModel.limitations.includes('evidence_summary_invalid');
  const heldAuthority = !refusedSummary
    && selected.model.state === 'unchecked'
    && selected.model.evidenceSummary?.independence?.status === 'unvalidated'
    && selected.model.evidenceSummary?.window?.closed === true;
  /* Discover is for choosing a candidate; Briefing carries the editorial
     lead. The package specimen alone owns the proposition here, so the route
     carries one h1 for the selected candidate below the folio band. Round 9
     of the 42 in Black programme removed the host EditorialLead that
     repeated the title and the Why Now sentence above the instrument. */

  return (
    <div className="page explore-page" data-explore-state="ready" data-duplicate-count={view.diagnostic.duplicateCount}>
      <div className="explore-layout">
        <header className="explore-folio">
          {/* Quiet register, 23 Sept 2026: the heading says what the page is,
              so no eyebrow sits over it, and the market is not repeated here
              because the shell header states the scope on every page. */}
          <h1>Discover</h1>
          {/* The run is named by its date, with the story link beside it. Its
              id is a reference to quote, not a heading, so it waits in a
              disclosure on its own line under that row, set as the bare id a
              reader copies (the summary already says it is the run's), and it
              breaks only where its own underscores join it. Opening it grows
              the band downward and moves no control beside it. */}
          {facts.method ? <p className="explore-folio__run">{runId ? releasedRunLabel(runId) : facts.method.label}</p> : null}
          {heldAuthority ? null : <p className="explore-folio__open"><a className="route-open-action" href={buildTopicHash(selected.id, String(region || 'ALL'))}>Open topic story</a></p>}
          {facts.method && runId ? <details className="explore-folio__run-reference"><summary>Run reference</summary><code>{breakableReference(runId)}</code></details> : null}
        </header>
        {heldAuthority ? <ExploreState state="held" facts={facts} view={{
          state: 'unavailable',
          title: 'Held until the evidence authority is checked',
          body: selected.model.limitations[0] || 'Source independence has not been validated for this candidate.',
          reason: 'evidence_authority_unchecked',
          missing: ['source_independence'],
          actions: [OPEN_SOURCE_LAB],
        }} /> : null}
        <p className="explore-filters__count" role="status">
          {filtered.length} of {signals.length} signals shown
        </p>
        {/* The route always selects a candidate, filtered[0] where the reader
            has picked none, so the pick prompt only ever applies where more
            than one is on offer. It printed under a list holding the only
            candidate there is, which asks the reader to choose between one
            thing. The count the route offered is stated here and the sheet
            holds the prompt back at one. */}
        <div className="explore-selection" data-candidate-count={filtered.length} ref={leadRoot}>
          <DiscoverInstrument
            candidates={filtered}
            selectedId={selected.id}
            filters={filters}
            sourceSpecimens={[]}
            onSelect={(signalId) => selectExploreSignal(signalId, setSelectedId, leadRoot.current)}
            onFiltersChange={setFilters}
            onOpenEvidence={openEvidence}
          />
        </div>
      </div>
      <EvidenceRoom
        open={drawerOpen}
        signalTitle={selected.model.title}
        summary={selected.model.evidenceSummary}
        ribbonModel={selected.model.ribbonModel}
        onClose={closeEvidence}
      />
    </div>
  );
}

export default ExplorePage;
