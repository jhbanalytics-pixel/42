import {useEffect, useMemo, useState} from 'react';
import {ComparisonInstrument, StateView} from 'ogilvy-intelligence-design-system';

import {admitExploreSignals, filterExploreSignals} from './explore.jsx';
import {adaptSignalToInstrumentModel} from './instrumentAdapters.js';
import {RULED_COPY, packageBriefingModel, routeRunFacts, routeStateView, tryAgainAction} from './instrumentRouteModels.js';
import {go} from './router.js';
/* Route-surface rules travel with the chunk; see explore.jsx. */
import './styles/instrument-route-surfaces.css';
import './ogilvy-intelligence.css';

const COMPARISON_FIELDS = Object.freeze([
  {id: 'whyNow', label: 'Why now', unit: null},
  {id: 'possibleResponse', label: 'Possible response', unit: null},
]);

const OPEN_DISCOVER = Object.freeze({id: 'open-discover', label: 'Open Discover', onClick: () => go('/explore')});

/* Every error, loading and insufficient branch of Compare renders the package
   state frame through the shared route mapping, and the package words the
   loading and error frames. The insufficient frame is Compare's own words on
   the empty variant, because only Compare knows the admitted count. Each
   wrapper writes its own data-compare-state literally, because the
   responsive contract reads this file's source for the three handles. */
function CompareState({view, facts}){
  return <StateView {...routeStateView(view, facts)} />;
}

function sourceSignalId(row){
  if (!row || typeof row !== 'object') return null;
  return row.signal_id || row.signal?.signal_id || null;
}

const DROP_COPY = Object.freeze({
  instrument_payload_unavailable: 'an unreadable payload',
  evidence_summary_invalid: 'an evidence summary the validator refused',
  evidence_authority_unchecked: 'an unchecked evidence authority',
  ribbon_series_missing: 'no evidence ribbon',
});

/* A row the comparison cannot place says why, in the adapter's own terms:
   the payload was refused, the package validator refused the summary, the
   engine sent an unchecked authority, or the ribbon has no strands. */
function comparisonModel(row){
  const adapted = adaptSignalToInstrumentModel(row);
  if (!adapted || adapted.state === 'unavailable') return {drop: 'instrument_payload_unavailable'};
  const briefing = packageBriefingModel(adapted);
  const limitations = Array.isArray(briefing?.ribbonModel?.limitations) ? briefing.ribbonModel.limitations : [];
  if (!briefing || limitations.includes('evidence_summary_invalid')) return {drop: 'evidence_summary_invalid'};
  if (adapted.evidenceSummary.state === 'unchecked') return {drop: 'evidence_authority_unchecked'};
  if (!Array.isArray(briefing.ribbonModel?.strands) || briefing.ribbonModel.strands.length === 0) return {drop: 'ribbon_series_missing'};
  return {model: {
    id: adapted.id,
    title: adapted.title,
    evidenceSummary: briefing.evidenceSummary,
    ribbonModel: briefing.ribbonModel,
    audienceBasis: adapted.audienceBasis,
    metrics: {whyNow: adapted.whyNow, possibleResponse: adapted.possibleResponse},
  }};
}

/* The frame for admitted signals the comparison cannot place. Only a set of
   unchecked authorities is a hold; any other drop is named by its reason. */
function unplacedView(signals, dropped, actions){
  const codes = [...new Set(dropped)];
  if (codes.length === 1 && codes[0] === 'evidence_authority_unchecked'){
    return {
      state: 'unavailable',
      title: 'Held until the evidence authority is checked',
      body: `The completed run admitted ${signals.length} signals, but their evidence authority is unchecked, so nothing is placed side by side.`,
      reason: 'evidence_authority_unchecked',
      missing: ['source_independence'],
      actions,
    };
  }
  const counts = codes.map((code) => `${dropped.filter((item) => item === code).length} with ${DROP_COPY[code]}`).join(', ');
  return {
    state: 'unavailable',
    title: 'Not enough could be placed side by side',
    body: `The completed run admitted ${signals.length} signals. ${dropped.length} could not be placed: ${counts}.`,
    reason: codes[0],
    missing: codes,
    actions,
  };
}

