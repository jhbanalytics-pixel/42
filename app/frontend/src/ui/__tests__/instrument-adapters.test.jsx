import {describe, expect, spyOn, test} from 'bun:test';
import manifest from 'ogilvy-intelligence-design-system/manifest.json';
import {
  adaptSignalToAudienceBasis,
  adaptSignalToEvidenceSummary,
  adaptSignalToInstrumentModel,
  adaptSignalToRibbonSeries,
  railSummaryForTopics,
} from '../../instrumentAdapters.js';
import {
  assertEngineContractCompatibility,
  assertInstrumentPackageManifest,
} from '../../instrumentPackageContract.js';
import {safePlainData} from '../../privatePlainData.js';
import {
  INFERRED_AUDIENCE,
  MEASURED_AUDIENCE,
  NESTED_SIGNAL,
  NO_DISCOVERY,
  READY_RIBBON,
  READY_SUMMARY,
  ROOT_SIGNAL,
  UNAVAILABLE_AUDIENCE,
  freshFixture,
} from './fixtures/instrument-payloads.js';

const UNAVAILABLE_PAYLOAD = {state: 'unavailable', error: 'instrument_payload_unavailable'};

function hostileValues(){
  const getter = {};
  Object.defineProperty(getter, 'contract_version', {get(){ throw new Error('getter ran'); }});
  const cycle = {};
  cycle.self = cycle;
  const symbol = {contract_version: 'desk_dynamic_signal_v2'};
  symbol[Symbol('hostile')] = true;
  return [
    ['getter', getter],
    ['proxy', new Proxy({}, {ownKeys(){ throw new Error('proxy trap'); }})],
    ['cycle', cycle],
    ['symbol', symbol],
    ['unsupported prototype', new Date('2026-08-25T00:00:00Z')],
  ];
}

function pollutionPayload(key, authority){
  return JSON.parse(`{"${key}":${JSON.stringify(authority)}}`);
}

