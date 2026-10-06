/* The Intelligence dossier: one document surface, flat chapters, rows.

   This is where an investigation becomes something a strategist can put in
   front of senior leadership. Two things therefore have to be true of every
   line it renders. The claim must show the receipt it rests on, because a
   claim without its receipt is an assertion. And what was excluded has to be
   acknowledged even though its text stays out, because silence about
   disagreement reads as consensus that was never reached.

   Rows, rules and typographic hierarchy. Not cards. */

import {IntelligenceConsole, validateEvidenceSummary} from 'ogilvy-intelligence-design-system';
import {CLIENT_READ_SECTIONS, computeArtifactReadiness, projectClientRead} from '../intelligenceDossierContract.js';

const SECTION_TITLE = Object.freeze({
  what_changed: 'What changed',
  why_now: 'Why now',
  evidence: 'Evidence',
  contradictions: 'Contradictions',
  market_differences: 'Market differences',
  precedent: 'Precedent',
  cultural_tension: 'Cultural tension',
  possible_response: 'Possible response',
  what_would_change_my_mind: 'What would change my mind',
});

/* Which claim kinds belong in which chapter. A recommendation never lands in
   Evidence, and a limitation never lands anywhere that would read as a
   finding. */
const SECTION_KINDS = Object.freeze({
  what_changed: new Set(['observation']),
  why_now: new Set(['interpretation']),
  possible_response: new Set(['recommendation']),
  what_would_change_my_mind: new Set(['limitation', 'abstention']),
});

/* What a failed gate means, in the reader's language. The engine's own gate
   names are its vocabulary, not theirs, and a reader who has to translate an
   identifier is reading the system instead of the work. */
const GATE_REASON = Object.freeze({
  concise_answer: 'no approved answer yet',
  decision: 'awaiting approval',
  claim_not_approved: 'a claim is not approved',
  claim_not_ready: 'a claim rests on thin evidence',
  claim_not_cited: 'a claim has no usable citation',
  relationship_not_approved: 'a link between claims is not approved',
  blocking_question: 'an open question still blocks it',
  precedent_not_earlier_than_cutoff: 'a precedent is not established before the cutoff',
  precedent_missing_transfer_limits: 'a precedent states no transfer limits',
});

export function readableGates(failed){
  const seen = [];
  for (const gate of failed || []){
    /* An unmapped gate is named as unstated rather than printed raw. Showing
       the identifier would leak engine vocabulary; hiding it entirely would
       under-report why the artifact is held. */
    const reason = GATE_REASON[gate] || 'a gate not yet described here';
    if (!seen.includes(reason)) seen.push(reason);
  }
  return seen;
}

const KIND_LABEL = Object.freeze({
  observation: 'Observation',
  interpretation: 'Interpretation',
  recommendation: 'Recommendation',
  limitation: 'Limitation',
  abstention: 'Abstention',
});

function ClaimRow({claim}){
  return (
    <article className="dossier-claim" data-claim-kind={claim.kind}>
      <p className="dossier-claim__kind">{KIND_LABEL[claim.kind] || 'Claim'}</p>
      <p className="dossier-claim__text">{claim.text}</p>
      {/* The receipt travels with the claim. Moving it elsewhere would let a
          reader take the sentence without the evidence. */}
      <p className="dossier-claim__citations">
        {claim.citations.map((id) => (
          <span className="dossier-citation" key={id}>{id}</span>
        ))}
      </p>
    </article>
  );
}

const UNAVAILABLE_AUDIENCE = Object.freeze({
  basis: 'unavailable', label: 'Audience measurement unavailable', source: null,
  authority: null, window: null, confidence: null,
  limitations: ['No measured audience is available for this brief.'],
});

function uncheckedEvidence(reason){
  return {
    contractVersion: '1.0.0', state: 'unchecked', receipts: [],
    independence: {status: 'unvalidated', familyCount: 0, groupingAuthority: null},
    direction: {status: 'unknown', supportingReceiptIds: [], opposingReceiptIds: []},
    window: {start: '', end: '', method: '', closed: false}, checkedAt: '', limitations: [reason],
  };
}

