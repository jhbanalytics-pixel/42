/* What a tester reads when a workspace cannot answer. The sentence says what
   happened in plain words and the engine's reason code stays one press away
   under Details, so support can still read it without it being the first
   thing a person sees. Retry buttons use the same words as every other
   failed read in the product. */
import {expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {EvidenceRoomView} from '../../evidenceRoom.jsx';
import {HistoricalWorkspaceView} from '../../historicalWorkspace.jsx';
import {runDisplayLabel} from '../../explore.jsx';

/* The code keeps the label that says whose code it is (recorded by the
   producer, or derived by the desk); both sit inside the closed Details. */
const CODE_IN_DETAILS = /<details[^>]*><summary>Details<\/summary><p>[^<]+ <code>historical_backend_gate_open<\/code><\/p><\/details>/;

test('Historical keeps its reason code under Details, not as bare text', () => {
  const markup = renderToStaticMarkup(
    <HistoricalWorkspaceView state="error" mode="analogue" error={{code: 'historical_backend_gate_open'}} onRetry={() => {}} />,
  );
  expect(markup).toContain('Historical evidence is unavailable for this investigation.');
  expect(markup).toMatch(CODE_IN_DETAILS);
  expect(markup).toContain('>Try again</button>');
  expect(markup).not.toContain('>Retry</button>');
});

test('Evidence Room keeps its reason code under Details on a failed read', () => {
  const markup = renderToStaticMarkup(
    <EvidenceRoomView state="error" error={{code: 'workspace_unavailable'}} onRetry={() => {}} />,
  );
  expect(markup).toContain('The investigation is unavailable.');
  expect(markup).toMatch(/<details[^>]*><summary>Details<\/summary><p>[^<]+ <code>workspace_unavailable<\/code><\/p><\/details>/);
  expect(markup).toContain('>Try again</button>');
  expect(markup).not.toContain('>Retry</button>');
});

test('a run is named by its date, and the run id is kept for support', () => {
  expect(runDisplayLabel('Run run_20260912_dynamic_apply_v2_r1')).toBe('Run of 12 Sept 2026');
  expect(runDisplayLabel('Run gate-c-run')).toBe('Run gate-c-run');
  expect(runDisplayLabel('Run run_20261305_apply')).toBe('Run run_20261305_apply');
  expect(runDisplayLabel('Run run_20260231_apply')).toBe('Run run_20260231_apply');
  expect(runDisplayLabel('Run sig_ab20250101cd')).toBe('Run sig_ab20250101cd');
  expect(runDisplayLabel('Observation method')).toBe('Observation method');
});
