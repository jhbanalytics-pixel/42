export const PRIMARY_NAV = Object.freeze([
  {label: 'Briefing', path: '/pulse'},
  {label: 'Discover', path: '/explore'},
  {label: 'Compare', path: '/compare'},
  /* Build has one landing, the console workbench; the brief is a step
     inside it, never a second front door. */
  {label: 'Build', path: '/console'},
  /* Fieldwork is the source and research operations dossier. It reads; it
     never executes. Source Lab stays the authority for capability and budget
     detail, and Intelligence Console for investigation detail, so Fieldwork
     links to them rather than duplicating them. */
  {label: 'Fieldwork', path: '/fieldwork'},
]);

export const UTILITY_NAV = Object.freeze([
  {label: 'Method', path: '/method'},
  {label: 'Source Lab', path: '/source-lab'},
  {label: 'Historical', path: '/historical'},
  {label: 'Network', path: '/network'},
  {label: 'Lexicon', path: '/lexicon'},
  {label: 'My board', path: '/board'},
  {label: 'Browse', path: '/browse'},
  {label: 'Journey map', path: '/map'},
]);

/* The views the hash router serves, as one list. Every guard that asks
   whether a hash leads anywhere reads this set rather than restating it, so
   a view added here is served, refused and inventoried from the same line.
   Object.freeze does not freeze a Set's contents, so the set is held here and
   only its read side is exported. */
const SERVED_VIEW_SET = new Set([
  'pulse', 'explore', 'compare', 'console', 'source-lab', 'historical', 'method', 'network', 'lexicon',
  'board', 'browse', 'map', 'seeds', 'seedpath', 'fieldwork',
  'listen', 'topic', 'creator', 'research',
]);
export const SERVED_VIEWS = Object.freeze({
  has: (view) => SERVED_VIEW_SET.has(view),
  size: SERVED_VIEW_SET.size,
  values: () => SERVED_VIEW_SET.values(),
  [Symbol.iterator]: () => SERVED_VIEW_SET.values(),
});

export function normalizeView(view){
  return SERVED_VIEWS.has(view) ? view : 'pulse';
}

export function showFooter(view, deskState){
  return view !== 'pulse' || deskState !== 'loading';
}

export function capacityFailure(error, input){
  if (!error || error.code !== 'capacity_busy') return null;
  return {
    busy: true,
    message: 'The desk is at capacity. Try again shortly.',
    retryAfterSeconds: Number(error.retryAfterSeconds) || 30,
    input: String(input || ''),
  };
}

export function apiFailureDetails(status, payload, retryAfter){
  if (status === 429 && payload && payload.error === 'capacity_busy'){
    return {
      message: 'The desk is at capacity. Try again shortly.',
      code: 'capacity_busy',
      retryAfterSeconds: Number(payload.retry_after_seconds || retryAfter) || 30,
    };
  }
  const detail = payload && (payload.detail ?? payload.message);
  if (detail && typeof detail === 'object' && !Array.isArray(detail)){
    return {
      ...(detail.code ? {code: String(detail.code)} : {}),
      message: detail.message ? String(detail.message) : '',
    };
  }
  if (status === 503 && detail === 'Passcode gate not configured'){
    return {message: 'The intelligence source is unavailable in this session.'};
  }
  if (Array.isArray(detail)) return {detail};
  return {message: detail ? String(detail) : ''};
}

export function normalizeApiDetail(detail){
  if (Array.isArray(detail)) return detail.map((item) => item && item.msg ? item.msg : item).filter(Boolean).join('; ');
  return detail ? String(detail) : '';
}

function receiptKey(receipt){
  if (!receipt) return '';
  if (typeof receipt === 'string') return receipt;
  if (Array.isArray(receipt)) return String(receipt[5] || receipt.slice(0, 3).join('|')).trim();
  if (typeof receipt !== 'object') return String(receipt);
  return String(receipt.id || receipt.url || receipt.uri || '').trim();
}

function uniqueCount(value, key=(item) => item){
  return new Set((Array.isArray(value) ? value : []).map(key).filter(Boolean)).size;
}

export function signalEvidence(topic){
  const cited = uniqueCount(topic && (topic.receipts || topic.social_refs || topic.references), receiptKey);
  const channels = uniqueCount(topic && topic.platforms);
  if (!cited && !channels) return 'Cited-post and source-channel counts unavailable';
  return `${cited} cited ${cited === 1 ? 'post' : 'posts'} · ${channels} source ${channels === 1 ? 'channel' : 'channels'}`;
}

export function leadReading(topic){
  const t = topic || {};
  const brief = t.brief || {};
  const idea = brief.idea && typeof brief.idea === 'object' ? brief.idea.text : brief.idea;
  return {
    changed: t.why || 'No completed-run change note is available.',
    matters: t.cultural_context || t.synthesis || brief.relevance || 'No evidence-backed cultural context is available.',
    nextMove: t.recommendation || t.action || brief.opportunity || idea || 'No evidence-backed recommendation is available.',
  };
}

export function todayMetricLabels(delta){
  const n = Number(delta);
  const direction = n >= 0 ? '▲ ' : '▼ ';
  return {
    currentSignal: 'Current signal',
    engagement30d: 'Summed engagement · 30d',
    boardEngagement30d: 'Summed engagement · 30d',
    delta: Number.isFinite(n) ? direction + Math.abs(n).toFixed(3) + ' index change · 0 to 1' : 'Signal index change unavailable',
  };
}

export function splitMomentum(topics, limit=3){
  const rising = [];
  const weakening = [];
  for (const topic of Array.isArray(topics) ? topics : []){
    const momentum = String(topic && topic.momentum || '').toLowerCase();
    const delta = Number(topic && topic.delta);
    if ((momentum === 'rising' || momentum === 'building' || delta > 0) && rising.length < limit) rising.push(topic);
    else if ((momentum === 'cooling' || momentum === 'weakening' || delta < 0) && weakening.length < limit) weakening.push(topic);
  }
  return {rising, weakening};
}
