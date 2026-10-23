import {expect, test} from 'bun:test';
import {isAbsolute, join, resolve} from 'node:path';
import config, {configureCertificationEvidenceDirectory} from '../../../playwright.config.mjs';

/* resolve() yields a path that is absolute on the running platform, so these
   fixtures stay absolute on Linux and on Windows alike. */
const temporaryDirectory = resolve('/temp');
const operatorRun = resolve('/evidence/operator-run');

test('Playwright creates a fresh certification evidence directory unless an absolute directory is supplied', () => {
  expect(isAbsolute(temporaryDirectory)).toBe(true);
  expect(isAbsolute(operatorRun)).toBe(true);
  expect(isAbsolute('relative')).toBe(false);

  let sequence = 0;
  const create = () => join(temporaryDirectory, `certification-${++sequence}`);
  const first = {};
  const second = {};

  expect(configureCertificationEvidenceDirectory(first, create, temporaryDirectory)).toBe(join(temporaryDirectory, 'certification-1'));
  expect(configureCertificationEvidenceDirectory(second, create, temporaryDirectory)).toBe(join(temporaryDirectory, 'certification-2'));
  expect(first.CERTIFICATION_EVIDENCE_DIR).not.toBe(second.CERTIFICATION_EVIDENCE_DIR);
  expect(configureCertificationEvidenceDirectory({CERTIFICATION_EVIDENCE_DIR: operatorRun}, create, temporaryDirectory)).toBe(operatorRun);
  expect(() => configureCertificationEvidenceDirectory({CERTIFICATION_EVIDENCE_DIR: 'relative'}, create, temporaryDirectory)).toThrow(/absolute/);
});

/* Audit W28-02: a spec with its own config pins that config's origin, so the
   aggregate run at 4176 leaves it out instead of failing every case. */
test('the aggregate run leaves out every spec that has a config of its own', () => {
  expect(config.use.baseURL).toBe('http://127.0.0.1:4176');
  for (const spec of ['functional-history-communities.pw.mjs', 'evening-controls.pw.mjs', 'today-held-details.pw.mjs', '42-full-journey.pw.mjs']){
    expect(config.testIgnore).toContain(spec);
  }
  expect(config.testIgnore).not.toContain('seed-reads.pw.mjs');
});