describe('fail-closed instrument adapters', () => {
  test('missing server independence authority fails to unchecked', () => {
    const summary = adaptSignalToEvidenceSummary({
      contract_version: 'desk_dynamic_signal_v2',
      evidence_summary: {
        ...READY_SUMMARY,
        independence: {status: 'unvalidated', familyCount: 0, groupingAuthority: null},
      },
      evidence_state: 'ready',
      receipts: [{id: 'r1', family: 'video'}, {id: 'r2', family: 'search'}],
    });

    expect(summary.state).toBe('unchecked');
    expect(summary.limitations).toContain('Server-authorized source independence is unavailable.');
  });

  test('missing Ribbon series produces no visual strands', () => {
    expect(adaptSignalToRibbonSeries({
      contract_version: 'desk_dynamic_signal_v2',
      evidence_summary: READY_SUMMARY,
    }, READY_SUMMARY)).toEqual([]);
  });

  test('scalar scores cannot become Ribbon points', () => {
    expect(adaptSignalToRibbonSeries({
      contract_version: 'desk_dynamic_signal_v2',
      evidence_summary: READY_SUMMARY,
      velocity_score: 0.8,
      independence_score: 0.7,
    }, READY_SUMMARY)).toEqual([]);
  });

  test.each([
    ['ready', READY_SUMMARY],
    ['thin', {
      ...READY_SUMMARY,
      state: 'thin',
      receipts: [READY_SUMMARY.receipts[0]],
      independence: {...READY_SUMMARY.independence, familyCount: 1},
      direction: {...READY_SUMMARY.direction, supportingReceiptIds: ['receipt-forum']},
    }],
    ['contradictory', {
      ...READY_SUMMARY,
      state: 'contradictory',
      receipts: [
        READY_SUMMARY.receipts[0],
        {...READY_SUMMARY.receipts[1], direction: 'opposing'},
      ],
      direction: {
        status: 'mixed',
        supportingReceiptIds: ['receipt-forum'],
        opposingReceiptIds: ['receipt-video'],
      },
    }],
    ['unchecked', {
      ...READY_SUMMARY,
      state: 'unchecked',
      receipts: [],
      independence: {status: 'unvalidated', familyCount: 0, groupingAuthority: null},
      direction: {status: 'unknown', supportingReceiptIds: [], opposingReceiptIds: []},
      limitations: ['Server verification is pending.'],
    }],
  ])('preserves a valid server evidence state of %s', (state, evidenceSummary) => {
    expect(adaptSignalToEvidenceSummary({
      contract_version: 'desk_dynamic_signal_v2',
      evidence_summary: evidenceSummary,
    }).state).toBe(state);
  });

  test('package evidence validation rejects a checked time before the closed window ends', () => {
    expect(adaptSignalToEvidenceSummary({
      contract_version: 'desk_dynamic_signal_v2',
      evidence_summary: {...READY_SUMMARY, checkedAt: '2026-08-24T12:00:00Z'},
    }).state).toBe('unchecked');
  });

  test('package Ribbon validation rejects an anchor that is not a plotted point', () => {
    const changed = structuredClone(READY_RIBBON);
    changed[0].receiptAnchors[0].value = 0.6;
    expect(adaptSignalToRibbonSeries({
      contract_version: 'desk_dynamic_signal_v2',
      ribbon_series: changed,
    }, READY_SUMMARY)).toEqual([]);
  });

  test.each([
    ['measured', MEASURED_AUDIENCE],
    ['inferred', INFERRED_AUDIENCE],
    ['unavailable', UNAVAILABLE_AUDIENCE],
  ])('preserves a server-authorized %s audience basis', (_basis, audience) => {
    expect(adaptSignalToAudienceBasis({
      contract_version: 'desk_dynamic_signal_v2',
      audience_basis: audience,
    })).toEqual(audience);
  });

  test('demographic proxies cannot become audience authority', () => {
    expect(adaptSignalToAudienceBasis({audience: 'Gen Z', demographics: ['18-24']})).toEqual(
      UNAVAILABLE_AUDIENCE,
    );
  });

  test('package audience validation rejects mismatched authority method type', () => {
    expect(adaptSignalToAudienceBasis({
      contract_version: 'desk_dynamic_signal_v2',
      audience_basis: {
        ...MEASURED_AUDIENCE,
        authority: {...MEASURED_AUDIENCE.authority, methodType: 'inferred'},
      },
    })).toEqual(UNAVAILABLE_AUDIENCE);
  });

  test.each([
    ['root', ROOT_SIGNAL],
    ['nested', NESTED_SIGNAL],
  ])('maps only explicit versioned %s dynamic signal fields', (_shape, payload) => {
    expect(adaptSignalToInstrumentModel(freshFixture(payload))).toEqual({
      id: 'sig_root',
      title: 'Repair routine',
      whyNow: 'Two independent families moved against baseline.',
      rival: 'A short-lived promotion could explain the movement.',
      contradiction: 'Video growth slowed on the final day.',
      precedent: {match: 'Earlier repair cultures', difference: 'Current distribution is creator-led.'},
      transferLimit: 'The precedent predates short-form video.',
      culturalTension: 'Replacement speed conflicts with repair pride.',
      possibleResponse: 'Show the repair sequence and credit the makers.',
      whatWouldChangeMyMind: 'A closed rerun with no independent agreement.',
      evidenceSummary: READY_SUMMARY,
      ribbonSeries: READY_RIBBON,
      audienceBasis: MEASURED_AUDIENCE,
    });
  });

  test('a completed no-discovery run closes to null', () => {
    expect(adaptSignalToInstrumentModel(freshFixture(NO_DISCOVERY))).toBeNull();
  });

  test.each(['2026-08-27T22:00:00Z', '2026-08-27T23:59:59Z'])(
    'no-discovery admits a Johannesburg-closed run at %s', (closedAt) => {
      const payload = freshFixture(NO_DISCOVERY);
      payload.run.closed_at = closedAt;
      expect(adaptSignalToInstrumentModel(payload)).toBeNull();
    },
  );

  test('no-discovery refuses a run one millisecond before Johannesburg day close', () => {
    const payload = freshFixture(NO_DISCOVERY);
    payload.run.closed_at = '2026-08-27T21:59:59.999Z';
    expect(adaptSignalToInstrumentModel(payload)).toEqual(UNAVAILABLE_PAYLOAD);
  });

  test('the rail keeps unchecked admission separate from ready evidence', () => {
    const unchecked = freshFixture(ROOT_SIGNAL);
    delete unchecked.evidence_summary;
    expect(adaptSignalToInstrumentModel(unchecked).evidenceSummary.state).toBe('unchecked');
    expect(railSummaryForTopics([unchecked])).toEqual({ready: 0, thin: 0, contradictory: 0});
    expect(railSummaryForTopics([freshFixture(ROOT_SIGNAL), unchecked])).toMatchObject({ready: 1, thin: 0, contradictory: 0});
  });

  test('all-market no-discovery uses producer all semantics without scope membership', () => {
    const payload = freshFixture(NO_DISCOVERY);
    payload.requested_market = 'all';
    expect(adaptSignalToInstrumentModel(payload)).toBeNull();
  });

  test.each([
    ['contradictory nonempty scope', 'ng', ['za']],
    ['duplicate scope', 'za', ['za', 'za']],
    ['unsupported market', 'uk', ['za', 'ng', 'ke']],
    ['unnormalized all alias', 'ALL', ['za', 'ng', 'ke']],
  ])('no-discovery rejects %s', (_label, requestedMarket, marketScope) => {
    const payload = freshFixture(NO_DISCOVERY);
    payload.requested_market = requestedMarket;
    payload.run.market_scope = marketScope;
    expect(adaptSignalToInstrumentModel(payload)).toEqual(UNAVAILABLE_PAYLOAD);
  });

  test.each([
    ['missing run', (value) => { delete value.run; }],
    ['missing run ID', (value) => { delete value.run.run_id; }],
    ['missing market scope', (value) => { value.run.market_scope = []; }],
    ['malformed signal date', (value) => { value.run.signal_date = '2026-99-99'; }],
    ['malformed observation start', (value) => { value.run.observation_start = null; }],
    ['reversed observation window', (value) => { value.run.observation_start = '2026-08-28'; }],
    ['missing observation method', (value) => { value.run.observation_method = ''; }],
    ['malformed close time', (value) => { value.run.closed_at = null; }],
    ['close time before window completion', (value) => { value.run.closed_at = '2026-08-27T12:00:00Z'; }],
    ['nonempty signals', (value) => { value.signals.push({signal: {}}); }],
    ['producer error', (value) => { value.error = {code: 'run_integrity_failed'}; }],
  ])('no-discovery with %s is unavailable', (_label, mutate) => {
    const payload = freshFixture(NO_DISCOVERY);
    mutate(payload);
    expect(adaptSignalToInstrumentModel(payload)).toEqual(UNAVAILABLE_PAYLOAD);
  });

  test.each(hostileValues())('hostile %s adapter input never throws', (_label, hostile) => {
    expect(() => adaptSignalToEvidenceSummary(hostile)).not.toThrow();
    expect(() => adaptSignalToRibbonSeries(hostile, READY_SUMMARY)).not.toThrow();
    expect(() => adaptSignalToAudienceBasis(hostile)).not.toThrow();
    expect(adaptSignalToInstrumentModel(hostile)).toEqual(UNAVAILABLE_PAYLOAD);
  });

  test('adapter boundary rejects an accessor without invoking its getter', () => {
    let getterCalls = 0;
    const hostile = {};
    Object.defineProperty(hostile, 'contract_version', {
      enumerable: true,
      get(){ getterCalls += 1; return 'desk_dynamic_signal_v2'; },
    });
    expect(adaptSignalToInstrumentModel(hostile)).toEqual(UNAVAILABLE_PAYLOAD);
    expect(getterCalls).toBe(0);
  });

  test('adapter boundary rejects a transparent proxy around valid data', () => {
    expect(adaptSignalToInstrumentModel(new Proxy(freshFixture(ROOT_SIGNAL), {}))).toEqual(
      UNAVAILABLE_PAYLOAD,
    );
  });

  test.each(['__proto__', 'constructor', 'prototype'])(
    'adapter boundary cannot inherit authority from own JSON key %s',
    (key) => {
      const hostile = pollutionPayload(key, freshFixture(ROOT_SIGNAL));
      expect(Object.hasOwn(hostile, key)).toBe(true);
      expect(adaptSignalToInstrumentModel(hostile)).toEqual(UNAVAILABLE_PAYLOAD);
      expect({}.contract_version).toBeUndefined();
    },
  );

  test('own JSON __proto__ cannot forge inherited no-discovery authority', () => {
    const hostile = pollutionPayload('__proto__', freshFixture(NO_DISCOVERY));
    expect(adaptSignalToInstrumentModel(hostile)).toEqual(UNAVAILABLE_PAYLOAD);
  });

  test('plain-data cloning preserves dangerous JSON names as own data without changing prototype', () => {
    const source = JSON.parse('{"__proto__":{"polluted":true},"constructor":"own","prototype":"own"}');
    const result = safePlainData(source);
    expect(result.ok).toBe(true);
    expect(Object.getPrototypeOf(result.value)).toBe(Object.prototype);
    expect(Object.hasOwn(result.value, '__proto__')).toBe(true);
    expect(Object.hasOwn(result.value, 'constructor')).toBe(true);
    expect(Object.hasOwn(result.value, 'prototype')).toBe(true);
    expect(result.value.__proto__).toEqual({polluted: true});
    expect({}.polluted).toBeUndefined();
  });

  test('canonical fixtures are deeply frozen and each test receives a fresh clone', () => {
    expect(Object.isFrozen(ROOT_SIGNAL)).toBe(true);
    expect(Object.isFrozen(ROOT_SIGNAL.evidence_summary.receipts[0])).toBe(true);
    expect(Object.isFrozen(ROOT_SIGNAL.ribbon_series[0].points[0])).toBe(true);
    expect(Object.isFrozen(ROOT_SIGNAL.audience_basis.authority)).toBe(true);
    const left = freshFixture(ROOT_SIGNAL);
    const right = freshFixture(ROOT_SIGNAL);
    left.precedent.match = 'changed';
    expect(right.precedent.match).toBe('Earlier repair cultures');
  });

  test('adapter outputs are detached from every authority input', () => {
    const input = freshFixture(ROOT_SIGNAL);
    const output = adaptSignalToInstrumentModel(input);
    input.evidence_summary.receipts[0].excerpt = 'changed input';
    input.evidence_summary.direction.supportingReceiptIds[0] = 'changed-input';
    input.evidence_summary.window.start = '2020-01-01';
    input.evidence_summary.limitations.push('changed input');
    input.audience_basis.authority.authorityId = 'changed-input';
    input.ribbon_series[0].points[0].value = 0.99;
    input.ribbon_series[0].receiptAnchors[0].value = 0.99;
    input.precedent.match = 'changed input';

    expect(output.evidenceSummary.receipts[0].excerpt).toBe(READY_SUMMARY.receipts[0].excerpt);
    expect(output.evidenceSummary.direction.supportingReceiptIds[0]).toBe('receipt-forum');
    expect(output.evidenceSummary.window.start).toBe('2026-08-18');
    expect(output.evidenceSummary.limitations).toEqual([]);
    expect(output.audienceBasis.authority.authorityId).toBe('panel-2026-08');
    expect(output.ribbonSeries[0].points[0].value).toBe(0.2);
    expect(output.ribbonSeries[0].receiptAnchors[0].value).toBe(0.7);
    expect(output.precedent.match).toBe('Earlier repair cultures');
  });

  test('mutating adapter outputs cannot mutate authority inputs', () => {
    const input = freshFixture(ROOT_SIGNAL);
    const output = adaptSignalToInstrumentModel(input);
    output.evidenceSummary.receipts[0].excerpt = 'changed output';
    output.evidenceSummary.direction.supportingReceiptIds[0] = 'changed-output';
    output.evidenceSummary.window.start = '2020-01-01';
    output.evidenceSummary.limitations.push('changed output');
    output.audienceBasis.authority.authorityId = 'changed-output';
    output.ribbonSeries[0].points[0].value = 0.99;
    output.ribbonSeries[0].receiptAnchors[0].value = 0.99;
    output.precedent.match = 'changed output';

    expect(input).toEqual(freshFixture(ROOT_SIGNAL));
  });

  test('a malformed or unversioned payload is unavailable, never repaired', () => {
    expect(adaptSignalToInstrumentModel({label: 'Curated fallback', platforms: ['video']})).toEqual({
      state: 'unavailable',
      error: 'instrument_payload_unavailable',
    });
  });
});

