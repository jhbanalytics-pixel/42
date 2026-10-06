/* One released signal's story, opened from the Briefing lead's action and
   from Discover's "Open topic story" at #/topic/<signal id>.

   The topic API answers curated topics only, so a released signal id is read
   from the same released run the Briefing and Discover render: the desk's
   dynamic_discovery member through exploreDynamicProps. Nothing is fetched
   here and nothing is filled in. Every line is the run's own field, a field
   the run did not send is left out, and a signal the run does not hold reads
   as plainly unavailable with a way back, never as a failed topic read. */
import {adaptSignalToInstrumentModel} from './instrumentAdapters.js';
import {readableDate, readableDates} from './plainLabels.js';
import {readerWord, regionLabel} from './model.js';

const SIGNAL_ID = /^sig_[0-9a-f]{64}$/;
const HELD_TITLE = 'Held until the evidence authority is checked';

export function isReleasedSignalId(id){
  return typeof id === 'string' && SIGNAL_ID.test(id);
}

function signalOf(row){
  if (!row || typeof row !== 'object') return null;
  return row.signal && typeof row.signal === 'object' ? row.signal : row;
}

function text(value){
  return typeof value === 'string' && value.trim() ? value.trim() : '';
}

/* The read the page renders: loading, error, unavailable, held or ready.
   The same holds the Briefing applies to its lead apply here, so a signal the
   Briefing would not brief is not briefed by its own page either. */
export function releasedSignalRead({signalId, topics, loading = false, error = null}){
  if (loading) return {state: 'loading'};
  if (error) return {state: 'error', error};
  const row = (Array.isArray(topics) ? topics : []).find((candidate) => signalOf(candidate)?.signal_id === signalId);
  if (!isReleasedSignalId(signalId) || !row) return {state: 'unavailable'};
  const model = adaptSignalToInstrumentModel(row);
  if (!model || model.state === 'unavailable' || !model.title || model.evidenceSummary?.window?.closed !== true){
    return {state: 'unavailable'};
  }
  if (model.evidenceSummary.state === 'unchecked' && model.evidenceSummary.independence?.status === 'unvalidated'){
    return {state: 'held', model, reason: model.evidenceSummary.limitations?.[0] || 'Source independence has not been validated for this signal.'};
  }
  return {state: 'ready', model, signal: signalOf(row)};
}

function Frame({state, children, signal}){
  return (
    <section
      className="page released-signal"
      data-screen-label="Signal story"
      data-released-signal-state={state}
      data-signal-id={signal ? signal.signal_id : undefined}
      data-run-date={signal ? signal.signal_date : undefined}
    >
      <div className="page-shell">
        <p><a className="cp-back" href="#/explore">Discover</a></p>
        {children}
      </div>
    </section>
  );
}

function WaysOn(){
  return <p><a className="route-open-action" href="#/explore">Open Discover</a> <a className="route-open-action" href="#/pulse">Open the Briefing</a></p>;
}

function Receipt({receipt}){
  const url = text(receipt.url);
  const linkable = /^https?:\/\//i.test(url);
  const published = text(receipt.published_at);
  const facts = [text(receipt.author_label), text(receipt.platform) ? readerWord(receipt.platform) : '', published ? readableDate(published.slice(0, 10)) : ''].filter(Boolean);
  return (
    <li className="released-signal__receipt">
      {text(receipt.excerpt) ? <blockquote>{text(receipt.excerpt)}</blockquote> : null}
      {facts.length ? <p>{facts.join(' · ')}</p> : null}
      {linkable ? <p><a className="route-open-action" href={url} target="_blank" rel="noopener noreferrer">View source</a></p> : null}
    </li>
  );
}

export function ReleasedSignalStory({signalId, topics, loading = false, error = null, onRetry}){
  const read = releasedSignalRead({signalId, topics, loading, error});
  if (read.state === 'loading'){
    return <Frame state="loading"><h1 tabIndex={-1}>Signal story</h1><p role="status">Reading the released run.</p></Frame>;
  }
  if (read.state === 'error'){
    return (
      <Frame state="error">
        <h1 tabIndex={-1}>The released run could not load</h1>
        <p>{text(read.error.message) || 'The desk did not answer, so this signal cannot be shown yet.'}</p>
        <p><button type="button" className="route-open-action" onClick={() => (typeof onRetry === 'function' ? onRetry() : window.location.reload())}>Try again</button></p>
      </Frame>
    );
  }
  if (read.state === 'unavailable'){
    return (
      <Frame state="unavailable">
        <h1 tabIndex={-1}>This signal is not in the released run</h1>
        <p>The released run for this market does not hold this signal, so there is no story to show. Discover lists the signals the run does hold.</p>
        <WaysOn />
      </Frame>
    );
  }
  if (read.state === 'held'){
    return (
      <Frame state="held">
        <h1 tabIndex={-1}>{HELD_TITLE}</h1>
        <p>{read.reason}</p>
        <WaysOn />
      </Frame>
    );
  }
  const {model, signal} = read;
  const market = text(signal.market) ? regionLabel(signal.market.toUpperCase()) : '';
  const start = text(signal.observation_start);
  const end = text(signal.observation_end);
  const receipts = Array.isArray(signal.receipts) ? signal.receipts.filter((receipt) => receipt && typeof receipt === 'object') : [];
  const runLine = [
    text(signal.signal_date) ? 'Run ' + readableDate(signal.signal_date) : '',
    market,
    start && end ? 'Observed ' + readableDates(start + ' to ' + end) : '',
  ].filter(Boolean).join('. ');
  return (
    <Frame state="ready" signal={signal}>
      <h1 tabIndex={-1}>{model.title}</h1>
      {runLine ? <p className="released-signal__run">{runLine}.</p> : null}
      {model.whyNow ? <section><h2>Why now</h2><p>{model.whyNow}</p></section> : null}
      {model.possibleResponse ? <section><h2>Possible response</h2><p>{model.possibleResponse}</p></section> : null}
      <section>
        <h2>Evidence</h2>
        {receipts.length
          ? <><p>{receipts.length === 1 ? '1 source record' : receipts.length + ' source records'} in the released run.</p><ul>{receipts.map((receipt, index) => <Receipt key={text(receipt.evidence_id) || index} receipt={receipt} />)}</ul></>
          : <p>The released run lists no source records for this signal.</p>}
      </section>
    </Frame>
  );
}

export default ReleasedSignalStory;
