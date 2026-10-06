import {expect, test} from 'bun:test';
import {isAbsolute, join, resolve} from 'node:path';
import {configureCertificationEvidenceDirectory} from '../../../playwright.config.mjs';

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
