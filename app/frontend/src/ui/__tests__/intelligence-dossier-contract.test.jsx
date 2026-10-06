/* Intelligence Console and Evidence Room: the truth boundary.

   Two rules carry almost all the risk here. Only approved, ready, selected and
   cited material may reach a client-facing read. And nothing excluded may be
   deleted or rewritten: a cleaner artifact must mean a narrower approved
   projection, never hidden uncertainty. Every test below defends one of those. */

import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {IntelligenceConsole} from 'ogilvy-intelligence-design-system';

import {
  CLIENT_READ_SECTIONS,
  computeArtifactReadiness,
  projectClientRead,
} from '../../intelligenceDossierContract.js';
import * as dossierPresentation from '../IntelligenceDossier.jsx';

const {IntelligenceDossier, readableGates} = dossierPresentation;

const claim = (overrides = {}) => ({
  claim_id: 'clm_1',
  kind: 'observation',
  text: 'Repair tutorials recur across two independent source families.',
  status: 'approved',
  evidence_state: 'ready',
  selected: true,
  citations: ['ev_1'],
  support_evidence_ids: ['ev_1'],
  challenge_evidence_ids: [],
  ...overrides,
});

const evidence = (overrides = {}) => ({
  evidence_id: 'ev_1',
  client_citable: true,
  in_scope: true,
  published_at: '2026-08-20T00:00:00Z',
  ...overrides,
});

const dossier = (overrides = {}) => ({
  investigation_id: 'inv_1',
  cutoff: '2026-08-28',
  concise_answer: 'Repair is becoming a weekend social activity, not a chore.',
  decision: 'approved',
  claims: [claim()],
  evidence: [evidence()],
  relationships: [{relationship_id: 'rel_1', kind: 'support', status: 'approved'}],
  precedents: [],
  unanswered_questions: [],
  ...overrides,
});

const richAuthorityDossier = (overrides = {}) => dossier({
  decision_question: 'What response should leadership approve?',
  market_scope: ['za'],
  observation_window: {start: '2026-08-20', end: '2026-08-20',
    method: 'closed dossier evidence window', closed: true, checked_at: '2026-08-21T00:00:00Z'},
  evidence_authority: {status: 'validated', authority_id: 'host-dossier-evidence-v1'},
  claims: [claim(), claim({claim_id: 'clm_2', kind: 'interpretation', text: 'Search confirms the shift.',
    citations: ['ev_2'], support_evidence_ids: ['ev_2']})],
  evidence: [
    evidence({source_name: 'Research evidence', source_family: 'social',
      record_authority: 'warehouse:evidence:ev_1', excerpt: 'Repair tutorials recur.'}),
    evidence({evidence_id: 'ev_2', source_name: 'Search evidence', source_family: 'search',
      record_authority: 'warehouse:evidence:ev_2', excerpt: 'Search confirms the shift.'}),
  ],
  ...overrides,
});

describe('dossier citation integrity', () => {
  test.each([
    [], [evidence({in_scope: false})], [evidence({client_citable: false})],
    [evidence({published_at: '2026-09-01T00:00:00Z'})], [evidence({published_at: null})],
    [evidence(), evidence()], [evidence({client_citable: 'true'})],
  ])('unusable evidence blocks and withholds the claim', (...items) => {
    const source = dossier({evidence: items});
    expect(computeArtifactReadiness(source).state).toBe('blocked');
    expect(projectClientRead(source).claims).toEqual([]);
    expect(projectClientRead(source).evidence).toEqual([]);
  });
  test('one unresolved citation withholds the whole claim', () => {
    const source = dossier({claims: [claim({citations: ['ev_1', 'ev_missing']})]});
    expect(computeArtifactReadiness(source).state).toBe('blocked');
    expect(projectClientRead(source).claims).toEqual([]);
  });
  test('duplicate claim identity is blocked and withheld', () => {
    const source = dossier({claims: [claim(), claim({text: 'A different assertion.'})]});
    expect(computeArtifactReadiness(source).state).toBe('blocked');
    expect(projectClientRead(source).claims).toEqual([]);
  });
  test('empty selection is not a ready artifact', () => {
    expect(computeArtifactReadiness(dossier({claims: []})).state).toBe('blocked');
  });
  test('the final fractional second is included but impossible dates are refused', () => {
    expect(computeArtifactReadiness(dossier({evidence: [evidence({published_at: '2026-08-28T23:59:59.999Z'})]})).state).toBe('client_ready');
    expect(computeArtifactReadiness(dossier({cutoff: '2026-02-30'})).state).toBe('blocked');
    expect(computeArtifactReadiness(dossier({evidence: [evidence({published_at: '2026-02-30T10:00:00Z'})]})).state).toBe('blocked');
    expect(computeArtifactReadiness(dossier({evidence: [evidence({published_at: '2026-08-29T00:30:00'})]})).state).toBe('blocked');
    expect(computeArtifactReadiness(dossier({evidence: [evidence({published_at: '2026-08-28T24:00:00Z'})]})).state).toBe('blocked');
  });
  test('whitespace does not form an approved answer or claim', () => {
    expect(computeArtifactReadiness(dossier({concise_answer: ' \n '})).state).toBe('blocked');
    expect(computeArtifactReadiness(dossier({claims: [claim({text: ' \n '})]})).state).toBe('blocked');
  });
  test.each([
    {occurred_on: null, material_differences: ['different'], nontransferable: ['limited']},
    {occurred_on: '0', material_differences: [''], nontransferable: ['']},
    {occurred_on: '2026-08-20', material_differences: {text: 'different'}, nontransferable: ['limited']},
  ])('unproven precedent is blocked', precedent => {
    expect(computeArtifactReadiness(dossier({precedents: [precedent]})).state).toBe('blocked');
  });
  test('malformed summary and status objects are not projected or rendered', () => {
    const source = dossier({concise_answer: {model_memory: 'INTERNAL_MARKER'}});
    expect(renderToStaticMarkup(<IntelligenceDossier dossier={source} />)).not.toContain('INTERNAL_MARKER');
    expect(JSON.stringify(projectClientRead(dossier({claims: [claim({status: {model_memory: 'INTERNAL_MARKER'}})]})))).not.toContain('INTERNAL_MARKER');
    for (const field of ['evidence_state', 'kind', 'claim_id']) expect(JSON.stringify(projectClientRead(dossier({claims: [claim({[field]: {model_memory: 'INTERNAL_MARKER'}})]})))).not.toContain('INTERNAL_MARKER');
  });
  test.each([{citations: ['ev_1'], state: 'ready'}, {citations: ['ev_1'], state: 'thin'}, {citations: 123, state: 'ready'}])('excluded internal claims cannot alter or crash package evidence', ({citations, state}) => {
    const source = richAuthorityDossier();
    const before = dossierPresentation.buildDossierConsoleModel(source);
    source.claims.push(claim({claim_id: 'hidden', kind: 'model_memory', status: 'rejected', selected: false, citations, evidence_state: state, challenge_evidence_ids: ['ev_1']}));
    expect(dossierPresentation.buildDossierConsoleModel(source)).toEqual(before);
  });
});

