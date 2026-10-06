/* Every entry a capability job names is covered.

   A manifest row can name more than one way into the same job. The creator
   row names three: the creator profile, the network graph and the scoped
   listen read. A row that drove only its first entry would report a green
   state table while two of its routes were never opened, so the required set
   is read from the manifest itself, every route the job names, and held
   against what the fixture module declares: the router has to serve the
   route, a route with no entry has to be declared uncovered in writing, the
   producer file has to exist, and every required state of the row has to be
   either driven by that entry or refused against a named producer symbol,
   never both and never neither. A route added to a job with nothing behind
   it fails here; an entry added to a row that has none does not. */
import {expect, test} from 'bun:test';
import {existsSync, readFileSync, readdirSync, statSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

import {CAPABILITY_FIXTURES} from '../../../tests/browser/fixtures/42-capability-routes/index.js';
import {LISTEN_MARKET, LISTEN_TERM} from '../../../tests/browser/fixtures/42-capability-routes/creators_network_language.js';
import {SERVED_VIEWS} from '../../redesignContract.js';
import {buildScopedReadHash, parseWorkspaceHash} from '../../router.js';

const MANIFEST_URL = new URL('../../../../docs/capability-journeys.json', import.meta.url);
const manifest = JSON.parse(readFileSync(MANIFEST_URL, 'utf8'));
const apiRoot = fileURLToPath(new URL('../../../../src/api/', import.meta.url));
const fixtureRoot = fileURLToPath(new URL('../../../tests/browser/fixtures/42-capability-routes/', import.meta.url));

/* A state every entry here drives, because driving it needs no producer
   feature: a read can be held, a list can come back with no rows, and a read
   can fail. A producer limit is a claim about what the rows mean, so it can
   only be claimed for the states that read the rows. */
const ALWAYS_DRIVEN = ['populated', 'loading', 'empty', 'failed'];

function requiredStates(row){
  return row.required_states.map((entry) => (typeof entry === 'string' ? entry : entry.state));
}

function moduleOf(capabilityId){
  return CAPABILITY_FIXTURES[capabilityId];
}

function entriesOf(capabilityId){
  return moduleOf(capabilityId).entries || [];
}

function entryGapsOf(capabilityId){
  return moduleOf(capabilityId).entry_gaps || {};
}

function view(route){
  const parsed = parseWorkspaceHash(route);
  return parsed.error ? null : parsed.view;
}

function source(capabilityId){
  return readFileSync(fixtureRoot + capabilityId + '.js', 'utf8');
}

/* The routes a job names, read from the job text the manifest publishes: a
   written hash, a scoped read built for a named view, a workspace hash built
   for a named view, and the topic builder, which names the topic route. */
export function routesNamed(row){
  const job = String(row.job || '');
  const views = new Set();
  for (const match of job.matchAll(/#\/([a-z-]+)/g)) views.add(match[1]);
  for (const match of job.matchAll(/buildScopedReadHash with the ([a-z-]+) view/g)) views.add(match[1]);
  for (const match of job.matchAll(/buildWorkspaceHash\(\{view: '([a-z-]+)'/g)) views.add(match[1]);
  if (/\bbuildTopicHash\b/.test(job)) views.add('topic');
  return views;
}

/* A row whose job names more than one route has to account for each of them.
   A row with an unimplemented producer drives nothing at all, and the suite
   runs it as one honest unimplemented case. */
const MULTI_ROUTE = manifest.filter((row) => row.producer !== 'unimplemented' && routesNamed(row).size > 1);
const WITH_ENTRIES = manifest.filter((row) => entriesOf(row.capability_id).length > 0);

test('the manifest names more than one route for at least one served row, and every route it names is a view the router serves', () => {
  expect(MULTI_ROUTE.length).toBeGreaterThan(0);
  for (const row of MULTI_ROUTE){
    for (const named of routesNamed(row)){
      expect(SERVED_VIEWS.has(named), `${row.capability_id} names ${named}`).toBe(true);
    }
  }
});

test('every route a job names is driven by the row route, driven by an entry, or declared uncovered in writing', () => {
  const findings = [];
  for (const row of MULTI_ROUTE){
    const module = moduleOf(row.capability_id);
    const covered = new Set([view(module.route), ...entriesOf(row.capability_id).map((entry) => entry.name)]);
    const declared = entryGapsOf(row.capability_id);
    for (const named of routesNamed(row)){
      if (covered.has(named)) continue;
      const gap = declared[named];
      if (typeof gap !== 'string' || gap.trim().length <= 40 || !gap.trim().endsWith('.') || !gap.includes(named)){
        findings.push(`${row.capability_id} names the ${named} route with no entry and no written gap naming it`);
      }
    }
  }
  expect(findings).toEqual([]);
});

test('every declared entry names the route its job names and the router serves it', () => {
  for (const row of WITH_ENTRIES){
    const named = routesNamed(row);
    for (const entry of entriesOf(row.capability_id)){
      expect(view(entry.route), `${entry.name} route ${entry.route}`).toBe(entry.name);
      expect(SERVED_VIEWS.has(view(entry.route)), `${entry.name} is a served view`).toBe(true);
      expect(named.has(entry.name), `the ${row.capability_id} job names the ${entry.name} route`).toBe(true);
      expect(view(moduleOf(row.capability_id).route), 'the row route is not one of its entries').not.toBe(entry.name);
    }
  }
});

test('the scoped listen entry is built by the manifest route builder, not written by hand', () => {
  const row = manifest.find((item) => item.capability_id === 'creators_network_language');
  const listen = entriesOf('creators_network_language').find((entry) => entry.name === 'listen');
  expect(row.route_builder).toContain('buildScopedReadHash');
  /* The declaration itself is read: a hash typed out by hand carries no call,
     and the router builder is the only thing that keeps the two in step. */
  const written = source('creators_network_language').match(/name: 'listen',[\s\S]{0,200}?\n\s*route: ([^\n]+?),\n/);
  expect(written, 'the listen entry declares a route').not.toBeNull();
  expect(
    row.route_builder.some((builder) => written[1].trim().startsWith(builder + '(')),
    `the listen route is written as ${written[1].trim()} rather than a call to a manifest route builder`,
  ).toBe(true);
  /* The expectation is built from the module's own term and market, never
     from the route being checked. */
  expect(listen.route).toBe(buildScopedReadHash('listen', LISTEN_TERM, LISTEN_MARKET));
  const parsed = parseWorkspaceHash(listen.route);
  expect(parsed.view).toBe('listen');
  expect(parsed.readRegion).toBe(LISTEN_MARKET.toUpperCase());
  expect(listen.route).toContain('/' + encodeURIComponent(LISTEN_TERM) + '?');
});

test('every entry names producer files that exist in this repository', () => {
  for (const row of WITH_ENTRIES){
    for (const entry of entriesOf(row.capability_id)){
      const files = entry.producer.match(/src\/api\/[a-z_]+\.py/g) || [];
      expect(files.length, `${entry.name} names a producer file`).toBeGreaterThan(0);
      for (const file of files){
        expect(existsSync(apiRoot + file.slice('src/api/'.length)), `${entry.name} producer ${file}`).toBe(true);
      }
    }
  }
});

test('every required state of the row is driven or refused by each entry, and never both', () => {
  for (const row of WITH_ENTRIES){
    const required = requiredStates(row);
    for (const entry of entriesOf(row.capability_id)){
      const driven = entry.states;
      const refused = Object.keys(entry.reasons);
      expect([...driven, ...refused].sort(), `${entry.name} covers every required state`).toEqual([...required].sort());
      expect(driven.filter((state) => refused.includes(state)), `${entry.name} states both driven and refused`).toEqual([]);
      for (const state of ALWAYS_DRIVEN){
        if (!required.includes(state)) continue;
        expect(driven, `${entry.name} has to drive ${state} rather than refuse it`).toContain(state);
      }
    }
  }
});

test('every refusal names a producer symbol that exists in the file the entry names, and says what that symbol cannot supply', () => {
  for (const row of WITH_ENTRIES){
    for (const entry of entriesOf(row.capability_id)){
      const files = entry.producer.match(/src\/api\/[a-z_]+\.py/g) || [];
      const producerText = files.map((file) => readFileSync(apiRoot + file.slice('src/api/'.length), 'utf8')).join('\n');
      for (const [state, refusal] of Object.entries(entry.reasons)){
        const where = `${entry.name} ${state}`;
        expect(typeof refusal, where).toBe('object');
        const {symbol, reason} = refusal;
        expect(typeof symbol, `${where} names a producer symbol`).toBe('string');
        expect(
          new RegExp(`(?:^|[^\\w])${symbol}(?:[^\\w]|$)`, 'm').test(producerText),
          `${where} names ${symbol}, which ${entry.producer} does not define`,
        ).toBe(true);
        expect(typeof reason, where).toBe('string');
        expect(reason.trim().length, where).toBeGreaterThan(20);
        expect(reason.trim(), where).toMatch(/\.$/);
        expect(reason, `${where} is not a bare restatement`).not.toBe(state);
        expect(reason.includes(symbol), `${where} says what ${symbol} cannot supply`).toBe(true);
      }
    }
  }
});

test('every driven state has a payload and every refused state has none', () => {
  for (const row of WITH_ENTRIES){
    for (const entry of entriesOf(row.capability_id)){
      for (const state of entry.states){
        const fixtures = entry.fixtures(state);
        expect(Object.keys(fixtures).length, `${entry.name} ${state} payload`).toBeGreaterThan(0);
        for (const value of Object.values(fixtures)) expect(value).toBeDefined();
      }
      for (const state of Object.keys(entry.reasons)){
        expect(() => entry.fixtures(state), `${entry.name} ${state} has no payload`).toThrow();
      }
    }
  }
});

test('a failed entry read carries a body the producer can actually send', () => {
  for (const row of WITH_ENTRIES){
    for (const entry of entriesOf(row.capability_id)){
      if (!entry.states.includes('failed')) continue;
      const failures = Object.values(entry.fixtures('failed')).filter((reply) => reply && reply.__status);
      expect(failures.length, `${entry.name} failed read`).toBeGreaterThan(0);
      for (const failure of failures){
        const detail = failure.body && failure.body.detail;
        const code = detail && typeof detail === 'object' ? detail.code : null;
        if (!code) continue;
        const emitted = ['app/src', 'engine'].some((tree) => {
          const root = fileURLToPath(new URL('../../../../../' + tree + '/', import.meta.url));
          return existsSync(root) && grepTree(root, code);
        });
        expect(emitted, `${entry.name} failed read asserts ${code}, which no producer emits`).toBe(true);
      }
    }
  }
});

function grepTree(root, needle){
  for (const name of readdirSync(root)){
    const path = root + name;
    let stats;
    try { stats = statSync(path); } catch { continue; }
    if (stats.isDirectory()){
      if (name === '__pycache__' || name === 'node_modules') continue;
      if (grepTree(path + '/', needle)) return true;
      continue;
    }
    if (!/\.(?:py|json|sql)$/.test(name)) continue;
    if (readFileSync(path, 'utf8').includes(needle)) return true;
  }
  return false;
}
