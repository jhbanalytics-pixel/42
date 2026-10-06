/* Build cited brief where the brief writer is closed (the staging profile).

   There only 42's reviewed question engine may call the model, so the page
   asks it for the brief the way Ask does: one question, built from the
   reader's own topic, market and framing and shown to them before it is sent,
   goes through Ask's transport under Ask's reservation, pricing policy and
   passcode. An answered request opens at its own address, where the cited
   answer, its sources and its HTML and PDF download are the brief. */
import {useEffect, useRef, useState} from 'react';
import {apiGet} from './api.js';
import {pollChat, submitChat, terminalChatTurn} from './chatTransport.js';
import {missingWorkItems, validateIntelligenceReply} from './generalIntelligence.js';
import {COVERAGE_PATH, askCoverage, coverageText} from './askPresentation.js';
import {buildWorkbenchHash} from './workbenchRoute.js';
import {navigateWorkbench} from './researchLib.jsx';

export const BRIEF_TOPIC_MAX = 120;
export const BRIEF_FRAMING_MAX = 200;

const PLACE = {
  za: 'in South Africa',
  ng: 'in Nigeria',
  ke: 'in Kenya',
  all: 'across South Africa, Nigeria and Kenya',
};
const MARKET_OPTIONS = [['za', 'South Africa'], ['ng', 'Nigeria'], ['ke', 'Kenya'], ['all', 'All three markets']];

/* The reader's words on one line, without control characters, trailing
   full stops or anything past the bound. */
function clean(value, limit){
  const text = String(value || '').replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').trim();
  return Array.from(text).slice(0, limit).join('').replace(/[\s.!?]+$/, '');
}

/* The whole question 42 is asked: the reader's topic, market and framing in
   one fixed sentence, and nothing else. An empty topic or an unknown market
   asks nothing. */
export function briefQuestion({topic, market, framing}){
  const subject = clean(topic, BRIEF_TOPIC_MAX);
  if (!subject || !Object.hasOwn(PLACE, market)) return '';
  const frame = clean(framing, BRIEF_FRAMING_MAX);
  return 'Write a cited brief on ' + subject + ' ' + PLACE[market] + '.' + (frame ? ' Framing: ' + frame + '.' : '');
}

const briefKey = () => 'brief-' + (globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID() : [8, 4, 4, 4, 12].map(size => Array.from({length: size}, () => Math.floor(Math.random() * 16).toString(16)).join('')).join('-'));

