function deepFreeze(value){
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Reflect.ownKeys(value).forEach((key) => deepFreeze(value[key]));
  return Object.freeze(value);
}

export function freshFixture(value){
  return structuredClone(value);
}

export const READY_SUMMARY = deepFreeze({
  contractVersion: '1.0.0',
  state: 'ready',
  receipts: [
    {
      id: 'receipt-forum',
      sourceName: 'Forum source',
      familyId: 'forum',
      url: 'https://evidence.invalid/forum',
      observedAt: '2026-08-24T09:00:00Z',
      direction: 'supporting',
      excerpt: 'Repair routines are rising in forum evidence.',
      quality: 'qualifying',
      qualifying: true,
    },
    {
      id: 'receipt-video',
      sourceName: 'Video source',
      familyId: 'video',
      url: 'https://evidence.invalid/video',
      observedAt: '2026-08-24T11:00:00Z',
      direction: 'supporting',
      excerpt: 'Repair routines are rising in video evidence.',
      quality: 'qualifying',
      qualifying: true,
    },
  ],
  independence: {
    status: 'validated',
    familyCount: 2,
    groupingAuthority: 'engine-family-v1',
  },
  direction: {
    status: 'agree',
    supportingReceiptIds: ['receipt-forum', 'receipt-video'],
    opposingReceiptIds: [],
  },
  window: {start: '2026-08-18', end: '2026-08-24', method: 'closed_7d', closed: true},
  checkedAt: '2026-08-25T06:30:00Z',
  limitations: [],
});

export const READY_RIBBON = deepFreeze(READY_SUMMARY.receipts.map((receipt) => ({
  familyId: receipt.familyId,
  familyLabel: `${receipt.familyId} evidence`,
  independenceAuthority: 'engine-family-v1',
  direction: 'supporting',
  quality: 'qualifying',
  axis: {start: '2026-08-18', end: '2026-08-24', interval: 'daily'},
  normalization: 'bounded_change_0_1',
  valueDomain: {min: 0, max: 1},
  points: [
    {at: '2026-08-18T09:00:00Z', value: 0.2},
    {at: '2026-08-24T09:00:00Z', value: 0.7},
  ],
  receiptIds: [receipt.id],
  receiptAnchors: [{receiptId: receipt.id, at: '2026-08-24T09:00:00Z', value: 0.7}],
})));

export const MEASURED_AUDIENCE = deepFreeze({
  basis: 'measured',
  label: 'Panel respondents',
  source: 'Audience panel',
  authority: {
    status: 'validated',
    authorityId: 'panel-2026-08',
    methodId: 'weighted-panel-v1',
    methodType: 'measured',
  },
  window: {start: '2026-08-18', end: '2026-08-24', closed: true},
  confidence: 0.82,
  limitations: [],
});

export const INFERRED_AUDIENCE = deepFreeze({
  basis: 'inferred',
  label: 'Practical information seekers',
  source: 'Observed language cues',
  authority: {
    status: 'validated',
    authorityId: 'lens-2026-08',
    methodId: 'cue-model-v1',
    methodType: 'inferred',
  },
  window: {start: '2026-08-18', end: '2026-08-24', closed: true},
  confidence: 0.61,
  limitations: ['This is an inferred lens, not a population estimate.'],
});

export const UNAVAILABLE_AUDIENCE = deepFreeze({
  basis: 'unavailable',
  label: 'Audience measurement unavailable',
  source: null,
  authority: null,
  window: null,
  confidence: null,
  limitations: ['No server-authorized audience basis is available.'],
});

export const ROOT_SIGNAL = deepFreeze({
  contract_version: 'desk_dynamic_signal_v2',
  signal_id: 'sig_root',
  label: 'Repair routine',
  why_now: 'Two independent families moved against baseline.',
  rival: 'A short-lived promotion could explain the movement.',
  contradiction: 'Video growth slowed on the final day.',
  precedent: {match: 'Earlier repair cultures', difference: 'Current distribution is creator-led.'},
  transfer_limit: 'The precedent predates short-form video.',
  cultural_tension: 'Replacement speed conflicts with repair pride.',
  possible_response: 'Show the repair sequence and credit the makers.',
  what_would_change_my_mind: 'A closed rerun with no independent agreement.',
  evidence_summary: READY_SUMMARY,
  ribbon_series: READY_RIBBON,
  audience_basis: MEASURED_AUDIENCE,
});

export const NESTED_SIGNAL = deepFreeze({
  contract_version: 'desk_dynamic_signal_v2',
  signal: ROOT_SIGNAL,
});

