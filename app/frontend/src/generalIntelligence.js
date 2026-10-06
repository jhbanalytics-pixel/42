const VERSION = 'general_cultural_question_v1';
const SECTIONS = ['answer', 'evidence', 'interpretation', 'actions'];
const STATUSES = ['complete', 'partial', 'needs_clarification', 'unavailable', 'refused'];
/* Receipts carry the seventeen legacy keys, or those plus the two binding
   fields the engine has emitted since the challenge_v5 adapter. Either set
   exactly; an eighteen key receipt is withheld. */
const RECEIPT_KEYS = ['receipt_id','citation_label','kind','snapshot_id','market','source_label','source_family','platform','author','url','source_row_id','published_at','collected_at','excerpt','reading_ids','limitations','content_digest'];
const BOUND_RECEIPT_KEYS = [...RECEIPT_KEYS, 'evidence_purposes', 'quote_bindings'];
const PURPOSES = ['support', 'challenge', 'context'];
const text = value => typeof value === 'string' && Boolean(value.trim());
const nullableText = value => value === null || text(value);
const strings = value => Array.isArray(value) && value.every(text);
const unique = value => strings(value) && new Set(value).size === value.length;
const integer = value => Number.isSafeInteger(value) && value >= 0;
const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/i.test(value);
const record = value => value && typeof value === 'object' && !Array.isArray(value);
const keys = (value, names) => record(value) && Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));

/* Synchronous SHA-256 so the validator can recompute a quote digest on the
   same path that admits the reply for rendering and history. */
const SHA_K = new Uint32Array([0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]);
export function sha256Hex(value){
  const bytes = new TextEncoder().encode(value);
  const length = ((bytes.length + 9 + 63) >> 6) << 6;
  const data = new Uint8Array(length); data.set(bytes); data[bytes.length] = 0x80;
  const view = new DataView(data.buffer);
  view.setUint32(length - 8, Math.floor(bytes.length / 0x20000000)); view.setUint32(length - 4, (bytes.length << 3) >>> 0);
  const h = new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]);
  const w = new Uint32Array(64);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));
  for (let offset = 0; offset < length; offset += 64){
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(offset + i * 4);
    for (let i = 16; i < 64; i++){
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    let [a, b, c, d, e, f, g, k] = h;
    for (let i = 0; i < 64; i++){
      const t1 = (k + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + SHA_K[i] + w[i]) >>> 0;
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      k = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += k;
  }
  return Array.from(h, word => word.toString(16).padStart(8, '0')).join('');
}
/* The engine counts quote offsets in Unicode code points, not UTF-16 units. */
export function quotedSpan(excerpt, binding){ return Array.from(excerpt).slice(binding.start, binding.end).join(''); }
/* A binding or evidence purpose counts only when its claim cites this receipt. */
function cites(claims, claimId, receipt){
  const claim = claims.get(claimId);
  return Boolean(claim) && Array.isArray(claim.receipt_ids) && claim.receipt_ids.includes(receipt.receipt_id);
}
function quoteBound(receipt, item, claims){
  return cites(claims, item.claim_id, receipt)
    && integer(item.start) && integer(item.end) && item.end > item.start && item.end <= Array.from(receipt.excerpt).length
    && item.quote_sha256.toLowerCase() === sha256Hex(quotedSpan(receipt.excerpt, item));
}
function day(value){
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || Number(value.slice(0, 4)) < 1) return NaN;
  const at = Date.parse(value + 'T00:00:00Z');
  return Number.isFinite(at) && new Date(at).toISOString().slice(0, 10) === value ? at : NaN;
}
function timestamp(value){
  if (typeof value !== 'string') return NaN;
  const shape = /^\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-](\d{2}):(\d{2}))$/.exec(value);
  if (!shape || shape[0] !== value || !Number.isFinite(day(value.slice(0, 10))) || Number(shape[1]) > 23 || Number(shape[2]) > 59 || Number(shape[3]) > 59 || Number(shape[4] || 0) > 23 || Number(shape[5] || 0) > 59) return NaN;
  return Date.parse(value);
}
function windowValid(value){
  return keys(value, ['start', 'end', 'closed']) && Number.isFinite(day(value.start)) && Number.isFinite(day(value.end)) && day(value.start) <= day(value.end) && typeof value.closed === 'boolean';
}
function indexRows(rows, field){
  if (!Array.isArray(rows)) throw new Error(field);
  const index = new Map();
  for (const row of rows){ if (!record(row) || !text(row[field]) || index.has(row[field])) throw new Error(field); index.set(row[field], row); }
  return index;
}

