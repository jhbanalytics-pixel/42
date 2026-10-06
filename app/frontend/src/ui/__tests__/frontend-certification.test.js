import {describe, expect, test} from 'bun:test';
import {existsSync, mkdtempSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {
  BINDING_FIELDS, CACHE_STATES, FIXTURE_ANSWER, PRESSURE_ROUTES, PRINCIPLES, THRESHOLDS, THEMES, UNBOUND, UNBOUND_REASON, VIEWPORTS,
  buildProofDocument, evidenceDirectory, implementedCapabilities, proofPath, readProofDocument, rowKey,
  readScorerIdentity, scorerIdentityPath, validateCertification,
} from '../../../tests/browser/support/certification.mjs';

const testEvidenceDirectory = mkdtempSync(join(tmpdir(), 'frontend-certification-test-'));
process.env.CERTIFICATION_EVIDENCE_DIR = testEvidenceDirectory;

/* The proof output the certification reporter writes is validated against
   its contract: the frozen thresholds, the workload, a candidate binding that
   stays null with its pending reason until the native run, one row per
   measurement key, fixture answers that never count as fast useful answers,
   and a visual floor that can only carry a score when the scorer identity is
   present in its private file. The validator is exercised on mutated copies
   so a document that drifts is refused, and the emitted file, when the
   browser suites have written it beside this checkout, is validated too. */

function row(over = {}){
  return {
    suite: '42-layout-a11y.pw.mjs', check: 'horizontal_overflow', route: '#/pulse', capability_id: 'today_briefing',
    theme: 'daylight', viewport: '390x844', value: 0, unit: 'px', threshold: 0, pass: true, ...over,
  };
}

function vitals(over = {}){
  return {
    suite: '42-request-pressure.pw.mjs', check: 'lcp', route: '#/compare', capability_id: 'compare', theme: 'midnight',
    viewport: '1440x1000', cache_state: 'cold', throttling: 'none', value: 640.5, unit: 'ms', threshold: 2500, pass: true, ...over,
  };
}

function answer(over = {}){
  return {
    suite: '42-request-pressure.pw.mjs', route: '#/console?work=ask', theme: 'daylight', viewport: '768x1024', kind: FIXTURE_ANSWER,
    answer_state: 'held', latency_ms: null, waited_ms: 1500, answer_rendered: false, deadline_ms: null,
    deadline_note: 'bound at the native run', counts_as_fast_useful_answer: false, ...over,
  };
}

function sample(){
  const rows = [];
  for (const capability of implementedCapabilities()){
    for (const theme of THEMES){
      for (const viewport of VIEWPORTS){
        const base = {suite: '42-layout-a11y.pw.mjs', capability_id: capability.capability_id, route: capability.module.route, theme, viewport: `${viewport.width}x${viewport.height}`};
        rows.push(
          row({...base}),
          row({...base, check: 'key_panel_reachable', value: 2, unit: 'panels reachable', threshold: 2}),
          row({...base, check: 'text_ranges_contained', value: 0, unit: 'ranges outside of 8 measured', threshold: 0}),
          row({...base, check: 'focus_reaches_primary', subject: 'primary action', value: 1, unit: 'tab presses', threshold: 'reached with a visible ring, expected at press 1'}),
          row({...base, check: 'contrast_body_text', value: 5, unit: 'ratio, minimum of 8 runs', threshold: 4.5}),
          row({...base, check: 'contrast_primary_action', value: 5, unit: 'ratio', threshold: 4.5}),
        );
        if (viewport.width === 390) rows.push(row({...base, check: 'action_targets', value: 48, unit: 'px, smallest side of 4 controls', threshold: 44}));
      }
      rows.push(row({suite: '42-layout-a11y.pw.mjs', capability_id: capability.capability_id, route: capability.module.route, theme, viewport: '1024x768', check: 'reduced_motion', value: 0, unit: 'moving elements', threshold: 0}));
    }
  }
  for (const route of PRESSURE_ROUTES){
    for (const theme of THEMES){
      for (const viewport of VIEWPORTS){
        const base = {suite: '42-request-pressure.pw.mjs', capability_id: route === '#/compare' ? 'compare' : 'today_briefing', route, theme, viewport: `${viewport.width}x${viewport.height}`, throttling: 'none'};
        for (const cache_state of CACHE_STATES){
          rows.push(vitals({...base, cache_state}));
          rows.push(vitals({...base, check: 'cls', cache_state, value: 0.01, unit: 'score', threshold: 0.1}));
        }
        rows.push(vitals({...base, check: 'feedback', cache_state: 'warm', value: 10, unit: 'ms', threshold: 100}));
      }
    }
  }
  return buildProofDocument({
    rows,
    modelAnswers: THEMES.flatMap((theme) => VIEWPORTS.map((viewport) => answer({theme, viewport: `${viewport.width}x${viewport.height}`}))),
    screenshots: [{suite: '42-layout-a11y.pw.mjs', test: 'layout: compare populated daylight 390x844', file: 'test-results/browser/failure.png'}],
    results: [{suite: '42-layout-a11y.pw.mjs', title: 'layout: compare populated daylight 390x844', status: 'passed', expected: 'passed', duration_ms: 1200}],
    suites: new Set(['42-layout-a11y.pw.mjs', '42-request-pressure.pw.mjs']),
    capturedAt: '2026-09-14T00:00:00.000Z',
  });
}

function scored(document, identityInDocument = false){
  const copy = JSON.parse(JSON.stringify(document));
  copy.visual_floor.principles = copy.visual_floor.principles.map((principle) => ({...principle, score: 3}));
  copy.visual_floor.total = 30;
  copy.visual_floor.scored = true;
  copy.visual_floor.scorer_identity_present = true;
  if (identityInDocument) copy.visual_floor.note = 'scored by reviewer-one';
  return copy;
}

describe('the frontend certification proof output', () => {
  test('a document built by the reporter validates and carries the unbound binding', () => {
    const document = sample();
    expect(validateCertification(document)).toBe(true);
    expect(document.candidate).toBe(UNBOUND);
    for (const field of BINDING_FIELDS) expect(document.candidate_binding[field]).toBeNull();
    expect(document.candidate_binding.reason).toBe(UNBOUND_REASON);
    expect(document.thresholds).toEqual(THRESHOLDS);
    expect(document.workload.throttling).toBe('none');
    expect(document.measurements.length).toBeGreaterThan(100);
    expect(document.visual_floor.principles.map((principle) => principle.name)).toEqual([...PRINCIPLES]);
    expect(document.visual_floor.total).toBeNull();
    expect(document.summary.lcp).toEqual({measured: 64, passed: 64, failed: 0});
  });

  test('evidence output requires an absolute configured directory', () => {
    const configured = process.env.CERTIFICATION_EVIDENCE_DIR;
    delete process.env.CERTIFICATION_EVIDENCE_DIR;
    expect(() => evidenceDirectory()).toThrow(/evidence directory/);
    process.env.CERTIFICATION_EVIDENCE_DIR = configured;
    expect(evidenceDirectory()).toBe(testEvidenceDirectory);
  });

  test('rows are sorted by key and a rerun case replaces only its own rows', () => {
    const previous = sample();
    const replacement = previous.measurements
      .filter((entry) => entry.suite === '42-layout-a11y.pw.mjs' && entry.route === '#/pulse' && entry.theme === 'daylight' && entry.viewport === '390x844')
      .map((entry) => ({...entry}));
    const replaced = replacement.find((entry) => entry.check === 'horizontal_overflow');
    replaced.value = 3;
    replaced.pass = false;
    const rerun = buildProofDocument({
      rows: replacement,
      suites: new Set(['42-layout-a11y.pw.mjs']),
      previous,
      capturedAt: '2026-09-14T01:00:00.000Z',
    });
    const layout = rerun.measurements.filter((entry) => entry.suite === '42-layout-a11y.pw.mjs');
    expect(layout.length).toBeGreaterThan(2);
    expect(layout.some((entry) => entry.value === 3 && entry.pass === false)).toBe(true);
    expect(layout.some((entry) => entry.theme === 'midnight' && entry.check === 'horizontal_overflow' && entry.value === 0)).toBe(true);
    expect(rerun.suites_run).toEqual(['42-layout-a11y.pw.mjs', '42-request-pressure.pw.mjs']);
    expect(rerun.measurements.filter((entry) => entry.suite === '42-request-pressure.pw.mjs').length).toBeGreaterThan(2);
    expect(rerun.model_answer_latency).toHaveLength(8);
    const keys = rerun.measurements.map(rowKey);
    expect([...keys].sort((a, b) => a.localeCompare(b))).toEqual(keys);
    expect(validateCertification(rerun)).toBe(true);
  });

  test('a scored visual floor is refused without the scorer identity and accepted with it', () => {
    expect(() => validateCertification(scored(sample()))).toThrow(/scorer identity/);
    expect(() => validateCertification(scored(sample()), {scorerIdentity: {identity: '   '}})).toThrow(/scorer identity/);
    expect(validateCertification(scored(sample()), {scorerIdentity: {identity: 'reviewer-one'}})).toBe(true);
    expect(() => validateCertification(scored(sample(), true), {scorerIdentity: {identity: 'reviewer-one'}})).toThrow(/copied into the proof output/);
    const partial = scored(sample());
    partial.visual_floor.principles[4].score = null;
    expect(() => validateCertification(partial, {scorerIdentity: {identity: 'reviewer-one'}})).toThrow(/unscored/);
    const wrongTotal = scored(sample());
    wrongTotal.visual_floor.total = 29;
    expect(() => validateCertification(wrongTotal, {scorerIdentity: {identity: 'reviewer-one'}})).toThrow(/sum of its principles/);
  });

  test('a fixture answer can never count as a fast useful answer', () => {
    const counted = sample();
    counted.model_answer_latency[0].counts_as_fast_useful_answer = true;
    expect(() => validateCertification(counted)).toThrow(/fast useful answer/);
    const timed = sample();
    timed.model_answer_latency[0].latency_ms = 12;
    expect(() => validateCertification(timed)).toThrow(/held or empty answer carries a latency/);
    const real = sample();
    real.model_answer_latency[0].kind = 'model answer';
    expect(() => validateCertification(real)).toThrow(/not marked as a fixture/);
  });

  test('a drifted binding, threshold, workload or measurement is refused', () => {
    const bound = sample();
    bound.candidate_binding.repo_commit = '9c24ca4';
    expect(() => validateCertification(bound)).toThrow(/unbound candidate carries repo_commit/);
    const missing = sample();
    delete missing.candidate_binding.image_digest;
    expect(() => validateCertification(missing)).toThrow(/binding fields differ/);
    const halfBound = sample();
    halfBound.candidate = 'staging-00120';
    halfBound.candidate_binding.repo_commit = '9c24ca4';
    expect(() => validateCertification(halfBound)).toThrow(/bound candidate lacks image_digest/);
    const loose = sample();
    loose.thresholds.lcp_ms = 4000;
    expect(() => validateCertification(loose)).toThrow(/not the frozen value/);
    const throttled = sample();
    throttled.workload.throttling = 'slow 3G';
    expect(() => validateCertification(throttled)).toThrow(/workload/);
    const inventedSuite = sample();
    inventedSuite.suites_run.push('invented-suite.pw.mjs');
    expect(() => validateCertification(inventedSuite)).toThrow(/suites run/);
    const unpassed = sample();
    delete unpassed.measurements[0].pass;
    expect(() => validateCertification(unpassed)).toThrow(/pass missing/);
    const noCache = sample();
    delete noCache.measurements.find((entry) => entry.check === 'lcp').cache_state;
    expect(() => validateCertification(noCache)).toThrow(/cache state/);
    const passedWithoutValue = sample();
    passedWithoutValue.measurements[0].value = null;
    expect(() => validateCertification(passedWithoutValue)).toThrow(/passed without a value/);
    const duplicate = sample();
    duplicate.measurements.push({...duplicate.measurements[0]});
    expect(() => validateCertification(duplicate)).toThrow(/duplicate measurement/);
  });

  test('a partial or forged measurement proof is refused', () => {
    const partial = sample();
    partial.measurements = [];
    expect(() => validateCertification(partial)).toThrow(/coverage/);

    const forged = sample();
    const cls = forged.measurements.find((entry) => entry.check === 'cls' && entry.route === '#/compare' && entry.theme === 'midnight' && entry.viewport === '1440x1000' && entry.cache_state === 'cold');
    cls.value = 0.2;
    cls.pass = false;
    forged.summary.cls = {measured: 64, passed: 63, failed: 1};
    expect(validateCertification(forged)).toBe(true);
    cls.pass = true;
    expect(() => validateCertification(forged)).toThrow(/cls pass/);

    const summary = sample();
    summary.summary.lcp.passed = 1;
    expect(() => validateCertification(summary)).toThrow(/summary/);
  });

  test('the emitted proof output beside this checkout, when the browser suites have written it, validates', () => {
    const path = proofPath();
    const emitted = readProofDocument(path);
    if (!existsSync(path)){
      expect(emitted).toBeNull();
      return;
    }
    expect(emitted).not.toBeNull();
    const expectedSuites = ['42-layout-a11y.pw.mjs', '42-request-pressure.pw.mjs'];
    if (emitted.suites_run.join() === expectedSuites.join()) expect(validateCertification(emitted, {scorerIdentity: readScorerIdentity(scorerIdentityPath())})).toBe(true);
    else expect(() => validateCertification(emitted, {scorerIdentity: readScorerIdentity(scorerIdentityPath())})).toThrow(/suites run/);
    expect(emitted.measurements.length).toBeGreaterThan(0);
    console.log(`validated ${emitted.measurements.length} measurements in ${path}`);
  });
});
