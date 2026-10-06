// The app adapter's contract check for a 42 answer: core/eval/answer.schema.json written out by
// hand, then the cross-field checks of core/agent/answer.py. No imports, so it runs anywhere.
// The app copies this file verbatim to app/frontend/src/answerContract.js, because Vite cannot
// import from outside its root. core/eval/tests/test_answer_contract.py holds any copy byte-identical.

const STATUSES = ['complete', 'partial', 'insufficient_evidence', 'refused'];
const LABELS = ['observed', 'corroborated', 'single_source', 'inferred'];
const KINDS = ['observation', 'interpretation', 'recommendation', 'proposal'];
const PLATFORMS = ['tiktok', 'instagram', 'youtube', 'reddit', 'x', 'threads', 'bluesky', 'facebook', 'linkedin', 'telegram', 'news', 'music_chart', 'wikipedia', 'web'];
const MARKETS = ['ZA', 'NG', 'KE'];

const str = { type: 'string' };
const id = { type: 'string', minLength: 1 };
const list = (items, minItems = 0) => ({ type: 'array', items, minItems });
const obj = (required, props, extra) => ({ type: 'object', required, props, extra });

// proposal: a claim of kind proposal also requires basis and falsifier (the schema's if/then).
const CLAIM = { proposal: true, ...obj(['id', 'text', 'label', 'kind', 'evidence_ids'], {
  id,
  text: str,
  label: { enum: LABELS },
  kind: { enum: KINDS },
  evidence_ids: list(id, 1),
  quotes: list(obj(['evidence_id', 'text'], { evidence_id: id, text: id })),
  numbers: list(obj(['value', 'unit', 'query_id', 'run_id', 'result_hash'], {
    value: { type: 'number' },
    unit: str,
    query_id: id,
    run_id: id,
    result_hash: { type: 'string', pattern: /^sha256:[0-9a-f]{64}$/ },
  })),
  basis: str,
  falsifier: str,
}) };

const EVIDENCE = obj(['id', 'platform', 'handle', 'url', 'posted_at', 'market', 'text'], {
  id,
  platform: { enum: PLATFORMS },
  handle: id,
  url: id,
  posted_at: str,
  market: { enum: MARKETS },
  source_market: { enum: [...MARKETS, null] },
  text: id,
  engagement: obj([], {}, { type: 'number', minimum: 0 }),
  flags: list(str),
  thumbnail_url: { type: 'string', pattern: /^https?:\/\/\S+$/ },
  duration_s: { type: 'number', minimum: 0 },
  transcript_span: obj(['start_s', 'end_s', 'text'], {
    start_s: { type: 'number', minimum: 0 },
    end_s: { type: 'number', minimum: 0 },
    text: str,
  }),
  creator_tier: id,
});

const ANSWER = obj(['status', 'as_of', 'short_answer', 'claims', 'evidence', 'so_what', 'watch_next', 'gaps'], {
  status: { enum: STATUSES },
  as_of: str,
  short_answer: str,
  claims: list(CLAIM),
  evidence: list(EVIDENCE),
  so_what: list(obj(['text', 'claim_ids'], { text: str, claim_ids: list(str) })),
  watch_next: list(obj(['text', 'claim_ids', 'forecast'], { text: str, claim_ids: list(str), forecast: { type: 'boolean' } })),
  gaps: list(obj(['what', 'searched', 'why'], { what: str, searched: str, why: str })),
  context: str,
});

const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const TYPES = {
  string: (v) => typeof v === 'string',
  number: (v) => typeof v === 'number' && Number.isFinite(v),
  boolean: (v) => typeof v === 'boolean',
  array: Array.isArray,
  object: isObject,
};

function repr(v) {
  if (typeof v === 'string') return `'${v}'`;
  if (Array.isArray(v)) return `[${v.map(repr).join(', ')}]`;
  return JSON.stringify(v);
}