export const NO_DISCOVERY = deepFreeze({
  contract_version: 'desk_dynamic_signal_v2',
  status: 'no_discovery',
  requested_market: 'za',
  run: {
    run_id: 'run_20260827_dynamic_apply_v1',
    signal_date: '2026-08-27',
    market_scope: ['za', 'ng', 'ke'],
    observation_start: '2026-08-27',
    observation_end: '2026-08-27',
    observation_method: 'dynamic_source_copy_apply_v1',
    closed_at: '2026-08-29T19:03:28Z',
  },
  signals: [],
  error: null,
});

/* The shape the desk actually sends, captured from
   `/api/desk?region=za` on listening-post-staging on 4 September 2026, run
   `run_20260903_dynamic_apply_v2_r16`. Two things about it decide three
   route behaviours and neither is guessed here: the wrapper carries only the
   `signal` key and no `contract_version` of its own, and the nested signal
   carries no `evidence_summary`, `ribbon_series` or `audience_basis`. Kept
   verbatim so a change in the desk's envelope shows up as a test failure
   rather than as an empty screen on staging. */
export const DESK_WRAPPED_SIGNAL = deepFreeze({
  signal: {
    contract_version: 'desk_dynamic_signal_v2',
    run_id: 'run_20260903_dynamic_apply_v2_r16',
    signal_id: 'sig_92298de03ff953c6981d690bf280335f09f50355f561a527cfcaaddb315bfd05',
    signal_date: '2026-09-03',
    market: 'za',
    signal_name: 'johannesburg',
    discovery_mode: 'entity',
    evidence_state: 'ready',
    why_now: null,
    possible_response: null,
    observation_start: '2026-08-21',
    observation_end: '2026-09-03',
    observation_method: 'dynamic_source_copy_apply_v1',
    receipts: [
      {
        evidence_id: 'ev_15a718eaae2178b19d70cd2df9fd3dd0f63afd8e3a5f24b471cbb4d8c36d62f9',
        url: 'https://www.youtube.com/watch?v=Z-pAs7SGuOU',
        platform: 'youtube',
        source_family: 'youtube',
        author_label: 'ucazipe4ayxzcajabossplhw',
        excerpt: 'Amapiano Balcony Mix Live XPERIENCE B2B with Busta 929 | S4 | Ep 11',
        metric_label: null,
        published_at: '2026-09-03T00:32:27.781212Z',
      },
      {
        evidence_id: 'ev_57944ab22999bcd8155723a5fbea42f9628f52e7dc1db68f717af06032bb2b86',
        url: 'https://www.youtube.com/watch?v=Z4IODCorcQs',
        platform: 'youtube',
        source_family: 'youtube',
        author_label: 'ucazipe4ayxzcajabossplhw',
        excerpt: 'Amapiano Balcony Mix w OSCAR MBO Live at Grand Africa Cafe & Beach, South Africa, Amapiano Mix 2024',
        metric_label: null,
        published_at: '2026-09-03T00:32:27.781212Z',
      },
      {
        evidence_id: 'ev_a79e3f4c0b032bffd8ee6aaf462ea2d135cd3da656d11329fe1a5f5717ec3041',
        url: 'https://www.youtube.com/watch?v=IZyH4Y-gnOE',
        platform: 'youtube',
        source_family: 'youtube',
        author_label: 'ucazipe4ayxzcajabossplhw',
        excerpt: 'Amapiano Balcony Mix Live XPERIENCE In Johannesburg South Africa | S4 | Ep6',
        metric_label: null,
        published_at: '2026-09-03T00:32:27.781212Z',
      },
      {
        evidence_id: 'ev_aa844904c31d71babfb13d3043e5c9b67b404dbedaa61e86fcd62b5c4c70773d',
        url: null,
        platform: 'news',
        source_family: 'news',
        author_label: null,
        excerpt: null,
        metric_label: null,
        published_at: '2026-09-02T13:46:00Z',
      },
    ],
    qualities: {
      velocity: 'low',
      novelty: 'low',
      breadth: 'moderate',
      independence: 'high',
      history: 'unmeasured',
      geo_confidence: 'high',
      topic_tags: [],
    },
  },
});

/* The desk freshness stamp beside that run: the ingest pipeline was 42 hours
   behind while `dynamic_discovery.status` read ready. */
export const DESK_AMBER_FRESHNESS = deepFreeze({
  stamp_utc: '2026-09-03T00:32:27.781212+00:00',
  age_hours: 42.65,
  status: 'amber',
});