describe('Client Read projection', () => {
  test('an approved ready selected cited claim is included', () => {
    const read = projectClientRead(dossier());
    expect(read.claims.map((item) => item.claim_id)).toEqual(['clm_1']);
  });

  test.each([
    ['pending'],
    ['rejected'],
    ['superseded'],
    ['disputed'],
  ])('a %s claim is excluded from the client read', (status) => {
    const read = projectClientRead(dossier({claims: [claim({status})]}));
    expect(read.claims).toEqual([]);
  });

  test('excluded material is retained internally with a reason, never deleted', () => {
    /* This is the rule that keeps a clean artifact honest. Dropping the claim
       silently would make the dossier look like it never disagreed. */
    const read = projectClientRead(dossier({claims: [claim({status: 'rejected'})]}));
    expect(read.excluded).toHaveLength(1);
    expect(read.excluded[0].claim_id).toBe('clm_1');
    expect(read.excluded[0].excluded_from).toBe('client_read');
    expect(read.excluded[0].reason).toBeTruthy();
  });

  test('an unselected claim is excluded even when approved and ready', () => {
    const read = projectClientRead(dossier({claims: [claim({selected: false})]}));
    expect(read.claims).toEqual([]);
    expect(read.excluded[0].reason).toContain('selected');
  });

  test('a claim with no citation cannot reach a client read', () => {
    const read = projectClientRead(dossier({claims: [claim({citations: []})]}));
    expect(read.claims).toEqual([]);
  });

  test('thin evidence is admissible only as a limitation, never as a finding', () => {
    const finding = projectClientRead(dossier({
      claims: [claim({evidence_state: 'thin'})],
    }));
    expect(finding.claims).toEqual([]);

    const limitation = projectClientRead(dossier({
      claims: [claim({kind: 'limitation', evidence_state: 'thin'})],
    }));
    expect(limitation.claims.map((item) => item.kind)).toEqual(['limitation']);
  });

  test('model proposals and internal scores are excluded always', () => {
    const read = projectClientRead(dossier({
      claims: [claim(), claim({claim_id: 'clm_2', kind: 'model_proposal'})],
      graph_scores: {clm_1: 0.82},
      model_memory: ['a remembered idea'],
    }));
    const rendered = JSON.stringify(read);
    expect(rendered).not.toContain('graph_scores');
    expect(rendered).not.toContain('model_memory');
    expect(rendered).not.toContain('0.82');
    expect(read.claims.map((item) => item.claim_id)).toEqual(['clm_1']);
  });

  test('only client citable, in scope, non future evidence is carried', () => {
    const read = projectClientRead(dossier({
      evidence: [
        evidence(),
        evidence({evidence_id: 'ev_2', client_citable: false}),
        evidence({evidence_id: 'ev_3', in_scope: false}),
        evidence({evidence_id: 'ev_4', published_at: '2026-09-30T00:00:00Z'}),
      ],
      claims: [claim(), claim({claim_id: 'clm_2', citations: ['ev_1', 'ev_2', 'ev_3', 'ev_4']})],
    }));
    expect(read.evidence.map((item) => item.evidence_id)).toEqual(['ev_1']);
    expect(read.claims.map((item) => item.claim_id)).toEqual(['clm_1']);
  });
});

