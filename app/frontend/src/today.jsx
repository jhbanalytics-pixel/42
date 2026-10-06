import React from 'react';
import {InstrumentBriefing, StateView} from 'ogilvy-intelligence-design-system';

import {buildBriefingState} from './briefingContract.js';
import {adaptSignalToInstrumentModel} from './instrumentAdapters.js';
import {HELD_FRAME_ROWS, RULED_COPY, packageBriefingModel, routeRunFacts, routeStateView, tryAgainAction} from './instrumentRouteModels.js';
import {readableDate} from './plainLabels.js';
import {go} from './router.js';

const BROWSE_EVIDENCE = Object.freeze({id: 'browse-evidence', label: 'Browse evidence', onClick: () => go('/browse')});
const OPEN_DISCOVER = Object.freeze({id: 'open-discover', label: 'Open Discover', onClick: () => go('/explore')});

/* Every held, withheld, loading and unavailable branch of the briefing renders
   the package state frame through the shared route mapping, and the package
   words each frame: the route passes the task, the stamp, a reason code, the
   missing field names and its controls. The wrapper keeps data-briefing-state
   for the route assertions and the QA harness, and the boot sheet reaches it
   by that attribute to carry the held card's width law; the frame carries
   the one live region and draws its own boundary. */
const BRIEFING_STATES = Object.freeze({
  loading: () => ({
    state: 'loading',
    task: 'Checking the completed observation',
    reservedRows: HELD_FRAME_ROWS.stale,
  }),
  error: (state, onRetry) => ({
    state: 'error',
    code: state.error?.code,
    actions: [tryAgainAction(onRetry)],
  }),
  stale: (state) => ({
    state: 'stale',
    checkedAt: state.checkedAt,
    actions: [BROWSE_EVIDENCE],
  }),
  unavailable: (state) => ({
    state: 'unavailable',
    ...(state.title !== undefined ? {title: state.title} : {}),
    ...(state.body !== undefined ? {body: state.body} : {}),
    reason: state.reason,
    missing: state.missing,
    actions: [OPEN_DISCOVER],
  }),
  /* The package briefing has its own no-discovery composition, but it never
     renders the state frame, so the run scope would leave the screen. The
     frame replaces it, carrying the ruled no discovery copy on the empty
     variant because the desk sends no run authority. */
  no_discovery: () => ({
    state: 'empty',
    ...RULED_COPY.noDiscovery,
    actions: [OPEN_DISCOVER],
  }),
});

/* The loading branch takes a class the held branches do not. The width law in
   the boot sheet governs held cards, and a loading card holds the place of the
   briefing rather than answering in its own right, so it takes the settle's
   corner instead of the card's centring. The class is the hook the sheet
   undoes the law through; the attribute is unchanged on every branch because
   the route assertions and the QA harness pin it. */
function BriefingRouteState({state, facts, onRetry}){
  const payload = BRIEFING_STATES[state.state];
  if (!payload) return null;
  return (
    <div className={state.state === 'loading' ? 'loading-card' : undefined} data-briefing-state={state.state}>
      <StateView {...routeStateView(payload(state, onRetry), facts)} />
    </div>
  );
}

