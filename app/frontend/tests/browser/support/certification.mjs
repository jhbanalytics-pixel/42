/* The frontend certification workload, its frozen thresholds and the proof
   document the two certification suites emit.

   Everything a measurement needs to be read later is fixed here: the four
   viewports, the two themes, the routes under request pressure, the C07
   thresholds frozen before execution, the ten rubric principles of the parent
   visual floor and the candidate binding fields. A measurement is recorded as
   an annotation on the test that took it, so the reporter under support/ can
   read every row from the run result without a shared scratch file, and the
   proof document is rebuilt sorted so two runs over the same candidate differ
   only in the values they measured. The module imports nothing from the test
   runner, so the shape test under src/ui/__tests__ can build and validate a
   document with the same code the reporter uses. */
import {existsSync, mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {isAbsolute, relative} from 'node:path';
import {CAPABILITY_FIXTURES} from '../fixtures/42-capability-routes/index.js';
import {readManifest} from './capability-harness.mjs';

export const CONTRACT_VERSION = 'frontend_certification_v1';

/* C07 thresholds, frozen before execution. Feedback is the time from the
   pointer press to the first observed change in the DOM or in the pressed
   control's computed style, measured inside the page. Overflow is the number
   of CSS pixels the document is wider than the viewport. */
export const THRESHOLDS = Object.freeze({
  lcp_ms: 2500,
  cls: 0.1,
  feedback_ms: 100,
  horizontal_overflow_px: 0,
  action_target_px: 44,
  contrast_ratio: 4.5,
  reduced_motion_moving_elements: 0,
});

export const VIEWPORTS = Object.freeze([
  Object.freeze({width: 390, height: 844}),
  Object.freeze({width: 768, height: 1024}),
  Object.freeze({width: 1024, height: 768}),
  Object.freeze({width: 1440, height: 1000}),
]);
export const THEMES = Object.freeze(['daylight', 'midnight']);
export const THEME_KEY = 'oi-theme';
export const PRESSURE_ROUTES = Object.freeze(['#/pulse', '#/explore', '#/compare', '#/console?work=ask']);
export const CACHE_STATES = Object.freeze(['cold', 'warm']);
export const FLOWS = Object.freeze(['keyboard', 'reduced_motion', 'cold_load', 'warm_load']);
export const PRINCIPLES = Object.freeze([
  'innovative', 'useful', 'aesthetic', 'understandable', 'unobtrusive',
  'honest', 'long-lasting', 'thorough', 'environmentally friendly', 'as little design as possible',
]);
export const FLOOR_TOTAL = 24;
export const FLOOR_PRINCIPLE_MINIMUM = 2;
export const BINDING_FIELDS = Object.freeze([
  'repo_commit', 'image_digest', 'service_revision', 'served_asset_hashes', 'design_package_version',
  'scope_digest', 'policy_digest', 'capture_identity', 'release_identity',
]);
export const UNBOUND = 'unbound';
export const UNBOUND_REASON = 'native binding pending';
export const FIXTURE_ANSWER = 'fixture, not a model answer';
export const ANSWER_STATES = Object.freeze(['held', 'empty', 'fixture']);
export const SUITES = Object.freeze({layout: '42-layout-a11y.pw.mjs', pressure: '42-request-pressure.pw.mjs'});
export const SUITE_FILES = new Set(Object.values(SUITES));

export function viewportName(viewport){
  return `${viewport.width}x${viewport.height}`;
}

export function implementedCapabilities(){
  return readManifest()
    .filter((row) => row.producer !== 'unimplemented')
    .map((row) => ({capability_id: row.capability_id, proof_panel: row.proof_panel, module: CAPABILITY_FIXTURES[row.capability_id]}));
}

/* The app reads its theme from this key before the first paint, in the
   inline script of index.html and again in App.jsx, so a preference stored
   before navigation is the theme the route renders with. */
export function themeInit(page, theme){
  return page.addInitScript(([key, value]) => { localStorage.setItem(key, value); }, [THEME_KEY, theme]);
}

export function record(testInfo, row){
  testInfo.annotations.push({type: 'certification', description: JSON.stringify(row)});
}

export function recordModelAnswer(testInfo, row){
  testInfo.annotations.push({type: 'certification-model-answer', description: JSON.stringify(row)});
}

/* A failed case keeps its full page screenshot under test-results and names
   the file on the test, so the proof output carries the file name. */
export async function retainFailureScreenshot(page, testInfo){
  if (testInfo.status === testInfo.expectedStatus) return null;
  const path = testInfo.outputPath('failure.png');
  try {
    await page.screenshot({path, fullPage: true});
  } catch (error){
    testInfo.annotations.push({type: 'certification-screenshot', description: `unavailable: ${error.message.split('\n')[0]}`});
    return null;
  }
  const name = relative(process.cwd(), path).split('\\').join('/');
  testInfo.annotations.push({type: 'certification-screenshot', description: name});
  return name;
}

/* Fonts loaded, every running animation finished and two frames painted. */
export async function settle(page){
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => Promise.all(document.getAnimations().map((animation) => animation.finished.catch(() => null))));
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

export function evidenceDirectory(){
  const directory = process.env.CERTIFICATION_EVIDENCE_DIR;
  if (typeof directory !== 'string' || !directory || !isAbsolute(directory)) throw new Error('certification evidence directory is not configured');
  return directory;
}

export function candidateName(){
  return process.env.CERTIFICATION_CANDIDATE || UNBOUND;
}

export function proofPath(){
  return `${evidenceDirectory().replace(/[\\/]+$/, '')}/frontend-certification-${candidateName()}.json`;
}

/* The scorer identity lives in a private file beside the proof output, never
   in the repository. The default name is deliberately outside every tracked
   path. */
export function scorerIdentityPath(){
  return process.env.CERTIFICATION_SCORER_FILE || `${evidenceDirectory().replace(/[\\/]+$/, '')}/scorer-identity.private.json`;
}

export function readScorerIdentity(path = scorerIdentityPath()){
  if (!existsSync(path)) return null;
  return JSON.parse(readFileSync(path, 'utf8'));
}

export function unboundBinding(){
  const binding = {};
  for (const field of BINDING_FIELDS) binding[field] = null;
  binding.reason = UNBOUND_REASON;
  return binding;
}

export function workload(){
  return {
    fixture_source: 'app/frontend/tests/browser/fixtures/42-capability-routes',
    fixture_state: 'populated',
    capabilities: implementedCapabilities().map((row) => row.capability_id),
    pressure_routes: [...PRESSURE_ROUTES],
    themes: [...THEMES],
    viewports: VIEWPORTS.map(viewportName),
    flows: [...FLOWS],
    cache_states: [...CACHE_STATES],
    throttling: 'none',
    network: 'every /api path answered by the route interception fixture; no network',
    fonts: 'bundled with the design package, awaited through document.fonts.ready',
    browser: 'system Chrome, headless',
  };
}

export function emptyVisualFloor(){
  return {
    rubric: 'Rams ten principles scored 0 to 3 each by an independent rendered critique',
    floor_total: FLOOR_TOTAL,
    floor_principle_minimum: FLOOR_PRINCIPLE_MINIMUM,
    principles: PRINCIPLES.map((name, index) => ({number: index + 1, name, score: null})),
    total: null,
    scored: false,
    scorer_identity_present: false,
    note: 'Unscored in this slice. A score is written only with the scorer identity in its private file beside the proof output.',
  };
}

const ROW_KEYS = ['suite', 'check', 'route', 'capability_id', 'theme', 'viewport', 'cache_state', 'subject'];

export function rowKey(row){
  return ROW_KEYS.map((key) => String(row[key] === undefined ? '' : row[key])).join('\u0001');
}

function compareRows(a, b){
  return rowKey(a).localeCompare(rowKey(b));
}

function dedupe(rows){
  const seen = new Map();
  for (const row of rows) seen.set(rowKey(row), row);
  return [...seen.values()].sort(compareRows);
}

/* A case that ran replaces its own rows in the previous document, keyed on
   suite, route, theme and viewport; a case that did not run keeps its rows,
   so a partial rerun refreshes only what it measured. */
export function caseKey(row){
  return [row.suite, row.route, row.theme, row.viewport].map((value) => String(value === undefined ? '' : value)).join('');
}

export function buildProofDocument({rows, modelAnswers = [], screenshots = [], results = [], suites, previous = null, capturedAt = null}){
  const ran = new Set(suites);
  const measured = new Set([...rows, ...modelAnswers].map(caseKey));
  const titles = new Set(results.map((result) => `${result.suite} ${result.title}`));
  const keptRows = (list) => (previous && Array.isArray(list) ? list.filter((row) => !measured.has(caseKey(row))) : []);
  const keptCases = (list, name) => (previous && Array.isArray(list) ? list.filter((entry) => !titles.has(`${entry.suite} ${entry[name]}`)) : []);
  const measurements = dedupe([...keptRows(previous && previous.measurements), ...rows]);
  const answers = dedupe([...keptRows(previous && previous.model_answer_latency), ...modelAnswers]);
  const retained = [...keptCases(previous && previous.retained_screenshots, 'test'), ...screenshots]
    .sort((a, b) => `${a.suite} ${a.test}`.localeCompare(`${b.suite} ${b.test}`));
  const cases = [...keptCases(previous && previous.cases, 'title'), ...results]
    .sort((a, b) => `${a.suite} ${a.title}`.localeCompare(`${b.suite} ${b.title}`));
  const summary = {};
  for (const row of measurements){
    const bucket = summary[row.check] || (summary[row.check] = {measured: 0, passed: 0, failed: 0});
    bucket.measured += 1;
    if (row.pass) bucket.passed += 1; else bucket.failed += 1;
  }
  return {
    contract_version: CONTRACT_VERSION,
    candidate: candidateName(),
    candidate_binding: previous && previous.candidate === candidateName() && previous.candidate_binding ? previous.candidate_binding : unboundBinding(),
    captured_at: capturedAt,
    thresholds: {...THRESHOLDS},
    workload: workload(),
    suites_run: [...new Set([...ran, ...(previous && Array.isArray(previous.suites_run) ? previous.suites_run : [])])].sort(),
    summary,
    measurements,
    model_answer_latency: answers,
    retained_screenshots: retained,
    cases,
    visual_floor: previous && previous.visual_floor ? previous.visual_floor : emptyVisualFloor(),
  };
}

export function readProofDocument(path = proofPath()){
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch (error){
    return null;
  }
}

export function writeProofDocument(document, path = proofPath()){
  mkdirSync(path.replace(/[\\/][^\\/]+$/, ''), {recursive: true});
  writeFileSync(path, JSON.stringify(document, null, 2) + '\n');
  return path;
}

/* Shape validation. The scorer identity is passed in by the caller, read
   from the private file, and a scored floor without it is refused. */
const VIEWPORT_NAMES = new Set(VIEWPORTS.map(viewportName));
const THEME_SET = new Set(THEMES);
const CHECKS = new Set([
  'horizontal_overflow', 'key_panel_reachable', 'text_ranges_contained', 'action_targets', 'focus_reaches_primary',
  'contrast_body_text', 'contrast_primary_action', 'reduced_motion', 'lcp', 'cls', 'feedback',
]);

function coverageKey(row){
  return [row.suite, row.check, row.route, row.suite === SUITES.layout ? row.capability_id : '', row.theme, row.viewport, row.cache_state || ''].join('\u0001');
}

function expectedCoverage(){
  const keys = new Set();
  for (const capability of implementedCapabilities()){
    for (const theme of THEMES){
      for (const viewport of VIEWPORTS){
        const base = {suite: SUITES.layout, route: capability.module.route, capability_id: capability.capability_id, theme, viewport: viewportName(viewport)};
        for (const check of ['horizontal_overflow', 'key_panel_reachable', 'text_ranges_contained', 'focus_reaches_primary', 'contrast_body_text', 'contrast_primary_action']){
          keys.add(coverageKey({...base, check}));
        }
        if (viewport.width === VIEWPORTS[0].width) keys.add(coverageKey({...base, check: 'action_targets'}));
      }
      keys.add(coverageKey({suite: SUITES.layout, route: capability.module.route, capability_id: capability.capability_id, theme, viewport: viewportName(VIEWPORTS[2]), check: 'reduced_motion'}));
    }
  }
  for (const route of PRESSURE_ROUTES){
    for (const theme of THEMES){
      for (const viewport of VIEWPORTS){
        for (const cache_state of CACHE_STATES){
          for (const check of ['lcp', 'cls']) keys.add(coverageKey({suite: SUITES.pressure, route, theme, viewport: viewportName(viewport), cache_state, check}));
        }
        keys.add(coverageKey({suite: SUITES.pressure, route, theme, viewport: viewportName(viewport), cache_state: 'warm', check: 'feedback'}));
      }
    }
  }
  return keys;
}

function expectedAnswerCoverage(){
  const keys = new Set();
  for (const theme of THEMES) for (const viewport of VIEWPORTS) keys.add([SUITES.pressure, '#/console?work=ask', theme, viewportName(viewport)].join('\u0001'));
  return keys;
}

function summaryOf(rows){
  const summary = {};
  for (const row of rows){
    const bucket = summary[row.check] || (summary[row.check] = {measured: 0, passed: 0, failed: 0});
    bucket.measured += 1;
    if (row.pass) bucket.passed += 1; else bucket.failed += 1;
  }
  return summary;
}

function sameKeys(actual, expected){
  return actual.size === expected.size && [...expected].every((key) => actual.has(key));
}

function finiteOrNull(value){
  return value === null || (typeof value === 'number' && Number.isFinite(value));
}

function validateMeasurement(row){
  if (!finiteOrNull(row.value)) refuse(`${row.check} value is not finite or null`);
  const numberPassesAtMost = (threshold) => row.pass === (row.value !== null && row.value <= threshold);
  const numberPassesAtLeast = (threshold) => row.pass === (row.value !== null && row.value >= threshold);
  if (row.check === 'lcp'){
    if (row.unit !== 'ms' || row.threshold !== THRESHOLDS.lcp_ms || !numberPassesAtMost(THRESHOLDS.lcp_ms)) refuse('lcp measurement relation differs');
  } else if (row.check === 'cls'){
    if (row.unit !== 'score' || row.threshold !== THRESHOLDS.cls || !numberPassesAtMost(THRESHOLDS.cls)) refuse('cls pass relation differs');
  } else if (row.check === 'feedback'){
    if (row.unit !== 'ms' || row.threshold !== THRESHOLDS.feedback_ms || !numberPassesAtMost(THRESHOLDS.feedback_ms)) refuse('feedback measurement relation differs');
  } else if (row.check === 'horizontal_overflow'){
    if (row.unit !== 'px' || row.threshold !== THRESHOLDS.horizontal_overflow_px || !numberPassesAtMost(THRESHOLDS.horizontal_overflow_px)) refuse('horizontal overflow relation differs');
  } else if (row.check === 'text_ranges_contained'){
    if (!row.unit.startsWith('ranges outside of ') || row.threshold !== 0 || !numberPassesAtMost(0)) refuse('text range relation differs');
  } else if (row.check === 'action_targets'){
    if (!row.unit.startsWith('px, smallest side of ') || row.threshold !== THRESHOLDS.action_target_px || !numberPassesAtLeast(THRESHOLDS.action_target_px)) refuse('action target relation differs');
  } else if (row.check === 'contrast_body_text' || row.check === 'contrast_primary_action'){
    if (!row.unit.startsWith('ratio') || row.threshold !== THRESHOLDS.contrast_ratio || !numberPassesAtLeast(THRESHOLDS.contrast_ratio)) refuse(`${row.check} relation differs`);
  } else if (row.check === 'reduced_motion'){
    if (row.unit !== 'moving elements' || row.threshold !== THRESHOLDS.reduced_motion_moving_elements || !numberPassesAtMost(THRESHOLDS.reduced_motion_moving_elements)) refuse('reduced motion relation differs');
  } else if (row.check === 'key_panel_reachable'){
    if (row.unit !== 'panels reachable' || !Number.isFinite(row.threshold) || row.threshold <= 0 || row.pass !== (row.value === row.threshold)) refuse('key panel relation differs');
  } else if (row.check === 'focus_reaches_primary'){
    if (row.unit !== 'tab presses' || typeof row.threshold !== 'string' || !row.threshold.startsWith('reached with a visible ring, expected at press ') || (row.value === null && row.pass)) refuse('focus relation differs');
  }
}

function refuse(message){
  throw new Error(`certification proof: ${message}`);
}

export function validateCertification(document, {scorerIdentity = null} = {}){
  if (!document || typeof document !== 'object') refuse('not an object');
  if (document.contract_version !== CONTRACT_VERSION) refuse('contract version differs');
  if (typeof document.candidate !== 'string' || !document.candidate) refuse('candidate missing');
  const binding = document.candidate_binding;
  if (!binding || typeof binding !== 'object') refuse('candidate binding missing');
  const expected = [...BINDING_FIELDS, 'reason'].sort().join();
  if (Object.keys(binding).sort().join() !== expected) refuse('candidate binding fields differ from the contract');
  if (document.candidate === UNBOUND){
    for (const field of BINDING_FIELDS) if (binding[field] !== null) refuse(`unbound candidate carries ${field}`);
    if (binding.reason !== UNBOUND_REASON) refuse('unbound candidate reason differs');
  } else {
    for (const field of BINDING_FIELDS) if (binding[field] === null || binding[field] === undefined || binding[field] === '') refuse(`bound candidate lacks ${field}`);
    if (binding.reason !== null) refuse('bound candidate carries a pending reason');
  }
  if (document.captured_at !== null && typeof document.captured_at !== 'string') refuse('captured_at is neither null nor a string');
  const thresholds = document.thresholds;
  if (!thresholds || Object.keys(thresholds).sort().join() !== Object.keys(THRESHOLDS).sort().join()) refuse('thresholds differ from the frozen set');
  for (const [key, value] of Object.entries(THRESHOLDS)) if (thresholds[key] !== value) refuse(`threshold ${key} is not the frozen value`);
  const load = document.workload;
  if (!load || typeof load !== 'object') refuse('workload missing');
  if (JSON.stringify(load) !== JSON.stringify(workload())) refuse('workload differs from the frozen matrix');
  if (!Array.isArray(document.suites_run) || document.suites_run.join() !== [...SUITE_FILES].sort().join()) refuse('suites run differs from the frozen set');
  for (const key of ['capabilities', 'pressure_routes', 'themes', 'viewports', 'flows', 'cache_states']){
    if (!Array.isArray(load[key]) || load[key].length === 0) refuse(`workload ${key} missing`);
  }
  if (load.themes.join() !== THEMES.join()) refuse('workload themes differ');
  if (load.viewports.join() !== VIEWPORTS.map(viewportName).join()) refuse('workload viewports differ');
  if (load.cache_states.join() !== CACHE_STATES.join()) refuse('workload cache states differ');
  if (!Array.isArray(document.measurements)) refuse('measurements missing');
  const keys = new Set();
  const coverage = new Set();
  for (const row of document.measurements){
    if (!SUITE_FILES.has(row.suite)) refuse(`measurement suite ${row.suite} unknown`);
    if (!CHECKS.has(row.check)) refuse(`measurement check ${row.check} unknown`);
    if (typeof row.route !== 'string' || !row.route.startsWith('#/')) refuse('measurement route missing');
    if (typeof row.capability_id !== 'string' || !row.capability_id) refuse('measurement capability missing');
    if (!THEME_SET.has(row.theme)) refuse(`measurement theme ${row.theme} unknown`);
    if (!VIEWPORT_NAMES.has(row.viewport)) refuse(`measurement viewport ${row.viewport} unknown`);
    if (!(typeof row.value === 'number' || row.value === null)) refuse('measurement value is not a number or null');
    if (typeof row.unit !== 'string') refuse('measurement unit missing');
    if (typeof row.pass !== 'boolean') refuse('measurement pass missing');
    if (row.threshold === undefined) refuse('measurement threshold missing');
    if (row.check === 'lcp' || row.check === 'cls'){
      if (!CACHE_STATES.includes(row.cache_state)) refuse(`${row.check} row lacks a cache state`);
      if (row.throttling !== 'none') refuse(`${row.check} row lacks its throttling statement`);
    }
    if (row.value === null && row.pass) refuse(`${row.check} passed without a value`);
    validateMeasurement(row);
    const key = rowKey(row);
    if (keys.has(key)) refuse(`duplicate measurement ${key.split('\u0001').join(' ')}`);
    keys.add(key);
    coverage.add(coverageKey(row));
  }
  if (!sameKeys(coverage, expectedCoverage())) refuse('measurement coverage differs from the frozen matrix');
  const expectedSummary = summaryOf(document.measurements);
  if (JSON.stringify(document.summary) !== JSON.stringify(expectedSummary)) refuse('summary differs from measurements');
  if (!Array.isArray(document.model_answer_latency)) refuse('model answer latency missing');
  const answerCoverage = new Set();
  for (const row of document.model_answer_latency){
    if (row.kind !== FIXTURE_ANSWER) refuse('a model answer row is not marked as a fixture');
    if (row.counts_as_fast_useful_answer !== false) refuse('a fixture answer counted as a fast useful answer');
    if (!ANSWER_STATES.includes(row.answer_state)) refuse(`answer state ${row.answer_state} unknown`);
    if ((row.answer_state === 'held' || row.answer_state === 'empty') && row.latency_ms !== null) refuse('a held or empty answer carries a latency');
    if (row.deadline_ms !== null && typeof row.deadline_ms !== 'number') refuse('deadline is neither null nor a number');
    if (row.suite !== undefined && row.suite !== SUITES.pressure) refuse('model answer suite differs');
    if (row.route !== '#/console?work=ask' || !THEME_SET.has(row.theme) || !VIEWPORT_NAMES.has(row.viewport)) refuse('model answer coverage row differs');
    const key = [SUITES.pressure, row.route, row.theme, row.viewport].join('\u0001');
    if (answerCoverage.has(key)) refuse('duplicate model answer coverage');
    answerCoverage.add(key);
  }
  if (!sameKeys(answerCoverage, expectedAnswerCoverage())) refuse('model answer coverage differs from the frozen matrix');
  if (!Array.isArray(document.retained_screenshots)) refuse('retained screenshots missing');
  for (const shot of document.retained_screenshots){
    if (typeof shot.file !== 'string' || typeof shot.test !== 'string' || !SUITE_FILES.has(shot.suite)) refuse('retained screenshot row incomplete');
  }
  const floor = document.visual_floor;
  if (!floor || !Array.isArray(floor.principles) || floor.principles.length !== PRINCIPLES.length) refuse('visual floor principles missing');
  floor.principles.forEach((principle, index) => {
    if (principle.number !== index + 1 || principle.name !== PRINCIPLES[index]) refuse(`principle ${index + 1} is not ${PRINCIPLES[index]}`);
    if (!(principle.score === null || (Number.isInteger(principle.score) && principle.score >= 0 && principle.score <= 3))) refuse(`principle ${index + 1} score out of range`);
  });
  if (floor.floor_total !== FLOOR_TOTAL || floor.floor_principle_minimum !== FLOOR_PRINCIPLE_MINIMUM) refuse('visual floor thresholds differ');
  const scored = floor.principles.some((principle) => principle.score !== null) || floor.total !== null || floor.scored === true;
  if (scored){
    if (!scorerIdentity || typeof scorerIdentity !== 'object' || typeof scorerIdentity.identity !== 'string' || !scorerIdentity.identity.trim()){
      refuse('scored visual floor without a scorer identity in the private file');
    }
    if (floor.principles.some((principle) => principle.score === null)) refuse('scored visual floor leaves a principle unscored');
    const total = floor.principles.reduce((sum, principle) => sum + principle.score, 0);
    if (floor.total !== total) refuse('visual floor total is not the sum of its principles');
    if (floor.scored !== true || floor.scorer_identity_present !== true) refuse('scored visual floor is not marked scored with its identity present');
    if (JSON.stringify(floor).includes(scorerIdentity.identity)) refuse('scorer identity copied into the proof output');
  } else if (floor.scored !== false || floor.scorer_identity_present !== false){
    refuse('unscored visual floor is not marked unscored');
  }
  return true;
}
