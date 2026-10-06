/* PULSE · Intelligence Console: a conversation with the engine. Ask is a quiet
   writing desk, not a chatbot. The main column reads like an article: each
   question heads its answer as a serif heading, the answer sits on the page
   with its evidence underneath, and the one framed control is the question
   field, first on an empty page and under the answer for a follow-up, with
   the starter questions listed beneath it. Threads persist to localStorage so
   a past conversation reopens from the rail. */
import {useState, useEffect, useId, useRef, useCallback, useMemo, useSyncExternalStore} from 'react';
import {FAILURE_ORIGIN_LABEL, useApi, apiGetFresh, failureCode, failureOrigin} from './api.js';
import {holdDelivery, pollChat, sendHeldDelivery, submitChat, terminalChatTurn, SUBMISSION_UNCERTAIN, REPLY_UNCERTAIN} from './chatTransport.js';
import {intelligenceHistoryText, validateIntelligenceReply} from './generalIntelligence.js';
import {GeneralIntelligence} from './ui/GeneralIntelligence.jsx';
import {Icon, SignalLoader} from './parts.jsx';
import {go, QUESTION_REQUEST_ID} from './router.js';
import {ClientLensSelector, GENERAL_LENS_LABEL, LENS_STATE_MESSAGE, clientLensRoster, resolveClientLensState, selectedClientLens} from './ui/ClientLensSelector.jsx';
import {buildWorkbenchHash, buildWorkbenchPath, parseWorkbenchRoute} from './workbenchRoute.js';
import {runSuggestions} from './suggestions.js';
import {COVERAGE_PATH, allowanceText, askCoverage, coverageDate, coverageText, marketNames, shareableRequestHash, storedWindowText} from './askPresentation.js';

const CHAT_KEY = 'pulse-chat';
const MAX_THREADS = 24;

/* Availability relative, so the planner resolves it inside the covered dates. */
const LATEST_WEEK_STARTER = 'What should we brief on from the latest available week?';

/* Quiet register, 23 Sept 2026: the questions offered under an empty field.
   When the desk read carries the completed run's admitted signals for the
   market in view, the questions are built from those signal names and
   nothing else (rule 20); otherwise the general questions name the market in
   words rather than as a code (rule 16). The latest week question closes
   either list. */
function starterQuestions(market, signals){
  if (signals.length) return [...signals.slice(0, 3).map((name) => 'What is driving ' + name + '?'), LATEST_WEEK_STARTER];
  const names = market === 'all' ? ['za', 'ng', 'ke'].map((code) => marketNames([code])) : [marketNames([market])];
  const place = names.length > 1 ? 'across ' + names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1] : 'in ' + names[0];
  return ['What is moving ' + place + '?', 'Who are the top voices right now?', 'How is sentiment trending?', LATEST_WEEK_STARTER];
}

/* Ask redesign, 23 Sept 2026: the field names the question it wants rather
   than inviting a search, and the one line on what a reply carries sits under
   it as its hint. */
const PLACEHOLDER = 'For example: are repair tutorials rising in South Africa?';
/* The follow-up field gives its own example, one that reads as a question
   about the answer above it rather than a fresh first question. */
const FOLLOW_UP_PLACEHOLDER = 'For example: which platforms carry most of this?';
const COMPOSER_HINT = 'Each reply carries its available evidence and limits.';

const newId = () => 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
/* One key per question. The server joins a resubmission under the same key to
   the request it already admitted, so Retry after an unconfirmed submission can
   never start a second request. */
const requestKey = () => 'ask-' + (globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID() : [8, 4, 4, 4, 12].map(size => Array.from({length: size}, () => Math.floor(Math.random() * 16).toString(16)).join('')).join('-'));

/* The console's own route, read from the hash. A hash that does not parse is a
   landing route, never a silent loss of what the caller asked for. */
function currentRoute(){
  try { return parseWorkbenchRoute(typeof window === 'undefined' ? '' : window.location.hash); }
  catch (_error){ return {}; }
}

/* The lens the route names, and the subscription that reports a later change to
   it. The hash is the console's identity: a deep link names the configuration a
   question is asked under, so the console reads the lens from the route on every
   change rather than only on the render that opened the page. */
export function routeLensId(){
  return currentRoute().clientLensId || '';
}

export function subscribeToRouteLens(onRouteChange){
  if (typeof window === 'undefined' || !window.addEventListener) return () => {};
  window.addEventListener('hashchange', onRouteChange);
  return () => window.removeEventListener('hashchange', onRouteChange);
}

/* Where choosing a lens sends the console. Every other field on the route belongs
   to the route, not to the lens: a console carrying markets and an artifact keeps
   both when the lens moves. A request id in the address is the share link of the
   last reply, not part of the live ask, so choosing a lens leaves it behind
   rather than opening the stored view. */
export function lensRoutePath(route, clientLensId){
  const {requestId: _requestId, error: _error, ...live} = route || {};
  return buildWorkbenchPath({...live, work: 'ask', clientLensId});
}

/* Whether the ask control refuses the draft it is holding. An empty draft has
   nothing to send, and a lens the console cannot resolve must not be answered
   as general 42, so both close the control while a running request stays
   stoppable. */
export function askIsBlocked({busy, draft, lensBlocked}){
  return !busy && (!String(draft || '').trim() || Boolean(lensBlocked));
}

function pausedTurn(turn){
  return {...turn, turnId: turn.turnId || newId(), pending: false, error: true, statusUncertain: true, content: turn.job ? REPLY_UNCERTAIN : SUBMISSION_UNCERTAIN};
}

export function loadThreads(){
  try { const v = JSON.parse(localStorage.getItem(CHAT_KEY) || '[]'); return Array.isArray(v) ? v.map(thread => ({...thread, messages: (thread.messages || []).map(turn => turn.role === 'assistant' && turn.pending ? pausedTurn(turn) : turn)})) : []; }
  catch (e){ return []; }
}
export function saveThreads(list){
  const unresolved = thread => (thread.messages || []).some(turn => turn.role === 'assistant' && (turn.pending || turn.statusUncertain));
  const ordered = [...list].sort((a, b) => (b.ts || 0) - (a.ts || 0));
  let settled = 0;
  const retained = ordered.filter(thread => unresolved(thread) || settled++ < MAX_THREADS);
  try { localStorage.setItem(CHAT_KEY, JSON.stringify(retained)); return true; } catch (e){ return false; }
}

export function chatContextReferences(messages){
  const answered = messages.filter(turn => turn.role === 'assistant' && !turn.pending && !turn.error && !turn.statusUncertain
    && ['complete', 'partial'].includes(turn.intelligence?.status)
    && turn.intelligence?.usage?.status === 'resolved'
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(turn.intelligence?.request_id || '')
    && validateIntelligenceReply(turn.intelligence).ok);
  if (!answered.length) return {};
  return {parent_request_id: answered[answered.length - 1].intelligence.request_id, thread_anchor_request_id: answered[0].intelligence.request_id};
}