describe('Artifact readiness', () => {
  test('host approval remains the package console authority', () => {
    expect(typeof dossierPresentation.buildDossierConsoleModel).toBe('function');
    const model = dossierPresentation.buildDossierConsoleModel(dossier({decision: 'pending'}));
    /* Quiet register, 23 Sept 2026: the held reason reads in plain words on
       the Build review, not as "host dossier gates". It is still held. */
    expect(model.generationState).toEqual({
      status: 'held',
      reason: 'The brief is held until it passes its approval checks.',
    });
    expect(model.hostReadiness.state).toBe('approval_required');
  });

  test('missing citation integrity remains blocking in package evidence', () => {
    const model = dossierPresentation.buildDossierConsoleModel(dossier({claims: [claim({citations: []})]}));
    expect(model.evidenceSummary.state).toBe('unchecked');
    expect(model.selectedEvidenceIds).toEqual([]);
    expect(model.hostReadiness.failed).toContain('claim_not_cited');
  });

  test('the normal approved dossier reaches package Console without future producer fields', () => {
    const source = dossier();
    const model = dossierPresentation.buildDossierConsoleModel(source);
    expect(model.hostReadiness).toEqual({state: 'client_ready', failed: []});
    expect(model.evidenceSummary.state).toBe('unchecked');
    expect(model.evidenceSummary.independence.status).toBe('unvalidated');
    expect(model.answer).toBeNull();
    expect(model.generationState.status).toBe('held');
    const markup = renderToStaticMarkup(<IntelligenceConsole {...model} />);
    expect(markup).toContain('intelligence-console');
    expect(markup).toContain(source.concise_answer);
    expect(markup).not.toContain('cited-answer');
  });

  test('blocked or contradictory host readiness remains withheld from package answer', () => {
    for (const source of [
      dossier({decision: 'pending'}),
      dossier({claims: [claim({evidence_state: 'contradictory'})]}),
    ]) {
      const model = dossierPresentation.buildDossierConsoleModel(source);
      expect(model.hostReadiness.state).not.toBe('client_ready');
      expect(model.generationState.status).toBe('held');
      expect(model.answer).toBeNull();
      const markup = renderToStaticMarkup(<IntelligenceConsole {...model} />);
      expect(markup).not.toContain(source.concise_answer);
      expect(markup).not.toContain('cited-answer');
    }
  });

  test('package runtime withholds CitedAnswer when host evidence is only thin', () => {
    const model = dossierPresentation.buildDossierConsoleModel(dossier());
    const markup = renderToStaticMarkup(<IntelligenceConsole {...model} answer={{title: dossier().concise_answer}} />);
    expect(markup).toContain('Answer integrity is unavailable');
    expect(markup).not.toContain('cited-answer');
  });

  test('complete genuine package authority reaches ready EvidenceSummary and CitedAnswer', () => {
    const source = richAuthorityDossier();
    const model = dossierPresentation.buildDossierConsoleModel(source);
    expect(model.hostReadiness).toEqual({state: 'client_ready', failed: []});
    expect(model.evidenceSummary.state).toBe('ready');
    expect(model.evidenceSummary.independence).toEqual({
      status: 'validated', familyCount: 2, groupingAuthority: 'host-dossier-evidence-v1',
    });
    expect(model.selectedEvidenceIds).toEqual(['ev_1', 'ev_2']);
    expect(model.generationState.status).toBe('success');
    expect(model.answer).toMatchObject({title: source.concise_answer});
    expect(renderToStaticMarkup(<IntelligenceConsole {...model} />)).toContain('cited-answer');
  });

  test('one missing package authority field falls back to held and never ready', () => {
    const source = richAuthorityDossier({evidence_authority: {status: 'validated', authority_id: null}});
    const model = dossierPresentation.buildDossierConsoleModel(source);
    expect(model.hostReadiness.state).toBe('client_ready');
    expect(model.evidenceSummary.state).toBe('unchecked');
    expect(model.generationState.status).toBe('held');
    expect(model.answer).toBeNull();
    expect(renderToStaticMarkup(<IntelligenceConsole {...model} />)).not.toContain('cited-answer');
  });

  test('blocked host decision prevents ready even with complete package authority', () => {
    const source = richAuthorityDossier({decision: 'pending'});
    const model = dossierPresentation.buildDossierConsoleModel(source);
    expect(model.hostReadiness.state).toBe('approval_required');
    expect(model.evidenceSummary.state).not.toBe('ready');
    expect(model.generationState.status).toBe('held');
    expect(model.answer).toBeNull();
    expect(renderToStaticMarkup(<IntelligenceConsole {...model} />)).not.toContain('cited-answer');
  });

  test('a complete dossier with every gate met is client ready', () => {
    const readiness = computeArtifactReadiness(dossier());
    expect(readiness.state).toBe('client_ready');
    expect(readiness.failed).toEqual([]);
  });

  test.each([
    [{concise_answer: null}, 'concise_answer'],
    [{decision: 'pending'}, 'decision'],
    [{claims: [claim({status: 'pending'})]}, 'claim_not_approved'],
    [{claims: [claim({evidence_state: 'thin'})]}, 'claim_not_ready'],
    [{claims: [claim({citations: []})]}, 'claim_not_cited'],
    [{relationships: [{relationship_id: 'rel_1', kind: 'support', status: 'pending'}]},
      'relationship_not_approved'],
    [{unanswered_questions: [{question_id: 'q1', blocking: true}]}, 'blocking_question'],
  ])('a failed gate blocks readiness and names its reason', (overrides, reason) => {
    const readiness = computeArtifactReadiness(dossier(overrides));
    expect(readiness.state).not.toBe('client_ready');
    expect(readiness.failed).toContain(reason);
  });

  test('a failed gate is never softened or hidden', () => {
    const readiness = computeArtifactReadiness(dossier({decision: 'pending'}));
    // The UI may not present a blocked artifact as merely awaiting polish.
    expect(['blocked', 'approval_required']).toContain(readiness.state);
    expect(readiness.failed.length).toBeGreaterThan(0);
  });

  test('a future dated precedent is refused', () => {
    const readiness = computeArtifactReadiness(dossier({
      precedents: [{precedent_id: 'p1', status: 'approved', occurred_on: '2026-09-15',
        material_differences: ['different market'], nontransferable: ['pricing']}],
    }));
    expect(readiness.failed).toContain('precedent_not_earlier_than_cutoff');
  });

  test('a precedent without material differences or transfer limits is refused', () => {
    const readiness = computeArtifactReadiness(dossier({
      precedents: [{precedent_id: 'p1', status: 'approved', occurred_on: '2025-03-01',
        material_differences: [], nontransferable: []}],
    }));
    expect(readiness.failed).toContain('precedent_missing_transfer_limits');
  });

  test('the nine required sections exist in exact order', () => {
    expect(CLIENT_READ_SECTIONS).toHaveLength(9);
    expect(CLIENT_READ_SECTIONS[0]).toBe('what_changed');
    expect(CLIENT_READ_SECTIONS[CLIENT_READ_SECTIONS.length - 1]).toBe('what_would_change_my_mind');
  });
});

