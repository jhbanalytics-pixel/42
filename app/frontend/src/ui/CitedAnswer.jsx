/* The cited answer, as the open-question contract froze it.

   An answer arrives with the plan that produced it. The plan is shown first,
   not hidden behind the prose, because a reader who cannot see the intent, the
   window and the evidence requirements cannot judge whether the answer covers
   their question. Every finding carries its citation marks, and every citation
   resolves to a receipt listed below it: a mark with no receipt would be a
   footnote to nothing.

   Nothing here composes an answer. It renders one the service already
   produced, and states plainly where that answer declined to conclude. */

import {webHref} from '../model.js';

const PLAN_ROWS = [
  ['intent', 'Intent'],
  ['decision', 'Decision'],
  ['markets', 'Markets'],
  ['window', 'Window'],
  ['source_families', 'Source families'],
  ['historical_comparison', 'Historical comparison'],
  ['evidence_requirements', 'Evidence requirements'],
  ['output_form', 'Output form'],
];

const READINESS_LABEL = {
  ready: 'Ready',
  thin: 'Thin evidence',
  contradictory: 'Contradictory evidence',
  unchecked: 'Unchecked',
};

function planValue(value){
  if (value === null || value === undefined || value === '') return 'Not stated';
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'None stated';
  if (typeof value === 'object'){
    if (value.start_date || value.end_date) return `${value.start_date || '?'} to ${value.end_date || '?'}`;
    if (typeof value.required === 'boolean'){
      return value.required
        ? `Required${value.window_days ? `, ${value.window_days} days` : ''}`
        : 'Not required';
    }
    return JSON.stringify(value);
  }
  return String(value);
}

/* Confidence reaches the reader as a word. A number here is an engine score
   arriving through the front door of the evidence component, in a product that
   bans raw scores everywhere else. */
const CONFIDENCE_WORDS = new Set(['low', 'moderate', 'high']);

function confidenceWord(value){
  return typeof value === 'string' && CONFIDENCE_WORDS.has(value) ? value : 'unmeasured';
}

function citationMarks(ids, index){
  return (ids || []).map((id) => index[id]).filter(Boolean);
}

export function CitedAnswer({answer, plan, evidence = [], lenses = [], limitations = [], contradictions = [], missingWork = [], readiness = 'unchecked'}){
  if (!answer || !Array.isArray(answer.sections)){
    return (
      <section className="cited-answer cited-answer--absent" data-answer-state="absent">
        <h2>No cited answer</h2>
        <p>No answer has been produced for this question yet, so there is nothing to read or to check.</p>
      </section>
    );
  }

  const index = {};
  evidence.forEach((item, position) => {
    index[item.evidence_id] = item.citation_label || `[E${position + 1}]`;
  });

  return (
    <article className="cited-answer" data-answer-state={readiness}>
      <header className="cited-answer__folio">
        <p className="cited-answer__eyebrow">Evidence plan</p>
        <h2 className="cited-answer__title">{answer.title || 'Untitled answer'}</h2>
        <p className="cited-answer__readiness" data-readiness={readiness}>
          {READINESS_LABEL[readiness] || READINESS_LABEL.unchecked}
        </p>
      </header>

      <dl className="cited-answer__plan">
        {PLAN_ROWS.map(([key, label]) => (
          <div className="cited-answer__plan-row" key={key}>
            <dt>{label}</dt>
            <dd>{planValue(plan ? plan[key] : undefined)}</dd>
          </div>
        ))}
      </dl>

      {lenses.length > 0 && (
        <section className="cited-answer__lenses" aria-label="Audience lenses">
          <h3>Audience lenses</h3>
          <ul>
            {lenses.map((lens) => (
              <li key={lens.lens_id} data-basis={lens.basis}>
                <strong>{lens.basis === 'measured' ? 'Measured' : 'Inferred'}</strong>
                <span>{lens.claim}</span>
                <span className="cited-answer__lens-meta">
                  {lens.source} · {planValue(lens.window)} · confidence {confidenceWord(lens.confidence)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {answer.sections.map((section) => (
        <section className="cited-answer__section" key={section.section_id} data-section={section.section_id}>
          <h3>{section.heading}</h3>
          <p>
            {section.body}
            {citationMarks(section.evidence_ids, index).map((mark) => (
              <span className="cited-answer__mark" key={mark}>{mark}</span>
            ))}
          </p>
        </section>
      ))}

      {(contradictions.length > 0 || limitations.length > 0 || missingWork.length > 0) && (
        <section className="cited-answer__limits" aria-label="What this answer does not establish">
          <h3>What this answer does not establish</h3>
          <ul>
            {contradictions.map((item, position) => <li key={`c${position}`}>{typeof item === 'string' ? item : item.statement || JSON.stringify(item)}</li>)}
            {limitations.map((item, position) => <li key={`l${position}`}>{typeof item === 'string' ? item : JSON.stringify(item)}</li>)}
            {missingWork.map((item, position) => <li key={`m${position}`}>{typeof item === 'string' ? item : JSON.stringify(item)}</li>)}
          </ul>
        </section>
      )}

      <section className="cited-answer__receipts" aria-label="Receipts">
        <h3>Receipts</h3>
        {evidence.length === 0 ? (
          <p>No receipt is attached to this answer, so nothing in it can be checked.</p>
        ) : (
          <ol>
            {evidence.map((item, position) => { const href = webHref(item.url); return (
              <li key={item.evidence_id}>
                <span className="cited-answer__mark">{item.citation_label || `[E${position + 1}]`}</span>
                <span>{item.source_label || item.source_family || 'Source unavailable'}</span>
                {href
                  ? <a href={href} target="_blank" rel="noreferrer">{item.excerpt || 'Open receipt'}</a>
                  : <span>{item.excerpt || 'Receipt text unavailable'}</span>}
              </li>
            ); })}
          </ol>
        )}
      </section>
    </article>
  );
}

export default CitedAnswer;