function check(value, rule, path, problems) {
  const at = path || 'answer';
  if (rule.enum) {
    if (!rule.enum.includes(value)) problems.push(`${at}: ${repr(value)} is not one of ${repr(rule.enum)}`);
    return;
  }
  if (!TYPES[rule.type](value)) {
    problems.push(`${at}: ${repr(value)} is not of type '${rule.type}'`);
    return;
  }
  const inner = (key) => (path ? `${path}/${key}` : String(key));
  if (rule.minLength && value.length < rule.minLength) problems.push(`${at}: ${repr(value)} should be non-empty`);
  if (rule.pattern && !rule.pattern.test(value)) problems.push(`${at}: ${repr(value)} does not match '${rule.pattern.source}'`);
  if (rule.minimum !== undefined && value < rule.minimum) problems.push(`${at}: ${value} is less than the minimum of ${rule.minimum}`);
  if (rule.type === 'array') {
    if (value.length < rule.minItems) problems.push(`${at}: [] should be non-empty`);
    value.forEach((item, i) => check(item, rule.items, inner(i), problems));
  }
  if (rule.type === 'object') {
    const required = rule.proposal && value.kind === 'proposal' ? [...rule.required, 'basis', 'falsifier'] : rule.required;
    for (const key of required) {
      if (!Object.hasOwn(value, key)) problems.push(`${at}: '${key}' is a required property`);
    }
    const unexpected = Object.keys(value).filter((key) => !Object.hasOwn(rule.props, key));
    if (unexpected.length && !rule.extra) {
      const verb = unexpected.length === 1 ? 'was' : 'were';
      problems.push(`${at}: Additional properties are not allowed (${unexpected.map(repr).join(', ')} ${verb} unexpected)`);
    }
    for (const key of Object.keys(value)) {
      const sub = Object.hasOwn(rule.props, key) ? rule.props[key] : rule.extra;
      if (sub) check(value[key], sub, inner(key), problems);
    }
  }
}

// Same normaliser as the citation_integrity assert in promptfooconfig.yaml and normalise() in answer.py.
function normalise(text) {
  return String(text || '').normalize('NFC').replace(/[‘’‚‛‹›]/g, "'").replace(/[“”„‟«»]/g, '"').replace(/\s+/g, ' ').trim();
}

const ISO = /^(\d{4}-\d{2}-\d{2})(?:[T ]([01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:[.,]\d+)?)?(Z|[+-]\d{2}(?::?\d{2})?)?)?$/;

function datetimeProblem(where, value) {
  if (typeof value !== 'string') return null;
  const m = ISO.exec(value);
  const day = m && new Date(`${m[1]}T00:00:00Z`);
  if (!m || isNaN(day) || day.toISOString().slice(0, 10) !== m[1]) return `${where}: ${repr(value)} is not an ISO 8601 date-time`;
  if (!m[2] || !m[3]) return `${where}: ${repr(value)} needs a time and a UTC offset`;
  return null;
}

const dicts = (value) => (Array.isArray(value) ? value.filter(isObject) : []);
const ids = (value) => (Array.isArray(value) ? value : []);

export function validateAnswer(answer) {
  const problems = [];
  check(answer, ANSWER, '', problems);
  if (!isObject(answer)) return { ok: false, problems };

  const claims = dicts(answer.claims);
  const evidence = dicts(answer.evidence);

  problems.push(datetimeProblem('as_of', answer.as_of));
  evidence.forEach((record, i) => problems.push(datetimeProblem(`evidence/${i}/posted_at`, record.posted_at)));

  for (const [kind, items] of [['claim', claims], ['evidence', evidence]]) {
    const seen = new Set();
    for (const item of items) {
      if (seen.has(item.id)) problems.push(`duplicate ${kind} id ${repr(item.id)}`);
      seen.add(item.id);
    }
  }

  const records = new Map(evidence.map((r) => [r.id, r]));
  for (const claim of claims) {
    const cited = ids(claim.evidence_ids);
    for (const eid of cited) {
      if (!records.has(eid)) problems.push(`${claim.id}: evidence ${repr(eid)} does not resolve to a post record`);
    }
    for (const quote of dicts(claim.quotes)) {
      const eid = quote.evidence_id;
      if (!cited.includes(eid)) {
        problems.push(`${claim.id}: quote attributed to ${repr(eid)}, which the claim does not cite`);
      } else if (records.has(eid) && !normalise(records.get(eid).text).includes(normalise(quote.text))) {
        problems.push(`${claim.id}: quote not found verbatim in record ${repr(eid)}`);
      }
    }
  }

  const claimIds = new Set(claims.map((c) => c.id));
  for (const section of ['so_what', 'watch_next']) {
    dicts(answer[section]).forEach((item, i) => {
      for (const ref of ids(item.claim_ids)) {
        if (!claimIds.has(ref)) problems.push(`${section}/${i}: claim id ${repr(ref)} does not resolve`);
      }
    });
  }

  if (answer.status === 'complete' && claims.length === 0) problems.push('status is complete but the answer has no claims');

  const found = problems.filter(Boolean);
  return { ok: found.length === 0, problems: found };
}
