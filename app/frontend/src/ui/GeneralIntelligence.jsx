import {useId, useMemo, useState} from 'react';
import {missingWorkItems, quotedSpan, validateIntelligenceReply} from '../generalIntelligence.js';
import {platformLabel} from './PlatformGlyph.jsx';
import {AnswerExport} from './AnswerExport.jsx';
import {claimNotes, coverageDate, coverageText, latestWeekQuestion, marketNames, outsideCoverage, presentedLimitations, readableWindow} from '../askPresentation.js';
import './general-intelligence.css';

/* The status is a word at the end of the meta line, in its state colour, never
   a header or a pill: complete is the default and reads quietly, the others
   say what happened. Quiet register, 23 Sept 2026: the answer's window is
   named as the dates its evidence comes from, so it is not read as a second
   statement of the dates Ask covers in the page head. */
const STATUS = {complete: 'Complete', partial: 'Partial', needs_clarification: 'Needs clarification', unavailable: 'Reply unavailable', refused: 'Request refused'};
const SECTION = {answer: 'The read', evidence: 'Supporting observations', interpretation: 'Interpretation', actions: 'Actions'};
const KIND = {observation: 'Observation', interpretation: 'Interpretation', inference: 'Inference', proposal: 'Proposal'};
const SUPPORT = {source_record: 'from a source record', derived: 'derived from cited claims', proposed: 'with no source record'};
const MARKET = {za: 'South Africa', ng: 'Nigeria', ke: 'Kenya'};
const KNOWN_PLATFORMS = new Set(['reddit', 'tiktok', 'instagram', 'youtube', 'threads', 'facebook', 'x', 'twitter', 'google_search', 'news', 'web', 'aggregate', 'bluesky']);
const PLATFORM = {web_attention: 'Web attention', app_store: 'App Store'};
const METHOD = {selected_receipt_count: 'Selected source records'};

function sourceUrl(value){
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
  catch { return null; }
}
function dateText(value){
  if (!value) return 'Unavailable';
  const at = new Date(value);
  return Number.isFinite(at.getTime()) ? coverageDate(at.toISOString()) : String(value);
}
/* A publication time the source never sent is the source's own gap, and is
   named as such. Every other missing receipt field is a gap in 42's released
   data (the released relation carries no collection time, and the retained
   capture has no source family column), so it is named as that and never
   blamed on the source. */
const NOT_RECORDED = 'not recorded by the source';
const NOT_RELEASED = 'Not available in the released data';
function recordedDate(value){ return value ? dateText(value) : NOT_RECORDED; }
/* A recorded enum such as "social" reads in sentence case, set here rather
   than through CSS. */
function sentenceCase(value){ const text = String(value); return text.charAt(0).toUpperCase() + text.slice(1); }
function readingValue(reading){ return reading.value === null ? 'Unmeasured' : typeof reading.value === 'number' ? reading.value.toLocaleString('en-ZA', {maximumSignificantDigits: 21}) : String(reading.value); }
function sourceLabel(receipt){
  if (!receipt.platform) return receipt.source_label;
  if (KNOWN_PLATFORMS.has(receipt.platform)) return platformLabel(receipt.platform);
  return Object.hasOwn(PLATFORM, receipt.platform) ? PLATFORM[receipt.platform] : receipt.platform;
}
function claimText(claim, readings){
  if (claim.kind !== 'observation' || claim.reading_ids.length !== 1) return claim.text;
  const reading = readings.get(claim.reading_ids[0]);
  const label = Object.hasOwn(METHOD, reading.method) ? METHOD[reading.method] : null;
  const supplied = `${reading.method}: ${reading.value} ${reading.unit} (${reading.window.start} to ${reading.window.end}).`;
  return label && Number.isInteger(reading.value) && reading.value >= 0 && reading.unit === 'records' && claim.text === supplied ? `${label}${claim.text.slice(reading.method.length)}` : claim.text;
}