function packageEvidence(source, read, hostReadiness){
  const fallback = () => uncheckedEvidence(hostReadiness.state === 'client_ready'
    && read.claims.length > 0 && read.evidence.length > 0
    ? 'The approved evidence has not yet been checked for its receipts, independence and dates.'
    : 'The evidence behind this brief has not been checked yet.');
  if (hostReadiness.state !== 'client_ready') return fallback();
  const window = source.observation_window || {};
  const authority = source.evidence_authority || {};
  if (authority.status !== 'validated' || typeof authority.authority_id !== 'string' || !authority.authority_id) return fallback();
  const claimsByEvidence = new Map();
  const approvedClaimIds = new Set(read.claims.map(claim => claim.claim_id));
  for (const claim of source.claims || []){
    if (!approvedClaimIds.has(claim.claim_id)) continue;
    for (const id of claim.citations || []){
      const linked = claimsByEvidence.get(id) || [];
      linked.push(claim);
      claimsByEvidence.set(id, linked);
    }
  }
  const projectedIds = new Set(read.evidence.map((item) => item.evidence_id));
  const receipts = (source.evidence || []).filter((item) => projectedIds.has(item.evidence_id)).map((item) => {
    const linked = claimsByEvidence.get(item.evidence_id) || [];
    const receiptAuthority = /^https?:\/\//.test(item.url || '') ? {url: item.url}
      : typeof item.record_authority === 'string' && item.record_authority ? {recordAuthority: item.record_authority} : null;
    const direction = linked.some((claim) => (claim.challenge_evidence_ids || []).includes(item.evidence_id))
      ? 'opposing' : linked.some((claim) => (claim.support_evidence_ids || claim.citations || []).includes(item.evidence_id))
        ? 'supporting' : 'unknown';
    const qualifying = linked.length > 0
      && linked.every((claim) => claim.evidence_state === 'ready')
      && authority.status === 'validated';
    if (!item.evidence_id || !item.source_name || !item.source_family || !receiptAuthority
      || !item.published_at || !item.excerpt || !qualifying) return null;
    return {id: item.evidence_id, sourceName: item.source_name, familyId: item.source_family,
      ...receiptAuthority, observedAt: item.published_at, direction, excerpt: item.excerpt,
      quality: 'qualifying', qualifying: true};
  }).filter(Boolean);
  if (receipts.length !== projectedIds.size || !receipts.length) return fallback();
  const familyCount = new Set(receipts.map((receipt) => receipt.familyId)).size;
  const supportingReceiptIds = receipts.filter((receipt) => receipt.direction === 'supporting').map((receipt) => receipt.id);
  const opposingReceiptIds = receipts.filter((receipt) => receipt.direction === 'opposing').map((receipt) => receipt.id);
  const candidate = {
    contractVersion: '1.0.0', state: 'ready', receipts,
    independence: {status: 'validated', familyCount, groupingAuthority: authority.authority_id},
    direction: {status: opposingReceiptIds.length ? 'mixed' : 'agree', supportingReceiptIds, opposingReceiptIds},
    window: {start: window.start, end: window.end, method: window.method, closed: window.closed === true},
    checkedAt: window.checked_at, limitations: [],
  };
  const validated = validateEvidenceSummary(candidate);
  return validated.ok && validated.value.state === 'ready' ? validated.value : fallback();
}

function packagePlan(source, evidenceSummary){
  const question = source.decision_question || source.research_plan?.questions?.[0] || '';
  const families = [...new Set((evidenceSummary.receipts || []).map((receipt) => receipt.familyId))];
  return {
    id: source.investigation_id || '', intent: question, decision: typeof source.concise_answer === 'string' ? source.concise_answer : '',
    markets: Array.isArray(source.market_scope) ? source.market_scope.map((market) => String(market).toUpperCase()) : [],
    window: evidenceSummary.window,
    sourceFamilies: families,
    historicalComparison: {required: false, windowDays: null, method: null},
    evidenceRequirements: families.length ? ['Host-approved cited evidence'] : [],
    outputForm: 'Cited strategic brief',
    blockingQuestions: (source.unanswered_questions || []).filter((item) => item.blocking !== false)
      .map((item) => item.question || item.text).filter(Boolean),
  };
}

function packageAnswer(source, read, plan, evidenceSummary){
  const selectedEvidenceIds = evidenceSummary.receipts.map((receipt) => receipt.id);
  const attemptId = `host-client-read:${source.investigation_id}`;
  const manifestId = `host-evidence:${selectedEvidenceIds.join(':')}`;
  const sections = read.claims.map((claim) => ({
    sectionId: claim.claim_id,
    heading: SECTION_TITLE[CLIENT_READ_SECTIONS.find((section) => SECTION_KINDS[section]?.has(claim.kind))]
      || KIND_LABEL[claim.kind] || 'Finding',
    body: claim.text,
    evidenceIds: claim.citations.filter((id) => selectedEvidenceIds.includes(id)),
  })).filter((section) => section.evidenceIds.length > 0);
  return {selectedEvidenceIds, attemptId, manifestId, answer: {
    id: attemptId, generationAttemptId: attemptId, planId: plan.id, evidenceManifestId: manifestId,
    title: source.concise_answer, sections, contradictions: [], limitations: [], missingWork: [],
  }};
}

/* Quiet register, 23 Sept 2026: the held reason and the evidence limitations
   this model hands the console are read on the Build review, so they say in
   plain words what is held and why (rule 16), rather than naming the host
   dossier gates, package authority and citation integrity. What is held, and
   when, is unchanged. */
