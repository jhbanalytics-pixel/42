const DISCOVERY_MODES = new Set([
  'phrase', 'hashtag', 'sound', 'creator', 'entity',
]);

const READINESS = new Set(['ready', 'thin', 'contradictory', 'unchecked']);

function unavailableWhyNow(){
  return {state: 'unavailable', text: 'No completed-run why-now interpretation is available.'};
}

function unavailablePrecedent(){
  return {state: 'unavailable', text: 'No qualifying historical analogue is available.'};
}

function unavailableResponse(){
  return {state: 'unavailable', text: 'No evidence-backed response is available.'};
}

function sourceField(topic, key){
  if (!topic || typeof topic !== 'object') return undefined;
  if (topic[key] !== undefined && topic[key] !== null) return topic[key];
  const signal = topic.signal;
  return signal && typeof signal === 'object' ? signal[key] : undefined;
}

function nonEmptyText(...values){
  for (const value of values){
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function isDynamic(topic){
  const signalId = sourceField(topic, 'signal_id');
  const discoveryMode = sourceField(topic, 'discovery_mode');
  const evidenceState = sourceField(topic, 'evidence_state');
  return Boolean(
    typeof signalId === 'string' &&
    signalId.trim() &&
    DISCOVERY_MODES.has(discoveryMode) &&
    READINESS.has(evidenceState),
  );
}

function receiptKey(receipt){
  if (typeof receipt === 'string') return receipt.trim();
  if (!receipt || typeof receipt !== 'object') return '';
  return nonEmptyText(
    receipt.id,
    receipt.evidence_id,
    receipt.receipt_id,
    receipt.row_id,
    receipt.url,
    receipt.uri,
  );
}

function receiptValue(receipt){
  if (typeof receipt === 'string'){
    const value = receipt.trim();
    if (!value) return null;
    return value.startsWith('http://') || value.startsWith('https://')
      ? {id: value, url: value}
      : {id: value};
  }
  return receipt && typeof receipt === 'object' ? {...receipt} : null;
}

function receiptsFor(topic){
  if (!topic || typeof topic !== 'object') return [];
  const raw = Array.isArray(topic.receipts)
    ? topic.receipts
    : Array.isArray(topic.evidence)
      ? topic.evidence
      : Array.isArray(topic.references)
        ? topic.references
        : [];
  const seen = new Set();
  const receipts = [];
  for (const item of raw){
    const value = receiptValue(item);
    if (!value) continue;
    const key = receiptKey(value) || JSON.stringify(value);
    if (seen.has(key)) continue;
    seen.add(key);
    receipts.push(value);
  }
  return receipts;
}

function proofFor(topic){
  const receipts = receiptsFor(topic);
  const declared = sourceField(topic, 'evidence_state');
  const state = receipts.length && READINESS.has(declared) ? declared : 'unchecked';
  return {state, receipts};
}

function textSection(value, fallback){
  const text = nonEmptyText(value);
  return text ? {state: 'ready', text} : fallback();
}

function precedentFor(topic){
  const brief = topic && topic.brief && typeof topic.brief === 'object' ? topic.brief : {};
  const value = topic && (topic.precedent || topic.historical_analogue || topic.analogue)
    || brief.precedent;
  if (typeof value === 'string') return textSection(value, unavailablePrecedent);
  if (!value || typeof value !== 'object') return unavailablePrecedent();
  const match = nonEmptyText(value.match, value.similarity, value.analogue);
  const difference = nonEmptyText(value.difference, value.contrast);
  if (!match && !difference) return unavailablePrecedent();
  return {
    state: 'ready',
    ...(match ? {match} : {}),
    ...(difference ? {difference} : {}),
  };
}

export function buildBriefingModel(topic){
  const brief = topic && topic.brief && typeof topic.brief === 'object' ? topic.brief : {};
  const proof = proofFor(topic);
  const whyNow = textSection(
    sourceField(topic, 'why_now'),
    unavailableWhyNow,
  );
  const response = textSection(
    sourceField(topic, 'possible_response') ||
      sourceField(topic, 'response') ||
      sourceField(topic, 'recommendation') ||
      sourceField(topic, 'action') ||
      brief.opportunity,
    unavailableResponse,
  );
  const title = nonEmptyText(
    sourceField(topic, 'signal_name'),
    sourceField(topic, 'topic'),
    sourceField(topic, 'label'),
  ) || 'Untitled signal';

  return {
    identity: isDynamic(topic)
      ? {kind: 'dynamic_signal', label: 'Dynamic signal'}
      : {kind: 'curated_category', label: 'Curated category'},
    title,
    whyNow,
    proof,
    precedent: precedentFor(topic),
    response,
    readiness: proof.state,
  };
}

export function buildBriefingQueue(topics, leadId, limit=4){
  const rows = Array.isArray(topics) ? topics : [];
  const lead = nonEmptyText(leadId);
  const seen = new Set();
  const queue = [];
  for (const topic of rows){
    const id = nonEmptyText(topic && topic.id);
    const signalId = nonEmptyText(sourceField(topic, 'signal_id'));
    if (lead && (lead === id || lead === signalId)) continue;
    const key = signalId || id || buildBriefingModel(topic).title;
    if (seen.has(key)) continue;
    seen.add(key);
    const model = buildBriefingModel(topic);
    const receiptCount = model.proof.receipts.length;
    queue.push({
      id: id || signalId,
      signalId: signalId || '',
      identity: model.identity,
      title: model.title,
      whyNow: model.whyNow,
      proof: model.proof,
      proofSummary: receiptCount
        ? `${receiptCount} direct receipt${receiptCount === 1 ? '' : 's'}`
        : 'No direct receipts are available.',
      movement: nonEmptyText(sourceField(topic, 'momentum'), sourceField(topic, 'status_tag')),
      readiness: model.readiness,
    });
    if (queue.length >= Math.max(0, Number(limit) || 0)) break;
  }
  return queue;
}

function checkedAt(freshness){
  return freshness && (
    freshness.checkedAt || freshness.checked_at || freshness.stamp_utc
  );
}

/* Readiness is the released run's own contract. `dynamic_discovery` carries
   its own closed observation window and its own status, and the desk freshness
   stamp measures the ingest pipeline behind it, which the shell already shows
   in the utility strip. So the run decides first: a released run that carries
   signals is read whatever the ingest stamp says. The stale card is still the
   hold for a run that carries nothing, where the stamp is the only thing that
   can date the wait. */
export function buildBriefingState({topics, freshness, error, loading}={}){
  if (error) return {state: 'error', error};
  if (loading) return {state: 'loading'};
  if (Array.isArray(topics) && topics.length > 0) return {state: 'ready'};
  if (freshness && freshness.status === 'amber'){
    const timestamp = checkedAt(freshness);
    return timestamp ? {state: 'stale', checkedAt: timestamp} : {state: 'stale'};
  }
  return {state: 'no_discovery'};
}