/* requestDetails are the stored request's bookkeeping lines (the requested and
   resolved scope, the allowance). They sit in the Request and usage details
   disclosure with the rest of the run record, never above the answer.
   onBuildBrief turns a completed answer into a cited brief from the actions
   row at its end. windowFromPlan is false only for a stored reply written
   before any plan: its window is the engine's default, the days before as_of,
   which the question never resolved to, so the meta line names none. */
export function GeneralIntelligence({intelligence, terminalError = false, coverage = null, coverageUnread = false, onRetryCoverage = null, onRephrase = null, firstQuestion = false, requestDetails = [], onBuildBrief = null, windowFromPlan = true}){
  const parsed = useMemo(() => validateIntelligenceReply(intelligence, {terminalError}), [intelligence, terminalError]);
  const instance = useId().replace(/:/g, '');
  const [openSources, setOpenSources] = useState(new Set());
  const [visibleSources, setVisibleSources] = useState(6);
  if (!parsed.ok) return <section className="general-reply" data-intelligence-state="invalid" role="alert"><h3>Reply withheld</h3><p>{parsed.reason}</p></section>;
  /* What the page shows beside the reply, never a change to it: a refusal about
     dates reads as one, with the covered dates named, and each limitation the
     reply supplied is listed once. */
  const refusedDates = parsed.value.status === 'unavailable' && outsideCoverage(parsed.value, coverage);
  /* A coverage refusal read while the covered dates could not be: the page
     cannot tell whether it is about dates, and says so rather than falling
     back to the outage line. */
  const datesUnread = parsed.value.status === 'unavailable' && !coverage && coverageUnread && Array.isArray(parsed.value.missing_work) && parsed.value.missing_work.includes('coverage_incomplete');
  /* An empty match is an answer, not an outage: retrieval ran and nothing in
     the records supports an answer, so the reply says that in plain words. */
  const insufficient = parsed.value.status === 'unavailable' && !refusedDates && !datesUnread && parsed.value.missing_work.includes('evidence_insufficient');
  const value = {...parsed.value,
    limitations: presentedLimitations(parsed.value, {firstQuestion, shownNotes: ['complete', 'partial'].includes(parsed.value.status) ? claimNotes(parsed.value) : []}),
    missing_work: refusedDates || datesUnread ? parsed.value.missing_work.filter(item => item !== 'retrieval_incomplete') : parsed.value.missing_work};
  /* The held projection: the answer is not ready for downstream use while its
     usage record is unresolved, so its citations are held with it. */
  const held = value.ready_for_downstream === false && value.usage.status === 'unresolved';
  const answered = ['complete', 'partial'].includes(value.status);
  const receipts = [...parsed.receipts.values()];
  const receiptId = id => `source-${instance}-${receipts.findIndex(receipt => receipt.receipt_id === id)}`;
  const open = id => {
    setVisibleSources(count => Math.max(count, receipts.findIndex(receipt => receipt.receipt_id === id) + 1));
    setOpenSources(current => new Set([...current, id]));
    requestAnimationFrame(() => {
      const node = document.getElementById(receiptId(id));
      node?.querySelector('summary')?.focus({preventScroll: true});
      node?.scrollIntoView({block: 'nearest', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
    });
  };
  const citationIds = claim => {
    const ids = new Set(claim.receipt_ids);
    const visited = new Set();
    const collect = item => {
      if (visited.has(item.claim_id)) return;
      visited.add(item.claim_id);
      item.receipt_ids.forEach(id => ids.add(id));
      item.reading_ids.forEach(id => parsed.readings.get(id).source_receipt_ids.forEach(source => ids.add(source)));
      item.parent_claim_ids.forEach(id => collect(parsed.claims.get(id)));
    };
    collect(claim);
    return [...ids];
  };
  const citation = id => <button type="button" className="general-citation" key={id} disabled={held} aria-label={`Open ${parsed.receipts.get(id).citation_label}: ${sourceLabel(parsed.receipts.get(id))}`} aria-controls={receipts.findIndex(receipt => receipt.receipt_id === id) < visibleSources ? receiptId(id) : undefined} aria-expanded={openSources.has(id)} onClick={() => open(id)}>{parsed.receipts.get(id).citation_label}</button>;
  return (
    <section className="general-reply" data-intelligence-state={value.status}>
      <header className="general-reply-header"><p className="general-meta">{marketNames(value.resolved_scope.market_scope)}{' \u00b7 '}{value.window && windowFromPlan ? 'Evidence from ' + readableWindow(value.window.start, value.window.end) + (value.window.closed ? '' : ', window still open') : 'Dates not settled'}{' \u00b7 '}<span className="general-status" data-status={value.status}>{insufficient ? 'Not enough evidence' : STATUS[value.status]}</span></p>
        {value.review_required && <p className="general-review">Review required before downstream use.</p>}
      </header>
      {value.status === 'needs_clarification' && <p className="general-clarification">{value.clarification}</p>}
      {value.status === 'unavailable' && (refusedDates
        ? <div className="general-coverage" role="note"><p>This question falls outside the dates 42 can answer. No answer was generated.</p><p>Ask covers {coverageText(coverage)}.</p>{onRephrase && <button type="button" className="capacity-retry" onClick={() => onRephrase(latestWeekQuestion(value.resolved_scope.market_scope))}>Ask about the latest available week</button>}</div>
        : datesUnread
          ? <div className="general-coverage" role="note"><p>The dates 42 can answer could not be read, so this page cannot say whether the question falls outside them. No answer was generated.</p>{onRetryCoverage && <button type="button" className="capacity-retry" onClick={onRetryCoverage}>Read the covered dates again</button>}</div>
        : insufficient
          ? <p>There is not enough evidence to answer this question. Nothing in the records for these dates supports an answer, so none was generated.</p>
          : <p>This request could not be completed from the admitted evidence and execution results.</p>)}
      {value.status === 'refused' && <p>This request was refused under the current scope or execution rules.</p>}
      {answered && value.sections.map(section => <section className="general-section" key={section.kind}><h3>{SECTION[section.kind]}</h3>{section.claim_ids.map(id => {
        const claim = parsed.claims.get(id);
        return <article className="general-claim" key={id}><p className="general-kind">{KIND[claim.kind]}, {SUPPORT[claim.support_state]}</p><p className="general-claim-text"><span>{claimText(claim, parsed.readings)}</span>{' '}<span className="general-citations" role="group" aria-label="Supporting sources">{citationIds(claim).map(citation)}</span></p>
          {claim.reading_ids.map(readingId => {
            const reading = parsed.readings.get(readingId);
            return <div className="general-reading" key={readingId}><p><strong>{readingValue(reading)}</strong> {reading.unit}</p><details><summary>Measurement details</summary><p>Method: {Object.hasOwn(METHOD, reading.method) ? METHOD[reading.method] : reading.method}</p><p>{readableWindow(reading.window.start, reading.window.end)} · {reading.window.closed ? 'Closed' : 'Open'}</p><p>Denominator: {reading.denominator === null ? 'Unmeasured' : reading.denominator.toLocaleString('en-ZA', {maximumSignificantDigits: 21})}</p>{reading.limitations.map((item, index) => <p key={index}>{item}</p>)}</details></div>;
          })}
          {claim.limitations.map((item, index) => <p className="general-note" key={index}>{item}</p>)}
          {claim.falsifier && <p className="general-note">What would change this: {claim.falsifier}</p>}
        </article>;
      })}</section>)}
      {value.limitations.length > 0 && <section className="general-section"><h3>Limits of this read</h3>{value.limitations.map((item, index) => <p key={index}>{item}</p>)}</section>}
      {value.missing_work.length > 0 && <section className="general-section"><h3>What is still needed</h3>{missingWorkItems(value.missing_work).map((item, index) => item.reference === null ? <p key={index}>{item.text}</p> : <div key={index}><p>{item.text}</p><details><summary>Requirement reference</summary><p className="general-reference">{item.reference}</p></details></div>)}</section>}
      {receipts.length > 0 && <section className="general-section"><h3>Source records</h3><p className="general-note">{receipts.length} {receipts.length === 1 ? 'record' : 'records'} in this reply. Record counts do not establish independent corroboration.</p>{receipts.slice(0, visibleSources).map(receipt => <details className="general-source" key={receipt.receipt_id} id={receiptId(receipt.receipt_id)} open={openSources.has(receipt.receipt_id)} onToggle={event => {
        const expanded = event.currentTarget.open;
        setOpenSources(current => { if (current.has(receipt.receipt_id) === expanded) return current; const next = new Set(current); expanded ? next.add(receipt.receipt_id) : next.delete(receipt.receipt_id); return next; });
      }}><summary>{receipt.citation_label} · {sourceLabel(receipt)}</summary>
        <p>{receipt.kind === 'aggregate' ? 'Aggregate evidence' : 'Content record'} · {receipt.market ? MARKET[receipt.market] || receipt.market.toUpperCase() : `Market: ${NOT_RELEASED}`}</p>
        <p>{receipt.platform ? sourceLabel(receipt) : `Platform: ${NOT_RELEASED}`}{receipt.author ? ` · ${receipt.author}` : ''}</p>
        {receipt.excerpt ? <blockquote>{receipt.excerpt}</blockquote> : <p>Excerpt: {NOT_RELEASED}</p>}
        {receipt.quote_bindings?.map((binding, index) => <p className="general-quote" key={index}>Cited span: <q>{quotedSpan(receipt.excerpt, binding)}</q></p>)}
        <p>Published: {recordedDate(receipt.published_at)} · {receipt.collected_at ? `Collected: ${dateText(receipt.collected_at)}` : `Collection time: ${NOT_RELEASED}`}</p>
        <p>Source family: {receipt.source_family ? sentenceCase(receipt.source_family) : NOT_RELEASED}</p>
        {sourceUrl(receipt.url) ? <a href={sourceUrl(receipt.url)} target="_blank" rel="noreferrer">View original source</a> : <p>{receipt.url ? 'Source link: withheld because it is not a web address' : `Source link: ${NOT_RELEASED}`}</p>}
        {receipt.limitations.map((item, index) => <p className="general-note" key={index}>{item}</p>)}
        <details><summary>Record reference</summary><p className="general-reference">{receipt.source_row_id || receipt.receipt_id}</p></details>
      </details>)}{visibleSources < receipts.length && <button type="button" className="general-more" onClick={() => setVisibleSources(count => count + 6)}>Show more source records</button>}</section>}
      {/* Quiet register, 23 Sept 2026: the actions row has one bounded
          control, the cited brief, which is the next step an answer leads
          to, and the two downloads follow it as quiet text links, so the row
          no longer reads as three equal buttons. */}
      {answered && ((!terminalError && value.usage.status === 'resolved') || onBuildBrief) && <div className="general-actions">
        {onBuildBrief && <button type="button" className="general-more" onClick={onBuildBrief}>Turn into a cited brief</button>}
        {!terminalError && value.usage.status === 'resolved' && <AnswerExport requestId={value.request_id} />}
      </div>}
      <details className="general-run"><summary>Request and usage details</summary>{requestDetails.map((line, index) => <p key={'request-' + index}>{line}</p>)}<p className="general-reference">{value.request_id}</p><p>As of {new Date(value.as_of).toLocaleString('en-ZA', {timeZone: 'UTC'})} UTC</p>
        {value.window && windowFromPlan && <p>Observation window: {value.window.closed ? 'closed' : 'open'}</p>}
        {value.usage.status === 'unresolved' && <p>Usage is unresolved. The recorded reservation remains held until reconciled.</p>}
        <p>Model calls: {value.usage.model_calls === null ? 'Unmeasured' : value.usage.model_calls}</p><p>Input tokens: {value.usage.input_tokens === null ? 'Unmeasured' : value.usage.input_tokens.toLocaleString('en-ZA')} · Output tokens: {value.usage.output_tokens === null ? 'Unmeasured' : value.usage.output_tokens.toLocaleString('en-ZA')}</p>
        <p>Reserved generation allowance: USD {value.usage.reserved_cost_usd}. This is a reservation, not measured spend.</p>
      </details>
    </section>
  );
}