export function validateIntelligenceReply(value, {terminalError = false} = {}){
  try {
    const require = (condition, field) => { if (!condition) throw new Error(field); };
    require(keys(value, ['contract_version','request_id','request_digest','status','resolved_scope','window','as_of','snapshot_id','sections','claims','receipts','readings','limitations','missing_work','clarification','review_required','ready_for_downstream','usage']), 'response');
    require(value.contract_version === VERSION && typeof value.request_id === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value.request_id) && digest(value.request_digest), 'request');
    require(STATUSES.includes(value.status) && Number.isFinite(timestamp(value.as_of)) && /(?:Z|\+00:00)$/.test(value.as_of), 'status');
    const scope = value.resolved_scope;
    require(keys(scope, ['client_scope_id','market_scope','brand_config_id','audience_lens_ids','theme_id']) && text(scope.client_scope_id) && unique(scope.market_scope) && scope.market_scope.length > 0 && scope.market_scope.every(market => /^[a-z]{2}$/.test(market)) && unique(scope.audience_lens_ids) && nullableText(scope.brand_config_id) && nullableText(scope.theme_id), 'scope');
    require(value.window === null || windowValid(value.window), 'window');
    require(nullableText(value.snapshot_id) && strings(value.limitations) && strings(value.missing_work) && nullableText(value.clarification), 'context');
    require(typeof value.review_required === 'boolean' && typeof value.ready_for_downstream === 'boolean' && !(value.review_required && value.ready_for_downstream), 'review');
    const answered = ['complete', 'partial'].includes(value.status);
    require(!(terminalError && value.status === 'complete'), 'terminal_inconsistent');
    require(!answered || (value.window !== null && text(value.snapshot_id)), 'answered_scope');
    require(value.window === null || !value.window.closed || day(value.window.end) + 86400000 <= timestamp(value.as_of), 'window_closure');
    require(value.status !== 'needs_clarification' || text(value.clarification), 'clarification');
    require(answered || !value.ready_for_downstream, 'downstream');
    const usage = value.usage;
    require(keys(usage, ['status','model_calls','input_tokens','output_tokens','usage_receipt_ids','call_receipt_ids','reservation_ids','reserved_cost_usd','reason']) && ['resolved','unresolved'].includes(usage.status), 'usage');
    require(unique(usage.usage_receipt_ids) && unique(usage.call_receipt_ids) && unique(usage.reservation_ids) && typeof usage.reserved_cost_usd === 'string' && /^\d+\.\d{6}$/.test(usage.reserved_cost_usd), 'usage_receipts');
    require(['model_calls','input_tokens','output_tokens'].every(key => integer(usage[key]) || (usage.status === 'unresolved' && usage[key] === null)), 'usage_counts');
    require(usage.status === 'resolved' ? usage.reason === null : ['response_usage_unavailable','metering_persistence_failed'].includes(usage.reason), 'usage_reason');
    require(usage.status !== 'unresolved' || (value.status !== 'complete' && !value.ready_for_downstream), 'unresolved_usage');
    require(usage.status !== 'unresolved' || (usage.call_receipt_ids.length > 0 && usage.reservation_ids.length > 0), 'unresolved_attempt');
    require(usage.status !== 'resolved' || !usage.model_calls || (usage.usage_receipt_ids.length > 0 && usage.call_receipt_ids.length >= usage.model_calls && usage.reservation_ids.length > 0), 'measured_usage_receipts');
    const claims = indexRows(value.claims, 'claim_id');
    const receipts = indexRows(value.receipts, 'receipt_id');
    const readings = indexRows(value.readings, 'reading_id');
    const refs = (ids, index) => unique(ids) && ids.every(id => index.has(id));
    const labels = new Set();
    for (const receipt of receipts.values()){
      require(keys(receipt, RECEIPT_KEYS) || keys(receipt, BOUND_RECEIPT_KEYS), 'receipt');
      require(typeof receipt.citation_label === 'string' && /^R[1-9]\d*$/.test(receipt.citation_label) && !labels.has(receipt.citation_label), 'citation_label'); labels.add(receipt.citation_label);
      require(['content','aggregate'].includes(receipt.kind) && receipt.snapshot_id === value.snapshot_id && text(receipt.snapshot_id) && digest(receipt.content_digest) && text(receipt.source_label), 'receipt_identity');
      require(receipt.market === null || scope.market_scope.includes(receipt.market), 'receipt_market');
      require(['source_family','platform','author','url','source_row_id','excerpt'].every(key => nullableText(receipt[key])) && strings(receipt.limitations) && refs(receipt.reading_ids, readings), 'receipt_fields');
      require(receipt.kind !== 'content' || text(receipt.source_row_id), 'source_row');
      require(['published_at','collected_at'].every(key => receipt[key] === null || (Number.isFinite(timestamp(receipt[key])) && timestamp(receipt[key]) <= timestamp(value.as_of))), 'receipt_dates');
      if (Object.hasOwn(receipt, 'quote_bindings')){
        require(Array.isArray(receipt.evidence_purposes) && receipt.evidence_purposes.every(item => keys(item, ['claim_id','evidence_purpose']) && cites(claims, item.claim_id, receipt) && PURPOSES.includes(item.evidence_purpose)), 'evidence_purposes');
        require(Array.isArray(receipt.quote_bindings) && receipt.quote_bindings.every(item => keys(item, ['claim_id','content_digest','end','quote_sha256','source_field','start']) && claims.has(item.claim_id) && item.content_digest === receipt.content_digest && digest(item.quote_sha256) && item.source_field === 'excerpt' && text(receipt.excerpt) && quoteBound(receipt, item, claims)), 'quote_bindings');
      }
    }
    for (const reading of readings.values()){
      require(keys(reading, ['reading_id','value','unit','window','method','denominator','source_receipt_ids','limitations']), 'reading');
      require(reading.value === null || ['string','boolean'].includes(typeof reading.value) || (typeof reading.value === 'number' && Number.isFinite(reading.value)), 'reading_value');
      require(text(reading.unit) && text(reading.method) && windowValid(reading.window) && strings(reading.limitations), 'reading_context');
      require(!reading.window.closed || day(reading.window.end) + 86400000 <= timestamp(value.as_of), 'reading_closure');
      require(reading.denominator === null || (typeof reading.denominator === 'number' && Number.isFinite(reading.denominator) && reading.denominator >= 0), 'denominator');
      require(refs(reading.source_receipt_ids, receipts) && reading.source_receipt_ids.length > 0, 'reading_sources');
    }
    for (const claim of claims.values()){
      require(keys(claim, ['claim_id','text','kind','receipt_ids','reading_ids','parent_claim_ids','support_state','limitations','falsifier']), 'claim');
      require(text(claim.text) && ['observation','interpretation','inference','proposal'].includes(claim.kind) && ['source_record','derived','proposed'].includes(claim.support_state), 'claim_kind');
      require(refs(claim.receipt_ids, receipts) && refs(claim.reading_ids, readings) && refs(claim.parent_claim_ids, claims) && strings(claim.limitations) && nullableText(claim.falsifier), 'claim_refs');
      require((claim.kind === 'proposal') === (claim.support_state === 'proposed'), 'proposal_type');
      if (claim.kind === 'proposal') require(value.review_required && !value.ready_for_downstream, 'proposal_review');
    }
    const visiting = new Set(), grounded = new Map();
    const hasBasis = id => {
      if (visiting.has(id)) throw new Error('claim_cycle');
      if (grounded.has(id)) return grounded.get(id);
      visiting.add(id);
      const claim = claims.get(id);
      const parents = claim.parent_claim_ids.map(hasBasis);
      const result = claim.support_state !== 'proposed' && (claim.receipt_ids.length > 0 || claim.reading_ids.length > 0 || parents.some(Boolean));
      visiting.delete(id); grounded.set(id, result); return result;
    };
    for (const id of claims.keys()) hasBasis(id);
    require(Array.isArray(value.sections), 'sections');
    const selected = new Set(); let prior = -1;
    for (const section of value.sections){
      const order = SECTIONS.indexOf(section.kind);
      require(keys(section, ['kind','claim_ids']) && order > prior && refs(section.claim_ids, claims) && section.claim_ids.length > 0, 'section_order'); prior = order;
      for (const id of section.claim_ids){
        require(!selected.has(id), 'repeated_claim'); selected.add(id);
        if (claims.get(id).kind === 'proposal') require(section.kind === 'actions' && value.review_required && !value.ready_for_downstream, 'proposal_review');
        else require(grounded.get(id), 'claim_basis');
      }
    }
    require(!answered || value.sections.some(section => section.kind === 'answer' && section.claim_ids.some(id => grounded.get(id))), 'grounded_answer');
    return {ok: true, value, claims, receipts, readings};
  } catch (error) { return {ok: false, reason: 'The structured reply is incomplete or inconsistent. Its answer has been withheld.', field: error instanceof Error && /^[a-z_]+$/.test(error.message) ? error.message : 'unknown'}; }
}