describe('Intelligence dossier rendering', () => {
  const render = (props) => renderToStaticMarkup(<IntelligenceDossier {...props} />);

  const full = () => dossier({
    claims: [
      claim({claim_id: 'clm_obs', kind: 'observation',
        text: 'Repair tutorials recur across two independent source families.'}),
      claim({claim_id: 'clm_rec', kind: 'recommendation',
        text: 'Run one repair clinic with a local maker collective.'}),
      claim({claim_id: 'clm_lim', kind: 'limitation', evidence_state: 'thin',
        text: 'Nigeria coverage is thin, so the pattern is South Africa only.'}),
      claim({claim_id: 'clm_rej', kind: 'observation', status: 'rejected',
        text: 'A rejected reading that must never reach a client.'}),
    ],
  });

  test('every rendered claim shows the citation it rests on', () => {
    const markup = render({dossier: full()});
    expect(markup).toContain('Repair tutorials recur');
    /* The receipt must be inside the claim's own row and actually visible.
       Present-but-hidden is the same as absent to a reader, and a claim
       without its receipt on screen is an assertion, not evidence. */
    const rows = markup.match(/<article class="dossier-claim"[\s\S]*?<\/article>/g) || [];
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(row).toContain('dossier-citation');
      expect(row).toContain('ev_1');
      expect(row).not.toContain('hidden');
      expect(row).not.toContain('display:none');
    }
  });

  test('package Brief Blueprint precedes package research controls', () => {
    const markup = render({dossier: dossier()});
    expect(markup).toContain('intelligence-console');
    /* Quiet register, 23 Sept 2026: package 2.0.21 names the Brief Blueprint
       "Brief plan" and its research controls "Write the brief", in the words a
       strategist would say, so the order is read off those names. */
    expect(markup).not.toContain('Brief Blueprint');
    expect(markup.indexOf('<h2>Brief plan</h2>')).toBeGreaterThan(-1);
    expect(markup.indexOf('<h2>Brief plan</h2>')).toBeLessThan(markup.indexOf('<h2>Write the brief</h2>'));
  });

  test('Console package presentation has no legacy dossier rendered beneath it', () => {
    const source = readFileSync(new URL('../../ConsoleWorkbench.jsx', import.meta.url), 'utf8');
    expect(source).not.toContain('showConsole={false}');
    expect(source).not.toMatch(/<IntelligenceConsole[\s\S]*?<IntelligenceDossier/);
  });

  test('a claim appears only in the chapter its kind belongs to', () => {
    const markup = render({dossier: full()});
    const chapter = (name) => {
      const at = markup.indexOf(`data-section="${name}"`);
      const next = markup.indexOf('data-section="', at + 1);
      return markup.slice(at, next === -1 ? undefined : next);
    };
    // A recommendation in the evidence chapter would read as a finding.
    expect(chapter('possible_response')).toContain('Run one repair clinic');
    expect(chapter('evidence')).not.toContain('Run one repair clinic');
    expect(chapter('what_changed')).toContain('Repair tutorials recur');
    expect(chapter('possible_response')).not.toContain('Repair tutorials recur');
  });

  test('a rejected claim never reaches the rendered read', () => {
    const markup = render({dossier: full()});
    expect(markup).not.toContain('must never reach a client');
  });

  test('the reader is told what was excluded and why, without seeing it', () => {
    /* Honesty cuts both ways: the excluded text stays out, but the fact that
       something was excluded stays in. Silence would read as consensus. */
    const markup = render({dossier: full()});
    expect(markup).toContain('excluded');
    expect(markup).not.toContain('must never reach a client');
  });

  test('a thin limitation is rendered as a limitation, in its own words', () => {
    const markup = render({dossier: full()});
    expect(markup).toContain('Nigeria coverage is thin');
    expect(markup).toContain('Limitation');
  });

  test('artifact readiness is stated, never softened', () => {
    const blocked = render({dossier: dossier({decision: 'pending'})});
    expect(blocked).toMatch(/blocked|approval required/i);
    // The exact failed gate is named rather than summarised as "in progress",
    // in the reader's language rather than the engine's gate identifier.
    expect(blocked).toContain('awaiting approval');
    expect(blocked).not.toContain('>decision<');
  });

  test('the nine sections appear in the contract order', () => {
    const markup = render({dossier: full()});
    let cursor = -1;
    for (const section of CLIENT_READ_SECTIONS) {
      const at = markup.indexOf(`data-section="${section}"`);
      expect(at).toBeGreaterThan(cursor);
      cursor = at;
    }
  });

  test('no internal score, digest or model memory is rendered', () => {
    const markup = render({dossier: {...full(), graph_scores: {clm_obs: 0.91},
      model_memory: ['an internal idea']}});
    expect(markup).not.toContain('0.91');
    expect(markup).not.toContain('an internal idea');
  });

  test('no h1 of its own, every heading an h2, and rows rather than nested cards', () => {
    const markup = render({dossier: full()});
    /* Quiet register, 23 Sept 2026: package 2.0.21 sets the brief plan at h2,
       so the console no longer brings an h1 of its own; the one h1 belongs to
       the page that hosts it, as "Build brief" does in the workbench. This
       presentation therefore carries none, and every heading it does carry
       is an h2, the brief plan first. */
    expect((markup.match(/<h1/g) || []).length).toBe(0);
    expect(markup.match(/<h[1-6]/g).every((heading) => heading === '<h2')).toBe(true);
    expect(markup.match(/<h2[^>]*>[^<]*/)[0]).toBe('<h2>Brief plan');
    expect(markup).not.toContain('card');
  });
});

