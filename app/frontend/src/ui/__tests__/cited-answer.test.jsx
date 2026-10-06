import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {CitedAnswer} from '../CitedAnswer.jsx';

/* The cited answer renders the frozen contract, not a shape invented here, so
   the fixture the engine froze is the input. If the contract drifts this test
   reads the drift rather than a mock that agreed with itself. */

const fixture = JSON.parse(
  readFileSync(
    new URL('../../../../tests/fixtures/open_intelligence/v2/golden_11_election_brand_role.json', import.meta.url),
    'utf8',
  ),
).payload;

const render = (over = {}) => renderToStaticMarkup(
  <CitedAnswer
    answer={fixture.answer}
    plan={fixture.evidence_plan}
    evidence={fixture.evidence}
    lenses={fixture.audience_lenses}
    limitations={fixture.limitations}
    contradictions={fixture.contradictions}
    missingWork={fixture.missing_work}
    readiness={fixture.evidence_state}
    {...over}
  />,
);

describe('Cited answer', () => {
  test('the evidence plan is shown before the answer, not behind it', () => {
    const markup = render();
    expect(markup.indexOf('Evidence plan')).toBeLessThan(markup.indexOf('cited-answer__section'));
    for (const label of ['Intent', 'Decision', 'Markets', 'Window', 'Source families', 'Evidence requirements', 'Output form']){
      expect(markup).toContain(label);
    }
  });

  test('every citation mark in the prose resolves to a listed receipt', () => {
    const markup = render();
    const marks = [...markup.matchAll(/class="cited-answer__mark">(\[[^<]+\])</g)].map((m) => m[1]);
    expect(marks.length).toBeGreaterThan(0);
    const receipts = new Set(fixture.evidence.map((item, index) => item.citation_label || `[E${index + 1}]`));
    for (const mark of marks) expect(receipts.has(mark)).toBe(true);
  });

  test('an inferred lens is labelled inferred and carries its source, window and confidence', () => {
    const markup = render();
    expect(markup).toContain('data-basis="inferred"');
    expect(markup).toContain('Inferred');
    /* Confidence is a word. A number here was an engine score reaching the
       reader through the evidence component's front door. */
    expect(markup).toContain('confidence unmeasured');
    expect(markup).not.toMatch(/confidence \d/);
    expect(markup).not.toContain('data-basis="measured"');
  });

  test('what the answer does not establish is rendered, never dropped', () => {
    const markup = render();
    expect(markup).toContain('What this answer does not establish');
    expect(markup).toContain('not representative polling');
  });

  test('an answer with no receipts says nothing in it can be checked', () => {
    const markup = render({evidence: []});
    expect(markup).toContain('nothing in it can be checked');
  });

  test('an absent answer is stated as absent rather than rendered empty', () => {
    const markup = renderToStaticMarkup(<CitedAnswer answer={null} />);
    expect(markup).toContain('data-answer-state="absent"');
    expect(markup).toContain('No answer has been produced');
  });

  test('no engine vocabulary reaches the reader through the answer', () => {
    const markup = render();
    for (const leaked of ['trends_v2', 'signal_candidates', 'graph_score', 'bigquery']){
      expect(markup.toLowerCase()).not.toContain(leaked);
    }
  });
});