export function intelligenceHistoryText(value){
  const result = validateIntelligenceReply(value);
  if (!result.ok) return null;
  if (value.status === 'needs_clarification') return value.clarification;
  if (!['complete','partial'].includes(value.status)) return null;
  const labels = {observation: 'Observation', interpretation: 'Interpretation', inference: 'Inference', proposal: 'Proposal'};
  return `Response: ${value.status}. ${value.review_required ? 'Review required. Not ready for downstream use.' : ''}\n`
    + value.sections.map(section => section.claim_ids.map(id => {
    const claim = result.claims.get(id);
    return `${labels[claim.kind]}: ${claim.text}`
      + claim.reading_ids.map(id => { const reading = result.readings.get(id); return `\n${reading.value === null ? 'Unmeasured' : String(reading.value)} ${reading.unit} (${reading.window.start} to ${reading.window.end}, ${reading.window.closed ? 'closed' : 'open'}). Method: ${reading.method}. Denominator: ${reading.denominator === null ? 'Unmeasured' : reading.denominator}. ${reading.limitations.join(' ')}`; }).join('')
      + (claim.limitations.length ? '\nClaim limitations: ' + claim.limitations.join(' ') : '')
      + (claim.falsifier ? '\nWhat would change this: ' + claim.falsifier : '');
  }).join('\n')).join('\n\n')
    + (value.limitations.length ? '\nLimitations: ' + value.limitations.join(' ') : '')
    + (value.missing_work.length ? '\nMissing work: ' + value.missing_work.join(' ') : '');
}