export function TodayPage({topics, freshness, error, loading, onOpen, deskDate, onRetry}){
  const orderedTopics = [...(Array.isArray(topics) ? topics : [])];
  const state = buildBriefingState({topics: orderedTopics, freshness, error, loading});
  const facts = routeRunFacts({topics: orderedTopics, freshness});
  if (['loading', 'error', 'stale'].includes(state.state)){
    return <div className="page" data-screen-label="Briefing"><BriefingRouteState state={state} facts={facts} onRetry={onRetry} /></div>;
  }
  if (state.state === 'no_discovery'){
    return <div className="page" data-screen-label="Briefing" data-run-date={deskDate || ''}><BriefingRouteState state={state} facts={facts} /></div>;
  }
  const adapted = adaptSignalToInstrumentModel(orderedTopics[0]);
  if (adapted && (adapted.state === 'unavailable' || adapted.evidenceSummary?.window?.closed !== true)){
    return <div className="page" data-screen-label="Briefing" data-run-date={deskDate || ''}><BriefingRouteState state={{state: 'unavailable', reason: 'evidence_authority_missing', missing: ['evidence_authority']}} facts={facts} /></div>;
  }
  /* A summary the package validator refused is a failed read, named by the
     validator's own token, which the ribbon model carries. */
  const briefing = adapted && adapted.state !== 'unavailable' ? packageBriefingModel(adapted) : null;
  if (briefing && Array.isArray(briefing.ribbonModel?.limitations) && briefing.ribbonModel.limitations.includes('evidence_summary_invalid')){
    return <div className="page" data-screen-label="Briefing" data-run-date={deskDate || ''}><BriefingRouteState state={{state: 'error', error: {code: 'evidence_summary_invalid'}}} facts={facts} onRetry={onRetry} /></div>;
  }
  /* An unchecked evidence authority the engine declared is a hold, not a
     missing ribbon: the signal exists and its source independence has not
     been validated, so the frame says so and carries the engine's own
     limitation. */
  if (adapted && adapted.evidenceSummary?.state === 'unchecked' && adapted.evidenceSummary?.independence?.status === 'unvalidated'){
    return (
      <div className="page" data-screen-label="Briefing" data-run-date={deskDate || ''}>
        <BriefingRouteState
          state={{state: 'unavailable', title: 'Held until the evidence authority is checked', body: adapted.evidenceSummary.limitations[0] || 'Source independence has not been validated for this signal.', reason: 'evidence_authority_unchecked', missing: ['source_independence']}}
          facts={facts}
        />
      </div>
    );
  }
  if (adapted && (!Array.isArray(adapted.ribbonSeries) || adapted.ribbonSeries.length === 0)){
    return (
      <div className="page" data-screen-label="Briefing" data-run-date={deskDate || ''}>
        <BriefingRouteState
          state={{state: 'unavailable', reason: 'ribbon_series_missing', missing: ['ribbon_series']}}
          facts={facts}
        />
      </div>
    );
  }
  const compareNext = orderedTopics.slice(1, 5).map(adaptSignalToInstrumentModel)
    .filter((model) => model && model.state !== 'unavailable')
    .map((model) => ({
      id: model.id,
      title: model.title,
      evidenceState: model.evidenceSummary.state,
      proofCue: model.evidenceSummary.receipts.length
        ? `${model.evidenceSummary.receipts.length} qualifying receipts`
        : 'No qualifying receipts',
      movement: null,
    }));
  const openComparison = (signalId) => {
    const topic = orderedTopics.find((row) => adaptSignalToInstrumentModel(row)?.id === signalId);
    if (topic && typeof onOpen === 'function') onOpen(topic);
  };
  return (
    <div className="page" data-screen-label="Briefing" data-run-date={deskDate || ''}>
      {/* The desk briefs the run's lead signal, so the numeral is its place in
          the ordered run. `answer` and `plan` are left off: they close the
          receipts column with a cited answer, and the desk payload carries
          neither. Those live on the investigation flow, not on a desk signal. */}
      <InstrumentBriefing
        signal={adapted.state !== 'unavailable' ? adapted : {}}
        compareNext={compareNext}
        onCompare={openComparison}
        ordinal={1}
      />
      {/* The lead's own action: its run date and the topic story it opens
          into, which the package briefing does not carry. The date is read,
          so it is written "12 Sept 2026"; the ISO date stays on
          data-run-date for the hosts that read it. */}
      <p className="briefing-lead-action">Run {deskDate ? readableDate(deskDate) : 'date not supplied'}. <a className="route-open-action" href={'#/topic/' + encodeURIComponent(adapted.id)}>Open {adapted.title}</a></p>
    </div>
  );
}