describe('Dossier presentation', () => {
  const css = () => readFileSync(new URL('../../styles/dossier.css', import.meta.url), 'utf8')
    + readFileSync(new URL('../../../node_modules/ogilvy-intelligence-design-system/dist/style.css', import.meta.url), 'utf8');

  test('the dossier stylesheet is loaded once, from the application entry', () => {
    const entry = readFileSync(new URL('../../main.jsx', import.meta.url), 'utf8');
    expect(entry).toContain("styles/dossier.css");
    const src = readFileSync(new URL('../IntelligenceDossier.jsx', import.meta.url), 'utf8');
    expect(src).not.toContain("styles/dossier.css");
  });

  test('every class the dossier renders has a rule', () => {
    /* An unstyled Client Read is not a document a strategist puts in front of
       leadership. Rendering a class with no rule is a surface that was built
       and never finished. */
    const markup = renderToStaticMarkup(<IntelligenceDossier dossier={dossier()} />);
    const sheet = css();
    const classes = new Set();
    for (const m of markup.matchAll(/class="([^"]+)"/g)){
      m[1].split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    }
    const missing = [...classes].filter((c) => c !== 'page' && !sheet.includes(`.${c}`));
    expect(missing).toEqual([]);
  });

  test('the read carries a mobile state, not only a desktop one', () => {
    expect(css()).toMatch(/@media[^{]*max-width/);
  });

  test('claims are rows, so no rule may turn one into a card', () => {
    const sheet = css();
    const claimRules = sheet.split('}').filter((b) => b.includes('.dossier-claim'));
    expect(claimRules.length).toBeGreaterThan(0);
    for (const block of claimRules){
      expect(block).not.toMatch(/box-shadow\s*:\s*(?!none)/);
      expect(block).not.toMatch(/border-radius\s*:\s*(?![0]\b)/);
    }
  });
});

describe('Client Read language', () => {
  const withExclusions = (n) => dossier({
    claims: [
      claim(),
      ...Array.from({length: n}, (_, i) =>
        claim({claim_id: `rej_${i}`, status: 'rejected', text: `Rejected ${i}`})),
    ],
  });

  test('the excluded note agrees in number', () => {
    const one = renderToStaticMarkup(<IntelligenceDossier dossier={withExclusions(1)} />);
    expect(one).toContain('1 internal item was excluded');
    const two = renderToStaticMarkup(<IntelligenceDossier dossier={withExclusions(2)} />);
    expect(two).toContain('2 internal items were excluded');
  });

  test('no internal gate identifier is shown to a reader', () => {
    /* A strategist is owed the reason in their own language. A snake_case gate
       id is engine vocabulary, and putting it on the page makes the reader
       translate the system rather than read the work. */
    const markup = renderToStaticMarkup(
      <IntelligenceDossier dossier={dossier({decision: 'pending', claims: [claim({status: 'pending'})]})} />
    );
    /* Visible text only. Section ids live in data attributes, which a reader
       never sees, so matching raw markup would fail on the wrong thing. */
    const visible = markup.replace(/<[^>]*>/g, ' ');
    expect(visible).not.toMatch(/[a-z]+_[a-z]+/);
    expect(visible).toContain('awaiting approval');
    expect(visible).toContain('a claim is not approved');
  });

  test('a blocked artifact still names what is missing, in words', () => {
    const markup = renderToStaticMarkup(
      <IntelligenceDossier dossier={dossier({concise_answer: null})} />
    );
    expect(markup).toMatch(/Blocked/i);
    expect(markup.toLowerCase()).toContain('answer');
  });
});