/* The client lens a conversation was asked under: the lens of the answer a
   follow-up would name as its parent. General 42 is the empty id. A turn saved
   before lenses were recorded names none, so it reads as general 42, which is
   what every request was before lenses existed. */
export function chatThreadLensId(messages){
  const references = chatContextReferences(messages);
  if (!references.parent_request_id) return null;
  const parent = messages.filter(turn => turn.role === 'assistant' && turn.intelligence?.request_id === references.parent_request_id).at(-1);
  return typeof parent?.clientLensId === 'string' ? parent.clientLensId : '';
}

/* Every lens the answered turns of a conversation were asked under, so a
   follow-up is refused when any of them, its anchor included, differs. */
function answeredLensIds(messages){
  return new Set(messages.filter(turn => turn.role === 'assistant' && turn.intelligence?.request_id && !turn.pending && !turn.error)
    .map(turn => typeof turn.clientLensId === 'string' ? turn.clientLensId : ''));
}

export function lensName(roster, clientLensId){
  if (!clientLensId) return GENERAL_LENS_LABEL;
  const entry = clientLensRoster(roster).find(item => item.clientLensId === clientLensId);
  return entry ? entry.label : clientLensId;
}

/* The one line that names the lens of the conversation on screen. */
function ThreadLens({roster, clientLensId}){
  return <p className="chat-thread-lens" data-thread-lens={clientLensId || 'general'}>{'Client lens: ' + lensName(roster, clientLensId)}</p>;
}

/* A follow-up takes the earlier answers as its context, and the service will
   not use an answer asked under one lens as the context of a question asked
   under another. The console says so before anything is sent. */
export function lensCrossingMessage(roster, threadLensId, pageLensId){
  const asked = threadLensId ? 'was asked under the client lens ' + lensName(roster, threadLensId) : 'was asked with no client lens';
  const back = threadLensId ? 'choose ' + lensName(roster, threadLensId) + ' again' : 'choose None again';
  const fresh = pageLensId ? 'under ' + lensName(roster, pageLensId) : 'with no client lens';
  return 'This conversation ' + asked + '. A follow-up keeps the lens of its conversation, so nothing was sent. To follow up here, ' + back + '. To ask ' + fresh + ', start a new question.';
}

/* Whether the reply at this index answers the first question of its thread,
   so no earlier conversation prose travelled with it. */
export function answersFirstQuestion(messages, index){
  return messages.slice(0, index).filter(turn => turn.role === 'user').length <= 1;
}

export function threadTitle(t){
  const first = (t.messages || []).find((m) => m.role === 'user');
  const text = first ? first.content : 'New conversation';
  return text.length > 46 ? text.slice(0, 45).trimEnd() + '…' : text;
}

export function timeAgo(ts){
  if (!ts) return '';
  const m = Math.max(1, Math.round((Date.now() - ts) / 60000));
  if (m < 60) return m + 'm ago';
  const h = Math.round(m / 60);
  if (h < 24) return h + 'h ago';
  return Math.round(h / 24) + 'd ago';
}

/* ===== inline style tokens, ported from the prototype console template ===== */

const railStyle = {
  border: '1px solid var(--line)', background: 'var(--surface)', overflow: 'hidden', position: 'sticky', top: '18px', alignSelf: 'start',
};
const railHeadStyle = { padding: '16px 18px', borderBottom: '1px solid var(--hairline)' };
const railHeadKickStyle = {
  fontFamily: 'var(--mono)', fontSize: '9.5px', letterSpacing: '0.16em',
  textTransform: 'uppercase', color: 'var(--accent-text)', fontWeight: 700,
};
const railHeadSubStyle = {
  fontFamily: 'var(--mono)', fontSize: '10px', color: 'var(--faint)', marginTop: '4px',
};
const railTitleStyle = {
  display: 'block', fontFamily: 'var(--sans)', fontSize: '13px', fontWeight: 600,
  color: 'var(--ink)', whiteSpace: 'nowrap', overflow: 'hidden',
  textOverflow: 'ellipsis', textAlign: 'left',
};
const railMetaStyle = {
  display: 'block', fontFamily: 'var(--mono)', fontSize: '9.5px', letterSpacing: '0.06em',
  textTransform: 'uppercase', color: 'var(--faint)', marginTop: '3px', textAlign: 'left',
};

/* Ask redesign, 23 Sept 2026: the question heads its answer and the answer sits
   on the page. The bubbles, the "You asked" and "42 analyst" kickers, the red
   left border and the frame around the answer are gone; what is left is the
   prose of a legacy text answer on the reading measure. */
const botBodyStyle = { padding: '0 0 16px' };
const botAnswerStyle = {
  fontFamily: 'var(--sans)', fontSize: 'var(--type-4)', lineHeight: 1.55, color: 'var(--ink)',
  margin: 0, maxWidth: '66ch',
};
const botParaStyle = { ...botAnswerStyle, margin: '0 0 12px' };
const botListStyle = { fontFamily: 'var(--sans)', margin: '0 0 8px', padding: 0, listStyle: 'none', maxWidth: '66ch' };
const botLiStyle = {
  display: 'flex', gap: '12px', marginBottom: '8px', fontSize: 'var(--type-4)', lineHeight: 1.55, color: 'var(--ink)',
};
const botLiGlyphStyle = { color: 'var(--muted)', flexShrink: 0 };

