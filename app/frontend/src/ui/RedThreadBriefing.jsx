import React from 'react';

import {
  buildBriefingModel,
  buildBriefingQueue,
  buildBriefingState,
} from '../briefingContract.js';
import {webHref} from '../model.js';
import {SignalComparisonRow} from './SignalComparisonRow.jsx';
import {useLiveMargin} from './useLiveMargin.js';

const READINESS_LABELS = {
  ready: 'Ready',
  thin: 'Thin evidence',
  contradictory: 'Contradictory evidence',
  unchecked: 'Unchecked',
};

const LIVE_MARGIN_LABELS = {
  'why-now': 'Why now',
  proof: 'Direct receipts',
  precedent: 'Precedent',
  response: 'Possible response',
};

function value(receipt, ...keys){
  for (const key of keys){
    const candidate = receipt && receipt[key];
    if (candidate !== undefined && candidate !== null && String(candidate).trim()){
      return String(candidate).trim();
    }
  }
  return '';
}

function receiptFields(receipt){
  return {
    source: value(receipt, 'source', 'platform', 0),
    title: value(receipt, 'title', 'label', 'text', 1, 'id'),
    author: value(receipt, 'author', 'handle', 2),
    metric: value(receipt, 'metric', 'reach', 3),
    age: value(receipt, 'age', 'date', 4),
    url: value(receipt, 'url', 'uri', 5),
  };
}

function closedWindow(topic){
  if (!topic || typeof topic !== 'object') return '';
  return value({
    window_label: topic.window_label,
    closed_window: typeof topic.closed_window === 'string' ? topic.closed_window : '',
    nested_window: topic.window && topic.window.label,
  }, 'window_label', 'closed_window', 'nested_window');
}

function safeReceiptUrl(url){
  return webHref(url) || '';
}

function TextSection({section}){
  return <p className="oi-body" data-section-state={section.state}>{section.text}</p>;
}