test('a gate with no description is still reported, never dropped', () => {
  /* A gate this page has no wording for is still a reason the artifact is
     held. Dropping it would shorten the list and make a blocked read look
     closer to ready than it is, which is the one direction that must never
     happen. Printing the identifier would leak engine vocabulary, so it is
     named as undescribed instead. */
  const reasons = readableGates(['decision', 'a_brand_new_gate']);
  expect(reasons).toHaveLength(2);
  expect(reasons).toContain('awaiting approval');
  expect(reasons.join(' ')).toContain('not yet described');
  expect(reasons.join(' ')).not.toContain('a_brand_new_gate');
});

test('the same reason is never listed twice', () => {
  expect(readableGates(['claim_not_approved', 'claim_not_approved'])).toHaveLength(1);
});

test('a size token is never used as a font shorthand', () => {
  /* The --dossier-type-* tokens are plain sizes. `font: 18px` has no family,
     so the whole declaration is invalid and the browser drops it silently.
     The page still renders, just with no hierarchy at all, which is the worst
     kind of failure: it looks like a design choice. */
  const sheets = ['dossier.css', 'console.css'].map((name) => ({
    name,
    text: readFileSync(new URL(`../../styles/${name}`, import.meta.url), 'utf8'),
  }));
  const offences = [];
  for (const {name, text} of sheets){
    for (const m of text.matchAll(/font:\s*var\(--dossier-type-[^)]*\)[^;]*;/g)){
      offences.push(`${name}: ${m[0].trim()}`);
    }
  }
  expect(offences).toEqual([]);
});

test('the dossier states a real typographic hierarchy', () => {
  /* Editorial, not flat. The folio title, chapter titles and body must not all
     resolve to the same size. */
  const css = readFileSync(new URL('../../styles/dossier.css', import.meta.url), 'utf8');
  const sizeOf = (selector) => {
    const block = css.split('}').find((b) => b.includes(selector + ' {'));
    const m = block && block.match(/font-size:\s*([^;]+);/);
    return m ? m[1].trim() : null;
  };
  const title = sizeOf('.dossier-folio__title');
  const heading = sizeOf('.dossier-chapter__title');
  const body = sizeOf('.dossier-claim__text');
  expect(title).toBeTruthy();
  expect(heading).toBeTruthy();
  expect(body).toBeTruthy();
  expect(new Set([title, heading, body]).size).toBe(3);
});

/* The Client Read, measured in a real browser.

   A source level check catches the spelling of a mistake, not the mistake.
   `font: var(--dossier-type-heading)` shipped once already: the token is a
   plain size, the shorthand had no family, and the browser dropped every one
   of those declarations without a word. The page still rendered, with no
   hierarchy, which reads as a design choice rather than a fault. Only a real
   layout engine reporting real computed sizes catches that, and it catches it
   however it is next spelled.

   Headless Chrome clamps its own window width on Windows, so --window-size
   cannot produce a 390 pixel viewport. An iframe can: media queries and layout
   inside it resolve against the frame's own width. */
/* Where the browser lives, without assuming this desk.

   These tests do not run in CI today. When they do, the runner is Linux and a
   Windows path is not there, so the location is resolved rather than assumed.
   A missing browser makes them skip out loud: it never counts as a pass,
   because a measurement that did not happen is not one that succeeded. */
function resolveChrome(){
  const candidates = [
    process.env.CHROME_PATH,
    process.env.CHROME_BIN,
    String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
    String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
    '/usr/bin/google-chrome',
    '/usr/bin/chromium-browser',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate)) || null;
}

const CHROME = resolveChrome();

