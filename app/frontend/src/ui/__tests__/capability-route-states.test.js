import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {CAPABILITY_FIXTURES} from '../../../tests/browser/fixtures/42-capability-routes/index.js';

/* The emitted state table is checked against the manifest it was generated
   from: every capability and required state exactly once, one row for every
   declared entry beside it in the same states, a reason on every inapplicable
   row, and a vocabulary a reader can rely on. An entry that refuses a state
   carries the refusal it declared as its gate, so a refusal cannot be dropped
   or rewritten without the table saying so. The validator is exercised on
   mutated copies so a table that quietly drifts is refused. */
const MANIFEST_URL = new URL('../../../../docs/capability-journeys.json', import.meta.url);
const STATES_URL = new URL('../../../../docs/capability-route-states.json', import.meta.url);
const manifest = JSON.parse(readFileSync(MANIFEST_URL, 'utf8'));
const raw = readFileSync(STATES_URL, 'utf8');
const emitted = JSON.parse(raw);

const STATES = new Set(['populated', 'loading', 'empty', 'thin', 'stale', 'contradictory', 'failed', 'held']);
const RESULTS = new Set(['passed', 'failed', 'inapplicable', 'unimplemented', 'refused']);
const FIELDS = ['capability_id', 'entry', 'state', 'ui_receipt', 'native_data_binding', 'test_result', 'remaining_gate', 'inapplicable_reason'].sort().join();
const key = (capabilityId, entry, state) => `${capabilityId}|${entry || ''}|${state}`;

function declaredEntries(capabilityId){
  return (CAPABILITY_FIXTURES[capabilityId] || {}).entries || [];
}

export function validateStateRows(rows, journeys){
  const expected = new Map();
  for (const row of journeys){
    for (const entry of row.required_states){
      const state = typeof entry === 'string' ? entry : entry.state;
      if (!STATES.has(state)) throw new Error(`unknown manifest state ${row.capability_id} ${state}`);
      const reason = typeof entry === 'string' ? null : entry.inapplicable_reason;
      expected.set(key(row.capability_id, null, state), {reason, refusal: null});
      for (const declared of declaredEntries(row.capability_id)){
        expected.set(key(row.capability_id, declared.name, state), {reason, refusal: declared.reasons[state] || null});
      }
    }
  }
  const seen = new Set();
  for (const row of rows){
    if (Object.keys(row).sort().join() !== FIELDS) throw new Error('row fields differ from the contract');
    if (!STATES.has(row.state)) throw new Error(`unknown state ${row.state}`);
    const id = key(row.capability_id, row.entry, row.state);
    const label = `${row.capability_id} ${row.entry ? row.entry + ' ' : ''}${row.state}`;
    if (!expected.has(id)) throw new Error(`row outside the manifest ${label}`);
    if (seen.has(id)) throw new Error(`duplicate row ${label}`);
    seen.add(id);
    if (!RESULTS.has(row.test_result)) throw new Error(`unknown result ${row.test_result}`);
    if (row.native_data_binding !== 'fixture') throw new Error('binding is not the fixture binding');
    if (typeof row.remaining_gate !== 'string' || !row.remaining_gate.trim()) throw new Error(`gate missing on ${label}`);
    if (!row.ui_receipt.startsWith(row.capability_id + ' ')) throw new Error(`receipt does not name the capability ${label}`);
    const {reason, refusal} = expected.get(id);
    if (row.entry){
      if (!row.ui_receipt.startsWith(`${row.capability_id} ${row.entry} `)) throw new Error(`receipt does not name the entry ${label}`);
      if (refusal){
        if (row.ui_receipt !== `${row.capability_id} ${row.entry} ${row.state} reason`) throw new Error(`refusal receipt is not the reason case ${label}`);
        if (!['refused', 'failed'].includes(row.test_result)) throw new Error(`refused state ran as ${row.test_result} on ${label}`);
        if (row.remaining_gate !== refusal.reason) throw new Error(`refusal gate does not carry the declared reason ${label}`);
      } else {
        if (row.ui_receipt !== `${row.capability_id} ${row.entry} ${row.state}`) throw new Error(`driven receipt is not the entry case ${label}`);
        if (row.test_result === 'refused') throw new Error(`driven state reported as refused ${label}`);
      }
    } else if (row.test_result === 'refused'){
      throw new Error(`a capability row cannot be refused ${label}`);
    }
    if (reason){
      if (row.inapplicable_reason !== reason || !/\S/.test(reason)) throw new Error(`missing reason on ${label}`);
      if (!['inapplicable', 'unimplemented', 'failed', 'refused'].includes(row.test_result)) throw new Error(`inapplicable row ran as ${row.test_result}`);
    } else {
      if (row.inapplicable_reason !== null) throw new Error(`reason on an applicable row ${label}`);
      if (row.test_result === 'inapplicable') throw new Error(`inapplicable without a manifest reason ${label}`);
    }
  }
  for (const id of expected.keys()) if (!seen.has(id)) throw new Error(`missing row ${id}`);
  return rows.length;
}