function ReceiptList({proof}){
  if (!proof.receipts.length){
    return <p className="oi-body" data-section-state="unavailable">No direct receipts are available.</p>;
  }
  return (
    <ol className="oi-briefing__receipts">
      {proof.receipts.map((receipt, index) => {
        const fields = receiptFields(receipt);
        const title = fields.title || 'Receipt label unavailable';
        const href = safeReceiptUrl(fields.url);
        return (
          <li className="oi-receipt" key={value(receipt, 'id', 'evidence_id', 'receipt_id', 'row_id') || fields.url || index}>
            <p className="oi-metadata">{fields.source}</p>
            <p className="oi-body">
              {href
                ? <a className="oi-briefing__receipt-link" href={href} target="_blank" rel="noreferrer">{title}</a>
                : title}
              {fields.author ? <> · {fields.author}</> : null}
            </p>
            {fields.url && !href && <p className="oi-provenance">{fields.url}</p>}
            {(fields.metric || fields.age) && (
              <p className="oi-provenance">{[fields.metric, fields.age].filter(Boolean).join(' · ')}</p>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Precedent({section}){
  if (section.state !== 'ready' || section.text){
    return <TextSection section={section} />;
  }
  return (
    <div data-section-state="ready">
      {section.match && <><h3 className="oi-small">Match</h3><p className="oi-body">{section.match}</p></>}
      {section.difference && <><h3 className="oi-small">Difference</h3><p className="oi-body">{section.difference}</p></>}
    </div>
  );
}

function Queue({items, onOpen}){
  if (!items.length) return null;
  const interactive = typeof onOpen === 'function';
  return (
    <aside className="oi-briefing__queue" aria-label="Compare next">
      <h2 className="oi-heading">Compare next</h2>
      <div className="oi-briefing__queue-list">
        {items.map((item) => (
          <SignalComparisonRow
            key={item.signalId || item.id || item.title}
            signal={item.title}
            comparison={item.whyNow.state === 'ready' ? item.whyNow.text : 'Comparison unavailable.'}
            movement={item.movement || undefined}
            proof={item.proofSummary}
            readiness={item.readiness}
            onEvidence={interactive ? () => onOpen(item) : undefined}
          />
        ))}
      </div>
    </aside>
  );
}

export function RedThreadBriefing({topics, freshness, error, loading, onOpen, runDate}){
  const {activeChapter, progress, registerChapter, navigateToChapter} = useLiveMargin();
  const rows = Array.isArray(topics) ? topics : [];
  const state = buildBriefingState({topics: rows, freshness, error, loading});
  if (state.state === 'error'){
    return (
      <section className="oi-briefing-state" data-briefing-state="error" role="alert">
        <h1 className="oi-heading">Briefing unavailable</h1>
        <p className="oi-body">{state.error.message || state.error.code || 'Error state returned without a message.'}</p>
      </section>
    );
  }
  if (state.state === 'loading'){
    return (
      <section className="oi-briefing-state" data-briefing-state="loading" role="status" aria-live="polite">
        <p className="oi-folio">Today’s briefing</p>
        <h1 className="oi-heading">Preparing the evidence window</h1>
        <p className="oi-body">Checking the latest completed run before showing a recommendation.</p>
      </section>
    );
  }
  if (!rows.length){
    if (state.state === 'stale'){
      const checked = state.checkedAt ? new Date(state.checkedAt) : null;
      const checkedLabel = checked && Number.isFinite(checked.getTime())
        ? `Last checked ${String(checked.getUTCDate()).padStart(2, '0')} ${['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][checked.getUTCMonth()]} ${checked.getUTCFullYear()} at ${String(checked.getUTCHours()).padStart(2, '0')}:${String(checked.getUTCMinutes()).padStart(2, '0')} UTC.`
        : 'Last completed check unavailable.';
      return (
        <section className="oi-briefing-state oi-briefing-state--stale" data-briefing-state="stale" role="status">
          <div className="oi-briefing-state__mark" aria-hidden="true">42</div>
          <div className="oi-briefing-state__copy">
            <p className="oi-briefing-state__kicker">Evidence window expired</p>
            <h1 className="oi-briefing-state__title">Briefing held</h1>
            <p className="oi-briefing-state__body">42 is withholding a recommendation until a fresh evidence window closes.</p>
            <time className="oi-briefing-state__time" dateTime={state.checkedAt || undefined}>{checkedLabel}</time>
            <p className="oi-briefing-state__note">No recommendation, proof state, or possible response is inferred from stale data.</p>
          </div>
        </section>
      );
    }
    return (
      <section className="oi-briefing-state" data-briefing-state="no_discovery">
        <h1 className="oi-heading">No discovery</h1>
        <p className="oi-body">No signals were discovered in the completed run.</p>
      </section>
    );
  }

  const lead = rows[0];
  const model = buildBriefingModel(lead);
  const window = closedWindow(lead);
  const leadId = lead && (lead.id || lead.signal_id || lead.signal && lead.signal.signal_id);
  const queue = buildBriefingQueue(rows, leadId, 4);
  return (
    <article
      className="oi-briefing"
      data-briefing-state={state.state}
      style={{'--live-margin-progress': progress}}
    >
      <header className="oi-briefing__folio">
        <p className="oi-folio">{model.identity.label}</p>
        <p className="oi-provenance">{window ? 'Closed window' : 'Closed window unavailable'}</p>
        {window && <p className="oi-metadata">{window}</p>}
        {runDate && <>
          <p className="oi-provenance">Completed run date</p>
          <time className="oi-metadata" dateTime={runDate}>{runDate}</time>
        </>}
        <p className="oi-metadata" data-readiness={model.readiness}>{READINESS_LABELS[model.readiness]}</p>
      </header>

      <div className="oi-briefing__lead oi-briefing__lead-margin">
        <h1 className="oi-display">{model.title}</h1>
        {model.response.state === 'ready' && (
          <p className="oi-briefing__response-cue">
            <span className="oi-metadata">Possible response</span> {model.response.text}
          </p>
        )}
        <nav className="oi-briefing__chapter-nav" aria-label="Live Margin chapters">
          {Object.entries(LIVE_MARGIN_LABELS).map(([chapterId, label]) => (
            <button
              type="button"
              key={chapterId}
              aria-current={activeChapter === chapterId ? 'step' : undefined}
              data-active={activeChapter === chapterId}
              data-live-margin-chapter={chapterId}
              onClick={() => navigateToChapter(chapterId)}
            >
              {label}
            </button>
          ))}
        </nav>
        <section
          id="why-now"
          ref={registerChapter('why-now')}
          aria-labelledby="briefing-why-now"
          data-live-margin-chapter="why-now"
        >
          <h2 className="oi-heading" id="briefing-why-now">Why now</h2>
          <TextSection section={model.whyNow} />
        </section>
      </div>

      <section
        className="oi-briefing__proof oi-thread"
        id="proof"
        ref={registerChapter('proof')}
        aria-labelledby="briefing-receipts"
        data-live-margin-chapter="proof"
        data-transition="active"
      >
        <h2 className="oi-heading" id="briefing-receipts">Direct receipts</h2>
        <ReceiptList proof={model.proof} />
      </section>

      <div className="oi-briefing__decisions oi-thread" data-transition="active">
        <section
          id="precedent"
          ref={registerChapter('precedent')}
          aria-labelledby="briefing-precedent"
          data-live-margin-chapter="precedent"
        >
          <h2 className="oi-heading" id="briefing-precedent">Precedent</h2>
          <Precedent section={model.precedent} />
        </section>
        <section
          id="response"
          ref={registerChapter('response')}
          aria-labelledby="briefing-response"
          data-live-margin-chapter="response"
        >
          <h2 className="oi-heading" id="briefing-response">Possible response</h2>
          <TextSection section={model.response} />
        </section>
      </div>

      <Queue items={queue} onOpen={onOpen} />
    </article>
  );
}
