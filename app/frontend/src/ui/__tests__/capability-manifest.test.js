import {describe, expect, test} from 'bun:test';
import {existsSync, readdirSync, readFileSync, statSync} from 'node:fs';
import {extname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import * as router from '../../router.js';
import * as workbenchRoute from '../../workbenchRoute.js';

/* The capability inventory is a manifest of real code. Every builder it names
   must be an exported function of the current router, every producer it names
   must be a file in this repository, and every contract version it names must
   be a literal the producer or a consumer actually carries. A row whose
   producer does not exist yet says so with the exact word below; it is a
   failing row in the report and the test refuses to let it be blank. */
const APP_ROOT = fileURLToPath(new URL('../../../../', import.meta.url));
const MANIFEST_PATH = join(APP_ROOT, 'docs', 'capability-journeys.json');
const UNIMPLEMENTED = 'unimplemented';
const UNVERSIONED = 'unversioned';

const FIELDS = ['capability_id', 'route_builder', 'job', 'producer', 'contract_version', 'primary_action', 'proof_panel', 'required_states'];
const STATES = ['populated', 'loading', 'empty', 'thin', 'stale', 'contradictory', 'failed', 'held'];
const REQUIRED_JOBS = [
  'today_briefing',
  'discovery_exploration',
  'compare',
  'sources_evidence',
  'creators_network_language',
  'history',
  'questions_followups',
  'fieldwork',
  'coverage_source_lab',
  'investigation_build_review',
  'bsa_configuration',
];

const BUILDERS = {...router, ...workbenchRoute};

function nonEmptyString(value){
  return typeof value === 'string' && value.trim().length > 0;
}

function walk(directory, found = []){
  for (const name of readdirSync(directory)){
    if (name === '__tests__' || name === 'node_modules') continue;
    const path = join(directory, name);
    if (statSync(path).isDirectory()) walk(path, found);
    else if (['.py', '.js', '.jsx'].includes(extname(name))) found.push(path);
  }
  return found;
}

let sourceText = null;
function contractLiteralExists(version){
  if (sourceText === null){
    sourceText = [...walk(join(APP_ROOT, 'src', 'api')), ...walk(join(APP_ROOT, 'frontend', 'src'))]
      .map(path => readFileSync(path, 'utf8')).join('\n');
  }
  return sourceText.includes(`'${version}'`) || sourceText.includes(`"${version}"`);
}

export function validateManifest(records){
  if (!Array.isArray(records) || records.length === 0) throw new Error('manifest must be a nonempty array of records');
  const seen = new Set();
  for (const record of records){
    if (!record || typeof record !== 'object' || Array.isArray(record)) throw new Error('record must be an object');
    const keys = Object.keys(record).sort();
    if (keys.join(',') !== [...FIELDS].sort().join(',')) throw new Error(`record fields must be exactly ${FIELDS.join(', ')}; got ${keys.join(', ')}`);
    const id = record.capability_id;
    if (!nonEmptyString(id)) throw new Error('capability_id must be a nonempty string');
    if (seen.has(id)) throw new Error(`duplicate capability_id ${id}`);
    seen.add(id);
    if (!Array.isArray(record.route_builder) || record.route_builder.length === 0) throw new Error(`${id}: route_builder must name at least one builder`);
    if (new Set(record.route_builder).size !== record.route_builder.length) throw new Error(`${id}: route_builder repeats a builder`);
    for (const name of record.route_builder){
      if (typeof BUILDERS[name] !== 'function') throw new Error(`${id}: unregistered route builder ${name}`);
    }
    if (!nonEmptyString(record.job)) throw new Error(`${id}: job must be a nonempty string`);
    if (!nonEmptyString(record.producer)) throw new Error(`${id}: empty producer name`);
    if (record.producer !== UNIMPLEMENTED && !existsSync(join(APP_ROOT, record.producer))) throw new Error(`${id}: producer ${record.producer} is not a file under app/`);
    if (!nonEmptyString(record.contract_version)) throw new Error(`${id}: empty contract name`);
    if (record.producer === UNIMPLEMENTED && record.contract_version !== UNIMPLEMENTED) throw new Error(`${id}: an unimplemented producer cannot carry a contract version`);
    if (![UNIMPLEMENTED, UNVERSIONED].includes(record.contract_version) && !contractLiteralExists(record.contract_version)) throw new Error(`${id}: contract literal ${record.contract_version} is not in the source`);
    if (!nonEmptyString(record.primary_action)) throw new Error(`${id}: absent primary action`);
    if (!nonEmptyString(record.proof_panel)) throw new Error(`${id}: proof_panel must be a nonempty string`);
    if (!Array.isArray(record.required_states) || record.required_states.length === 0) throw new Error(`${id}: required_states must be a nonempty array`);
    const covered = new Set();
    for (const entry of record.required_states){
      let state = entry;
      if (entry && typeof entry === 'object'){
        const entryKeys = Object.keys(entry).sort().join(',');
        if (entryKeys !== 'inapplicable_reason,state') throw new Error(`${id}: an inapplicable state is exactly {state, inapplicable_reason}`);
        if (!nonEmptyString(entry.inapplicable_reason)) throw new Error(`${id}: inapplicable state ${entry.state} needs a nonempty reason`);
        state = entry.state;
      }
      if (!STATES.includes(state)) throw new Error(`${id}: unknown required state ${JSON.stringify(state)}`);
      if (covered.has(state)) throw new Error(`${id}: required state ${state} listed twice`);
      covered.add(state);
    }
    const missing = STATES.filter(state => !covered.has(state));
    if (missing.length) throw new Error(`${id}: required_states must account for every state; missing ${missing.join(', ')}`);
  }
  const missingJobs = REQUIRED_JOBS.filter(job => !seen.has(job));
  if (missingJobs.length) throw new Error(`manifest is missing required jobs ${missingJobs.join(', ')}`);
  const extra = [...seen].filter(id => !REQUIRED_JOBS.includes(id));
  if (extra.length) throw new Error(`manifest names jobs outside the required table: ${extra.join(', ')}`);
  return records;
}

function loadManifest(){
  if (!existsSync(MANIFEST_PATH)) throw new Error(`capability manifest is missing at ${MANIFEST_PATH}`);
  return JSON.parse(readFileSync(MANIFEST_PATH, 'utf8'));
}

function mutate(change){
  const copy = JSON.parse(JSON.stringify(loadManifest()));
  change(copy);
  return copy;
}

describe('capability manifest', () => {
  test('lists one validated record per required job', () => {
    const records = validateManifest(loadManifest());
    expect(records.map(record => record.capability_id).sort()).toEqual([...REQUIRED_JOBS].sort());
  });

  test('unimplemented rows are visible, not hidden', () => {
    const rows = loadManifest().filter(record => record.producer === UNIMPLEMENTED || record.contract_version === UNVERSIONED);
    for (const row of rows) expect(REQUIRED_JOBS).toContain(row.capability_id);
  });

  test.each([
    ['duplicate capability id', records => { records.push({...records[0]}); }, /duplicate capability_id/],
    ['absent primary action', records => { records[0].primary_action = ''; }, /absent primary action/],
    ['unregistered route builder', records => { records[0].route_builder = ['buildMissingHash']; }, /unregistered route builder/],
    ['empty producer name', records => { records[0].producer = ' '; }, /empty producer name/],
    ['producer that is not a file', records => { records[0].producer = 'src/api/not_a_module.py'; }, /not a file/],
    ['empty contract name', records => { records[0].contract_version = ''; }, /empty contract name/],
    ['contract literal absent from the source', records => { records[0].contract_version = 'desk_dynamic_signal_v99'; }, /not in the source/],
    ['unknown required state', records => { records[0].required_states[0] = 'ready'; }, /unknown required state/],
    ['inapplicable state without a reason', records => { records[0].required_states[0] = {state: 'held', inapplicable_reason: ''}; }, /nonempty reason/],
    ['incomplete state coverage', records => { records[0].required_states.pop(); }, /account for every state/],
    ['missing required job', records => { records.pop(); }, /missing required jobs/],
  ])('rejects %s', (_label, change, message) => {
    expect(() => validateManifest(mutate(change))).toThrow(message);
  });
});