// Render **bold** spans without a markdown library: build React nodes from the
// parsed text, so model output can never inject HTML.
function renderInline(text){
  const out = [];
  const re = /\*\*(.+?)\*\*/g;
  let last = 0;
  let m;
  let k = 0;
  while ((m = re.exec(text))){
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(<strong key={'b' + k++}>{m[1]}</strong>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// Turn the analyst's answer into a scannable layout: a run of numbered or
// dashed lines becomes a list with a muted "›" glyph per item, and prose
// reads as plain paragraphs. Bold labels render throughout.
function AnswerBody({text}){
  const blocks = String(text || '').trim().split(/\n{2,}/);
  let headlineUsed = false;
  return blocks.map((block, bi) => {
    const lines = block.split(/\n/).map((l) => l.trim()).filter(Boolean);
    const listLike = lines.length > 1 && lines.every((l) => /^(\d+[.)]|[-•*])\s+/.test(l));
    if (listLike){
      const items = lines.map((l, i) => (
        <li key={i} style={botLiStyle}>
          <span style={botLiGlyphStyle} aria-hidden="true">&rsaquo;</span>
          <span>{renderInline(l.replace(/^(\d+[.)]|[-•*])\s+/, ''))}</span>
        </li>
      ));
      return <ul key={bi} style={botListStyle}>{items}</ul>;
    }
    if (!headlineUsed){
      headlineUsed = true;
      return <p key={bi} style={botParaStyle}>{renderInline(lines.join(' '))}</p>;
    }
    return <p key={bi} style={botParaStyle}>{renderInline(lines.join(' '))}</p>;
  });
}

const botErrStyle = { fontSize: 'var(--type-3)', lineHeight: 1.55, color: 'var(--down-text)', margin: 0 };

const sourcesBarStyle = {
  borderTop: '1px solid var(--hairline)',
  padding: '12px 0', display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap',
};
const sourcesLabStyle = {
  fontFamily: 'var(--sans)', fontSize: 'var(--type-3)', color: 'var(--muted)', marginRight: '4px',
};
const sourcePillStyle = {
  display: 'inline-flex', alignItems: 'center', gap: '8px', fontFamily: 'var(--sans)',
  fontSize: 'var(--type-3)', color: 'var(--ink)',
};
const sourcesBridgeBtnStyle = {
  marginLeft: 'auto', minHeight: '48px', padding: '0 16px', background: 'transparent', color: 'var(--ink)', border: '1px solid var(--muted)',
  font: '600 var(--type-3) var(--sans)', cursor: 'pointer', flexShrink: 0,
};

// Sources sit in a plain line under the answer. When the caller can bridge
// into a brief, "Build brief from this" renders inline at the line's right
// rather than as a second, separate control below the answer.
function Sources({sources, bridge}){
  const list = Array.isArray(sources) ? sources.filter(Boolean) : [];
  if (!list.length && !bridge) return null;
  return (
    <div style={sourcesBarStyle}>
      {list.length > 0 && <span style={sourcesLabStyle}>Sources</span>}
      {list.map((s, i) => {
        const base = typeof s === 'string' ? s : (s.label || s.name || s.source || s.title || s.tool || '');
        const label = base && s && typeof s === 'object' && s.market ? base + ' · ' + String(s.market).toUpperCase() : base;
        if (!label) return null;
        const url = s && typeof s === 'object' ? (s.url || s.href) : null;
        return url
          ? <a key={i} style={sourcePillStyle} href={url} target="_blank" rel="noreferrer">{label} <span style={{color: 'var(--accent-text)'}}>{'→'}</span></a>
          : <span key={i} style={sourcePillStyle}>{label}</span>;
      })}
      {bridge && (
        <button type="button" style={sourcesBridgeBtnStyle} onClick={bridge.onClick}>
          Build brief from this
        </button>
      )}
    </div>
  );
}

function summariseAnswer(text){
  const s = String(text || '').trim();
  if (!s) return '';
  return s.length > 220 ? s.slice(0, 219).trimEnd() + '…' : s;
}

/* The advisory delivery report for one answer turn, sent after React has
   committed it. A structured reply the client check withholds renders the
   withheld notice instead, which is reported as render_failed. */
export function useTurnDelivery(turn){
  const settled = turn?.role !== 'user' && turn?.pending === false;
  const eventType = !settled ? null
    : Object.hasOwn(turn, 'intelligence') && !validateIntelligenceReply(turn.intelligence, {terminalError: turn.error === true}).ok
      ? 'render_failed' : 'rendered';
  useEffect(() => {
    if (eventType) sendHeldDelivery(turn.turnId, eventType);
  }, [turn?.turnId, eventType]);
}

function Bubble({turn, prevQuestion, threadId, market, onBuildBrief, onRetry, onCheck, onResubmit, busy, coverage = null, coverageUnread = false, onRetryCoverage = null, onRephrase = null, firstQuestion = false, requestDetails = [], windowFromPlan = true}){
  useTurnDelivery(turn);
  if (turn.role === 'user'){
    return <h2 className="reveal chat-question">{turn.content}</h2>;
  }
  const structured = Object.hasOwn(turn, 'intelligence');
  const canBridge = !structured && !turn.pending && !turn.error && onBuildBrief && prevQuestion;
  const bridge = canBridge ? {
    onClick: () => onBuildBrief({
      threadId,
      question: String(prevQuestion).trim(),
      answerSummary: summariseAnswer(turn.content),
      market,
    }),
  } : null;
  /* A completed answer can become a cited brief from the actions row at its
     end, the one place the offer means something; it waits while a request
     runs, as the flow bar action it replaces did. */
  const briefFromAnswer = structured && onBuildBrief && !busy && !turn.pending && !turn.error && ['complete', 'partial'].includes(turn.intelligence?.status)
    ? () => onBuildBrief(null) : null;
  return (
    <div className={'reveal chat-answer' + (structured ? ' chat-structured-bubble' : '')}>
      <div className={structured ? 'chat-structured-body' : undefined} style={structured ? undefined : botBodyStyle}>
        {turn.pending
          ? <div className="chat-pending"><SignalLoader /><span className="chat-pending-label">{turn.rechecking ? 'Checking the existing request' : turn.job ? 'Waiting for the reply' : 'Submitting the question'}</span></div>
          : structured
            ? <GeneralIntelligence key={typeof turn.intelligence?.request_id === 'string' ? turn.intelligence.request_id : turn.turnId} intelligence={turn.intelligence} terminalError={turn.error} coverage={coverage} coverageUnread={coverageUnread} onRetryCoverage={onRetryCoverage} onRephrase={onRephrase} firstQuestion={firstQuestion} requestDetails={requestDetails} onBuildBrief={briefFromAnswer} windowFromPlan={windowFromPlan} />
          : turn.error
            ? <div><p style={botErrStyle}>{turn.content}</p>{turn.retryText && <button type="button" className="capacity-retry" onClick={() => onRetry(turn.retryText)}>Retry</button>}{!turn.retryText && turn.statusUncertain && !turn.job && turn.submission && onResubmit && <button type="button" className="capacity-retry" disabled={busy} onClick={() => onResubmit(threadId, turn.turnId)}>Retry</button>}</div>
            : <AnswerBody text={turn.content} />}
      </div>
      {turn.statusUncertain && typeof turn.job?.id === 'string' && <div style={botBodyStyle}><button type="button" className="capacity-retry" disabled={busy} onClick={() => onCheck(threadId, turn.turnId, turn.job)}>Check existing request</button></div>}
      {typeof turn.job?.id === 'string' && <details className="chat-run"><summary>Request reference</summary><code style={{overflowWrap: 'anywhere'}}>{turn.job.id}</code></details>}
      {!structured && !turn.pending && !turn.error && <Sources sources={turn.sources} bridge={bridge} />}
    </div>
  );
}

/* The question field: the one framed control on the Ask page. It carries its
   visible label, a hint on what a reply carries, the lens select when the
   page offers one, and Send as the single primary action. The first question
   and a follow-up use the same field under different labels. */
function Composer({label, placeholder = PLACEHOLDER, draft, setDraft, onSend, onStop, busy = false, blocked = false, lens = null, alert = null, fieldRef = null}){
  const id = useId();
  const onKey = (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent?.isComposing){ e.preventDefault(); onSend(draft); } };
  return (
    <div className="chat-composer reveal">
      <label className="chat-composer-label" htmlFor={id + '-field'}>{label}</label>
      <textarea
        id={id + '-field'}
        ref={fieldRef}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKey}
        placeholder={busy ? 'Answering…' : placeholder}
        rows={3}
        spellCheck="false"
        aria-describedby={id + '-hint'}
        className="workbench-ask-field"
      />
      <div className="chat-composer-foot">
        <p className="chat-composer-hint" id={id + '-hint'}>{COMPOSER_HINT}</p>
        {lens}
        <button type="button" onClick={() => busy ? onStop() : onSend(draft)} disabled={blocked} aria-label={busy ? 'Stop waiting' : 'Send'} className="workbench-ask-send">
          {busy ? 'Stop waiting' : 'Send'}
        </button>
      </div>
      {alert}
    </div>
  );
}

