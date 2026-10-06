/* Writes the frontend certification proof output from the two certification
   suites. Every measurement arrives as an annotation on its test, so the
   document is a pure function of the run result: rows are keyed and sorted,
   a suite that ran replaces its own rows in the previous document, a suite
   that did not run keeps them, and the candidate binding stays unbound until
   the native run supplies it. A run that carries no certification test
   leaves the file alone. */
import {basename} from 'node:path';
import {SUITE_FILES, buildProofDocument, proofPath, readProofDocument, writeProofDocument} from './certification.mjs';

export default class CertificationReporter {
  constructor(){
    this.rows = [];
    this.modelAnswers = [];
    this.screenshots = [];
    this.results = [];
    this.suites = new Set();
  }

  onTestEnd(test, result){
    const file = basename(test.location.file);
    if (!SUITE_FILES.has(file)) return;
    this.suites.add(file);
    this.results.push({suite: file, title: test.title, status: result.status, expected: test.expectedStatus, duration_ms: result.duration});
    const seen = new Set();
    const annotations = [...(result.annotations || []), ...(test.annotations || [])];
    for (const annotation of annotations){
      if (!annotation.description) continue;
      const stamp = `${annotation.type}${annotation.description}`;
      if (seen.has(stamp)) continue;
      seen.add(stamp);
      if (annotation.type === 'certification') this.rows.push({...JSON.parse(annotation.description), suite: file});
      if (annotation.type === 'certification-model-answer') this.modelAnswers.push({...JSON.parse(annotation.description), suite: file});
      if (annotation.type === 'certification-screenshot') this.screenshots.push({suite: file, test: test.title, file: annotation.description});
    }
  }

  onEnd(){
    if (this.suites.size === 0) return;
    const document = buildProofDocument({
      rows: this.rows,
      modelAnswers: this.modelAnswers,
      screenshots: this.screenshots,
      results: this.results,
      suites: this.suites,
      previous: readProofDocument(),
      capturedAt: new Date().toISOString(),
    });
    const path = writeProofDocument(document, proofPath());
    const failed = document.measurements.filter((row) => !row.pass).length;
    console.log(`certification proof: ${path}`);
    console.log(`certification measurements: ${document.measurements.length} in this document, ${this.rows.length} from this run, ${failed} failing, ${this.screenshots.length} screenshots retained`);
  }
}
