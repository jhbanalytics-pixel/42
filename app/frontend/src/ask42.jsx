/* 42 Ask: a question, the research as it happens, then the answer. The
   finished record is read back and its answer checked against the shared
   answer contract (answerContract.js, copied from core/eval) before any of it
   is shown; an answer that fails is refused with its reasons. */
import {leadSentence} from './nightdesk.js';
import {useEffect, useId, useRef, useState} from 'react';
import './styles/ask42.css';
import {readerFigure} from './api.js';
import {plainUnit, unitFor} from './readerUnits.js';
import {validateAnswer} from './answerContract.js';
import {createDossier, fetchDiscover, saveFinding} from './api42.js';
import {downloadExport, getAsk, startAsk, stopAsk, streamAsk} from './askTransport42.js';
import {go} from './router.js';
import {marketToSend} from './askMarkets.js';
import {costWords, itemsWords} from './costWords.js';
import {EvidenceChip, monthName} from './ui/EvidenceChip.jsx';
import {PostStrip} from './ui/PostStrip.jsx';
import {RankedAnswer} from './RankedAnswer.jsx';
import {EntityLists} from './EntityLists.jsx';
import {ResearchLog, inMarket} from './ui/ResearchLog.jsx';
import {SourcePanel} from './ui/SourcePanel.jsx';
import {CostConfirm} from './ui/SpikeConfirm.jsx';
import {SkillForms, t2ReadyFrom} from './skills42.jsx';
import {platformLabel} from './ui/PlatformGlyph.jsx';
import {platformWord} from './ui/TrendCard.jsx';
import {BarList} from './ui/Charts42.jsx';
import {safeUrl} from './safeUrl.js';
import {consumeAsk} from './askConsent.js';

const MARKETS = [
  {code: 'ZA', name: 'South Africa'},
  {code: 'NG', name: 'Nigeria'},
  {code: 'KE', name: 'Kenya'},
];
const MARKET_NAME = Object.fromEntries(MARKETS.map((m) => [m.code, m.name]));

/* The shared contract covers the evidence media fields, so the answer is
   checked exactly as it arrives. */
export const checkAnswer = validateAnswer;

const CONFIDENCE = {
  observed: {word: 'Observed', shape: '●'},
  corroborated: {word: 'Corroborated', shape: '■'},
  single_source: {word: 'Single source', shape: '▲'},
  inferred: {word: 'Inferred', shape: '○'},
};

/* The most an ask may spend at each tier (AGENT.md). With no tier the agent
   picks T0 or T1, so the higher ceiling is the one to name. */
const CEILING = {T0: 10, T1: 60};
/* The tier in words, as the skill confirms name it. */
const TIER_WORDS = {T0: 'Quick lookup', T1: 'Quick scan', T2: 'Deep read'};

const CHECK_WORD = {checking: 'Checking', verified: 'Verified', downgraded: 'Downgraded', cut: 'Cut'};

const ANSWER_STATUS = {
  partial: 'Partial answer: part of the picture is missing',
  insufficient_evidence: 'Not enough evidence for a full answer',
  refused: '42 did not answer this question',
};

/* Visual QA, 5 October 2026: an answer the model budget stopped was headed
   "Not enough evidence", which blames the evidence for a spending stop. The
   stop's own gap (core/agent/ask.py _stopped_answer) names why it stopped,
   so the heading says that instead. */
const BUDGET_STOP = 'model cost or usage could not be verified within the per-question budget';
export function answerStatusWords(answer){
  const gaps = Array.isArray(answer && answer.gaps) ? answer.gaps : [];
  if (answer && answer.status === 'insufficient_evidence'){
    if (gaps.some((gap) => gap && gap.why === BUDGET_STOP)) return "Stopped at this question's model budget, not for lack of evidence";
    if (gaps.some((gap) => gap && gap.why === 'stopped on request')) return 'Stopped before an answer was written';
  }
  return ANSWER_STATUS[answer && answer.status] || null;
}

/* A partial answer can arrive with no short answer at all (the one-line
   summary failed a check). The space says what did pass instead of standing
   empty or only saying what did not (core/api/export.py shortAnswerFallback
   writes the same words). */
export function noShortAnswer(answer){
  const claims = Array.isArray(answer && answer.claims) ? answer.claims : [];
  if (claims.length === 0) return 'Nothing passed the checks to sum up.';
  const evidence = Array.isArray(answer.evidence) ? answer.evidence : [];
  const platforms = [...new Set(evidence.map((item) => platformLabel(item && item.platform) || (item && item.platform)).filter(Boolean))];
  const found = readerFigure(claims.length) + (claims.length === 1 ? ' checked finding' : ' checked findings');
  const from = evidence.length ? ' from ' + readerFigure(evidence.length) + (evidence.length === 1 ? ' post' : ' posts') + (platforms.length ? ' on ' + listWords(platforms) : '') : '';
  return found + from + (claims.length === 1 ? ' is' : ' are') + ' below. The one-line summary did not pass the checks.';
}

const WHY = {
  empty: 'nothing found',
  partial: 'only partly read',
  rate_limited: 'rate limited',
  auth_failed: 'access failed',
  schema_drift: 'the source changed shape',
  not_in_replay: 'not in the stored posts',
  ok: 'read',
};
const why = (value) => WHY[value] || String(value || '').replace(/_/g, ' ');
/* A gap's why is either source status codes ("empty, rate_limited"), read
   as words in one list, or a sentence, which stays whole: splitting a
   sentence at its commas and joining the parts with "and" wrote "market
   and and text" (live review, 5 October 2026). */
const listWords = (words) => (words.length > 1 ? words.slice(0, -1).join(', ') + ' and ' + words[words.length - 1] : words[0] || '');
export function whyAll(value){
  const text = String(value || '').trim();
  const parts = text.split(/,\s*/).filter(Boolean);
  if (parts.length > 1 && parts.every((part) => /^[a-z_]+$/.test(part))) return listWords([...new Set(parts.map(why))]);
  return why(text);
}

/* What a gap searched, in reader words: the leading route
   ("instagram/hashtag") becomes its platform, a search term after it reads
   "for <term>", and an empty search's ": 0 found" is dropped, since the gap
   itself says nothing was found. */
/* Last line of defence for gap text written by code or by the model: a
   check's rule code, an "item 0" index or a warehouse table or column name
   (snake_case, not a hashtag or handle) never reaches the reader. */
