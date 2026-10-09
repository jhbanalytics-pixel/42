/* The typed summary state on the pages (C1 v2 section 6.1, with the v2.1
   amendment). The API sends each Ask record with answer_meta already made by
   the server: null while it runs, {check: 'legacy_unknown'} for a record from
   before the field existed, {check: 'unverified', problem} when the stored
   value did not pass the server's checks, or a verified value naming how the
   run ended and what became of the one-line summary. The pages trust only
   check === 'verified', and only a value of exactly that shape: any other
   value says the summary is not available and claims no check failed.

   The sentences are the producer's (core/agent/answer_state.py
   READER_SENTENCES and STATUS_WORDS), kept under the same keys so a test can
   hold the two tables to each other. Reader text never prints a check code. */

export const NEUTRAL = 'The one-line summary is not available for this answer.';
export const OMITTED = 'The one-line summary is left out because not every finding is kept.';

export const READER_SENTENCES = {
  'removal:first_check|recheck:K2': 'The one-line summary was removed because it used a figure that no checked finding holds.',
  'removal:first_check|recheck|field_check:K3': "The one-line summary was removed because it named a place no cited post is located in, or relied on a post outside the question's window or market.",
  'removal:first_check|recheck:K6': 'The one-line summary was removed because it used a term or source the trust rules do not allow.',
  'removal:first_check|recheck:K8': 'The one-line summary was removed because it quoted words that are not in the posts it cites.',
  'removal:first_check|recheck|field_check:K9': 'The one-line summary was removed because it made a forecast, and forecasts stay held until they beat a simple no-change forecast.',
  'removal:support_check|critic:claim_cut': 'The one-line summary was removed after a claim it may have rested on did not pass its checks.',
  'removal:support_check:claim_narrowed': 'The one-line summary was removed after a claim it may have rested on was narrowed.',
  'removal:field_check:K6': 'The one-line summary was removed because the text check found it describes people in a way the trust rules do not allow.',
  'removal:field_check:field_unchecked': 'The one-line summary was too long to check with the posts it rests on, so it was left out.',
  'removal:any:unattributed': 'The one-line summary was removed by the checks.',
  'rewrite:removed_after_check': 'A rewritten summary did not pass the checks either.',
  shown_rewritten: 'This summary was rewritten once from the findings that passed.',
  blank_unexplained: 'No one-line summary was written for this answer.',
  no_answer: 'There is no answer: the run did not finish.',
  legacy_or_unverified: NEUTRAL,
};

/* Keyed by the execution state and the stop reason, joined with a bar; an
   empty reason is the part after the bar. */
export const STATUS_WORDS = {
  'stopped_on_budget|budget_full': "Stopped at this question's model budget, not for lack of evidence",
  'stopped_on_budget|model_call_unverified': 'Stopped because a model call failed or did not report what it cost, not for lack of evidence',
  'stopped_on_budget|price_unreadable': 'Stopped because the cost of a model call could not be worked out, not for lack of evidence',
  'stopped_on_request|': 'Stopped before an answer was written',
  'refused_budget_spent|': 'Not researched: the model budget for today is spent',
};

export const ENUMS = {
  execution: ['completed', 'stopped_on_request', 'stopped_on_budget', 'refused_budget_spent', 'failed'],
  stopReasons: ['budget_full', 'model_call_unverified', 'price_unreadable'],
  summary: ['shown', 'shown_rewritten', 'fixed_text', 'removed', 'blank_unexplained', 'no_answer'],
  stages: ['first_check', 'support_check', 'recheck', 'field_check', 'critic'],
  causes: ['K2', 'K3', 'K6', 'K8', 'K9', 'claim_cut', 'claim_narrowed', 'field_unchecked', 'unattributed'],
  rewrites: ['not_attempted', 'kept', 'removed_after_check', 'used_up', 'no_budget', 'call_failed', 'too_long', 'empty', 'repeated_removed_claim'],
};

const NEUTRAL_NOTICE = Object.freeze({kind: 'neutral', sentence: NEUTRAL});
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const hasExactly = (value, keys) => isObject(value) && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));

/* The wire value when it is a verified one of exactly the shape the server
   sends, else null. The server's check (check_wire) is the judge; this is the
   page declining to read a value that is not the server's own shape. */