describe('package and engine compatibility', () => {
  test('accepts the exact installed package manifest', () => {
    expect(assertInstrumentPackageManifest(manifest)).toEqual({available: true, error: null});
  });

  test.each([
    ['package name', (value) => { value.package.name = 'wrong-package'; }],
    ['package major', (value) => { value.package.version = '3.0.0'; }],
    ['contract', (value) => { value.contractVersion = '1.0.0'; }],
    ['exports', (value) => { value.exportList = value.exportList.filter((name) => name !== 'validateRibbonSeries'); }],
    ['source binding', (value) => { value.packageSourceCommit = 'f'.repeat(40); }],
    ['evidence binding', (value) => { value.gateBEvidence.receiptCommit = 'f'.repeat(40); }],
  ])('rejects a mismatched manifest %s', (_label, mutate) => {
    const changed = structuredClone(manifest);
    mutate(changed);
    expect(assertInstrumentPackageManifest(changed)).toEqual({
      available: false,
      error: 'instrument_package_mismatch',
    });
  });

  test.each([
    ['2.evil'],
    ['2.0.0-rc.1'],
    ['2.0.0+build.1'],
    ['02.0.0'],
    [' 2.0.0'],
    [2],
  ])('rejects unapproved package version %p', (version) => {
    const changed = structuredClone(manifest);
    changed.package.version = version;
    const error = spyOn(console, 'error').mockImplementation(() => {});
    expect(assertInstrumentPackageManifest(changed)).toEqual({
      available: false,
      error: 'instrument_package_mismatch',
    });
    expect(error).toHaveBeenCalledTimes(1);
    error.mockRestore();
  });

  test.each(hostileValues())('hostile %s manifest fails closed once', (_label, hostile) => {
    const error = spyOn(console, 'error').mockImplementation(() => {});
    let result;
    expect(() => { result = assertInstrumentPackageManifest(hostile); }).not.toThrow();
    expect(result).toEqual({
      available: false,
      error: 'instrument_package_mismatch',
    });
    expect(error).toHaveBeenCalledTimes(1);
    error.mockRestore();
  });

  test('compatibility boundaries reject accessors without invoking getters', () => {
    let getterCalls = 0;
    const hostile = {};
    Object.defineProperty(hostile, 'package', {
      enumerable: true,
      get(){ getterCalls += 1; return manifest.package; },
    });
    const error = spyOn(console, 'error').mockImplementation(() => {});
    expect(assertInstrumentPackageManifest(hostile)).toEqual({
      available: false,
      error: 'instrument_package_mismatch',
    });
    expect(assertEngineContractCompatibility(hostile)).toEqual({
      available: false,
      error: 'engine_contract_mismatch',
    });
    expect(getterCalls).toBe(0);
    expect(error).toHaveBeenCalledTimes(2);
    error.mockRestore();
  });

  test('compatibility boundaries reject transparent proxies around valid data', () => {
    const error = spyOn(console, 'error').mockImplementation(() => {});
    expect(assertInstrumentPackageManifest(new Proxy(structuredClone(manifest), {}))).toEqual({
      available: false,
      error: 'instrument_package_mismatch',
    });
    expect(assertEngineContractCompatibility(new Proxy({
      engineSourceSha: manifest.sourceCommit,
      evidenceSummaryVersion: '1.0.0',
      ribbonSeriesVersion: '1.0.0',
      audienceBasisVersion: '1.0.0',
    }, {}))).toEqual({
      available: false,
      error: 'engine_contract_mismatch',
    });
    expect(error).toHaveBeenCalledTimes(2);
    error.mockRestore();
  });

  test.each(['__proto__', 'constructor', 'prototype'])(
    'manifest cannot inherit authority from own JSON key %s',
    (key) => {
      const error = spyOn(console, 'error').mockImplementation(() => {});
      const hostile = pollutionPayload(key, structuredClone(manifest));
      expect(Object.hasOwn(hostile, key)).toBe(true);
      expect(assertInstrumentPackageManifest(hostile)).toEqual({
        available: false,
        error: 'instrument_package_mismatch',
      });
      expect(error).toHaveBeenCalledTimes(1);
      expect({}.package).toBeUndefined();
      error.mockRestore();
    },
  );

  test.each(['__proto__', 'constructor', 'prototype'])(
    'engine metadata cannot inherit authority from own JSON key %s',
    (key) => {
      const error = spyOn(console, 'error').mockImplementation(() => {});
      const authority = {
        engineSourceSha: manifest.sourceCommit,
        evidenceSummaryVersion: '1.0.0',
        ribbonSeriesVersion: '1.0.0',
        audienceBasisVersion: '1.0.0',
      };
      const hostile = pollutionPayload(key, authority);
      expect(Object.hasOwn(hostile, key)).toBe(true);
      expect(assertEngineContractCompatibility(hostile)).toEqual({
        available: false,
        error: 'engine_contract_mismatch',
      });
      expect(error).toHaveBeenCalledTimes(1);
      expect({}.engineSourceSha).toBeUndefined();
      error.mockRestore();
    },
  );

  test.each(hostileValues())('hostile %s engine metadata fails closed once', (_label, hostile) => {
    const error = spyOn(console, 'error').mockImplementation(() => {});
    let result;
    expect(() => { result = assertEngineContractCompatibility(hostile); }).not.toThrow();
    expect(result).toEqual({
      available: false,
      error: 'engine_contract_mismatch',
    });
    expect(error).toHaveBeenCalledTimes(1);
    error.mockRestore();
  });

  test.each([
    ['missing source SHA', (value) => { delete value.engineSourceSha; }],
    ['mismatched source SHA', (value) => { value.engineSourceSha = 'f'.repeat(40); }],
    ['missing EvidenceSummary version', (value) => { delete value.evidenceSummaryVersion; }],
    ['mismatched EvidenceSummary version', (value) => { value.evidenceSummaryVersion = '2.0.0'; }],
    ['missing RibbonSeries version', (value) => { delete value.ribbonSeriesVersion; }],
    ['mismatched RibbonSeries version', (value) => { value.ribbonSeriesVersion = '2.0.0'; }],
    ['missing AudienceBasis version', (value) => { delete value.audienceBasisVersion; }],
    ['mismatched AudienceBasis version', (value) => { value.audienceBasisVersion = '2.0.0'; }],
  ])('rejects %s without fallback', (_label, mutate) => {
    const error = spyOn(console, 'error').mockImplementation(() => {});
    const metadata = {
      engineSourceSha: manifest.sourceCommit,
      evidenceSummaryVersion: '1.0.0',
      ribbonSeriesVersion: '1.0.0',
      audienceBasisVersion: '1.0.0',
    };
    mutate(metadata);

    expect(assertEngineContractCompatibility(metadata)).toEqual({
      available: false,
      error: 'engine_contract_mismatch',
    });
    expect(error).toHaveBeenCalledTimes(1);
    error.mockRestore();
  });

  test('accepts exact engine contract metadata without a development error', () => {
    const error = spyOn(console, 'error').mockImplementation(() => {});
    expect(assertEngineContractCompatibility({
      engineSourceSha: manifest.sourceCommit,
      evidenceSummaryVersion: '1.0.0',
      ribbonSeriesVersion: '1.0.0',
      audienceBasisVersion: '1.0.0',
    })).toEqual({available: true, error: null});
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });
});