const INTERNAL_NAME = /(^|[^#@\w])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b/g;
export function plainGapWhat(value){
  return String(value || '')
    .replace(/\s*\((?:K|G)\d{1,2}\)/g, '')
    .replace(/\bwatch_next item \d+ removed\b/gi, 'A watch-next line was removed')
    .replace(/\bitem \d+\b/gi, 'one line')
    .replace(INTERNAL_NAME, (all, lead, name) => lead + name.replace(/_/g, ' '))
    .trim();
}

function searchedWords(gap){
  const raw = String(gap.searched || '');
  INTERNAL_NAME.lastIndex = 0;
  if (!/^[a-z0-9_]+\/[a-z0-9_/]+/i.test(raw) && INTERNAL_NAME.test(raw)) return 'the stored posts and trend records';
  const text = raw.replace(/\s*\((?:K|G)\d{1,2}\)/g, '');
  const route = /^([a-z0-9_]+)\/[a-z0-9_/]+(?:\s+([^,:]+?))?(?=,|:|$)(.*)$/i.exec(text);
  let words = text;
  if (route){
    const platform = platformLabel(route[1].toLowerCase()) || sentenceWord(route[1]);
    words = platform + (route[2] ? ' for ' + route[2].trim() : '') + route[3];
  }
  if (gap.why === 'empty') words = words.replace(/:\s*0 found$/, '');
  return words;
}
const sentenceWord = (value) => String(value).charAt(0).toUpperCase() + String(value).slice(1).replace(/_/g, ' ');

function marketCode(value){
  const code = String(value || '').toUpperCase();
  return MARKET_NAME[code] ? code : '';
}

/* Plain questions that work on any day, one per market. */
const EXAMPLES = [
  {text: 'Which sounds are rising on TikTok in South Africa this week?', market: 'ZA'},
  {text: 'How are people in Nigeria talking about food prices right now?', market: 'NG'},
  {text: 'Which creators are driving the football conversation in Kenya?', market: 'KE'},
];
const STARTER_LIMIT = 4;

/* Starting points for an empty Ask: the questions for the trends 42 is
   following in the chosen market, then the examples. Choosing one fills in
   the question and asks nothing: asking spends credits, so the reader still
   presses Ask. A trend list that cannot load leaves the examples alone. */
function AskStarters({market, onPick}){
  const [items, setItems] = useState([]);
  const headingId = useId();
  useEffect(() => {
    const ctrl = new AbortController();
    setItems([]);
    fetchDiscover({market, limit: STARTER_LIMIT}, {signal: ctrl.signal})
      .then((data) => {
        if (ctrl.signal.aborted) return;
        const list = data && Array.isArray(data.items) ? data.items : [];
        setItems(list.filter((item) => item && typeof item.ask === 'string' && item.ask.trim()).slice(0, STARTER_LIMIT));
      })
      .catch(() => {});
    return () => ctrl.abort();
  }, [market]);
  const marketName = MARKET_NAME[market] || 'this market';
  return (
    <section className="ask42-starters" aria-labelledby={headingId} data-ask-starters="">
      <h2 className="ask42-starters-title" id={headingId}>{items.length ? 'Start from a trend' : 'Start from an example'}</h2>
      <p className="ask42-starters-note">
        {items.length ? 'Trends 42 is following in ' + marketName + '. ' : ''}Choosing one fills in the question. Nothing is asked until you press Ask.
      </p>
      {items.length > 0 && (
        <ul className="ask42-starter-list">
          {items.map((item) => (
            <li key={item.item_id || item.ask}>
              <button type="button" className="ask42-starter" data-ask-starter="trend" onClick={() => onPick(item.ask, marketCode(item.market) || market)}>
                <span className="ask42-starter-text">{item.ask}</span>
                {item.state_word && <span className="ask42-starter-meta">{item.state_word}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
      {items.length > 0 && <h3 className="ask42-starters-subtitle">Or try</h3>}
      <ul className="ask42-starter-list">
        {EXAMPLES.map((example) => (
          <li key={example.text}>
            <button type="button" className="ask42-starter" data-ask-starter="example" onClick={() => onPick(example.text, example.market)}>
              <span className="ask42-starter-text">{example.text}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/* The question field starts at three lines and grows with what is typed.
   Browsers that size a field to its content do it in CSS; elsewhere the
   field takes its scroll height on each change, up to the CSS max-height. */
function fitField(field){
  if (!field || typeof CSS === 'undefined' || !CSS.supports || CSS.supports('field-sizing', 'content')) return;
  field.style.height = 'auto';
  field.style.height = field.scrollHeight + 2 + 'px';
}

/* Under an empty composer, what an answer holds, so the reader knows what a
   question buys before spending credits on it. */
const RETURNS = [
  ['A short answer first', 'Two or three sentences on what is happening, then each point on its own line.'],
  ['The posts behind every point', 'Each claim names the posts it rests on and how sure 42 is, from corroborated (different people on two platforms, or three unrelated people behind a counted figure) to inferred.'],
  ['What it could not find', 'Platforms with no posts in the window and anything 42 could not check are listed, not left out.'],
];
function AskReturns(){
  const headingId = useId();
  return (
    <section className="ask42-returns" aria-labelledby={headingId}>
      <h2 className="ask42-returns-title" id={headingId}>What you get back</h2>
      <dl className="ask42-returns-list">
        {RETURNS.map(([term, detail]) => (
          <div key={term}><dt>{term}</dt><dd>{detail}</dd></div>
        ))}
      </dl>
    </section>
  );
}

function windowWords(window){
  const from = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(window && window.from || ''));
  const to = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(window && window.to || ''));
  if (!from || !to) return '';
  const [, y1, m1, d1] = from;
  const [, y2, m2, d2] = to;
  const day = (d, m) => Number(d) + ' ' + monthName(Number(m) - 1);
  if (y1 === y2 && m1 === m2) return Number(d1) + ' to ' + day(d2, m2) + ' ' + y2;
  if (y1 === y2) return day(d1, m1) + ' to ' + day(d2, m2) + ' ' + y2;
  return day(d1, m1) + ' ' + y1 + ' to ' + day(d2, m2) + ' ' + y2;
}

/* A figure in the reader's view. Its query id is for support, so it rides
   on the figure as data instead of beside the number. */
const figureText = (number) => readerFigure(number.value) + ' ' + unitFor(number.value, plainUnit(number.unit)) + '.';

function AnswerFigure({number}){
  if (!number || typeof number.value !== 'number') return null;
  return <p className="ask42-figure" data-query-id={number.query_id || undefined}><span className="ask42-figure-text"><span className="ask42-figure-value">{readerFigure(number.value)}</span>{figureText(number).slice(readerFigure(number.value).length)}</span></p>;
}

/* The figures of one claim, then the claim they count, once, so "18 posts"
   never stands alone with no subject (Albert, 4 October 2026). */
function FigureGroup({numbers, about}){
  const shown = numbers.filter((number) => number && typeof number.value === 'number');
  if (shown.length === 0) return null;
  return (
    <div className="ask42-figure-group">
      {shown.map((number, index) => <AnswerFigure key={index} number={number} />)}
      {about && <p className="ask42-figure-about" title={about}>{about}</p>}
    </div>
  );
}

/* The short answer leads with its first sentence in weight, so a reader
   who stops there has the answer (styles/nightdesk.css). Same words. */
function ShortAnswer({text}){
  const {lead, rest} = leadSentence(text);
  if (!rest) return text;
  return <><span className="ask42-short-lead">{lead}</span>{rest}</>;
}

function metaLine(record){
  const run = record.run || {};
  const parts = [];
  if (MARKET_NAME[record.market]) parts.push(MARKET_NAME[record.market]);
  const span = windowWords(run.window);
  if (span) parts.push('posts from ' + span);
  const store = run.store || {};
  if (typeof store.posts === 'number' && typeof store.creators === 'number'){
    // Albert, 4 October: the line counts the whole store the answer drew on; the posts read are its sample.
    let line = readerFigure(store.posts) + (store.posts === 1 ? ' post' : ' posts') + ' by '
      + readerFigure(store.creators) + (store.creators === 1 ? ' creator' : ' creators');
    if (typeof store.platforms === 'number') line += ' on ' + store.platforms + (store.platforms === 1 ? ' platform' : ' platforms');
    parts.push(line + ' in the store');
    if (typeof run.posts === 'number') parts.push(readerFigure(run.posts) + (run.posts === 1 ? ' post read' : ' posts read'));
  } else if (typeof run.posts === 'number'){
    let line = readerFigure(run.posts) + (run.posts === 1 ? ' post' : ' posts');
    if (typeof run.platforms === 'number') line += ' from ' + run.platforms + (run.platforms === 1 ? ' platform' : ' platforms');
    parts.push(line);
  }
  return parts.join(' · ');
}

const EMPTY_RUN = {phase: 'idle', request: null, askId: null, steps: [], evidence: [], claims: [], record: null, error: null, stopping: false};

/* The phase of a running ask, read only from the kind of the latest step the
   stream has sent. A note is a side remark and a kind this map does not know
   is left out, so the phase is never guessed. */
const PHASE_OF_KIND = {
  plan: 'Planning',
  search: 'Reading posts',
  found: 'Reading posts',
  read: 'Reading posts',
  transcribe: 'Reading posts',
  write: 'Writing the answer',
  check: 'Checking claims',
  critic: 'Reviewing claims',
};

export function phaseOf(steps){
  const list = Array.isArray(steps) ? steps : [];
  for (let index = list.length - 1; index >= 0; index -= 1){
    const phase = list[index] && PHASE_OF_KIND[list[index].kind];
    if (phase) return phase;
  }
  return '';
}

export function elapsedWords(seconds){
  const whole = Math.max(0, Math.floor(Number(seconds) || 0));
  return whole < 60 ? whole + ' s' : Math.floor(whole / 60) + ' min ' + (whole % 60) + ' s';
}

function applyEvent(run, event){
  const data = event.data || {};
  if (event.type === 'step'){
    if (run.steps.some((step) => step.seq === data.seq)) return run;
    return {...run, steps: [...run.steps, data]};
  }
  if (event.type === 'evidence' && data.evidence){
    if (run.evidence.some((record) => record.id === data.evidence.id)) return run;
    return {...run, evidence: [...run.evidence, data.evidence]};
  }
  if (event.type === 'claim' && data.claim){
    const entry = {claim: data.claim, check: data.check, reason: data.reason || null};
    const at = run.claims.findIndex((item) => item.claim.id === data.claim.id);
    const claims = at === -1 ? [...run.claims, entry] : run.claims.map((item, index) => (index === at ? entry : item));
    return {...run, claims};
  }
  return run;
}

function quotesFor(answer, evidenceId){
  const quotes = [];
  for (const claim of answer.claims || []){
    for (const quote of claim.quotes || []){
      if (quote.evidence_id === evidenceId) quotes.push(quote.text);
    }
  }
  return quotes;
}

function ClaimSources({claim, records, answer, pinnedId, onPin}){
  const [expanded, setExpanded] = useState(false);
  const cited = (claim.evidence_ids || []).map((id) => records.get(id)).filter(Boolean);
  if (!cited.length) return null;
  const more = cited.length - 1;
  return (
    <span className="ask42-chips">
      {cited.slice(0, 1).map((record) => (
        <EvidenceChip key={record.id} evidence={record} quotes={quotesFor(answer, record.id)} pinned={pinnedId === record.id} onPin={onPin} />
      ))}
      {more > 0 && (
        <button type="button" className="ask42-chip ask42-chip-more" aria-expanded={expanded} aria-label={expanded ? 'Show fewer sources' : 'Show ' + more + ' more ' + (more === 1 ? 'source' : 'sources')} onClick={() => setExpanded((value) => !value)}>
          {expanded ? 'Fewer' : 'and ' + more + ' more'}
        </button>
      )}
      {expanded && cited.slice(1).map((record) => (
        <EvidenceChip key={record.id} evidence={record} quotes={quotesFor(answer, record.id)} pinned={pinnedId === record.id} onPin={onPin} />
      ))}
    </span>
  );
}

function Answer({record, onFollowup, onFailure, tail = null, followAction}){
  const answer = record.answer;
  const askId = record.ask_id;
  const shortId = useId();
  const nextId = useId();
  const run = record.run || {};
  const [pinnedId, setPinnedId] = useState(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState('');
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState('');
  const [findingSave, setFindingSave] = useState({askId, phase: 'idle', error: ''});
  const findingSaveRequest = useRef(null);
  const findingSaveState = findingSave.askId === askId ? findingSave : {phase: 'idle', error: ''};
  const canSaveFinding = Boolean(askId && record.status === 'complete' && record.skin_id == null && record.investigation_id == null && record.parent_id == null && ['complete', 'partial'].includes(answer.status) && Array.isArray(answer.claims) && answer.claims.length > 0);
  const records = new Map((answer.evidence || []).map((item) => [item.id, item]));
  /* Up to four figures, kept with the claim each one counts. */
  const figures = [];
  let figureCount = 0;
  for (const claim of answer.claims || []){
    const numbers = (claim.numbers || []).slice(0, Math.max(0, 4 - figureCount));
    if (numbers.length === 0) continue;
    figureCount += numbers.length;
    figures.push({numbers, about: String(claim.text || '').trim()});
  }
  const notices = Array.isArray(run.notices) ? run.notices : [];
  const followups = Array.isArray(run.followups) ? run.followups.slice(0, 3) : [];

  useEffect(() => {
    findingSaveRequest.current = null;
    setFindingSave({askId, phase: 'idle', error: ''});
    return () => { findingSaveRequest.current = null; };
  }, [askId]);

  async function exportAnswer(){
    setExporting(true);
    setExportError('');
    try { await downloadExport(record.ask_id); }
    catch (error){
      setExportError(error && error.message ? error.message : 'The export failed.');
      if (onFailure) onFailure(error);
    }
    finally { setExporting(false); }
  }

  /* The draft assembles itself on the server from this answer; the page
     then opens it for review. */
  async function addToDossier(){
    setAdding(true);
    setAddError('');
    try {
      const made = await createDossier(record.ask_id);
      go('/dossiers/' + encodeURIComponent(made.dossier_id));
    } catch (error){
      setAddError(error && error.message ? error.message : 'The dossier could not be started.');
      if (onFailure) onFailure(error);
    } finally { setAdding(false); }
  }

  async function saveAnswerToFindings(){
    if (!canSaveFinding || (findingSaveRequest.current && findingSaveRequest.current.askId === askId && findingSaveRequest.current.phase === 'pending')) return;
    const request = {askId, phase: 'pending'};
    findingSaveRequest.current = request;
    setFindingSave({askId, phase: 'pending', error: ''});
    try {
      const result = await saveFinding(askId);
      if (findingSaveRequest.current !== request) return;
      if (!result || typeof result.finding_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(result.finding_id) || result.source_ask_id !== askId){
        throw new Error('The Finding save could not be verified.');
      }
      request.phase = 'saved';
      setFindingSave({askId, phase: 'saved', error: ''});
    } catch (error){
      if (findingSaveRequest.current !== request) return;
      findingSaveRequest.current = null;
      setFindingSave({askId, phase: 'error', error: error && error.message ? error.message : 'The Finding could not be saved.'});
      if (onFailure) onFailure(error);
    }
  }

  /* A quiet hand-over to the Schedules form (contract section 14.2), filled
     with this question and its market. */
  function askEveryMonday(){
    const query = new URLSearchParams({q: record.question || ''});
    if (MARKET_NAME[record.market]) query.set('market', record.market);
    go('/schedules?' + query.toString());
  }

  const pinned = pinnedId ? records.get(pinnedId) : null;
  return (
    <div className="ask42-answer-layout">
      <article className="ask42-answer" aria-describedby={shortId}>
        {notices.length > 0 && (
          <ul className="ask42-notices" aria-label="Notices">
            {notices.map((notice, index) => <li key={index}>{notice}</li>)}
          </ul>
        )}
        <h2 className="ask42-question">{record.question}</h2>
        <p className="ask42-meta">{metaLine(record)}</p>
        {/* Wide screens: the answer reads down the main column and its evidence
           (posts, figures, what each platform returned) sits beside it, so the
           page uses the whole width without stretching any line of prose. */}
        <div className="ask42-answer-body">
        <div className="ask42-answer-main">
        {answerStatusWords(answer) && <p className="ask42-status">{answerStatusWords(answer)}</p>}
        {record.status === 'stopped' && <p className="ask42-status">Stopped early: this answer holds only what had passed its checks</p>}
        <RankedAnswer record={record} windowLabel={windowWords(run.ranked_list?.window)}
          renderSources={(claim) => <ClaimSources claim={claim} records={records} answer={answer} pinnedId={pinnedId} onPin={setPinnedId} />} />
        <EntityLists record={record} windowLabel={windowWords}
          renderSources={(claim, evidence_ids) => <ClaimSources claim={{...claim, evidence_ids}} records={records} answer={answer} pinnedId={pinnedId} onPin={setPinnedId} />} />
        <p id={shortId} className="ask42-short">{String(answer.short_answer || '').trim() ? <ShortAnswer text={answer.short_answer} /> : noShortAnswer(answer)}</p>

        {answer.claims && answer.claims.length > 0 && (
          <ol className="ask42-claims" aria-label="Claims">
            {answer.claims.map((claim) => {
              const confidence = CONFIDENCE[claim.label];
              return (
                <li className="ask42-claim" key={claim.id}>
                  {confidence && (
                    <span className="ask42-confidence" data-label={claim.label}>
                      <span className="ask42-confidence-shape" aria-hidden="true">{confidence.shape}</span>
                      {confidence.word}
                    </span>
                  )}
                  <span className="ask42-claim-text">{claim.text}</span>
                  <ClaimSources claim={claim} records={records} answer={answer} pinnedId={pinnedId} onPin={setPinnedId} />
                </li>
              );
            })}
          </ol>
        )}


        {answer.so_what && answer.so_what.length > 0 && (
          <section className="ask42-section" aria-labelledby="ask42-sowhat-title">
            <h3 className="ask42-section-title" id="ask42-sowhat-title">What it means for a brand</h3>
            <ul className="ask42-list">{answer.so_what.map((item, index) => <li key={index}>{item.text}</li>)}</ul>
          </section>
        )}

        {answer.watch_next && answer.watch_next.length > 0 && (
          <section className="ask42-section" aria-labelledby="ask42-watch-title">
            <h3 className="ask42-section-title" id="ask42-watch-title">What to watch</h3>
            <ul className="ask42-list">
              {answer.watch_next.map((item, index) => <li key={index}>{item.forecast ? 'Forecast: ' + item.text : item.text}</li>)}
            </ul>
          </section>
        )}

        <section className="ask42-section" aria-labelledby="ask42-gaps-title">
          <h3 className="ask42-section-title" id="ask42-gaps-title">What we do not know</h3>
          {answer.gaps && answer.gaps.length > 0
            ? (
              <ul className="ask42-list">
                {answer.gaps.map((gap, index) => (
                  /* Each middot is tied to the words before it by a no-break
                     space and each fact after it stays whole, so a wrapped
                     row never starts with a middot. */
                  <li key={index}>{plainGapWhat(gap.what)}<span className="ask42-muted">{'\u00a0· '}<span className="fact-unit">{'Searched ' + searchedWords(gap)}</span>{gap.why === 'empty' ? null : <>{'\u00a0· '}<span className="fact-unit">{whyAll(gap.why)}</span></>}</span></li>
                ))}
              </ul>
            )
            : <p className="ask42-muted">No gaps were recorded for this answer.</p>}
        </section>

        {answer.context && (
          <section className="ask42-section ask42-context" aria-labelledby="ask42-context-title">
            <h3 className="ask42-section-title" id="ask42-context-title">Background, not evidence</h3>
            <p>{answer.context}</p>
          </section>
        )}

        <div className="ask42-actions">
          <button type="button" className="ask42-primary" onClick={addToDossier} disabled={adding} aria-busy={adding ? 'true' : 'false'}>Add to dossier</button>
          {followAction}
          {/* The text actions travel as one group, so a narrow row moves
              them under the button together rather than leaving one alone. */}
          <div className="ask42-actions-more">
          {canSaveFinding && findingSaveState.phase !== 'saved' && (
            <button type="button" className="ask42-quiet" onClick={saveAnswerToFindings} disabled={findingSaveState.phase === 'pending'} aria-busy={findingSaveState.phase === 'pending' ? 'true' : 'false'}>
              {findingSaveState.phase === 'pending' ? 'Saving checked claims…' : 'Save checked claims to Findings'}
            </button>
          )}
          <button type="button" className="ask42-quiet" onClick={exportAnswer} disabled={exporting} aria-busy={exporting ? 'true' : 'false'}>Export answer</button>
          <button type="button" className="ask42-quiet" onClick={askEveryMonday}>Ask every Monday</button>
          </div>
        </div>
        {addError && <p className="ask42-error" role="alert">{addError}</p>}
        {canSaveFinding && findingSaveState.phase === 'error' && <p className="ask42-error" role="alert">{findingSaveState.error}</p>}
        {canSaveFinding && findingSaveState.phase === 'saved' && (
          <p className="ask42-status" role="status">Checked claims saved to Findings · <a className="ask42-quiet" href="#/history?tab=findings">Open Findings</a></p>
        )}
        {exportError && <p className="ask42-error" role="alert">{exportError}</p>}
        {tail && <div className="ask42-answer-tail">{tail}</div>}

        </div>
        <aside className="ask42-answer-side" aria-label="Evidence">
        <PostStrip evidence={answer.evidence} />
        {figures.length > 0 && (
          <section className="ask42-section" aria-labelledby="ask42-figures-title">
            <h3 className="ask42-section-title" id="ask42-figures-title">The numbers</h3>
            {figures.map((figure, index) => <FigureGroup key={index} numbers={figure.numbers} about={figure.about} />)}
          </section>
        )}
        <footer className="ask42-footer">
          {/* Charts, 3 October 2026: what each platform returned, drawn. A
              source with no items number is not drawn as zero. run.posts is
              left out: it counts differently from the per source sum. */}
          <BarList title="Posts read by platform" data="data-ask-source-chart"
            caption={'Posts each platform returned' + (windowWords(run.window) ? ', ' + windowWords(run.window) : '')}
            rows={(run.source_status || []).map((source, index) => ({
              key: (source.platform || '') + ':' + index,
              label: platformWord(source.platform),
              value: typeof source.items === 'number' ? source.items : null,
              note: source.status && source.status !== 'ok' ? why(source.status) : undefined,
            }))} />
          {/* Cost, depth and what each platform returned, closed behind a quiet
              toggle. The run id stays on the page as data for support; tokens,
              query ids and source routes are not reader words (QA, 2 Oct 2026). */}
          <details className="ask42-technical" data-run-id={run.run_id || undefined}>
            <summary>Technical details</summary>
            <dl className="ask42-details">
              <dt>Cost</dt><dd>{costWords(run)}</dd>
              <dt>Depth</dt><dd>{TIER_WORDS[run.tier] || run.tier}</dd>
              <dt>Sources</dt>
              <dd>
                <ul className="ask42-list">
                  {(run.source_status || []).map((source, index) => (
                    <li key={index}>{(platformLabel(source.platform) || source.platform) + ' · ' + why(source.status) + ' · ' + itemsWords(source.items)}</li>
                  ))}
                </ul>
              </dd>
            </dl>
          </details>
        </footer>
        {followups.length > 0 && (
          /* Suggested questions are where the reader goes next, not things to
             do with this answer, so they sit apart under their own label,
             beside the answer with its evidence on a wide screen. */
          <div className="ask42-next" role="group" aria-labelledby={nextId} data-ask-followups="">
            <h3 className="ask42-section-title" id={nextId}>Ask next</h3>
            <ul className="ask42-next-list">
              {followups.map((followup) => (
                <li key={followup}><button type="button" className="ask42-quiet ask42-next-question" onClick={() => onFollowup(followup)}>{followup}</button></li>
              ))}
            </ul>
          </div>
        )}
        </aside>
        </div>
      </article>
      <SourcePanel evidence={pinned} quotes={pinned ? quotesFor(answer, pinned.id) : []} onClose={() => setPinnedId(null)} />
    </div>
  );
}

/* While 42 researches: the question, then the live instrument panel with Stop
   as its one secondary button, the posts gathered so far as a quiet list of
   handles and the claims as they pass or fail their checks. */
const GATHERED_LIMIT = 12;

/* A post whose creator 42 holds no handle for carries the bare platform id
   (a long number for a Facebook page), which reads as noise: it shows as
   the platform's account instead. */
export function gatheredName(record){
  const handle = String((record && record.handle) || '').trim();
  if (handle && !/^\d+$/.test(handle)) return handle;
  return ((record && record.platform) || 'Source') + ' account';
}

/* The asked market's posts first, so a South Africa question does not lead
   with a Kenyan outlet the search also found; the rest keep their order. */
export function gatheredFirst(evidence, market){
  if (!market) return evidence;
  const own = evidence.filter((record) => record.market === market);
  return own.length ? [...own, ...evidence.filter((record) => record.market !== market)] : evidence;
}

/* How long this question has been running and which phase it is in. The time
   counts from when the page sent the question, or from when it began watching
   an ask started elsewhere, and says so. */
function ProgressLine({steps, startedAt, watching}){
  const [began] = useState(() => startedAt || Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return (
    <p className="ask42-progress" data-ask-progress="">
      <span className="ask42-progress-phase">{phaseOf(steps) || 'Starting'}</span>
      <span className="ask42-muted">{' · ' + (watching ? 'watching for ' : 'running for ') + elapsedWords((now - began) / 1000)}</span>
    </p>
  );
}

/* The market the posts are counted by: the one the server resolved, sent on
   its first plan step (null there means no single market), and the one the page
   sent only until that step arrives. */
export function countedMarket(run){
  const planned = (run.steps || []).find((step) => step && Object.hasOwn(step, 'market'));
  return planned ? marketCode(planned.market) : marketCode(run.request && run.request.mkt);
}

function Running({run, onStop}){
  const question = run.request ? run.request.text : '';
  const market = countedMarket(run);
  const counted = run.evidence.filter((record) => inMarket(record, market));
  const elsewhere = run.evidence.filter((record) => !inMarket(record, market));
  const gathered = counted.slice(0, GATHERED_LIMIT);
  const moreGathered = counted.length - gathered.length;
  const stopButton = <button type="button" className="ask42-quiet ask42-stop" onClick={onStop} disabled={run.stopping || !run.askId}>{run.stopping ? 'Stopping' : 'Stop'}</button>;
  return (
    <div className="ask42-running">
      <h2 className="ask42-question">{question}</h2>
      <ProgressLine key={run.startedAt || 0} steps={run.steps} startedAt={run.startedAt} watching={Boolean(run.request && run.request.extra && run.request.extra.follow)} />
      <ResearchLog steps={run.steps} running evidence={run.evidence} claims={run.claims} market={market} clock={false} action={stopButton}>
        {run.evidence.length > 0 && (
          <div className="ask42-scan-block">
            <h4 className="ask42-scan-log-title">{'Posts gathered so far · ' + counted.length}</h4>
            <ul className="ask42-gathered" aria-label="Sources gathered">
              {gathered.map((record) => {
                const thumbnail = safeUrl(record.thumbnail_url);
                return (
                  <li key={record.id}>
                    {thumbnail
                      ? <img src={thumbnail} alt={gatheredName(record)} loading="lazy" />
                      : <span className="ask42-gathered-handle">{gatheredName(record)}</span>}
                  </li>
                );
              })}
              {moreGathered > 0 && <li className="ask42-gathered-more">{'and ' + moreGathered + ' more'}</li>}
            </ul>
          </div>
        )}
        {elsewhere.length > 0 && (
          /* Found, but located in another market: kept in view under their own
             label and left out of the counts above. */
          <div className="ask42-scan-block" data-gathered-other-markets="">
            <h4 className="ask42-scan-log-title">{'Located in other markets, not counted · ' + elsewhere.length}</h4>
            <ul className="ask42-gathered" aria-label="Posts located in other markets">
              {elsewhere.slice(0, GATHERED_LIMIT).map((record) => (
                <li key={record.id}><span className="ask42-gathered-handle">{gatheredName(record) + ' · ' + (MARKET_NAME[record.market] || record.market)}</span></li>
              ))}
              {elsewhere.length > GATHERED_LIMIT && <li className="ask42-gathered-more">{'and ' + (elsewhere.length - GATHERED_LIMIT) + ' more'}</li>}
            </ul>
          </div>
        )}
        {run.claims.length > 0 && (
          <div className="ask42-scan-block">
            <h4 className="ask42-scan-log-title">Claims being checked</h4>
            <ul className="ask42-checking" aria-label="Claims being checked">
              {run.claims.map(({claim, check, reason}) => (
                <li key={claim.id} data-check={check}>
                  <span className="ask42-confidence">{CHECK_WORD[check] || 'Checking'}</span>
                  <span className="ask42-claim-text">{claim.text}</span>
                  {reason && <span className="ask42-muted">{' · ' + reason}</span>}
                </li>
              ))}
            </ul>
          </div>
        )}
      </ResearchLog>
    </div>
  );
}

export function AskPage({region, setRegion, query, onAuth, health = null}){
  const q = query || {};
  const [question, setQuestion] = useState(q.q || '');
  const [market, setMarket] = useState(() => marketCode(q.market) || marketCode(region));
  /* The select starts on the page's region. Only a market the reader chose, or a link or starter carried in, is held against the market the question names. */
  /* A draft that names the answer it follows (an investigation's follow-up) posts that answer as its parent, once. */
  const draftParent = useRef(null);
  /* A draft opened from a card keeps the card, so the ask the reader presses still reads from it. */
  const draftCard = useRef(null);
  const marketPicked = useRef(Boolean(marketCode(q.market)));
  /* The header market is a choice the reader makes: when it changes after the
     page mounts, a question asks in it and no country named in the question
     overrides it. A change this page made itself already matches. */
  const lastRegion = useRef(marketCode(region));
  useEffect(() => {
    const next = marketCode(region);
    if (next === lastRegion.current) return;
    lastRegion.current = next;
    if (!next || next === market) return;
    marketPicked.current = true;
    setMarket(next);
  }, [region]);
  const [run, setRun] = useState(EMPTY_RUN);
  const [retry, setRetry] = useState(null);
  /* A reopened answer (follow) leads with the answer: the composer folds to
     "Ask a follow-up" under it, and a question asked from there is a
     follow-up of that answer. */
  const [composerOpen, setComposerOpen] = useState(false);
  const questionField = useRef(null);
  const folded = Boolean(q.follow) && !composerOpen;
  useEffect(() => { setComposerOpen(false); }, [q.follow]);
  useEffect(() => { if (composerOpen && questionField.current) questionField.current.focus(); }, [composerOpen]);
  function openComposer(){
    setQuestion('');
    setComposerOpen(true);
  }
  function pickStarter(text, mkt){
    setQuestion(text);
    if (mkt){ marketPicked.current = true; setMarket(mkt); }
    if (questionField.current) questionField.current.focus();
  }
  function submitQuestion(){
    if (q.follow && run.record && run.record.ask_id){ followUp(question); return; }
    const parent = draftParent.current;
    const card = draftCard.current;
    draftParent.current = null;
    draftCard.current = null;
    const sent = marketFor(question);
    const extra = parent ? {parent_id: parent} : {};
    if (card) extra.from_card = {...card, market: card.market || sent || null};
    ask(question, sent, extra);
  }
  function marketFor(text){
    const sent = marketToSend({question: text, selected: market, picked: marketPicked.current});
    if (sent !== market) setMarket(sent);
    return sent;
  }
  const control = useRef(null);
  const asked = useRef(null);
  const authRef = useRef(onAuth);
  authRef.current = onAuth;
  const needPasscode = (error) => { if (error && error.auth && authRef.current) authRef.current(); };

  useEffect(() => () => { if (control.current) control.current.abort(); }, []);

  async function ask(text, mkt, extra = {}){
    const words = String(text || '').trim();
    if (words.length < 3) return;
    if (control.current) control.current.abort();
    const ctrl = new AbortController();
    control.current = ctrl;
    const requestHash = typeof window === 'undefined' ? '' : window.location.hash;
    const request = {text: words, mkt, extra};
    setRun({...EMPTY_RUN, phase: 'running', startedAt: Date.now(), request});
    const body = {question: words, market: mkt || null, parent_id: extra.parent_id || null};
    if (extra.from_card) body.from_card = extra.from_card;
    if (extra.tier) body.tier = extra.tier;
    try {
      const started = await startAsk(body);
      if (ctrl.signal.aborted) return;
      setRun((current) => ({...current, askId: started.ask_id}));
      if (
        typeof window !== 'undefined' &&
        typeof started.ask_id === 'string' &&
        /^[A-Za-z0-9_-]{1,128}$/.test(started.ask_id) &&
        /^#\/?ask(?:\?|$)/.test(requestHash) &&
        window.location.hash === requestHash
      ){
        asked.current = 'follow|' + started.ask_id;
        const nextHash = '#/ask?follow=' + encodeURIComponent(started.ask_id);
        if (nextHash !== requestHash){
          window.history.replaceState(window.history.state, '', nextHash);
          window.dispatchEvent(new window.Event('hashchange'));
        }
      }
      await streamAsk(started.ask_id, (event) => {
        if (!ctrl.signal.aborted) setRun((current) => applyEvent(current, event));
      }, {signal: ctrl.signal});
      const record = await getAsk(started.ask_id, ctrl.signal);
      if (ctrl.signal.aborted) return;
      setRun((current) => ({...current, phase: 'done', record, steps: Array.isArray(record.steps) ? record.steps : current.steps}));
    } catch (error){
      if (ctrl.signal.aborted) return;
      setRun((current) => ({...current, phase: 'error', error}));
      needPasscode(error);
    }
  }

  /* An ask started elsewhere (a spike on a chart, a scheduled answer) is
     read, then followed through its live log like one asked here; a
     finished one is shown as it is. Nothing is posted. */
  async function follow(askId){
    if (control.current) control.current.abort();
    const ctrl = new AbortController();
    control.current = ctrl;
    /* Opening reads; it never shows the live timer or Stop until the record says the ask is still running. */
    setRun({...EMPTY_RUN, phase: 'running', opening: true, startedAt: Date.now(), askId, request: {text: '', mkt: '', extra: {follow: askId}}});
    try {
      let record = await getAsk(askId, ctrl.signal);
      if (ctrl.signal.aborted) return;
      setQuestion(record.question || '');
      const mkt = marketCode(record.market);
      setMarket(mkt);
      /* The header shows the market of the answer on the page. */
      if (mkt && setRegion) setRegion(mkt);
      setRun((current) => ({...current, opening: record.status !== 'running', request: {text: record.question || '', mkt, extra: {follow: askId}}, steps: Array.isArray(record.steps) ? record.steps : []}));
      if (record.status === 'running'){
        await streamAsk(askId, (event) => {
          if (!ctrl.signal.aborted) setRun((current) => applyEvent(current, event));
        }, {signal: ctrl.signal});
        record = await getAsk(askId, ctrl.signal);
        if (ctrl.signal.aborted) return;
      }
      setRun((current) => ({...current, phase: 'done', opening: false, record, steps: Array.isArray(record.steps) ? record.steps : current.steps}));
    } catch (error){
      if (ctrl.signal.aborted) return;
      setRun((current) => ({...current, phase: 'error', opening: false, error}));
      needPasscode(error);
    }
  }

  useEffect(() => {
    if (!q.follow) return;
    const key = 'follow|' + q.follow;
    if (asked.current === key) return;
    asked.current = key;
    follow(q.follow);
  }, [q.follow]);

  useEffect(() => {
    if (!q.q || q.follow) return;
    const key = [q.q, q.market, q.item, q.date].join('|');
    if (marketCode(q.market)) marketPicked.current = true;
    const mkt = marketCode(q.market) || marketToSend({question: q.q, selected: market, picked: marketPicked.current});
    setQuestion(q.q);
    setMarket(mkt);
    if (q.draft && !consumeAsk(q)){
      draftParent.current = q.parent || null;
      draftCard.current = q.item ? {item_id: q.item, market: mkt || null, date: q.date || null} : null;
      return;
    }
    if (asked.current === key) return;
    asked.current = key;
    const extra = q.item ? {from_card: {item_id: q.item, market: mkt || null, date: q.date || null}} : {};
    ask(q.q, mkt, extra);
  }, [q.q, q.market, q.item, q.date, q.draft]);

  function chooseMarket(value){
    marketPicked.current = true;
    setMarket(value);
    if (value && setRegion) setRegion(value);
  }

  async function stop(){
    if (!run.askId) return;
    setRun((current) => ({...current, stopping: true}));
    try { await stopAsk(run.askId); }
    catch (error){
      setRun((current) => ({...current, stopping: false, stopError: error && error.message}));
      needPasscode(error);
    }
  }

  function followUp(text){
    const record = run.record;
    setQuestion(text);
    ask(text, marketFor(text), {parent_id: record ? record.ask_id : null});
  }

  /* A followed ask was started elsewhere with its own confirm, so asking it
     again goes back there: a spike to its topic page with that day open, a
     scheduled answer to Schedules. Any other names its tier's ceiling first.
     Nothing is asked again while the record still reads as running. A
     failure after the ask got its id reads that ask again rather than
     posting a second one. */
  function tryAgain(){
    if (!run.request) return;
    const record = run.record;
    if (run.phase === 'done' && record && record.status === 'running') return;
    const followed = run.request.extra && run.request.extra.follow;
    if (run.phase === 'error' && (followed || run.askId)){ follow(followed || run.askId); return; }
    if (!followed){ ask(run.request.text, run.request.mkt, run.request.extra); return; }
    const spike = record && record.spike;
    if (spike && spike.item_id){
      const query = new URLSearchParams({market: spike.market || record.market || ''});
      if (spike.date) query.set('day', spike.date);
      if (spike.series) query.set('series', spike.series);
      go('/t/' + encodeURIComponent(spike.item_id) + '?' + query.toString());
      return;
    }
    if (record && record.schedule_id){ go('/schedules'); return; }
    const tier = record && ((record.run && record.run.tier) || record.tier);
    setRetry({tier: CEILING[tier] ? tier : null});
  }

  function askAgain(){
    const tier = retry && retry.tier;
    setRetry(null);
    ask(run.request.text, run.request.mkt, tier ? {tier} : {});
  }

  const record = run.record;
  const verdict = run.phase === 'done' && record && record.answer ? checkAnswer(record.answer) : null;
  /* A shown answer carries how it was researched and the follow-up at the
     foot of its own column; a failed or refused one keeps them below. */
  const showsAnswer = Boolean(record && record.status !== 'failed' && record.answer && !(verdict && !verdict.ok));
  const followAction = folded ? <button type="button" className="ask42-quiet ask42-followup-open" onClick={openComposer}>Ask a follow-up</button> : null;
  /* Under a shown answer, Ask a follow-up joins the answer's action row;
     under a failed or refused one it closes the page below the record. */
  const researchTail = <ResearchLog steps={run.steps} running={false} />;
  const running = run.phase === 'running';
  const idle = run.phase === 'idle' && !q.q && !q.follow;

  return (
    <main className="ask42" aria-labelledby="ask42-title">
      {/* A reopened answer has no composer above it, so its question is the
          visible title; Ask 42 stays as the page name for screen readers. */}
      <h1 className={'ask42-title' + (folded && run.phase === 'done' && record && record.answer ? ' sr-only' : '')} id="ask42-title">Ask 42</h1>
      <div className={'ask42-start' + (idle ? ' ask42-start-idle' : '')}>
      <div className="ask42-compose">
      <form className="ask42-form" hidden={folded} onSubmit={(event) => { event.preventDefault(); submitQuestion(); }}>
        <label className="ask42-label" htmlFor="ask42-question">Your question</label>
        <textarea
          ref={questionField}
          id="ask42-question"
          className="ask42-input"
          rows={3}
          maxLength={2000}
          value={question}
          placeholder="What is behind a trend, a hashtag or a conversation?"
          onChange={(event) => { setQuestion(event.target.value); fitField(event.target); }}
          onKeyDown={(event) => {
            /* Enter alone adds a line; Ctrl or Cmd with Enter asks, the way
               most writing tools send. Asking spends credits, so it takes the
               chord, never a bare key. */
            if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey) || event.nativeEvent.isComposing) return;
            event.preventDefault();
            if (!running && question.trim().length >= 3) submitQuestion();
          }}
        />
        {run.phase === 'idle' && <p className="ask42-status" data-ask-state="empty">Ask in plain words. 42 answers from the posts it has collected, shows the source behind every claim and says what it could not find. Press Ctrl+Enter, or Cmd+Enter on a Mac, to ask.</p>}
        <div className="ask42-form-row">
          <label className="ask42-label" htmlFor="ask42-market">Market</label>
          <select id="ask42-market" className="ask42-select" value={market} onChange={(event) => chooseMarket(event.target.value)}>
            <option value="">From the question</option>
            {MARKETS.map((m) => <option key={m.code} value={m.code}>{m.name}</option>)}
          </select>
          <button type="submit" className="ask42-submit" aria-keyshortcuts="Control+Enter Meta+Enter" disabled={running || question.trim().length < 3}>Ask</button>
        </div>
        {/* Credit pass, 3 October 2026: the cost is said before the first
            ask, beside the button that spends it. */}
        <p className="ask42-cost" data-ask-cost="">A question can use up to {CEILING.T1} credits when it reads fresh posts.</p>
      </form>

      {idle && <AskReturns />}
      </div>
      {idle && <AskStarters market={market || marketCode(region) || 'ZA'} onPick={pickStarter} />}
      </div>
      {running && !run.opening && <p className="ask42-status sr-only" role="status" aria-live="polite" data-ask-status="">{run.askId ? 'Research is in progress.' : 'Starting research.'}</p>}
      <div className="ask42-result" aria-busy={running ? 'true' : 'false'}>
        {running && run.opening && <p className="ask42-status" role="status" aria-live="polite" data-ask-opening="">Opening the saved answer.</p>}
        {running && !run.opening && <Running run={run} onStop={stop} />}
        {run.stopError && running && <p className="ask42-error" role="alert">{run.stopError}</p>}

        {run.phase === 'error' && (
          <div className="ask42-failed" role="alert">
            <p>{run.error && run.error.auth ? 'Enter the passcode to ask a question.' : (run.error && run.error.message) || 'The question could not be asked.'}</p>
            <button type="button" className="ask42-quiet" onClick={tryAgain}>Try again</button>
          </div>
        )}

        {run.phase === 'done' && record && (
          <div className="ask42-done">
            {record.status === 'failed' || !record.answer
              ? (
                <div className="ask42-failed" role="alert">
                  <h2 className="ask42-question">{record.question}</h2>
                  <p>
                    {record.status === 'running'
                      ? 'This question still reads as running, so it cannot be asked again yet.'
                      : record.error && record.error.message ? record.error.message : 'This question stopped before any answer passed its checks.'}
                  </p>
                  <button type="button" className="ask42-quiet" onClick={tryAgain} disabled={record.status === 'running'}>Try again</button>
                </div>
              )
              : verdict && !verdict.ok
                ? (
                  <div className="ask42-failed" role="alert">
                    <h2 className="ask42-question">{record.question}</h2>
                    <p>This answer did not pass its checks</p>
                    <ul className="ask42-list">{verdict.problems.map((problem, index) => <li key={index}>{problem}</li>)}</ul>
                  </div>
                )
                : <Answer record={record} onFollowup={followUp} onFailure={needPasscode} tail={researchTail} followAction={followAction} />}
            {!showsAnswer && researchTail}
            {!showsAnswer && followAction}
          </div>
        )}
      </div>

      {retry && run.request && (
        <CostConfirm
          title="Ask this question again?"
          where={run.request.text}
          line={'Asking again spends up to ' + CEILING[retry.tier || 'T1'] + ' credits.'}
          does="42 starts this question over and reads the posts again."
          get="A new answer on this page, with the posts behind each point."
          confirmWord="Ask again"
          onConfirm={askAgain}
          onClose={() => setRetry(null)}
        />
      )}

      <SkillForms key={q.fit || ''} market={marketCode(q.market) || market || marketCode(region)} fit={q.fit} t2Ready={t2ReadyFrom(health)} onAuth={() => { if (authRef.current) authRef.current(); }} />
    </main>
  );
}