/* Questions offered under the field: a muted heading over a plain list of
   text buttons, one per line, with a hairline between them. */
function QuestionList({title, items, onPick}){
  const id = useId();
  if (!items.length) return null;
  return (
    <div className="chat-starters">
      <p className="chat-starters-title" id={id}>{title}</p>
      <ul className="chat-starters-list" aria-labelledby={id}>
        {items.map((item) => (
          <li key={item}><button type="button" onClick={() => onPick(item)} className="workbench-ask-suggestion">{item}</button></li>
        ))}
      </ul>
    </div>
  );
}

/* The lens binding a stored request was admitted under. The detail carries
   it only when the request named a lens; general 42 names none. */
const LENS_BINDING_KEYS = ['client_lens_id','configuration_digest','lens_binding_version'];
export function validStoredLens(lens){
  return !!lens && typeof lens === 'object' && !Array.isArray(lens)
    && Object.keys(lens).sort().join(',') === LENS_BINDING_KEYS.join(',')
    && lens.lens_binding_version === 'client_lens_binding_v1'
    && typeof lens.client_lens_id === 'string' && !!lens.client_lens_id
    && typeof lens.configuration_digest === 'string' && /^[0-9a-f]{64}$/.test(lens.configuration_digest);
}

export function validateQuestionDetail(value, requestId){
  const fields = ['contract_version','request_id','observed_state','question','history','selected_market','requested_window','response','window_from_plan','reserved_microusd','missing_work'];
  const lensed = !!value && typeof value === 'object' && Object.hasOwn(value, 'client_lens');
  if (lensed && !validStoredLens(value.client_lens)) return false;
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length !== fields.length + (lensed ? 1 : 0) || !fields.every(key=>Object.hasOwn(value,key))
    || value.contract_version !== 'general_question_detail_v1' || value.request_id !== requestId || !QUESTION_REQUEST_ID.test(requestId)
    || typeof value.question !== 'string' || !value.question.trim()
    || !Array.isArray(value.history) || value.history.some(turn=>!turn || Object.keys(turn).length!==2 || !['user','assistant'].includes(turn.role) || typeof turn.text!=='string')
    || ![null,'za','ng','ke'].includes(value.selected_market)
    || !Number.isSafeInteger(value.reserved_microusd) || value.reserved_microusd<0
    || !Array.isArray(value.missing_work) || new Set(value.missing_work).size!==value.missing_work.length || value.missing_work.some(code=>!['execution_unconfirmed','terminal_record_unavailable','plan_record_unavailable','record_invalid','read_limit_reached','read_deadline_reached'].includes(code))
    || !['unconfirmed','complete','partial','needs_clarification','unavailable','refused','held'].includes(value.observed_state)
    || typeof value.window_from_plan !== 'boolean') return false;
  if (value.requested_window !== null){
    const window = value.requested_window;
    if (!window || Object.keys(window).length!==2 || !['start','end'].every(key=>typeof window[key]==='string' && /^\d{4}-\d{2}-\d{2}$/.test(window[key]) && Number.isFinite(Date.parse(window[key])) && new Date(window[key]).toISOString().slice(0,10)===window[key]) || window.start>window.end) return false;
  }
  if (value.observed_state === 'unconfirmed') return value.response === null && value.window_from_plan === false;
  const response = value.response;
  return !!response && typeof response.answer==='string' && Array.isArray(response.sources)
    && response.intelligence?.request_id === requestId && validateIntelligenceReply(response.intelligence, {terminalError:response.error === true}).ok
    && (value.observed_state==='held' ? ['partial','unavailable'].includes(response.intelligence.status) : response.intelligence.status===value.observed_state)
    /* An answered reply always rests on a plan. */
    && (value.window_from_plan || !['complete','partial'].includes(response.intelligence.status));
}

const HELD_REASON={response_usage_unavailable:'the usage record for this answer is unavailable',metering_persistence_failed:'the metering record for this answer failed to persist'};
function heldSentence(value){
  const reason=HELD_REASON[value.response?.intelligence?.usage?.reason];
  return 'Held request. The stored answer and allowance remain held'+(reason?' because '+reason:' until the usage record is reconciled')+'.';
}