export function verifiedWire(value){
  if (!hasExactly(value, ['check', 'v', 'execution', 'summary'])) return null;
  if (value.check !== 'verified' || value.v !== 1) return null;
  const {execution, summary} = value;
  if (!hasExactly(execution, ['state', 'stop_reason']) || !ENUMS.execution.includes(execution.state)) return null;
  if (execution.stop_reason !== null && !ENUMS.stopReasons.includes(execution.stop_reason)) return null;
  if ((execution.stop_reason !== null) !== (execution.state === 'stopped_on_budget')) return null;
  if (!hasExactly(summary, ['state', 'removals', 'rewrite']) || !ENUMS.summary.includes(summary.state) || !ENUMS.rewrites.includes(summary.rewrite)) return null;
  if (!Array.isArray(summary.removals)) return null;
  for (const removal of summary.removals){
    if (!hasExactly(removal, ['stage', 'cause']) || !ENUMS.stages.includes(removal.stage) || !ENUMS.causes.includes(removal.cause)) return null;
  }
  return value;
}

function removalSentence(stage, cause){
  for (const [key, sentence] of Object.entries(READER_SENTENCES)){
    if (!key.startsWith('removal:')) continue;
    const [, stages, wanted] = key.split(':');
    if (wanted === cause && (stages === 'any' || stages.split('|').includes(stage))) return sentence;
  }
  return READER_SENTENCES['removal:any:unattributed'];
}

const wireOf = (record) => verifiedWire(isObject(record) ? record.answer_meta : undefined);

/* What the typed state says about the one-line summary: its kind (a verified
   summary state, or 'neutral') and the sentence that goes with it. A
   removal speaks through its first entry, and adds that a rewrite failed too
   when the rewrite outcome says so. A removal list with no entry states
   nothing. */
export function summaryNotice(record){
  const wire = wireOf(record);
  if (!wire) return NEUTRAL_NOTICE;
  const {state, removals, rewrite} = wire.summary;
  if (state === 'removed'){
    if (removals.length === 0) return NEUTRAL_NOTICE;
    const first = removals[0];
    let sentence = removalSentence(first.stage, first.cause);
    if (rewrite === 'removed_after_check') sentence += ' ' + READER_SENTENCES['rewrite:removed_after_check'];
    return {kind: 'removed', sentence};
  }
  if (state === 'blank_unexplained' || state === 'no_answer') return {kind: state, sentence: READER_SENTENCES[state]};
  if (state === 'shown_rewritten') return {kind: state, sentence: READER_SENTENCES.shown_rewritten};
  return {kind: state, sentence: ''};
}

/* The sentence for a summary that is not there: the state's reason when it
   gives one, else the neutral sentence. */
export function missingSummarySentence(notice){
  return notice && (notice.kind === 'removed' || notice.kind === 'blank_unexplained' || notice.kind === 'no_answer') ? notice.sentence : NEUTRAL;
}

/* How the run ended, in words, when the state is verified and says more than
   the answer status does; else null and the page keeps its own words. */
export function statusWords(record){
  const wire = wireOf(record);
  if (!wire) return null;
  return STATUS_WORDS[wire.execution.state + '|' + (wire.execution.stop_reason || '')] || null;
}

/* Whether the "stopped early" line is shown: a stopped record, unless the
   verified state says the run completed and the stop came after the answer
   was final. */
export function stoppedEarly(record){
  if (!isObject(record) || record.status !== 'stopped') return false;
  const wire = wireOf(record);
  return !(wire && wire.execution.state === 'completed');
}

/* The sentence a dossier shows in place of a summary it does not hold. The
   state is worked out here from the version itself, as the server does for
   its export: not every claim kept, else the source's verified state where it
   agrees with whether the version holds a summary, else the neutral
   sentence. */
export function dossierSummaryWords(view){
  const claims = Array.isArray(view && view.claims) ? view.claims : [];
  if (claims.some((claim) => !claim || !claim.kept)) return OMITTED;
  const wire = view.body_v === 2 ? verifiedWire(view.source_answer_meta) : null;
  if (!wire) return NEUTRAL;
  const hasText = String(view.summary || '').trim() !== '';
  if (hasText) return NEUTRAL;
  const {state} = wire.summary;
  if (state !== 'removed' && state !== 'blank_unexplained' && state !== 'no_answer') return NEUTRAL;
  return missingSummarySentence(summaryNotice({answer_meta: wire}));
}
