import {expect, test} from 'bun:test';

import {buildBriefingModel} from '../../briefingContract.js';
import {buildComparisonRows} from '../../compareStrips.jsx';
import {admitExploreSignals} from '../../explore.jsx';

function row(id, name, velocity='high'){
  return {
    signal: {
      contract_version: 'desk_dynamic_signal_v2',
      run_id: 'run_20260827_dynamic_apply_v2',
      signal_id: `sig_${id.repeat(64)}`,
      signal_date: '2026-08-27',
      market: 'za',
      signal_name: name,
      discovery_mode: 'phrase',
      evidence_state: 'unchecked',
      why_now: `${name} why now`,
      possible_response: null,
      observation_start: '2026-08-14',
      observation_end: '2026-08-27',
      observation_method: 'dynamic_source_copy_apply_v1',
      receipts: [],
      qualities: {
        velocity,
        novelty: 'moderate',
        breadth: 'high',
        independence: 'moderate',
        history: 'unmeasured',
        geo_confidence: 'high',
        topic_tags: ['repair'],
      },
    },
  };
}

test('Discover and Briefing preserve v2 engine order and reject curated rows', () => {
  const engineRows = [row('b', 'First engine signal'), row('a', 'Second engine signal')];
  const curated = {
    id: 'music_amapiano',
    signal_id: `sig_${'c'.repeat(64)}`,
    signal_name: 'Curated topic dressed as a signal',
    discovery_mode: 'phrase',
    evidence_state: 'ready',
    market: 'za',
    receipts: [],
    qualities: row('c', 'ignored').signal.qualities,
  };
  const admitted = admitExploreSignals([...engineRows, curated]).signals;

  expect(admitted.map((signal) => signal.title)).toEqual([
    'First engine signal',
    'Second engine signal',
  ]);
  expect(buildBriefingModel(engineRows[0]).title).toBe('First engine signal');
});

test('Compare exposes the six qualities, why now, precedent availability, market and proof', () => {
  const admitted = admitExploreSignals([row('a', 'First'), row('b', 'Second', 'low')]).signals;
  const rows = buildComparisonRows(admitted);

  expect(rows.map((item) => item.key)).toEqual([
    'market',
    'readiness',
    'why_now',
    'precedent',
    'receipts',
    'velocity',
    'novelty',
    'breadth',
    'independence',
    'history',
    'geo_confidence',
  ]);
});
