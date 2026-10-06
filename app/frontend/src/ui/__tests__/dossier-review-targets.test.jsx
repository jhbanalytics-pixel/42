/* The dossier review controls are the app's 48px targets. Buttons take the
   shared action style with one primary per step; the verdict select, the
   note, the receipt and selection rows are 48px tall with 24px checkboxes;
   the approved export fills the page width and the exported text wraps.
   It wraps with break-word, not anywhere: the width law in
   round-10-consumer.test.jsx forbids a rule that breaks words anywhere. */
import {expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {DossierReview} from '../DossierReview.jsx';

const V1 = '1'.repeat(64);
const dossier = {
  contract_version: 'dossier_working_v1', investigation_id: 'inv_alpha', dossier_version: V1, state: 'ready',
  projection: {investigation_id: 'inv_alpha', cutoff: '2026-09-01', concise_answer: 'A', decision: 'pending',
    claims: [{claim_id: 'clm_1', kind: 'observation', text: 'One.', status: 'pending', selected: true, evidence_state: 'unchecked', citations: ['rcp_1']}],
    evidence: [], relationships: [{relationship_id: 'rel_1', parent_claim_id: 'clm_1', claim_id: 'clm_1', status: 'pending'}], unanswered_questions: []},
  resource_versions: {claim: {clm_1: 'c'.repeat(64)}, relationship: {rel_1: 'd'.repeat(64)}}, artifacts: [],
};

const button = (markup, name) => {
  const found = [...markup.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)].find((match) => match[2] === name);
  expect(found, `${name} is rendered`).toBeTruthy();
  return found[1];
};

test('every dossier review button is the shared 48px action, with the step action primary', () => {
  const markup = renderToStaticMarkup(<DossierReview investigationId="inv_alpha" resource={{status: 'ready', dossier}} reviewSession={{csrfToken: 't', role: 'dossier_editor'}} />);
  for (const name of ['Save as a new version', 'Approve claim', 'Approve link', 'Prepare internal artifact']){
    expect(button(markup, name)).toContain('class="legacy-action legacy-action--primary"');
  }
  for (const name of ['Reject claim', 'Reject link']) expect(button(markup, name)).toContain('class="legacy-action"');
  expect([...markup.matchAll(/<button([^>]*)>/g)].every((match) => /class="legacy-action/.test(match[1]))).toBe(true);
});

test('an artifact row has one primary: Inspect until the text is inspected, Approve only after', () => {
  const withArtifact = {...dossier, artifacts: [{artifact_id: 'art_1', artifact_version: 'e'.repeat(64), dossier_version: V1, state: 'pending_review'}]};
  const markup = renderToStaticMarkup(<DossierReview investigationId="inv_alpha" resource={{status: 'ready', dossier: withArtifact}} reviewSession={{csrfToken: 't', role: 'client_read_approver', roles: ['client_read_approver']}} />);
  expect(button(markup, 'Inspect exported text')).toContain('class="legacy-action legacy-action--primary"');
  expect(button(markup, 'Approve this exact version')).toContain('class="legacy-action"');
});

test('the review fields and the export meet the target and layout rules', () => {
  const css = readFileSync(fileURLToPath(new URL('../../styles/workspaces.css', import.meta.url)), 'utf8');
  expect(css).toMatch(/\.dossier-review__receipt, \.dossier-review__select \{[^}]*display: flex;[^}]*min-height: 48px;/);
  expect(css).toMatch(/\.dossier-review__receipt:focus-within, \.dossier-review__select:focus-within \{/);
  expect(css).not.toMatch(/\.dossier-review__(?:receipt|select):focus-visible/);
  expect(css).toMatch(/\.dossier-review input\[type="checkbox"\] \{[^}]*width: 24px;[^}]*height: 24px;/);
  expect(css).toMatch(/\.dossier-review select, \.dossier-review textarea \{[^}]*min-height: 48px;/);
  expect(css).toMatch(/\.dossier-client-read__frame \{[^}]*display: block;[^}]*width: 100%;[^}]*min-height: 70vh;[^}]*border: 1px solid var\(--line\);/);
  expect(css).toMatch(/\.dossier-review__export \{[^}]*white-space: pre-wrap;[^}]*overflow-wrap: break-word;/);
});
