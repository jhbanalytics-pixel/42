import {
  validateAudienceBasis,
  validateEvidenceSummary,
  validateRibbonSeries,
} from 'ogilvy-intelligence-design-system';
import {readableDates} from './plainLabels.js';
import {safePlainData} from './privatePlainData.js';

const DYNAMIC_SIGNAL_VERSION = 'desk_dynamic_signal_v2';
const REQUEST_MARKETS = new Set(['za', 'ng', 'ke', 'all']);
const SCOPE_MARKETS = new Set(['za', 'ng', 'ke']);
const INDEPENDENCE_LIMITATION = 'Server-authorized source independence is unavailable.';
const AUDIENCE_LIMITATION = 'No server-authorized audience basis is available.';

function objectValue(value){
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

/* The desk wraps every discovered signal as `{signal: {...}}` and states the
   contract on the nested signal only; the wrapper carries no version of its
   own. Discover's admission has always read that shape, through the same fall
   through to `row.signal` at explore.jsx, so this reader is the only one that
   refused it. A wrapper is read through to its nested signal, and the nested
   signal has to state the contract. A row that states it nowhere is still
   refused: an unversioned payload is not a signal. */
function versionedSignal(topic){
  const root = objectValue(topic);
  if (!root) return null;
  const nested = objectValue(root.signal);
  if (nested) return nested.contract_version === DYNAMIC_SIGNAL_VERSION ? nested : null;
  return root.contract_version === DYNAMIC_SIGNAL_VERSION ? root : null;
}

function explicitField(topic, field){
  const signal = versionedSignal(topic);
  return signal && Object.hasOwn(signal, field) ? signal[field] : undefined;
}

function cleanText(value){
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function unavailableAudienceShape(){
  return {
    basis: 'unavailable',
    label: 'Audience measurement unavailable',
    source: null,
    authority: null,
    window: null,
    confidence: null,
    limitations: [AUDIENCE_LIMITATION],
  };
}

function unavailableAudience(){
  return validateAudienceBasis(unavailableAudienceShape()).value;
}

function uncheckedEvidenceShape(){
  return {
    contractVersion: '1.0.0',
    state: 'unchecked',
    receipts: [],
    independence: {status: 'unvalidated', familyCount: 0, groupingAuthority: null},
    direction: {status: 'unknown', supportingReceiptIds: [], opposingReceiptIds: []},
    window: {start: '', end: '', method: '', closed: false},
    checkedAt: '',
    limitations: [INDEPENDENCE_LIMITATION],
  };
}

function unavailablePayload(){
  return {state: 'unavailable', error: 'instrument_payload_unavailable'};
}

function detached(value, fallback){
  const result = safePlainData(value);
  return result.ok ? result.value : fallback;
}

function isoDate(value){
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function utcTimestamp(value){
  return typeof value === 'string'
    && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(value)
    && !Number.isNaN(Date.parse(value));
}

function completedNoDiscovery(root){
  const run = objectValue(root?.run);
  const marketScope = Array.isArray(run?.market_scope) ? run.market_scope : [];
  const requestedMarket = root?.requested_market;
  const marketAuthority = REQUEST_MARKETS.has(requestedMarket)
    && new Set(marketScope).size === marketScope.length
    && marketScope.every((market) => SCOPE_MARKETS.has(market))
    && (requestedMarket === 'all' || marketScope.includes(requestedMarket));
  return root?.contract_version === DYNAMIC_SIGNAL_VERSION
    && root.status === 'no_discovery'
    && marketAuthority
    && Array.isArray(root.signals)
    && root.signals.length === 0
    && root.error === null
    && run
    && typeof run.run_id === 'string'
    && Boolean(run.run_id.trim())
    && isoDate(run.signal_date)
    && marketScope.length > 0
    && isoDate(run.observation_start)
    && isoDate(run.observation_end)
    && run.observation_start <= run.observation_end
    && typeof run.observation_method === 'string'
    && Boolean(run.observation_method.trim())
    && utcTimestamp(run.closed_at)
    && Date.parse(run.closed_at) >= Date.parse(`${run.observation_end}T22:00:00Z`);
}

export function adaptSignalToEvidenceSummary(topic){
  const boundary = safePlainData(topic);
  const source = boundary.ok
    ? objectValue(explicitField(boundary.value, 'evidence_summary'))
    : null;
  if (!source){
    return detached(validateEvidenceSummary(uncheckedEvidenceShape()).value, uncheckedEvidenceShape());
  }

  const independence = objectValue(source.independence);
  const authorized = independence?.status === 'validated'
    && typeof independence.groupingAuthority === 'string'
    && Boolean(independence.groupingAuthority.trim());
  const candidate = authorized ? source : {
    ...source,
    state: 'unchecked',
    limitations: [...new Set([
      ...(Array.isArray(source.limitations) ? source.limitations : []),
      INDEPENDENCE_LIMITATION,
    ])],
  };
  return detached(validateEvidenceSummary(candidate).value, uncheckedEvidenceShape());
}

export function adaptSignalToRibbonSeries(topic, summary){
  const topicBoundary = safePlainData(topic);
  const summaryBoundary = safePlainData(summary);
  if (!topicBoundary.ok || !summaryBoundary.ok) return [];
  const series = explicitField(topicBoundary.value, 'ribbon_series');
  if (!Array.isArray(series)) return [];
  const result = validateRibbonSeries(series, summaryBoundary.value);
  return detached(result.value, []);
}

export function adaptSignalToAudienceBasis(topic){
  const boundary = safePlainData(topic);
  if (!boundary.ok) return detached(unavailableAudience(), unavailableAudienceShape());
  const basis = explicitField(boundary.value, 'audience_basis');
  if (!objectValue(basis)) return unavailableAudience();
  const result = validateAudienceBasis(basis);
  return detached(result.ok ? result.value : unavailableAudience(), unavailableAudienceShape());
}

export function adaptSignalToInstrumentModel(topic){
  const boundary = safePlainData(topic);
  if (!boundary.ok) return unavailablePayload();
  const root = objectValue(boundary.value);
  if (root?.status === 'no_discovery') return completedNoDiscovery(root) ? null : unavailablePayload();

  const signal = versionedSignal(boundary.value);
  if (!signal){
    return unavailablePayload();
  }

  const summary = adaptSignalToEvidenceSummary(boundary.value);
  return detached({
    id: cleanText(signal.signal_id),
    /* The desk names a signal in `signal_name`; `title` and `label` are the
       two earlier field names. Discover's admission reads the same three in
       the same order, so the specimen and the candidate row cannot disagree
       about what a signal is called. */
    title: cleanText(signal.signal_name) || cleanText(signal.title) || cleanText(signal.label),
    whyNow: cleanText(signal.why_now),
    rival: cleanText(signal.rival),
    contradiction: cleanText(signal.contradiction),
    precedent: objectValue(signal.precedent),
    transferLimit: cleanText(signal.transfer_limit),
    culturalTension: cleanText(signal.cultural_tension),
    possibleResponse: cleanText(signal.possible_response),
    whatWouldChangeMyMind: cleanText(signal.what_would_change_my_mind),
    evidenceSummary: summary,
    ribbonSeries: adaptSignalToRibbonSeries(boundary.value, summary),
    audienceBasis: adaptSignalToAudienceBasis(boundary.value),
  }, unavailablePayload());
}

/* The rail counts validated evidence states among admitted signals.
   Admission alone cannot make an unchecked signal ready. The window is the
   Briefing run's, and the rail says so: the Ask page names a different one.
   The rail is read, so its window is written as dates a reader says out loud
   ("18 to 24 Aug 2026"), never as the ISO pair the payload carries. */
export function railSummaryForTopics(topics){
  const models = (Array.isArray(topics) ? topics : [])
    .map((topic) => adaptSignalToInstrumentModel(topic))
    .filter((model) => model && model.state !== 'unavailable');
  if (!models.length) return undefined;
  const counts = {ready: 0, thin: 0, contradictory: 0};
  let observed = null;
  for (const model of models){
    const state = model.evidenceSummary?.state;
    if (state === 'thin' || state === 'contradictory') counts[state] += 1;
    else if (state === 'ready') counts.ready += 1;
    const window = model.evidenceSummary?.window;
    if (!observed && window?.closed === true && window.start && window.end){
      observed = `${window.start} to ${window.end}`;
    }
  }
  return observed ? {...counts, window: readableDates(observed) + ' (Briefing)'} : counts;
}