const MISSING_WORK = {
  answer_invalid: 'The draft answer did not pass evidence validation. No answer was published.',
  evidence_insufficient: 'The available evidence cannot support this answer.',
  no_matching_evidence: 'No admissible evidence matched this request. The requested answer cannot be established from this read.',
  parent_source_unavailable: 'A previously cited source could not be verified for this request.',
  retrieval_incomplete: 'The evidence retrieval did not complete. No answer was generated from it.',
  coverage_incomplete: 'The available evidence does not cover the requested dates or scope.',
  corroborated_foreign_local_only: 'The matching records describe local events outside the requested market. Relevant evidence is still needed.',
};

const REQUIRED_EVIDENCE = 'Missing required evidence: ';
const REQUIREMENT_ID = /^[A-Za-z0-9_.-]+$/;

/* The engine lists each unfulfilled mandatory requirement by its planned id,
   carried from the snapshot, ahead of the readable line built from the planned
   question, both in plan order. When the unknown ids before the first readable
   line match those lines one for one, and neither list repeats an entry, the
   view shows the readable line and keeps the id behind a reference, dropping
   each paired id only at its own position; otherwise every entry renders as
   supplied. The Ask answer export (ask_export.py) ports this pairing and is
   held to it item for item by test_ask_export_admission.py. */
function distinct(list){ return new Set(list).size === list.length; }
export function missingWorkItems(missing){
  const first = missing.findIndex(item => item.startsWith(REQUIRED_EVIDENCE));
  const readable = missing.filter(item => item.startsWith(REQUIRED_EVIDENCE));
  const positions = first < 0 ? [] : missing.slice(0, first).flatMap((item, index) => !Object.hasOwn(MISSING_WORK, item) && REQUIREMENT_ID.test(item) ? [index] : []);
  const ids = positions.map(index => missing[index]);
  const paired = readable.length > 0 && ids.length === readable.length && distinct(readable) && distinct(ids);
  const dropped = new Set(paired ? positions : []);
  const references = new Map(paired ? readable.map((item, index) => [item, ids[index]]) : []);
  return missing.flatMap((item, index) => dropped.has(index) ? [] : [{text: Object.hasOwn(MISSING_WORK, item) ? MISSING_WORK[item] : item, reference: references.get(item) ?? null}]);
}