async function mountedDossierGeometry({dossier: payload, width = 390, height = 844}){
  const root = fileURLToPath(new URL('../../../../', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'lp-dossier-geometry-'));
  const bundlePath = join(directory, 'probe.js');
  const htmlPath = join(directory, 'probe.html');
  const profilePath = join(directory, 'chrome-profile');
  const chrome = CHROME;
  /* Both sheets are required for a truthful measurement: the route CSS, and
     the token sheet every --dossier-* resolves against. Measuring without the
     tokens reports the geometry of unstyled markup and proves nothing, which
     is exactly how the dropped shorthand stayed invisible. */
  const tokenCss = readFileSync(new URL('../../tokens.css', import.meta.url), 'utf8');
  const routeCss = readFileSync(new URL('../../styles/dossier.css', import.meta.url), 'utf8');
  const entry = `
    import React from 'react';
    import {createRoot} from 'react-dom/client';
    import {flushSync} from 'react-dom';
    import {IntelligenceDossier} from './frontend/src/ui/IntelligenceDossier.jsx';
    const result = document.getElementById('result');
    const wait = (ms=25) => new Promise((r) => setTimeout(r, ms));
    const encode = (v) => btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(v))));
    (async () => {
      try {
        const frame = document.getElementById('frame');
        /* Wait for the frame to exist before reading it. A srcdoc document
           parses independently of this script, so under load the mount is not
           there yet and the probe reports a missing mount as though the page
           were broken. That is a race in the harness, not a fault in the page,
           and it is the only reason this suite was ever flaky. */
        const deadline = Date.now() + 10000;
        let frameDocument = null;
        let mount = null;
        while (Date.now() < deadline){
          frameDocument = frame.contentDocument;
          mount = frameDocument && frameDocument.getElementById('root');
          if (mount && frameDocument.readyState !== 'loading') break;
          await wait(25);
        }
        if (!mount) throw new Error('frame mount never appeared within 10s');
        const frameWindow = frame.contentWindow;
        flushSync(() => createRoot(mount).render(
          React.createElement(IntelligenceDossier, {dossier: ${JSON.stringify(payload)}})));
        await wait();
        await wait();
        if (frameDocument.fonts && frameDocument.fonts.ready) await frameDocument.fonts.ready;
        await wait();
        const page = frameDocument.querySelector('.dossier-page');
        if (!page) throw new Error('dossier page missing: ' + mount.innerHTML);
        const documentElement = frameDocument.documentElement;
        const sizeOf = (selector) => {
          const el = frameDocument.querySelector(selector);
          return el ? parseFloat(frameWindow.getComputedStyle(el).fontSize) : null;
        };
        const escaping = Array.from(frameDocument.querySelectorAll('.dossier-page *'))
          .filter((el) => {
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) return false;
            return r.right > documentElement.clientWidth + 0.5 || r.left < -0.5;
          })
          .map((el) => el.className || el.tagName);
        result.textContent = encode({
          viewport: [frameWindow.innerWidth, frameWindow.innerHeight],
          horizontalOverflow: documentElement.scrollWidth - documentElement.clientWidth,
          bodyOverflow: frameDocument.body.scrollWidth - documentElement.clientWidth,
          escaping,
          titleSize: sizeOf('.dossier-folio__title'),
          chapterSize: sizeOf('.dossier-chapter__title'),
          bodySize: sizeOf('.dossier-claim__text'),
          kindSize: sizeOf('.dossier-claim__kind'),
          /* Counted by what a reader can actually see. An element that
             exists but paints nothing is the same as a missing receipt,
             and a claim without its receipt on screen is an assertion. */
          citationCount: Array.from(frameDocument.querySelectorAll('.dossier-citation'))
            .filter((el) => el.getClientRects().length > 0
              && frameWindow.getComputedStyle(el).visibility !== 'hidden'
              && parseFloat(frameWindow.getComputedStyle(el).opacity) > 0).length,
        });
      } catch (error) {
        result.textContent = encode({error: String(error && error.stack || error)});
      }
    })();
  `;
  try {
    await build({
      stdin: {contents: entry, loader: 'jsx', resolveDir: root, sourcefile: 'dossier-geometry.jsx'},
      bundle: true,
      define: {'process.env.NODE_ENV': '"production"'},
      format: 'iife',
      jsx: 'automatic',
      nodePaths: [join(root, 'frontend', 'node_modules')],
      outfile: bundlePath,
      platform: 'browser',
      plugins: [{
        name: 'css-injected-by-the-harness',
        setup(builder){
          builder.onResolve({filter: /\.css$/}, () => ({path: 'empty-css', namespace: 'geometry-probe'}));
          builder.onLoad({filter: /.*/, namespace: 'geometry-probe'}, () => ({contents: '', loader: 'js'}));
        },
      }],
    });
    const frameDocument = `<!doctype html><html data-dir="daylight" data-accent="red"><head>`
      + `<meta name="viewport" content="width=device-width,initial-scale=1">`
      + `<style>*{margin:0}html,body{width:100%}</style>`
      + `<style>${tokenCss}</style><style>${routeCss}</style></head>`
      + `<body><div id="root"></div></body></html>`;
    writeFileSync(
      htmlPath,
      `<!doctype html><html><head><style>*{margin:0;padding:0}</style></head><body>`
        + `<iframe id="frame" style="width:${width}px;height:${height}px;border:0"`
        + ` srcdoc="${frameDocument.replace(/"/g, '&quot;')}"></iframe>`
        + `<pre id="result">pending</pre><script src="./probe.js"></script></body></html>`,
      'utf8',
    );
    const run = spawnSync(chrome, [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--run-all-compositor-stages-before-draw', '--hide-scrollbars',
      `--window-size=${Math.max(width, 900)},${Math.max(height, 900)}`,
      '--virtual-time-budget=6000', '--dump-dom', `--user-data-dir=${profilePath}`,
      pathToFileURL(htmlPath).href,
    ], {encoding: 'utf8', timeout: 25000, windowsHide: true});
    /* Name the failure. A bare "error is not undefined" sent one reader
       guessing for several minutes at what was really a spawn timeout under
       load, so the timeout says so and the exit code carries its own text. */
    if (run.error){
      const timedOut = String(run.error && run.error.code) === 'ETIMEDOUT';
      throw new Error(
        timedOut
          ? 'the browser did not return within 25s, most likely a cold start under load rather than a layout failure'
          : `the browser could not be started: ${run.error}`,
      );
    }
    if (run.status !== 0){
      throw new Error(`the browser exited ${run.status}: ${String(run.stderr || '').slice(0, 400)}`);
    }
    const encoded = run.stdout.match(/<pre id="result">([^<]+)<\/pre>/)?.[1];
    if (!encoded){
      throw new Error('the probe produced no measurement block; the page did not reach its script');
    }
    const measured = JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'));
    expect(measured.error).toBeUndefined();
    return measured;
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}

describe('Client Read rendered geometry', () => {
  const full = () => dossier({
    claims: [
      claim({claim_id: 'c1', text: 'Repair tutorials recur across two independent source families over six weeks.'}),
      claim({claim_id: 'c2', kind: 'interpretation', text: 'The shift tracks rising appliance prices rather than an environmental motive.'}),
      claim({claim_id: 'c3', kind: 'recommendation', text: 'Run one repair clinic with a local maker collective before committing media.'}),
      claim({claim_id: 'c4', kind: 'limitation', evidence_state: 'thin', text: 'Nigeria coverage is thin, so this reads as South Africa only.'}),
    ],
  });

  test.skipIf(!CHROME)('nothing escapes the viewport at 390 by 844', async () => {
    const measured = await mountedDossierGeometry({dossier: full()});
    expect(measured.viewport[0]).toBe(390);
    expect(measured.horizontalOverflow).toBe(0);
    expect(measured.bodyOverflow).toBeLessThanOrEqual(0);
    expect(measured.escaping).toEqual([]);
  }, 60000);

  test.skipIf(!CHROME)('the type hierarchy survives into the browser', async () => {
    const measured = await mountedDossierGeometry({dossier: full()});
    // The exact bug that shipped: every one of these resolved to the same
    // inherited size because the shorthand was invalid and silently dropped.
    expect(measured.titleSize).toBeGreaterThan(measured.chapterSize);
    expect(measured.chapterSize).toBeGreaterThan(measured.bodySize);
    expect(measured.bodySize).toBeGreaterThan(measured.kindSize);
  }, 60000);

  test.skipIf(!CHROME)('every claim still carries a visible citation once rendered', async () => {
    const measured = await mountedDossierGeometry({dossier: full()});
    expect(measured.citationCount).toBeGreaterThanOrEqual(4);
  }, 60000);
});

describe('A dossier that was never loaded', () => {
  test('renders as unavailable, not as an artifact with nothing approved', () => {
    /* Nine chapters saying no approved claim is selected, under a folio saying
       the artifact is blocked, is a set of statements about the investigation.
       None of them was established: nothing was read. An absent dossier is a
       fact about the read and must say so. */
    /* Visible text only. A class named dossier-chapter__unavailable would
       satisfy a raw markup match while the page still said the opposite. */
    const visible = (d) => renderToStaticMarkup(<IntelligenceDossier dossier={d} />).replace(/<[^>]*>/g, ' ');
    const text = visible(null);
    expect(text).toMatch(/unavailable|has not been read/i);
    expect(text).not.toContain('No approved claim is selected for this section');
    expect(text).not.toMatch(/Blocked:/);
  });

  test('an empty object is treated the same as nothing', () => {
    const text = renderToStaticMarkup(<IntelligenceDossier dossier={{}} />).replace(/<[^>]*>/g, ' ');
    expect(text).toMatch(/unavailable|has not been read/i);
    expect(text).not.toContain('No approved claim is selected for this section');
  });

  test('a real dossier still renders its chapters', () => {
    const markup = renderToStaticMarkup(<IntelligenceDossier dossier={dossier()} />);
    expect(markup).toContain('data-section="what_changed"');
  });
});

describe('A malformed boundary must not become a passing gate', () => {
  /* Found by independent review: three fail-open shapes, one law. A value
     the gate cannot read must refuse, not pass. */
  test('a malformed cutoff withholds dated evidence', () => {
    const read = projectClientRead(dossier({
      cutoff: 'pending',
      evidence: [evidence({published_at: '2026-09-30T00:00:00Z'})],
    }));
    expect(read.evidence).toEqual([]);
    expect(read.excluded.some((e) => e.evidence_id === 'ev_1')).toBe(true);
  });

  test('undated evidence is withheld, not admitted unchecked', () => {
    const read = projectClientRead(dossier({
      evidence: [evidence({published_at: null})],
    }));
    expect(read.evidence).toEqual([]);
  });

  test('unreadably dated evidence is withheld', () => {
    const read = projectClientRead(dossier({
      evidence: [evidence({published_at: 'soonish'})],
    }));
    expect(read.evidence).toEqual([]);
  });

  test('a question missing its blocking flag blocks readiness', () => {
    const readiness = computeArtifactReadiness(dossier({
      unanswered_questions: [{question_id: 'q1'}],
    }));
    expect(readiness.state).toBe('blocked');
    expect(readiness.failed).toContain('blocking_question');
  });

  test('a question explicitly not blocking does not block', () => {
    const readiness = computeArtifactReadiness(dossier({
      unanswered_questions: [{question_id: 'q1', blocking: false}],
    }));
    expect(readiness.state).toBe('client_ready');
  });
});