function ObservedQuestion({requestId, session, onAuth, embedded=false, onContinueQuestion, onNewQuestion, onBusyChange, onActiveAnswerChange, onBuildBrief, onStoredQuestion}){
  const [read, setRead] = useState({state:'loading',requestId});
  /* The covered dates, read as the live console reads them, so a stored
     refusal about dates says so when it is reopened from its address. */
  const [coverageLive, retryCoverage] = useApi(COVERAGE_PATH, session, onAuth);
  /* A re-read keeps the last settled answer on screen while it loads, so a
     retry after a failure does not flash the note away and back. */
  const settledCoverage = useRef(null);
  if (coverageLive.state === 'ready' || coverageLive.state === 'error') settledCoverage.current = coverageLive;
  const coverageRes = coverageLive.state === 'loading' && settledCoverage.current ? settledCoverage.current : coverageLive;
  const coverage = coverageRes.state === 'ready' ? askCoverage(coverageRes.data) : null;
  /* A failed read, or one the page cannot parse, is named as that. */
  const coverageUnread = coverageRes.state === 'error' || (coverageRes.state === 'ready' && !coverage);
  const [reload, setReload] = useState(0);
  const auth = useRef(onAuth); auth.current=onAuth;
  useEffect(()=>{
    let live=true;
    const controller=new AbortController();
    setRead({state:'loading',requestId});
    if (!QUESTION_REQUEST_ID.test(requestId)){ setRead({state:'error',requestId,message:'The request reference is invalid.'}); return ()=>{live=false;}; }
    const timer=setTimeout(()=>{controller.abort();if(live)setRead({state:'error',requestId,message:'The stored question could not be read within the allowed time.'});},25000);
    apiGetFresh('/api/internal/v2/fieldwork/question?contract_version=general_question_detail_v1&request_id='+encodeURIComponent(requestId),controller.signal)
      .then(value=>{if(!live||controller.signal.aborted)return;if(!validateQuestionDetail(value,requestId))throw Object.assign(new Error('Invalid stored request'),{code:'record_invalid',derived:true});setRead({state:'ready',requestId,value});})
      .catch(error=>{if(!live||controller.signal.aborted)return;if(error?.auth)auth.current?.();setRead({state:'error',requestId,code:failureCode(error),origin:error?.derived?'derived':failureOrigin(failureCode(error)),message:error?.auth?'Authentication is required to read this request.':error?.status===404?'This request is unavailable in this workspace.':'The stored question could not be verified.'});})
      .finally(()=>clearTimeout(timer));
    return ()=>{live=false;clearTimeout(timer);controller.abort();};
  },[requestId,session,reload]);
  const value=read.requestId===requestId && read.state==='ready' ? read.value : null;
  /* The lens is the stored request's own, read with the answer, never the one
     the page's address names. The roster only lends it a name. */
  const storedLensId=value?.client_lens?.client_lens_id||'';
  /* General 42 needs no name from the roster, so only a lensed answer reads it. */
  const [lensRes]=useApi('/api/chat/lenses',session,onAuth,Boolean(storedLensId));
  const lensRoster=lensRes.state==='ready'?lensRes.data:null;
  const turns=value ? [...value.history.map(turn=>({role:turn.role,content:turn.text})),{role:'user',content:value.question},...(value.response?[{role:'assistant',...terminalChatTurn(value.response),clientLensId:storedLensId}]:[])] : [];
  const eligible=value && ['complete','partial'].includes(value.observed_state) && Object.keys(chatContextReferences(turns)).length>0;
  useEffect(()=>{onBusyChange?.(false);},[onBusyChange]);
  useEffect(()=>{onActiveAnswerChange?.(!!eligible);},[eligible,onActiveAnswerChange]);
  const resolved=value?.response?.intelligence;
  /* The host lists the saved question it is showing among its recent
     questions, with the date the answer was recorded when there is one. */
  const storedQuestion=value?value.question:null;
  const storedAsOf=resolved?.as_of && Number.isFinite(Date.parse(resolved.as_of))?new Date(resolved.as_of).toISOString():null;
  useEffect(()=>{if(storedQuestion)onStoredQuestion?.({requestId,question:storedQuestion,asOf:storedAsOf});},[requestId,storedQuestion,storedAsOf,onStoredQuestion]);
  /* A read that fails leaves no saved question to list, so the host is told
     to clear the one it was holding. */
  const readFailed=read.requestId===requestId&&read.state==='error';
  useEffect(()=>{if(readFailed)onStoredQuestion?.(null);},[readFailed,onStoredQuestion]);
  const [draft,setDraft]=useState('');
  /* The follow-up is asked from the field under the answer. Sending it opens
     the conversation in the live console with this question's turns and asks
     it there, so the stored view never answers anything itself. */
  const continueQuestion=(question)=>{
    const text=String(question||'').trim();
    if(!eligible||!text)return;
    const market=value.selected_market||'all';
    const thread={id:newId(),ts:Date.now(),messages:turns,market};
    if(!saveThreads([thread,...loadThreads()])){setRead({...read,storageError:true});return;}
    if(onContinueQuestion)onContinueQuestion(thread.id,market.toUpperCase(),text,storedLensId);else go('/console?work=ask');
  };
  /* Ask redesign, 23 Sept 2026: the stored request's bookkeeping (its state,
     the requested and resolved scope and the allowance) moves into the Request
     and usage details disclosure at the foot of the answer. Nothing is lost;
     it is one click down rather than above the answer. An unconfirmed or held
     request still says so in plain words under its question. */
  const stateSentence=value?(value.observed_state==='unconfirmed'?'Execution unconfirmed. No validated terminal answer is available.':value.observed_state==='held'?heldSentence(value):''):'';
  const requestDetails=value?[
    ...(stateSentence?[]:['Stored response: '+value.observed_state+'.']),
    'Requested market: '+(value.selected_market?marketNames([value.selected_market]):'All markets')+'.',
    storedWindowText(value.requested_window, resolved?.window || null, {answered: ['complete', 'partial'].includes(resolved?.status), fromPlan: value.window_from_plan}),
    'Resolved market: '+(resolved?marketNames(resolved.resolved_scope.market_scope):'Unavailable')+'.',
    'Resolved window: '+(resolved?.window&&value.window_from_plan?coverageText(resolved.window):'Unavailable')+'.',
    'USD allowance: '+allowanceText(value.reserved_microusd)+'. This is a reserved allowance, not measured spend or vendor credits.',
  ]:[];
  const savedLine=stateSentence||('Saved answer'+(resolved?.as_of?', '+coverageDate(new Date(resolved.as_of).toISOString()):'')+'.');
  const lastQuestion=turns.map(turn=>turn.role).lastIndexOf('user');
  const lastAnswer=turns.map(turn=>turn.role).lastIndexOf('assistant');
  const answerCarriesDetails=lastAnswer>lastQuestion&&Object.hasOwn(turns[lastAnswer],'intelligence');
  /* A refused reply has no answer to follow up, so its offered rephrase opens
     as a fresh question in the live console, filled in and not sent. */
  const askNew=onNewQuestion&&value?(question=>onNewQuestion((value.selected_market||'all').toUpperCase(),question)):null;
  return <div className={'page chat'+(embedded?' chat-embedded':'')} data-screen-label="Stored question">
    <div className="wrap chat-desk">
      {/* Quiet register, 23 Sept 2026: inside the console the page head
          carries the one link back to Fieldwork, so the answer does not
          repeat it. */}
      {!embedded && <a href="#/fieldwork">Back to Fieldwork</a>}
      {/* Round three, 24 Sept 2026: a failed read states what happened and
          offers the reload; the origin label and code wait under a closed
          Details line, as on Fieldwork and Historical, for support. The
          reload is the app's secondary action, a 48px control that is not
          red, as the other retry controls are. */}
      {!value && (read.state==='error' ? <div role="alert"><p>{read.message}</p>{read.code && <details className="workspace-reason"><summary>Details</summary><p>{FAILURE_ORIGIN_LABEL[read.origin]} <code>{read.code}</code></p></details>}<button type="button" className="legacy-action" onClick={()=>setReload(n=>n+1)}>Reload stored request</button></div> : <p role="status">Reading the stored question.</p>)}
      {value && <>
        {turns.map((turn,index)=><div className="ask-turn-stack" key={index}><Bubble turn={turn} threadId={null} market={value.selected_market} busy={false} firstQuestion={answersFirstQuestion(turns,index)} requestDetails={index===lastAnswer&&answerCarriesDetails?requestDetails:[]} onBuildBrief={eligible&&index===lastAnswer?onBuildBrief:null} windowFromPlan={value.window_from_plan} coverage={coverage} coverageUnread={coverageUnread} onRetryCoverage={retryCoverage} onRephrase={index===lastAnswer?askNew:null} />{index===lastQuestion && <p className="chat-saved">{savedLine}</p>}</div>)}
        {!answerCarriesDetails && <details className="general-run chat-run"><summary>Request and usage details</summary>{requestDetails.map((line,index)=><p key={index}>{line}</p>)}</details>}
        <ThreadLens roster={lensRoster} clientLensId={storedLensId} />
        {read.storageError && <p role="alert">Conversation storage is unavailable. The follow-up was not opened.</p>}
        {eligible && <Composer label="Ask a follow-up" placeholder={FOLLOW_UP_PLACEHOLDER} draft={draft} setDraft={setDraft} onSend={continueQuestion} blocked={!draft.trim()} />}
      </>}
    </div>
  </div>;
}