export function ComparePanel({topics, region = 'ALL', loading = false, error = null, freshness = null, onRetry}){
  const {signals} = useMemo(() => admitExploreSignals(topics), [topics]);
  const placed = useMemo(() => signals.map((signal) => {
    const row = (Array.isArray(topics) ? topics : []).find((candidate) => sourceSignalId(candidate) === signal.signalId);
    return comparisonModel(row);
  }), [signals, topics]);
  const models = useMemo(() => placed.filter((entry) => entry.model).map((entry) => entry.model).slice(0, 5), [placed]);
  const dropped = placed.filter((entry) => entry.drop).map((entry) => entry.drop);
  const [selectedIds, setSelectedIds] = useState([]);
  const eligibleIds = useMemo(() => new Set(models.map((model) => model.id)), [models]);

  useEffect(() => {
    setSelectedIds((current) => {
      const next = current.filter((id) => eligibleIds.has(id));
      return next.length === current.length ? current : next;
    });
  }, [eligibleIds]);

  const facts = routeRunFacts({topics, freshness});

  if (error){
    return (
      <div className="page compare-page">
        <section className="explore-state explore-state--error" data-compare-state="error">
          <CompareState facts={facts} view={{state: 'error', code: error.code, actions: [tryAgainAction(onRetry)]}} />
        </section>
      </div>
    );
  }
  if (loading){
    return (
      <div className="page compare-page">
        <section className="explore-state explore-state--loading" data-compare-state="loading">
          <CompareState facts={facts} view={{state: 'loading', task: 'Checking which discovered signals can be compared'}} />
        </section>
      </div>
    );
  }
  /* The count in the sentence is what the run admitted, which is the count
     Compare is the only route to hold. It is not the count of comparable
     models: a run can admit a signal the comparison cannot place, and saying
     the run admitted nothing then is false about the run. The no discovery
     frame belongs to a run that admitted nothing at all. */
  /* A run that admitted nothing is dated by the freshness stamp, as on the
     other desk routes: amber with a stamp is stale, not empty. The stamp
     itself reaches the frame through the run facts, the one derivation every
     desk route reads it through. */
  if (signals.length === 0 && freshness && freshness.status === 'amber'){
    return (
      <div className="page compare-page">
        <section className="explore-state explore-state--stale" data-compare-state="stale">
          <CompareState facts={facts} view={{state: 'stale', actions: [OPEN_DISCOVER]}} />
        </section>
      </div>
    );
  }
  /* Signals the run admitted but the comparison cannot place are named by
     the reason each was dropped. Saying the run admitted two and needs two
     contradicted itself. */
  if (signals.length >= 2 && models.length < 2){
    const view = unplacedView(signals, dropped, [OPEN_DISCOVER]);
    const handle = view.reason === 'evidence_authority_unchecked' ? 'held' : 'unplaced';
    return (
      <div className="page compare-page">
        <section className={`explore-state explore-state--${handle}`} data-compare-state={handle}>
          <CompareState facts={facts} view={view} />
        </section>
      </div>
    );
  }
  if (models.length < 2){
    return (
      <div className="page compare-page">
        <section className="explore-state explore-state--insufficient" data-compare-state="insufficient">
          <CompareState facts={facts} view={{
            state: 'empty',
            ...(signals.length === 0 ? RULED_COPY.noDiscovery : RULED_COPY.insufficient(signals.length)),
            actions: [OPEN_DISCOVER],
          }} />
        </section>
      </div>
    );
  }

  const toggleSelection = (signalId) => {
    setSelectedIds((current) => {
      const eligible = current.filter((id) => eligibleIds.has(id));
      if (!eligibleIds.has(signalId)) return eligible;
      if (eligible.includes(signalId)) return eligible.filter((id) => id !== signalId);
      return eligible.length < 5 ? [...eligible, signalId] : eligible;
    });
  };

  return (
    <div className="page compare-page" data-compare-state="ready" data-market-scope={String(region).toUpperCase()}>
      <ComparisonInstrument
        signals={models}
        fields={COMPARISON_FIELDS}
        selectedIds={selectedIds}
        onSelect={toggleSelection}
      />
    </div>
  );
}

export {filterExploreSignals};
export default ComparePanel;