export function buildDossierConsoleModel(dossier){
  const source = dossier || {};
  const hostReadiness = computeArtifactReadiness(source);
  const read = projectClientRead(source);
  const citationIds = new Set(read.evidence.map((item) => item.evidence_id));
  const citationIntegrity = read.claims.length > 0
    && read.claims.every((item) => (item.citations || []).length > 0
      && item.citations.every((id) => citationIds.has(id)));
  const evidenceSummary = packageEvidence(source, read, hostReadiness);
  const plan = packagePlan(source, evidenceSummary);
  const output = hostReadiness.state === 'client_ready' && citationIntegrity
    && evidenceSummary.state === 'ready' ? packageAnswer(source, read, plan, evidenceSummary) : null;
  return {
    hostReadiness,
    plan,
    answer: output?.answer || null,
    evidenceSummary: citationIntegrity ? evidenceSummary : uncheckedEvidence('Not every claim has a usable citation yet.'),
    audienceBasis: source.audience_basis || UNAVAILABLE_AUDIENCE,
    selectedEvidenceIds: output?.selectedEvidenceIds || [],
    generationState: output ? {status: 'success', checkedAt: evidenceSummary.checkedAt, outputId: output.answer.id,
      binding: {generationAttemptId: output.attemptId, planId: plan.id, evidenceManifestId: output.manifestId,
        selectedEvidenceIds: output.selectedEvidenceIds,
        bindingAuthority: {status: 'validated', authorityId: `host-dossier:${source.investigation_id}`,
          methodId: 'computeArtifactReadiness'},
      }} : {status: 'held', reason: hostReadiness.state === 'client_ready' && citationIntegrity
        ? `Approved answer: ${source.concise_answer} The cited version is held until its evidence has been checked.`
        : 'The brief is held until it passes its approval checks.'},
  };
}

export function IntelligenceDossier({dossier, showConsole = true}){
  const source = dossier || {};
  /* Nothing was read. Nine chapters saying no approved claim is selected, under
     a folio saying the artifact is blocked, are statements about the
     investigation, and none of them was established. An absent read is a fact
     about the read, and it says so instead. */
  if (!source.investigation_id){
    return (
      <div className="page dossier-page" data-readiness="unavailable">
        <header className="dossier-folio">
          <p className="dossier-folio__kicker">Client Read</p>
          <h1 className="dossier-folio__title">This client read has not been read yet.</h1>
          <p className="dossier-folio__readiness">
            Unavailable. Nothing has been loaded, so nothing here is known either way.
          </p>
        </header>
      </div>
    );
  }
  const read = projectClientRead(source);
  const readiness = computeArtifactReadiness(source);
  const consoleModel = buildDossierConsoleModel(source);
  const claimsByKind = (section) => {
    const kinds = SECTION_KINDS[section];
    if (!kinds) return [];
    return read.claims.filter((claim) => kinds.has(claim.kind));
  };

  return (
    <div className="page dossier-page" data-readiness={readiness.state}>
      {showConsole ? <IntelligenceConsole {...consoleModel} /> : null}
      <header className="dossier-folio">
        <p className="dossier-folio__kicker">Client Read</p>
        <h2 className="dossier-folio__title">
          {typeof source.concise_answer === 'string' && source.concise_answer.trim() ? source.concise_answer : 'No concise answer has been approved yet.'}
        </h2>
        {/* Readiness is stated in the folio, in words, with its failed gates
            named. A blocked artifact is not one awaiting polish. */}
        <p className="dossier-folio__readiness">
          {readiness.state === 'client_ready'
            ? 'Client ready'
            : `${readiness.state === 'blocked' ? 'Blocked' : 'Approval required'}: ${readableGates(readiness.failed).join('; ')}`}
        </p>
      </header>

      {CLIENT_READ_SECTIONS.map((section) => {
        const claims = claimsByKind(section);
        return (
          <section className="dossier-chapter" data-section={section} key={section}>
            <h2 className="dossier-chapter__title">{SECTION_TITLE[section]}</h2>
            {claims.length
              ? claims.map((claim) => <ClaimRow claim={claim} key={claim.claim_id} />)
              : (
                /* Unavailable, in words. Never filler standing in for evidence
                   that was never gathered. */
                <p className="dossier-chapter__unavailable">
                  No approved claim is selected for this section.
                </p>
              )}
          </section>
        );
      })}

      {read.excluded.length ? (
        <section className="dossier-chapter" data-section="excluded_note">
          <h2 className="dossier-chapter__title">Excluded from this read</h2>
          {/* The count and the reasons, never the text. A reader is entitled to
              know the dossier held more than this, and not entitled to the
              material that failed its gates. */}
          <p className="dossier-chapter__unavailable">
            {read.excluded.length} internal item{read.excluded.length === 1 ? ' was' : 's were'}
            {' '}excluded and retained in the investigation, for these reasons:{' '}
            {[...new Set(read.excluded.map((item) => item.reason))].join('; ')}.
          </p>
        </section>
      ) : null}
    </div>
  );
}

export default IntelligenceDossier;