export function ChatPage(props){
  return props.requestId !== undefined && props.requestId !== null
    ? <ObservedQuestion key={props.requestId} {...props} />
    : <LiveChatPage {...props} />;
}

function LiveChatPage({region, session, onAuth, initialQuery, embedded=false, onThreadsChange, onBusyChange, onBuildBrief, onActiveAnswerChange, initialThreadId, initialSend, onInitialSent, initialDraft, onInitialDraftUsed}){
  /* "all" is a real console desk now: the engine answers across ZA, NG and KE
     and keeps each market distinct. A single market grounds in that desk only. */
  const market = String(region || 'ZA').toLowerCase();
  /* The dock's short chips are the completed run's admitted signals for this
     market. They prefill the field rather than send; a run that admitted
     nothing shows no chips. */
  const [deskRes] = useApi('/api/desk?region=' + encodeURIComponent(market), session, onAuth);
  /* The client lens this console is asking under. General 42 is the default, the
     roster is the server's authorized list for this scope, and the chosen lens
     stays on the hash so a reload reopens under the same configuration. */
  const [lensRes] = useApi('/api/chat/lenses', session, onAuth);
  /* The dates a question can be answered for, as the server's worker registry
     states them. Until they are read, or when they cannot be, no date is named. */
  const [coverageRes] = useApi(COVERAGE_PATH, session, onAuth);
  const coverage = coverageRes.state === 'ready' ? askCoverage(coverageRes.data) : null;
  const lensRoster = lensRes.state === 'ready' ? lensRes.data : null;
  /* The route is the only source of the lens, subscribed rather than copied once,
     so a later hash change moves the console instead of leaving it bound to the
     lens it opened with. A selection that cannot be written to the route does not
     take effect either: the console never asks under a lens its own link denies. */
  const clientLensId = useSyncExternalStore(subscribeToRouteLens, routeLensId, routeLensId);
  /* A lens this console cannot yet resolve is never answered as general 42: the
     deep link names a configuration, so until the server says that name is
     authorized the ask is refused and the reason is shown. */
  const lensState = resolveClientLensState(lensRes.state, lensRoster, clientLensId);
  const activeLens = lensState.lens || selectedClientLens(lensRoster, '');
  const lensBlocked = lensState.state !== 'ready';
  const chooseLens = (next) => {
    const chosen = selectedClientLens(lensRoster, next).clientLensId;
    try { go(lensRoutePath(currentRoute(), chosen)); }
    catch (_error) { /* the route did not move, so neither does the lens */ }
  };
  const suggest = runSuggestions(deskRes.state === 'ready' ? deskRes.data : null, market, 4);
  const deskLabel = market === 'all' ? 'all markets' : market.toUpperCase() + ' desk';

  const [threads, setThreads] = useState(loadThreads);
  const threadsRef = useRef(threads);
  /* The console drives thread identity from outside: null means a FRESH ask
     (New ask), a string opens that saved thread (Recent asks). Undefined keeps
     the old resume-latest default for the standalone page. Without this, the
     embedded console always reopened the latest thread and there was no way to
     start clean (Jo's report: "New Ask always takes me to the same one"). */
  const [activeId, setActiveId] = useState(() => {
    if (initialThreadId !== undefined) return initialThreadId;
    const t = loadThreads()[0];
    return t ? t.id : null;
  });
  const [draft, setDraft] = useState(() => initialDraft || '');
  /* The draft fills the field on this mount only and is never sent; the host
     drops it so a later return to Ask opens without it. */
  const draftUsed = useRef(false);
  useEffect(() => {
    if (!initialDraft || draftUsed.current) return;
    draftUsed.current = true;
    onInitialDraftUsed?.();
  }, [initialDraft]); // eslint-disable-line react-hooks/exhaustive-deps
  /* Quiet register, 23 Sept 2026: an offered question fills the field and
     puts the reader in it, so a question only leaves, and only spends its
     allowance, on a deliberate Send. */
  const fieldRef = useRef(null);
  const pickQuestion = useCallback((text) => {
    setDraft(text);
    if (fieldRef.current) fieldRef.current.focus();
  }, []);
  const [busy, setBusy] = useState(false);
  const [storageError, setStorageError] = useState(false);
  const persist = useCallback(next => { setStorageError(!saveThreads(next)); }, []);

  /* a seed deep-link opens a fresh conversation with the question pre-filled, so
     it never lands on top of the last thread. No auto-send. */
  useEffect(() => {
    if (initialQuery && !initialQuery.includes('persona_id=')){ setActiveId(null); setDraft(initialQuery); }
  }, [initialQuery]);

  const ctrl = useRef(null);
  const scrollRef = useRef(null);
  /* one live reply at a time: a new send or an unmount aborts the in-flight
     poll so rapid sends never leave overlapping polls running. */
  const pausePendingTurns = useCallback(() => {
      const next = threadsRef.current.map((t) => ({
        ...t,
        messages: (t.messages || []).map(m => m.role === 'assistant' && m.pending ? pausedTurn(m) : m),
      }));
      threadsRef.current = next;
      setThreads(next);
      persist(next);
  }, [persist]);
  const abort = useCallback(() => {
    if (ctrl.current){ ctrl.current.abort(); ctrl.current = null; }
    pausePendingTurns();
    setBusy(false);
  }, [pausePendingTurns]);
  useEffect(() => abort, [abort]);

  const active = threads.find((t) => t.id === activeId) || null;
  const activeRef = useRef(activeId);
  activeRef.current = activeId;
  /* The address carries the request id of the reply on screen, so it can be
     shared and reopened from the server. It is replaced, not pushed: the live
     thread stays mounted, and Back still leaves the console. */
  const shareReply = (id, intelligence, lensId) => {
    const hash = embedded && id === activeRef.current && currentRoute().work === 'ask' ? shareableRequestHash(intelligence, lensId) : null;
    if (!hash || typeof window === 'undefined' || hash === window.location.hash) return;
    try { window.history.replaceState(window.history.state, '', hash); } catch (_error){ /* the address stays as it was */ }
  };
  const leaveSharedReply = () => {
    if (typeof window === 'undefined' || !currentRoute().requestId) return;
    try { window.history.replaceState(window.history.state, '', buildWorkbenchHash({work: 'ask'})); } catch (_error){ /* the address stays as it was */ }
  };
  const messages = active ? active.messages : [];
  /* The lens of the conversation on screen, and whether a follow-up here would
     be asked under another one. */
  const threadLensId = chatThreadLensId(messages);
  const lensCrossed = threadLensId !== null && !lensBlocked
    && [...answeredLensIds(messages)].some(id => id !== activeLens.clientLensId);
  useEffect(() => { onThreadsChange && onThreadsChange(threads); }, [onThreadsChange, threads]);
  useEffect(() => { onBusyChange && onBusyChange(busy); }, [busy, onBusyChange]);
  /* The ask control refuses an empty draft and a lens the console cannot resolve. */
  const askBlocked = askIsBlocked({busy, draft, lensBlocked: lensBlocked || lensCrossed});
  /* the "answer" step in the flow is about the ACTIVE thread only, not "any
     thread ever answered". An old answered thread must not make a fresh
     ask read as answered. */
  const activeAnswered = useMemo(() => messages.some(m => m.role === 'assistant' && !m.pending && !m.error && (!Object.hasOwn(m, 'intelligence') || (['complete', 'partial'].includes(m.intelligence?.status) && validateIntelligenceReply(m.intelligence).ok))), [messages]);
  useEffect(() => { onActiveAnswerChange && onActiveAnswerChange(activeAnswered); }, [activeAnswered, onActiveAnswerChange]);

  /* persist on every thread mutation and keep the view pinned to the latest turn */
  const commit = (next) => { threadsRef.current = next; setThreads(next); persist(next); };
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, busy]);

  const startNew = () => {
    abort();
    setBusy(false);
    setActiveId(null);
    setDraft('');
  };

  const openThread = (id) => {
    abort();
    setBusy(false);
    setActiveId(id);
  };

  const removeThread = (id) => {
    const next = threads.filter((t) => t.id !== id);
    commit(next);
    if (id === activeId){ abort(); setBusy(false); setActiveId(next[0] ? next[0].id : null); }
  };

  const updateTurn = (id, turnId, patch) => {
    const next = threadsRef.current.map(thread => thread.id !== id ? thread : {...thread, ts: Date.now(), messages: thread.messages.map(turn => turn.turnId === turnId ? {...turn, ...patch} : turn)});
    commit(next);
  };

  const failTurn = (id, turnId, error, retryText = '') => {
    updateTurn(id, turnId, {pending: false, rechecking: false, error: true,
      statusUncertain: ['submission_uncertain', 'reply_uncertain'].includes(error?.code) || error?.auth === true,
      content: error?.auth ? 'Authentication is required to check this request.' : error?.message || REPLY_UNCERTAIN,
      retryText: error?.code === 'capacity_busy' ? retryText : '',
      reason: typeof error?.code === 'string' ? error.code : null,
      httpStatus: Number.isInteger(error?.status) ? error.status : null,
    });
    if (error?.auth) onAuth?.();
    if (error?.code === 'capacity_busy') setDraft(retryText);
  };

  const followJob = async (id, turnId, job, controller, lensId) => {
    try {
      const response = await pollChat(job, controller.signal);
      holdDelivery(turnId, response);
      updateTurn(id, turnId, {...terminalChatTurn(response), rechecking: false});
      if (Object.hasOwn(response, 'intelligence')) shareReply(id, response.intelligence, lensId);
    } catch (error){
      if (!controller.signal.aborted && !error?.aborted) failTurn(id, turnId, error);
    } finally {
      if (ctrl.current === controller) { ctrl.current = null; setBusy(false); }
    }
  };

  const checkRequest = async (id, turnId, job) => {
    if (busy || ctrl.current) return;
    const controller = new AbortController();
    ctrl.current = controller;
    setBusy(true);
    updateTurn(id, turnId, {pending: true, rechecking: true, error: false, statusUncertain: false});
    const turn = (threadsRef.current.find(thread => thread.id === id)?.messages || []).find(item => item.turnId === turnId);
    await followJob(id, turnId, job, controller, typeof turn?.clientLensId === 'string' ? turn.clientLensId : routeLensId());
  };

  const send = async (raw) => {
    const text = String(raw || '').trim();
    if (!text || busy || ctrl.current || lensBlocked || lensCrossed) return;
    abort();
    leaveSharedReply();
    const c = new AbortController();
    ctrl.current = c;
    setBusy(true);
    setDraft('');
    const lensId = activeLens.clientLensId;

    /* find or open the target thread, append the user turn plus a pending bot
       turn, and snapshot the prior history for the API call */
    let id = activeId;
    let prior = [];
    let references = {};
    let base = threadsRef.current;
    if (!id || !base.some((t) => t.id === id)){
      id = newId();
      base = [{id, ts: Date.now(), messages: []}, ...base];
    } else {
      const earlier = base.find((t) => t.id === id).messages || [];
      references = chatContextReferences(earlier);
      prior = earlier
        .filter((m) => !m.pending && !m.error)
        .map((m) => ({role: m.role, content: Object.hasOwn(m, 'intelligence') ? intelligenceHistoryText(m.intelligence) : m.content}))
        .filter(m => typeof m.content === 'string');
    }
    setActiveId(id);
    const turnId = newId();
    /* Exactly what was sent, so a Retry resends the same body under the same key. */
    const submission = {text, history: prior, market, references, clientLensId: lensId, idempotencyKey: requestKey()};
    const withUser = base.map((t) => t.id === id
      ? {...t, ts: Date.now(), messages: [...t.messages, {role: 'user', content: text}, {role: 'assistant', turnId, pending: true, content: '', clientLensId: lensId, submission}]}
      : t);
    commit(withUser);

    await submitTurn(id, turnId, submission, c);
  };

  const submitTurn = async (id, turnId, submission, c) => {
    try {
      const job = await submitChat(submission.text, submission.history, submission.market, c.signal, submission.references, submission.clientLensId, submission.idempotencyKey);
      /* Once the service names the job, the job is what a later check reads, so
         the resend record is dropped. */
      updateTurn(id, turnId, {job, submission: undefined, ...(c.signal.aborted ? {pending: false, error: true, statusUncertain: true, content: REPLY_UNCERTAIN} : {})});
      if (!c.signal.aborted) await followJob(id, turnId, job, c, submission.clientLensId);
    } catch (e){
      if (!c.signal.aborted && !e?.aborted) failTurn(id, turnId, e, submission.text);
      if (ctrl.current === c) { ctrl.current = null; setBusy(false); }
    }
  };

  /* Retry after an unconfirmed submission: the same body under the same key,
     into the same turn, so the server either joins the request it already
     admitted or admits it now, and never admits a second one. */
  const resubmit = async (id, turnId) => {
    if (busy || ctrl.current) return;
    const turn = (threadsRef.current.find(thread => thread.id === id)?.messages || []).find(item => item.turnId === turnId);
    const submission = turn?.submission;
    if (!submission || turn.job || typeof submission.idempotencyKey !== 'string') return;
    const c = new AbortController();
    ctrl.current = c;
    setBusy(true);
    updateTurn(id, turnId, {pending: true, error: false, statusUncertain: false, content: ''});
    await submitTurn(id, turnId, submission, c);
  };

  /* A follow-up asked from a stored answer arrives with its question: the
     console opens the continued thread and asks it once, as soon as the lens
     the route names can be resolved. The host is told it was asked, so it can
     drop the question and a later remount on this thread (leaving Ask and
     coming back, or Back then Forward) opens the thread without asking it
     again. */
  const initialSent = useRef(false);
  useEffect(() => {
    if (!initialSend || initialSent.current || lensBlocked) return;
    initialSent.current = true;
    send(initialSend);
    onInitialSent?.();
  }, [initialSend, lensBlocked]); // eslint-disable-line react-hooks/exhaustive-deps

  const empty = !messages.length;

  return (
    <div className={'page chat' + (embedded ? ' chat-embedded' : '')} data-screen-label="Intelligence Console · ask">
      {storageError && <p role="alert">Conversation storage is unavailable. Keep this tab open while the request runs.</p>}
      <div className="wrap" style={{padding: '0 36px'}}>
        {/* The rail and the question field take the page's entrance reveal; the
            rest of the desk does not, so it is painted from the first frame and
            counts as the page's largest paint, rather than a late line. */}
        <div className="m-stack" style={{display: 'grid', gridTemplateColumns: embedded ? '1fr' : '240px 1fr', gap: '18px', alignItems: 'start', minHeight: 0, flex: 1}}>

          {!embedded && <aside className="console-rail reveal" style={railStyle}>
            <div style={railHeadStyle}>
              <div style={railHeadKickStyle}>Intelligence console</div>
              <div style={railHeadSubStyle}>questions · sources · {deskLabel}</div>
            </div>
            <div style={{padding: '8px'}}>
              <button
                onClick={() => go('/research')}
                style={{
                  display: 'block', width: '100%', textAlign: 'left',
                  background: 'none',
                  border: 'none', padding: '10px 12px', marginBottom: '6px',
                }}
              >
                <span style={railTitleStyle}>Market Research</span>
                <span style={railMetaStyle}>Persona docs · cited · export</span>
              </button>
              <button
                onClick={startNew}
                style={{
                  display: 'block', width: '100%', textAlign: 'left',
                  background: activeId === null ? 'var(--accent-soft)' : 'none',
                  border: 'none', padding: '10px 12px', marginBottom: '2px',
                  boxShadow: activeId === null ? 'inset 2px 0 0 var(--accent)' : 'none',
                }}
              >
                <span style={railTitleStyle}>New conversation</span>
                <span style={railMetaStyle}>Start a fresh ask</span>
              </button>
              {threads.map((t) => {
                const on = t.id === activeId;
                return (
                  <div key={t.id} style={{position: 'relative'}}>
                    <button
                      onClick={() => openThread(t.id)}
                      style={{
                        display: 'block', width: '100%', textAlign: 'left',
                        background: on ? 'var(--accent-soft)' : 'none',
                        border: 'none', padding: '10px 30px 10px 12px', marginBottom: '2px',
                        boxShadow: on ? 'inset 2px 0 0 var(--accent)' : 'none',
                      }}
                    >
                      <span style={railTitleStyle}>{threadTitle(t)}</span>
                      <span style={railMetaStyle}>{(t.messages || []).filter((m) => m.role === 'user').length} asked · {timeAgo(t.ts)}</span>
                    </button>
                    <button
                      onClick={() => removeThread(t.id)}
                      aria-label="Delete conversation"
                      style={{
                        position: 'absolute', top: '8px', right: '6px', width: '22px', height: '22px',
                        display: 'grid', placeItems: 'center', color: 'var(--faint)', background: 'none', border: 'none',
                      }}
                    >
                      <Icon.close style={{width: 13, height: 13}} />
                    </button>
                  </div>
                );
              })}
            </div>
          </aside>}

          <div className="chat-desk">
            {/* Ask redesign, 23 Sept 2026: no hero. An empty page opens on the
                question field, top left, with the starter questions under it;
                an answered page reads question, answer, then the same field
                for a follow-up. */}
            <div ref={scrollRef} className="chat-turns">
              {messages.map((m, i) => {
                let prevQuestion = '';
                if (m.role === 'assistant' && !m.pending && !m.error){
                  for (let j = i - 1; j >= 0; j--){
                    const prior = messages[j];
                    if (prior && prior.role === 'user'){ prevQuestion = prior.content; break; }
                  }
                }
                return (
                  <div key={i} className="ask-turn-stack">
                    <Bubble
                      turn={m}
                      prevQuestion={prevQuestion}
                      threadId={activeId}
                      market={market}
                      onBuildBrief={embedded ? onBuildBrief : null}
                      onRetry={send}
                      onCheck={checkRequest}
                      onResubmit={resubmit}
                      busy={busy}
                      coverage={coverage}
                      onRephrase={setDraft}
                      firstQuestion={answersFirstQuestion(messages, i)}
                    />
                  </div>
                );
              })}
            </div>

            {threadLensId !== null && <ThreadLens roster={lensRoster} clientLensId={threadLensId} />}
            <Composer
              label={empty ? 'Your question' : 'Ask a follow-up'}
              placeholder={empty ? PLACEHOLDER : FOLLOW_UP_PLACEHOLDER}
              draft={draft}
              setDraft={setDraft}
              fieldRef={fieldRef}
              onSend={send}
              onStop={abort}
              busy={busy}
              blocked={askBlocked}
              lens={<ClientLensSelector roster={lensRoster} value={activeLens.clientLensId} onChange={chooseLens} />}
              alert={lensBlocked ? (
                <p role="alert" className="workbench-lens-refusal" data-client-lens-state={lensState.state}>
                  {LENS_STATE_MESSAGE[lensState.state]}
                </p>
              ) : lensCrossed && (
                <p role="alert" className="workbench-lens-refusal" data-thread-lens-crossing="">
                  {lensCrossingMessage(lensRoster, threadLensId, activeLens.clientLensId)}
                </p>
              )}
            />
            {empty && <QuestionList title={suggest.length ? 'From the latest signals' : 'Try one of these'} items={starterQuestions(market, suggest)} onPick={pickQuestion} />}
            {!empty && <QuestionList title="From the latest signals" items={suggest} onPick={pickQuestion} />}
          </div>

        </div>
      </div>
    </div>
  );
}