describe('adapter detachment failure contract', () => {
  const sparseSummaryTopic = () => ({
    contract_version: 'desk_dynamic_signal_v2',
    evidence_summary: {
      contractVersion: '1.0.0',
      state: 'ready',
      independence: {status: 'validated', familyCount: 2, groupingAuthority: 'grouping-2026-08'},
    },
  });

  test('an undetachable evidence summary returns the unchecked shape rather than null', () => {
    const summary = adaptSignalToEvidenceSummary(sparseSummaryTopic());

    expect(summary).not.toBeNull();
    expect(summary.state).toBe('unchecked');
    expect(summary.contractVersion).toBe('1.0.0');
    expect(summary.receipts).toEqual([]);
    expect(Array.isArray(summary.limitations)).toBe(true);
    expect(safePlainData(summary).ok).toBe(true);
  });

  test('the instrument model keeps a dereferenceable evidence summary for an undetachable source', () => {
    const model = adaptSignalToInstrumentModel(sparseSummaryTopic());

    expect(model).not.toBeNull();
    expect(model.evidenceSummary).not.toBeNull();
    expect(model.evidenceSummary.state).toBe('unchecked');
    expect(model.evidenceSummary.limitations).toBeDefined();
  });

  test.each([
    ['hostile topic', () => hostileValues()[2][1]],
    ['rejected basis', () => ({contract_version: 'desk_dynamic_signal_v2', audience_basis: {basis: 'measured'}})],
    ['absent basis', () => ({contract_version: 'desk_dynamic_signal_v2'})],
  ])('the audience adapter never returns null for %s', (_label, build) => {
    const basis = adaptSignalToAudienceBasis(build());

    expect(basis).not.toBeNull();
    expect(basis.basis).toBe('unavailable');
    expect(Array.isArray(basis.limitations)).toBe(true);
    expect(safePlainData(basis).ok).toBe(true);
  });
});