export function BriefViaQuestion({initialTopic = '', initialMarket = 'za', onAuth}){
  const [topic, setTopic] = useState(initialTopic);
  const [market, setMarket] = useState(Object.hasOwn(PLACE, initialMarket) ? initialMarket : 'za');
  const [framing, setFraming] = useState('');
  const [coverage, setCoverage] = useState(undefined);
  /* idle, sending, waiting, failed or refused. A failure keeps what was sent
     so Retry resends the same body under the same key, and a reply the page
     never confirmed keeps its job so Check again reads it without asking.
     Outside idle the question is locked, so what the page shows is always
     what a Retry resends and an unconfirmed start is never sent again under
     a new key. Start over is offered only once the request is settled. */
  const [run, setRun] = useState({state: 'idle'});
  const ctrl = useRef(null);
  const statusRef = useRef(null);
  const topicRef = useRef(null);
  /* Where focus goes after the next render: the status line once the question
     is sent, and Topic once the reader starts over and it is enabled again. */
  const focusNext = useRef(null);

  useEffect(() => {
    let live = true;
    apiGet(COVERAGE_PATH)
      .then((value) => { if (live) setCoverage(askCoverage(value)); })
      .catch((e) => { if (!live) return; if (e && e.auth) onAuth && onAuth(); setCoverage(null); });
    return () => { live = false; };
  }, [onAuth]);

  useEffect(() => () => { if (ctrl.current) ctrl.current.abort(); }, []);

  useEffect(() => {
    const target = focusNext.current === 'status' ? statusRef.current : focusNext.current === 'topic' ? topicRef.current : null;
    if (!target) return;
    focusNext.current = null;
    try { target.focus({preventScroll: true}); } catch (_error){ target.focus(); }
  }, [run.state]);

  const question = briefQuestion({topic, market, framing});
  const busy = run.state === 'sending' || run.state === 'waiting';
  const locked = run.state !== 'idle';
  const retryable = run.state === 'failed' && Boolean(run.job || run.submission);
  const settled = run.state === 'refused' || (run.state === 'failed' && !retryable);

  const fail = (error, submission, job) => {
    if (error && error.auth) onAuth && onAuth();
    setRun({
      state: 'failed',
      message: error && error.auth ? 'Authentication is required to ask for this brief.' : (error && error.message) || 'The brief could not be asked for just now.',
      /* Only a start the service never confirmed, or one it was too busy to
         take, is worth sending again; a refusal would only be refused again. */
      submission: !job && ['submission_uncertain', 'capacity_busy'].includes(error && error.code) ? submission : null,
      job: job || (error && error.job) || null,
    });
  };

  const follow = async (job, controller, submission) => {
    setRun({state: 'waiting', job});
    try {
      const response = await pollChat(job, controller.signal);
      const intelligence = response.intelligence;
      const checked = intelligence ? validateIntelligenceReply(intelligence, {terminalError: response.error === true}) : {ok: false};
      if (checked.ok && ['complete', 'partial'].includes(intelligence.status)){
        navigateWorkbench({work: 'ask', requestId: intelligence.request_id}, {replace: false});
        return;
      }
      if (checked.ok){
        const words = intelligence.status === 'needs_clarification' ? [intelligence.clarification] : missingWorkItems(intelligence.missing_work).map((item) => item.text);
        setRun({state: 'refused', words: words.length ? words : ['No answer was published for this question.'], href: buildWorkbenchHash({work: 'ask', requestId: intelligence.request_id})});
        return;
      }
      setRun({state: 'refused', words: [terminalChatTurn(response).content], href: null});
    } catch (error){
      if (!controller.signal.aborted && !error?.aborted) fail(error, submission, job);
    } finally {
      if (ctrl.current === controller) ctrl.current = null;
    }
  };

  const send = async (submission) => {
    if (ctrl.current) return;
    const controller = new AbortController();
    ctrl.current = controller;
    setRun({state: 'sending'});
    let job;
    try {
      job = await submitChat(submission.text, [], submission.market, controller.signal, undefined, undefined, submission.idempotencyKey);
    } catch (error){
      if (ctrl.current === controller) ctrl.current = null;
      if (!controller.signal.aborted && !error?.aborted) fail(error, submission, null);
      return;
    }
    await follow(job, controller, submission);
  };

  const start = () => {
    if (!question || locked) return;
    focusNext.current = 'status';
    send({text: question, market, idempotencyKey: briefKey()});
  };

  const retry = () => {
    if (busy || ctrl.current) return;
    focusNext.current = 'status';
    if (run.job){
      const controller = new AbortController();
      ctrl.current = controller;
      follow(run.job, controller, null);
    } else if (run.submission) send(run.submission);
  };

  const startOver = () => {
    if (!settled || ctrl.current) return;
    focusNext.current = 'topic';
    setRun({state: 'idle'});
  };

  const windowText = coverage ? coverageText(coverage) : '';

  return (
    <section className="bscan brief-question" aria-labelledby="brief-question-title">
      <header className="bscan-head">
        <div className="research-field-label">Build brief</div>
        <h2 id="brief-question-title" data-stage-heading tabIndex={-1}>Build a cited brief with 42's question engine</h2>
        <p className="bscan-sub">
          {"On this server the brief is written by 42's reviewed question engine, the same one Ask uses, from the covered window"}
          {windowText ? ', ' + windowText + '.' : coverage === null ? '. The covered window could not be read just now, so the answer will say which dates it covers.' : '.'}
          {' It answers one question built from your topic, market and framing, cites its sources, and can be downloaded as HTML or PDF.'}
        </p>
      </header>
      <div className="research-field">
        <label className="research-field-label" htmlFor="brief-question-topic">Topic</label>
        <input id="brief-question-topic" ref={topicRef} type="text" className="research-product-frame-input" value={topic} maxLength={BRIEF_TOPIC_MAX} disabled={locked}
          onChange={(e) => setTopic(e.target.value)} placeholder="For example, mobile data prices" />
      </div>
      <div className="research-field">
        <label className="research-field-label" htmlFor="brief-question-market">Market</label>
        <select id="brief-question-market" className="research-product-frame-input" value={market} disabled={locked} onChange={(e) => setMarket(e.target.value)}>
          {MARKET_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </div>
      <div className="research-field">
        <label className="research-field-label" htmlFor="brief-question-framing">Framing (optional)</label>
        <input id="brief-question-framing" type="text" className="research-product-frame-input" value={framing} maxLength={BRIEF_FRAMING_MAX} disabled={locked}
          onChange={(e) => setFraming(e.target.value)} placeholder="For example, a telecoms brand" />
      </div>
      {question && (
        <div className="research-field">
          <div className="research-field-label">The question 42 will be asked</div>
          <blockquote className="research-ask-bridge-q">{question}</blockquote>
        </div>
      )}
      {!locked && <button type="button" className="legacy-action legacy-action--primary" disabled={!question} onClick={start}>Ask 42 for a cited brief</button>}
      <div role="status" aria-live="polite" ref={statusRef} tabIndex={-1}>
        {run.state === 'sending' && <p className="bscan-sub">Sending the question.</p>}
        {run.state === 'waiting' && <p className="bscan-sub">42 is writing the brief. This can take a few minutes, and the page opens the answer when it is ready.</p>}
      </div>
      {run.state === 'failed' && (
        <div role="alert">
          <p className="bscan-sub">{run.message}</p>
          <div className="brief-question-actions">
            {retryable && <button type="button" className="legacy-action legacy-action--primary" onClick={retry}>{run.job ? 'Check again' : 'Retry'}</button>}
            {settled && <button type="button" className="legacy-action" onClick={startOver}>Start over</button>}
          </div>
        </div>
      )}
      {run.state === 'refused' && (
        <div role="alert">
          {run.words.map((text, index) => <p key={index} className="bscan-sub">{text}</p>)}
          <div className="brief-question-actions">
            {run.href && <a className="legacy-action" href={run.href}>Open the request</a>}
            <button type="button" className="legacy-action" onClick={startOver}>Start over</button>
          </div>
        </div>
      )}
    </section>
  );
}
