/* Emits app/docs/capability-route-states.json from the capability route
   suite: one row per manifest capability and required state, and one row per
   declared entry and required state beside it, written sorted with two space
   indent and LF so the file is identical across runs. A run that carries no
   capability test leaves the file alone.

   An entry case is titled `<capability> <entry> <state>` and a refusal case
   `<capability> <entry> <state> reason`, so the title reader takes two to
   four words. A title it dropped would leave the entry invisible here and a
   regression behind it would read as a green table. */
import {writeFileSync} from 'node:fs';
import {CAPABILITY_FIXTURES} from '../fixtures/42-capability-routes/index.js';
import {readManifest} from './capability-harness.mjs';

export const STATES_URL = new URL('../../../../docs/capability-route-states.json', import.meta.url);
const TITLE = /^[a-z_]+(?: [a-z_]+){1,3}$/;

function resultOf(results, title){
  const seen = results.get(title);
  return {seen, passed: Boolean(seen && seen.status === 'passed')};
}

function entryRows(row, reason, state, results){
  const rows = [];
  for (const entry of CAPABILITY_FIXTURES[row.capability_id].entries || []){
    const refusal = entry.reasons[state];
    const title = refusal
      ? `${row.capability_id} ${entry.name} ${state} reason`
      : `${row.capability_id} ${entry.name} ${state}`;
    const {seen, passed} = resultOf(results, title);
    rows.push({
      capability_id: row.capability_id, entry: entry.name, state, ui_receipt: title,
      native_data_binding: 'fixture',
      test_result: refusal ? (passed ? 'refused' : 'failed') : seen ? seen.status : 'not_run',
      remaining_gate: refusal ? refusal.reason : (seen && seen.gate) || 'native staging data',
      inapplicable_reason: reason,
    });
  }
  return rows;
}

export function buildStateRows(manifest, results){
  const rows = [];
  for (const row of manifest){
    const unimplemented = row.producer === 'unimplemented';
    const honest = unimplemented ? results.get(`${row.capability_id} unimplemented`) : null;
    for (const entry of row.required_states){
      const state = typeof entry === 'string' ? entry : entry.state;
      const reason = typeof entry === 'string' ? null : entry.inapplicable_reason;
      if (unimplemented){
        rows.push({
          capability_id: row.capability_id, entry: null, state, ui_receipt: `${row.capability_id} unimplemented`,
          native_data_binding: 'fixture',
          test_result: honest ? (honest.status === 'passed' ? 'unimplemented' : 'failed') : 'not_run',
          remaining_gate: `producer ${row.producer}`, inapplicable_reason: reason,
        });
        continue;
      }
      const title = `${row.capability_id} ${state}`;
      const {seen, passed} = resultOf(results, title);
      rows.push({
        capability_id: row.capability_id, entry: null, state, ui_receipt: title, native_data_binding: 'fixture',
        test_result: reason ? (passed ? 'inapplicable' : 'failed') : seen ? seen.status : 'not_run',
        remaining_gate: reason ? 'none' : (seen && seen.gate) || 'native staging data',
        inapplicable_reason: reason,
      });
      rows.push(...entryRows(row, reason, state, results));
    }
  }
  rows.sort((a, b) => a.capability_id.localeCompare(b.capability_id)
    || String(a.entry || '').localeCompare(String(b.entry || ''))
    || a.state.localeCompare(b.state));
  return rows;
}

export default class CapabilityStatesReporter {
  constructor(){ this.results = new Map(); }

  onTestEnd(test, result){
    if (!TITLE.test(test.title)) return;
    const annotations = [...(result.annotations || []), ...(test.annotations || [])];
    const fail = annotations.find((item) => item.type === 'fail' && item.description);
    this.results.set(test.title, {status: result.status, gate: fail ? fail.description : null});
  }

  onEnd(){
    if (this.results.size === 0) return;
    const rows = buildStateRows(readManifest(), this.results);
    writeFileSync(STATES_URL, JSON.stringify(rows, null, 2) + '\n');
    const expected = rows.filter((row) => row.test_result === 'failed' && this.results.get(row.ui_receipt)?.gate);
    console.log(`expected failures: ${expected.length} (producer gaps)`);
  }
}