const mutated = (change) => { const copy = structuredClone(emitted); change(copy); return copy; };
const entryCount = manifest.reduce((sum, row) => sum + declaredEntries(row.capability_id).length * row.required_states.length, 0);

describe('capability route state table', () => {
  test('covers every manifest row, required state and declared entry exactly once', () => {
    const total = manifest.reduce((sum, row) => sum + row.required_states.length, 0);
    expect(validateStateRows(emitted, manifest)).toBe(total + entryCount);
    expect(total).toBe(88);
    expect(entryCount).toBe(16);
  });

  test('is written sorted, two space indented and LF', () => {
    const sorted = [...emitted].sort((a, b) => a.capability_id.localeCompare(b.capability_id)
      || String(a.entry || '').localeCompare(String(b.entry || ''))
      || a.state.localeCompare(b.state));
    expect(raw).toBe(JSON.stringify(sorted, null, 2) + '\n');
    expect(raw.includes('\r')).toBe(false);
  });

  test('rejects an unknown state', () => {
    expect(() => validateStateRows(mutated((rows) => { rows[0].state = 'unknown'; }), manifest)).toThrow(/unknown state/);
  });

  test('rejects a missing reason on an inapplicable row', () => {
    expect(() => validateStateRows(mutated((rows) => { rows.find((row) => row.inapplicable_reason).inapplicable_reason = null; }), manifest)).toThrow(/missing reason/);
  });

  test('rejects a duplicate row and a missing row', () => {
    expect(() => validateStateRows(mutated((rows) => { rows.push({...rows[0]}); }), manifest)).toThrow(/duplicate row/);
    expect(() => validateStateRows(mutated((rows) => { rows.pop(); }), manifest)).toThrow(/missing row/);
  });

  test('rejects a dropped entry row, a rewritten refusal and a refusal reported as a pass', () => {
    expect(() => validateStateRows(mutated((rows) => {
      const at = rows.findIndex((row) => row.entry);
      rows.splice(at, 1);
    }), manifest)).toThrow(/missing row/);
    expect(() => validateStateRows(mutated((rows) => {
      const row = rows.find((item) => item.test_result === 'refused');
      row.remaining_gate = 'The producer cannot supply this state.';
    }), manifest)).toThrow(/refusal gate does not carry the declared reason/);
    expect(() => validateStateRows(mutated((rows) => {
      const row = rows.find((item) => item.test_result === 'refused');
      row.test_result = 'passed';
    }), manifest)).toThrow(/refused state ran as passed/);
  });

  test('every declared entry refusal is a row a reader can find', () => {
    const refusals = [];
    for (const module of Object.values(CAPABILITY_FIXTURES)){
      for (const entry of module.entries || []){
        for (const [state, refusal] of Object.entries(entry.reasons)){
          refusals.push({capability_id: module.capability_id, entry: entry.name, state, reason: refusal.reason});
        }
      }
    }
    expect(refusals.length).toBe(7);
    for (const refusal of refusals){
      const row = emitted.find((item) => item.capability_id === refusal.capability_id && item.entry === refusal.entry && item.state === refusal.state);
      expect(row, `${refusal.capability_id} ${refusal.entry} ${refusal.state}`).toBeDefined();
      expect(row.test_result).toBe('refused');
      expect(row.remaining_gate).toBe(refusal.reason);
    }
  });

  test('the failed rows are exactly the declared producer gaps', () => {
    const gaps = new Map();
    for (const module of Object.values(CAPABILITY_FIXTURES)){
      for (const [state, gap] of Object.entries(module.gaps)) gaps.set(`${module.capability_id} ${state}`, gap);
    }
    const failed = emitted.filter((row) => row.test_result === 'failed');
    expect(failed.length).toBe(gaps.size);
    for (const row of failed) expect(row.remaining_gate).toBe(gaps.get(`${row.capability_id} ${row.state}`));
    expect(gaps.size).toBe(10);
  });

  test('every failing row names the gate it waits on', () => {
    for (const row of emitted.filter((item) => item.test_result === 'failed')){
      expect(row.remaining_gate.length).toBeGreaterThan(20);
    }
  });
});
