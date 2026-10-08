/* 42 Investigations (contract.md section 13.1; EXPERIENCE.md, Investigations).
   #/investigations lists them by status and drafts a new one;
   #/investigations/<id> shows one. A draft shows its plan first, with the
   estimate against what is left today, Edit plan and Start, the one red
   action; every refusal is shown in the server's own words. A started run
   shows its live research log and Stop. A finished run's answer passes the
   shared contract check before any of it renders, and opens as a draft
   dossier with one tap. */
import {useEffect, useId, useRef, useState} from 'react';
import {MarketScopeNote, inMarket, pickedMarket} from './marketScope.jsx';
import './styles/ask42.css';
import './styles/today42.css';
import './styles/investigations42.css';
import {readerFigure} from './api.js';
import {validateAnswer} from './answerContract.js';
import {plainGapWhat} from './ask42.jsx';
import {createInvestigation, createInvestigationDossier, getInvestigation, listInvestigations, startInvestigation, stopInvestigation, streamInvestigation, updateInvestigationPlan} from './api42.js';
import {downloadExport} from './askTransport42.js';
import {go} from './router.js';
import {EvidenceChip, monthName, postDay} from './ui/EvidenceChip.jsx';
import {FigureLine} from './ui/FigureLine.jsx';
import {PostStrip} from './ui/PostStrip.jsx';
import {ResearchLog} from './ui/ResearchLog.jsx';
import {SourcePanel} from './ui/SourcePanel.jsx';
import {platformLabel} from './ui/PlatformGlyph.jsx';

const MARKETS = [
  {code: 'ZA', name: 'South Africa'},
  {code: 'NG', name: 'Nigeria'},
  {code: 'KE', name: 'Kenya'},
];
const MARKET_NAME = Object.fromEntries(MARKETS.map((m) => [m.code, m.name]));

/* The platform groups a plan may name: the groups the agent's researchers
   search (core/agent/ask.py PLATFORM_GROUPS, core/api/investigations.PLAN_PLATFORMS). */
const PLATFORMS = [
  ['tiktok', 'TikTok'], ['x', 'X'], ['instagram', 'Instagram'], ['youtube', 'YouTube'],
  ['facebook', 'Facebook'], ['reddit_threads', 'Reddit and Threads'], ['news', 'News and web search'],
];
/* Names older plans carry: the first three run as a group, the last two no
   researcher can search, so the server refuses them until they are unticked. */
const LEGACY_GROUP = {twitter: 'x', reddit: 'reddit_threads', threads: 'reddit_threads'};
const NOT_SEARCHABLE = {telegram: 'Telegram', apple_music: 'Apple Music'};
const PLATFORM_WORD = {...Object.fromEntries(PLATFORMS), twitter: 'X', reddit: 'Reddit', threads: 'Threads', ...NOT_SEARCHABLE};
const asGroups = (list) => [...new Set((Array.isArray(list) ? list : []).map((p) => LEGACY_GROUP[p] || p))];

const STATUS_WORD = {draft: 'Draft', running: 'Running', complete: 'Finished', stopped: 'Stopped', failed: 'Failed'};
const TABS = [
  {status: '', label: 'All'},
  {status: 'draft', label: 'Drafts', none: 'draft'},
  {status: 'running', label: 'Running', none: 'running'},
  {status: 'complete', label: 'Finished', none: 'finished'},
  {status: 'stopped', label: 'Stopped', none: 'stopped'},
  {status: 'failed', label: 'Failed', none: 'failed'},
];

/* The status filter is one row of tabs: a single Tab stop, the arrow keys,
   Home and End move the choice, and each tab names the list it filters. Shell
   consistency, 2 October 2026: it takes the Today tab row (an ink underline
   under the chosen word) in place of a boxed segmented control, so every tab
   row in 42 looks and answers the same. */
function StatusFilter({status, onChange, panelId}){
  const refs = useRef([]);
  const at = Math.max(0, TABS.findIndex((tab) => tab.status === status));
  const move = (event) => {
    const last = TABS.length - 1;
    const next = {ArrowRight: at === last ? 0 : at + 1, ArrowLeft: at === 0 ? last : at - 1, Home: 0, End: last}[event.key];
    if (next === undefined) return;
    event.preventDefault();
    onChange(TABS[next].status);
    if (refs.current[next]) refs.current[next].focus();
  };
  return (
    <div className="t42-tabs inv42-status-tabs" role="tablist" aria-label="Status">
      {TABS.map((tab, index) => (
        <button
          key={tab.label}
          ref={(node) => { refs.current[index] = node; }}
          type="button"
          role="tab"
          className="t42-tab"
          id={panelId + '-tab-' + index}
          aria-selected={index === at ? 'true' : 'false'}
          aria-controls={panelId}
          tabIndex={index === at ? 0 : -1}
          onKeyDown={move}
          onClick={() => onChange(tab.status)}
        >{tab.label}</button>
      ))}
    </div>
  );
}

const MAX_ANGLES = 8;

/* What pressing Draft a plan does, said before it is pressed. */
function draftWords(question, market){
  if (question.trim().length < 3) return 'Write the question first. Drafting the plan uses a little model spend; the research itself waits until you press Start.';
  const where = (MARKETS.find((m) => m.code === market) || {}).name;
  return '42 drafts a plan ' + (where ? 'for ' + where : 'and reads the market from the question') + ', which uses a little model spend. The research itself waits until you press Start.';
}

/* What was left today when a plan was drafted, saved or last read. A draft's
   read carries it too, so a reload shows it; when that read says null the
   estimate stands alone and Start checks again. */
const BUDGETS = new Map();

/* A draft made elsewhere (a skin's weekly report) hands its budget over the
   same way before it opens the investigation page. */
export function rememberBudget(investigationId, left){
  if (left) BUDGETS.set(investigationId, left);
}

const plural = (count, word) => readerFigure(count) + ' ' + word + (count === 1 ? '' : 's');
const usd = (value) => 'USD ' + (typeof value === 'number' ? value : 0).toFixed(2);
const platformWords = (list) => (Array.isArray(list) ? list : []).map((p) => PLATFORM_WORD[p] || p).join(', ');

function useAuthHandover(onAuth){
  const ref = useRef(onAuth);
  ref.current = onAuth;
  return (error) => { if (error && error.auth && ref.current) ref.current(); };
}

function failureWords(error, fallback){
  if (error && error.auth) return 'Enter the passcode to read investigations.';
  return (error && error.message) || fallback;
}

/* ---------------- the list, #/investigations ---------------- */

export function InvestigationsPage({onAuth, region}){
  const picked = pickedMarket(region);
  const [status, setStatus] = useState('');
  const [list, setList] = useState({phase: 'loading', items: [], error: null});
  const [attempt, setAttempt] = useState(0);
  const handover = useAuthHandover(onAuth);

  useEffect(() => {
    const ctrl = new AbortController();
    setList({phase: 'loading', items: [], error: null});
    listInvestigations({status}, {signal: ctrl.signal})
      .then((data) => {
        if (!ctrl.signal.aborted) setList({phase: 'ready', items: Array.isArray(data && data.investigations) ? data.investigations : [], error: null});
      })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        setList({phase: 'error', items: [], error});
        handover(error);
      });
    return () => ctrl.abort();
  }, [status, attempt]);

  const {phase, items: allItems, error} = list;
  const items = allItems.filter((item) => inMarket(item.market, picked));
  const panelId = 'inv42-list-' + useId().replace(/[^a-zA-Z0-9_-]/g, '');
  return (
    <main className="ask42 inv42" aria-labelledby="inv42-title">
      <h1 className="ask42-title" id="inv42-title">Investigations</h1>
      <p className="inv42-lede">What is 42 researching for you in depth? 42 drafts a research plan first, and drafting uses some of today's model spend. Check the plan, then press Start and the research runs in the background.</p>
      {/* Layout pass, 4 October 2026: the form sits beside what an
          investigation hands back, so the right of a wide screen says what
          the reader will get rather than standing empty. */}
      <div className="inv42-start">
        <NewInvestigation onAuth={handover} />
        <aside className="inv42-get" aria-labelledby="inv42-get-title">
          <h2 className="inv42-section-title" id="inv42-get-title">What you get back</h2>
          <dl className="inv42-get-list">
            <div><dt>The plan</dt><dd>The questions 42 will chase and where it will read, with an estimate before the research starts.</dd></div>
            <div><dt>The numbers</dt><dd>Each figure with the posts it rests on, which you can open.</dd></div>
            <div><dt>What it means</dt><dd>The so what for a brand, and the signals to watch next.</dd></div>
            <div><dt>What we do not know</dt><dd>The gaps, said plainly, so nothing is overclaimed.</dd></div>
          </dl>
          <p className="inv42-get-note">Open a finished investigation as a dossier to check its claims and share it.</p>
        </aside>
      </div>
      <section className="inv42-mine" aria-labelledby="inv42-list-title">
        <h2 className="inv42-section-title" id="inv42-list-title">Your investigations</h2>
        <StatusFilter status={status} onChange={setStatus} panelId={panelId} />
        <div className="inv42-panel" role="tabpanel" id={panelId} aria-labelledby={panelId + '-tab-' + Math.max(0, TABS.findIndex((tab) => tab.status === status))}>
          {phase === 'loading' && <p className="ask42-muted" role="status">Reading the investigations</p>}
          {phase === 'error' && <p className="ask42-error" role="alert">{failureWords(error, 'The investigations could not be read.')}</p>}
          {phase === 'error' && !(error && error.auth) && <button type="button" className="t42-button" onClick={() => setAttempt((n) => n + 1)}>Try again</button>}
          <MarketScopeNote picked={picked} hidden={allItems.length - items.length} what={allItems.length - items.length === 1 ? 'investigation is' : 'investigations are'} />
          {phase === 'ready' && allItems.length === 0 && (status
            ? <div className="inv42-empty">
                <p className="inv42-empty-line">{'No ' + ((TABS.find((tab) => tab.status === status) || {}).none || status) + ' investigations.'}</p>
                <button
                  type="button"
                  className="ask42-quiet"
                  onClick={() => {
                    /* This button leaves with the empty line, so focus goes to
                       the All tab the reader has just chosen. */
                    setStatus('');
                    const all = document.getElementById(panelId + '-tab-0');
                    if (all) all.focus();
                  }}
                >Show all investigations</button>
              </div>
            : <div className="inv42-empty inv42-empty-first">
                <p className="inv42-empty-line">No investigations yet. Write what 42 should find out above, then choose Draft a plan.</p>
                <p className="inv42-empty-note">Each one lists here with its state, market and the day it started, newest first.</p>
              </div>)}
          {items.length > 0 && (
            <ol className="inv42-list" aria-label="Investigations, newest first">
              {items.map((item) => {
                const meta = [MARKET_NAME[item.market], postDay(item.created_at)].filter(Boolean).join(' · ');
                return (
                  <li className="inv42-row" key={item.investigation_id} data-investigation={item.investigation_id}>
                    <span className="inv42-state" data-state={item.status}>{STATUS_WORD[item.status] || item.status}</span>
                    <a className="inv42-row-title" href={'#/investigations/' + encodeURIComponent(item.investigation_id)}>{item.question}</a>
                    {meta && <span className="ask42-muted">{meta}</span>}
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      </section>
    </main>
  );
}

function NewInvestigation({onAuth}){
  const [question, setQuestion] = useState('');
  const [market, setMarket] = useState('');
  const [angles, setAngles] = useState('');
  const [state, setState] = useState({busy: false, error: null});
  const named = angles.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const tooManyAngles = named.length > MAX_ANGLES;

  async function draft(event){
    event.preventDefault();
    const words = question.trim();
    if (words.length < 3 || state.busy || tooManyAngles) return;
    setState({busy: true, error: null});
    try {
      const made = await createInvestigation({question: words, market, angles: named});
      if (made.budget_left) BUDGETS.set(made.investigation_id, made.budget_left);
      go('/investigations/' + encodeURIComponent(made.investigation_id));
    } catch (error){
      setState({busy: false, error});
      onAuth(error);
    }
  }

  return (
    <section className="inv42-new" aria-labelledby="inv42-new-title">
      <h2 className="inv42-section-title" id="inv42-new-title">New investigation</h2>
      <form className="ask42-form" onSubmit={draft}>
        <label className="ask42-label" htmlFor="inv42-question">What should 42 find out?</label>
        <textarea id="inv42-question" className="ask42-input" rows={3} maxLength={2000} value={question} onChange={(event) => setQuestion(event.target.value)} />
        <label className="ask42-label" htmlFor="inv42-angles">Angles to cover, one per line (optional, up to 8)</label>
        <textarea id="inv42-angles" className="ask42-input" rows={2} value={angles} onChange={(event) => setAngles(event.target.value)} />
        {tooManyAngles && <p className="ask42-error" role="alert">Use at most 8 angles before drafting a plan.</p>}
        <span className="inv42-field">
          <label className="ask42-label" htmlFor="inv42-market">Market</label>
          <select id="inv42-market" className="ask42-select" value={market} onChange={(event) => setMarket(event.target.value)}>
            <option value="">From the question</option>
            {MARKETS.map((m) => <option key={m.code} value={m.code}>{m.name}</option>)}
          </select>
        </span>
        {/* The button ends a line that says what it will do, so it never
            stands alone under the form. */}
        <div className="ask42-form-row inv42-form-row">
          <p className="inv42-hint">{draftWords(question, market)}</p>
          <button type="submit" className="ask42-primary" disabled={state.busy || question.trim().length < 3 || tooManyAngles} aria-busy={state.busy ? 'true' : 'false'}>Draft a plan</button>
        </div>
        {state.busy && <p className="ask42-muted" role="status">Drafting the plan</p>}
        {state.error && <p className="ask42-error" role="alert">{failureWords(state.error, 'The plan could not be drafted.')}</p>}
      </form>
    </section>
  );
}

/* ---------------- one investigation, #/investigations/<id> ---------------- */

const EMPTY_LIVE = {steps: [], stopping: false, stopError: null, lost: null};
const FINISH_READS = 10;
const FINISH_WAIT_MS = 1000;

export function InvestigationPage({investigationId, onAuth, streamOptions}){
  const [load, setLoad] = useState({phase: 'loading', inv: null, error: null});
  const [budget, setBudget] = useState(() => BUDGETS.get(investigationId) || null);
  const [editing, setEditing] = useState(false);
  const [start, setStart] = useState({busy: false, error: null});
  const [live, setLive] = useState(EMPTY_LIVE);
  const control = useRef(null);
  const handover = useAuthHandover(onAuth);

  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; if (control.current) control.current.abort(); }, []);

  async function read(){
    if (control.current) control.current.abort();
    const ctrl = new AbortController();
    control.current = ctrl;
    setLoad((current) => ({...current, phase: current.inv ? current.phase : 'loading', error: null}));
    try {
      const {budget_left: left, ...inv} = await getInvestigation(investigationId, {signal: ctrl.signal});
      if (ctrl.signal.aborted) return;
      /* A draft's read carries what is left now; null means the spend could not be read. */
      if (left !== undefined){
        if (left) BUDGETS.set(investigationId, left); else BUDGETS.delete(investigationId);
        setBudget(left || null);
      }
      setLoad({phase: 'ready', inv, error: null});
      if (inv.status === 'running') follow(inv, ctrl);
    } catch (error){
      if (ctrl.signal.aborted) return;
      setLoad({phase: 'error', inv: null, error});
      handover(error);
    }
  }

  /* The live log until the run ends, then the investigation read again. */
  async function follow(inv, ctrl){
    const steps = inv.record && Array.isArray(inv.record.steps) ? inv.record.steps : [];
    setLive({...EMPTY_LIVE, steps});
    const seen = steps.length ? String(Math.max(...steps.map((step) => step.seq || 0))) : null;
    try {
      await streamInvestigation(investigationId, (event) => {
        if (ctrl.signal.aborted || event.type !== 'step') return;
        setLive((current) => (current.steps.some((step) => step.seq === event.data.seq) ? current : {...current, steps: [...current.steps, event.data]}));
      }, {...streamOptions, signal: ctrl.signal, lastEventId: seen});
      /* The finish row is written just after the run's done event, so a
         read that still says running is read again, for a short while. */
      let done = await getInvestigation(investigationId, {signal: ctrl.signal});
      for (let tries = 0; done.status === 'running' && tries < FINISH_READS; tries += 1){
        await new Promise((resolve) => setTimeout(resolve, FINISH_WAIT_MS));
        if (ctrl.signal.aborted) return;
        done = await getInvestigation(investigationId, {signal: ctrl.signal});
      }
      if (ctrl.signal.aborted) return;
      if (done.status === 'running'){
        setLive((current) => ({...current, lost: new Error('The run has ended but its result is not saved yet.')}));
        return;
      }
      setLoad({phase: 'ready', inv: done, error: null});
    } catch (error){
      if (ctrl.signal.aborted) return;
      setLive((current) => ({...current, lost: error}));
      handover(error);
    }
  }

  useEffect(() => { read(); }, [investigationId]);

  async function begin(){
    setStart({busy: true, error: null});
    try {
      const started = await startInvestigation(investigationId);
      // Left the page while the start was on its way: the run goes on server side, nothing here follows it.
      if (!mounted.current) return;
      if (control.current) control.current.abort();
      const ctrl = new AbortController();
      control.current = ctrl;
      const inv = {...load.inv, status: 'running', ask_id: started.ask_id, record: null};
      setStart({busy: false, error: null});
      setLoad({phase: 'ready', inv, error: null});
      follow(inv, ctrl);
    } catch (error){
      if (!mounted.current) return;
      setStart({busy: false, error});
      handover(error);
    }
  }

  async function stop(){
    setLive((current) => ({...current, stopping: true, stopError: null}));
    try { await stopInvestigation(investigationId); }
    catch (error){
      setLive((current) => ({...current, stopping: false, stopError: error}));
      handover(error);
    }
  }

  async function save(plan){
    const saved = await updateInvestigationPlan(investigationId, plan);
    const {budget_left: left, ...inv} = saved;
    if (left){
      BUDGETS.set(investigationId, left);
      setBudget(left);
    }
    setLoad((current) => ({...current, inv: {...current.inv, ...inv}}));
    setEditing(false);
  }

  if (load.phase === 'loading'){
    return (
      <main className="ask42 inv42" aria-busy="true">
        <p className="ask42-muted" role="status">Reading the investigation</p>
      </main>
    );
  }
  if (load.phase === 'error'){
    return (
      <main className="ask42 inv42" aria-labelledby="inv42-title">
        <h1 className="ask42-title" id="inv42-title">Investigation</h1>
        <p className="ask42-error" role="alert">{failureWords(load.error, 'The investigation could not be read.')}</p>
        <button type="button" className="ask42-quiet" onClick={read}>Try again</button>
        <p className="inv42-back"><a href="#/investigations">All investigations</a></p>
      </main>
    );
  }

  const inv = load.inv;
  const meta = [STATUS_WORD[inv.status] || inv.status, MARKET_NAME[inv.market], postDay(inv.created_at) && 'drafted ' + postDay(inv.created_at)].filter(Boolean).join(' · ');
  return (
    <main className="ask42 inv42" aria-labelledby="inv42-title">
      <p className="inv42-back"><a href="#/investigations">All investigations</a></p>
      <h1 className="inv42-question" id="inv42-title">{inv.question}</h1>
      <p className="ask42-meta">{meta}</p>

      {inv.status === 'draft' && (editing
        ? <PlanEditor plan={inv.plan} onSave={save} onCancel={() => setEditing(false)} onAuth={handover} />
        : (
          <>
            <PlanView plan={inv.plan} />
            <EstimateView estimate={inv.estimate} budget={budget} />
            <div className="ask42-actions">
              <button type="button" className="ask42-quiet" onClick={() => setEditing(true)} disabled={start.busy}>Edit plan</button>
              <button type="button" className="ask42-primary" onClick={begin} disabled={start.busy} aria-busy={start.busy ? 'true' : 'false'}>Start</button>
            </div>
            {start.error && <p className="ask42-error" role="alert">{failureWords(start.error, 'The investigation could not start.')}</p>}
          </>
        ))}

      {inv.status === 'running' && (
        <div className="ask42-running">
          <p className="inv42-lede">This runs in the background. You can leave this page; a notice arrives in Today when it is done.</p>
          <ResearchLog steps={live.steps} running />
          <button type="button" className="ask42-quiet" onClick={stop} disabled={live.stopping}>{live.stopping ? 'Stopping' : 'Stop'}</button>
          {live.stopError && <p className="ask42-error" role="alert">{failureWords(live.stopError, 'The investigation could not be stopped.')}</p>}
          {live.lost && (
            <div className="ask42-error" role="alert">
              <p>{failureWords(live.lost, 'The live log was lost.')}</p>
              <button type="button" className="ask42-quiet" onClick={read}>Read again</button>
            </div>
          )}
        </div>
      )}

      {['complete', 'stopped', 'failed'].includes(inv.status) && <Finished inv={inv} onAuth={handover} />}
    </main>
  );
}

function PlanView({plan}){
  const subs = plan && Array.isArray(plan.sub_questions) ? plan.sub_questions : [];
  return (
    <section className="inv42-section" data-section="plan" aria-labelledby="inv42-plan-title">
      <h2 className="inv42-section-title" id="inv42-plan-title">The plan</h2>
      <ol className="inv42-subs">
        {subs.map((sub) => (
          <li className="inv42-sub" key={sub.id} data-sub={sub.id}>
            <span className="inv42-sub-text">{sub.text}</span>
            <span className="ask42-muted">{platformWords(sub.platforms) + ' · ' + plural(sub.credits ?? 0, 'credit')}</span>
          </li>
        ))}
      </ol>
      <p className="inv42-line">{plural(plan.researchers ?? 0, 'researcher') + ' · ' + (plan.gap_round ? 'A gap round after the first pass' : 'No gap round')}</p>
      <p className="inv42-line">{'Stops at ' + plural(plan.max_credits ?? 0, 'credit') + ' or ' + usd(plan.max_model_usd) + ' of model spend, whichever comes first.'}</p>
    </section>
  );
}

function EstimateView({estimate, budget}){
  const e = estimate || {};
  return (
    <section className="inv42-section" data-section="estimate" aria-labelledby="inv42-estimate-title">
      <h2 className="inv42-section-title" id="inv42-estimate-title">The estimate</h2>
      <p className="inv42-line">{'About ' + plural(e.credits ?? 0, 'credit') + ', ' + usd(e.model_usd) + ' of model spend and ' + plural(Math.ceil(e.minutes ?? 0), 'minute') + '.'}</p>
      {budget
        ? <p className="inv42-line">{'Left today: ' + plural(budget.credits ?? 0, 'credit') + ' and ' + usd(budget.model_usd) + ' of model spend. Both are checked again when you start.'}</p>
        : <p className="inv42-line">What is left today is checked again when you start.</p>}
    </section>
  );
}

/* Credits as the planner drafts them: any number from 0, kept to cents. A
   blank or unreadable entry goes as null so the server names the problem. */
function toCredits(raw){
  const text = String(raw).trim();
  const value = Number(text);
  return text !== '' && Number.isFinite(value) ? Math.round(value * 100) / 100 : null;
}

function PlanEditor({plan, onSave, onCancel, onAuth}){
  const [subs, setSubs] = useState(() => plan.sub_questions.map((sub) => ({...sub, platforms: asGroups(sub.platforms), credits: String(sub.credits)})));
  const [gapRound, setGapRound] = useState(Boolean(plan.gap_round));
  const [state, setState] = useState({busy: false, error: null});

  const change = (id, patch) => setSubs((current) => current.map((sub) => (sub.id === id ? {...sub, ...patch} : sub)));
  const toggle = (sub, platform) => change(sub.id, {platforms: sub.platforms.includes(platform) ? sub.platforms.filter((p) => p !== platform) : [...sub.platforms, platform]});

  async function submit(event){
    event.preventDefault();
    setState({busy: true, error: null});
    try {
      await onSave({
        sub_questions: subs.map((sub) => ({id: sub.id, text: sub.text, platforms: sub.platforms, credits: toCredits(sub.credits)})),
        gap_round: gapRound,
        max_credits: plan.max_credits,
        max_model_usd: plan.max_model_usd,
      });
    } catch (error){
      setState({busy: false, error});
      onAuth(error);
    }
  }

  return (
    /* noValidate: the server checks the plan and names any problem in plain
       words, which the browser's own step and range bubbles do not. */
    <form className="inv42-section inv42-edit" data-section="plan" onSubmit={submit} noValidate aria-labelledby="inv42-edit-title">
      <h2 className="inv42-section-title" id="inv42-edit-title">Edit the plan</h2>
      {subs.map((sub, index) => (
        <fieldset className="inv42-edit-sub" key={sub.id} data-edit-sub={sub.id}>
          <legend className="ask42-label">{'Sub-question ' + (index + 1)}</legend>
          <label className="ask42-label" htmlFor={'inv42-text-' + sub.id}>Question</label>
          <textarea id={'inv42-text-' + sub.id} className="ask42-input" rows={2} maxLength={500} value={sub.text} onChange={(event) => change(sub.id, {text: event.target.value})} />
          <fieldset className="inv42-platforms">
            <legend className="ask42-label">Platforms</legend>
            {PLATFORMS.map(([id, word]) => (
              <label className="inv42-check" key={id}>
                <input type="checkbox" value={id} checked={sub.platforms.includes(id)} onChange={() => toggle(sub, id)} />
                {word}
              </label>
            ))}
            {sub.platforms.filter((id) => !PLATFORMS.some(([group]) => group === id)).map((id) => (
              <label className="inv42-check" key={id}>
                <input type="checkbox" value={id} checked onChange={() => toggle(sub, id)} />
                {(PLATFORM_WORD[id] || id) + ' (42 cannot search it)'}
              </label>
            ))}
          </fieldset>
          <label className="ask42-label" htmlFor={'inv42-credits-' + sub.id}>Credits</label>
          <input id={'inv42-credits-' + sub.id} className="ask42-input inv42-number" type="number" min={0} step={0.01} inputMode="decimal" value={sub.credits} onChange={(event) => change(sub.id, {credits: event.target.value})} />
        </fieldset>
      ))}
      <div className="ask42-form-row">
        <p className="inv42-line">{plural(subs.length, 'researcher')}, one per sub-question</p>
        <label className="inv42-check">
          <input id="inv42-gap-round" type="checkbox" checked={gapRound} onChange={() => setGapRound((value) => !value)} />
          Run a gap round after the first pass
        </label>
      </div>
      <div className="ask42-actions">
        <button type="submit" className="ask42-quiet" disabled={state.busy} aria-busy={state.busy ? 'true' : 'false'}>Save plan</button>
        <button type="button" className="ask42-quiet" onClick={onCancel} disabled={state.busy}>Cancel</button>
      </div>
      {state.error && <p className="ask42-error" role="alert">{failureWords(state.error, 'The plan could not be saved.')}</p>}
    </form>
  );
}

/* ---------------- finished ---------------- */

function Finished({inv, onAuth}){
  const record = inv.record;
  if (!record){
    return <p className="ask42-error" role="alert">The answer of this investigation is not held yet; try again in a minute.</p>;
  }
  const steps = Array.isArray(record.steps) ? record.steps : [];
  let body;
  if (record.status === 'failed' || inv.status === 'failed' || !record.answer){
    body = (
      <div className="ask42-failed" role="alert">
        <p>{record.error && record.error.message ? record.error.message : 'This investigation stopped before any answer passed its checks.'}</p>
      </div>
    );
  } else {
    const verdict = validateAnswer(record.answer);
    body = verdict.ok
      ? <InvestigationAnswer record={record} investigationId={inv.investigation_id} onFailure={onAuth} />
      : (
        <div className="ask42-failed" role="alert">
          <p>This answer did not pass its checks</p>
          <ul className="ask42-list">{verdict.problems.map((problem, index) => <li key={index}>{problem}</li>)}</ul>
        </div>
      );
  }
  return (
    <div className="ask42-done">
      {body}
      <ResearchLog steps={steps} running={false} />
    </div>
  );
}

/* The finished answer as Ask draws it (ask42.jsx), with the same classes
   and shared parts. The question is this page's heading, so it is not
   repeated; the red action opens the investigation as a draft dossier;
   a follow-up opens Ask with the question. */
const CONFIDENCE = {
  observed: {word: 'Observed', shape: '●'},
  corroborated: {word: 'Corroborated', shape: '■'},
  single_source: {word: 'Single source', shape: '▲'},
  inferred: {word: 'Inferred', shape: '○'},
};

const ANSWER_STATUS = {
  partial: 'Partial answer: part of the picture is missing',
  insufficient_evidence: 'Not enough evidence for a full answer',
  refused: '42 did not answer this question',
};

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
  const shown = expanded ? cited : cited.slice(0, 1);
  const more = cited.length - 1;
  return (
    <span className="ask42-chips">
      {shown.map((record) => (
        <EvidenceChip key={record.id} evidence={record} quotes={quotesFor(answer, record.id)} pinned={pinnedId === record.id} onPin={onPin} />
      ))}
      {cited.length > 1 && (
        <button key="source-disclosure" type="button" className="ask42-chip ask42-chip-more" aria-expanded={expanded} aria-label={expanded ? 'Show fewer sources' : 'Show ' + more + ' more ' + (more === 1 ? 'source' : 'sources')} onClick={() => setExpanded(!expanded)}>
          {expanded ? 'Fewer' : '+' + more}
        </button>
      )}
    </span>
  );
}

function followupHref(text, market, parentId){
  /* draft=1: Ask fills in the question and waits, so a follow-up never
     starts a paid ask on its own (ask42.jsx). */
  const query = new URLSearchParams({q: text});
  if (MARKET_NAME[market]) query.set('market', market);
  query.set('draft', '1');
  if (parentId) query.set('parent', parentId);
  return '/ask?' + query.toString();
}

function InvestigationAnswer({record, investigationId, onFailure}){
  const answer = record.answer;
  const shortId = useId();
  const run = record.run || {};
  const [pinnedId, setPinnedId] = useState(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState('');
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState('');
  const records = new Map((answer.evidence || []).map((item) => [item.id, item]));
  const figures = (answer.claims || []).flatMap((claim) => claim.numbers || []).slice(0, 4);
  const notices = Array.isArray(run.notices) ? run.notices : [];
  const followups = Array.isArray(run.followups) ? run.followups.slice(0, 3) : [];
  const tokens = run.tokens || {};

  async function exportAnswer(){
    setExporting(true);
    setExportError('');
    try { await downloadExport(record.ask_id); }
    catch (error){
      setExportError(error && error.message ? error.message : 'The export failed.');
      onFailure(error);
    }
    finally { setExporting(false); }
  }

  async function openAsDossier(){
    setOpening(true);
    setOpenError('');
    try {
      const made = await createInvestigationDossier(investigationId);
      go('/dossiers/' + encodeURIComponent(made.dossier_id));
    } catch (error){
      setOpenError(error && error.message ? error.message : 'The dossier could not be started.');
      onFailure(error);
    } finally { setOpening(false); }
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
        <p className="ask42-meta">{metaLine(record)}</p>
        {ANSWER_STATUS[answer.status] && <p className="ask42-status">{ANSWER_STATUS[answer.status]}</p>}
        {record.status === 'stopped' && <p className="ask42-status">Stopped early: this answer holds only what had passed its checks</p>}
        <p id={shortId} className="ask42-short">{String(answer.short_answer || '').trim() ? answer.short_answer : plainGapWhat((answer.gaps || []).find((gap) => /short[ _-]answer|summary/i.test(gap.searched))?.what) || 'No summary: see the claims below'}</p>

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

        <PostStrip evidence={answer.evidence} />

        {figures.length > 0 && (
          <section className="ask42-section" aria-labelledby="inv42-figures-title">
            <h3 className="ask42-section-title" id="inv42-figures-title">The numbers</h3>
            {figures.map((number, index) => <FigureLine key={index} number={number} />)}
          </section>
        )}

        {answer.so_what && answer.so_what.length > 0 && (
          <section className="ask42-section" aria-labelledby="inv42-sowhat-title">
            <h3 className="ask42-section-title" id="inv42-sowhat-title">What it means for a brand</h3>
            <ul className="ask42-list">{answer.so_what.map((item, index) => <li key={index}>{item.text}</li>)}</ul>
          </section>
        )}

        {answer.watch_next && answer.watch_next.length > 0 && (
          <section className="ask42-section" aria-labelledby="inv42-watch-title">
            <h3 className="ask42-section-title" id="inv42-watch-title">What to watch</h3>
            <ul className="ask42-list">
              {answer.watch_next.map((item, index) => <li key={index}>{item.forecast ? 'Forecast: ' + item.text : item.text}</li>)}
            </ul>
          </section>
        )}

        <section className="ask42-section" aria-labelledby="inv42-gaps-title">
          <h3 className="ask42-section-title" id="inv42-gaps-title">What we do not know</h3>
          {answer.gaps && answer.gaps.length > 0
            ? (
              <ul className="ask42-list">
                {answer.gaps.map((gap, index) => (
                  <li key={index}>{gap.what}<span className="ask42-muted">{' · Searched ' + gap.searched + ' · ' + why(gap.why)}</span></li>
                ))}
              </ul>
            )
            : <p className="ask42-muted">No gaps were recorded for this answer.</p>}
        </section>

        {answer.context && (
          <section className="ask42-section ask42-context" aria-labelledby="inv42-context-title">
            <h3 className="ask42-section-title" id="inv42-context-title">Background, not evidence</h3>
            <p>{answer.context}</p>
          </section>
        )}

        <div className="ask42-actions">
          <button type="button" className="ask42-primary" onClick={openAsDossier} disabled={opening} aria-busy={opening ? 'true' : 'false'}>Open as dossier</button>
          <button type="button" className="ask42-quiet" onClick={exportAnswer} disabled={exporting} aria-busy={exporting ? 'true' : 'false'}>Export answer</button>
          {followups.map((followup) => (
            <button type="button" className="ask42-quiet" key={followup} onClick={() => go(followupHref(followup, record.market, record.ask_id))}>{followup}</button>
          ))}
        </div>
        {openError && <p className="ask42-error" role="alert">{openError}</p>}
        {exportError && <p className="ask42-error" role="alert">{exportError}</p>}

        <footer className="ask42-footer">
          <span>{readerFigure(run.credits ?? 0) + ' credits · ' + (run.seconds ?? 0) + ' s'}</span>
          <details>
            <summary>Details</summary>
            <dl className="ask42-details">
              <dt>Run</dt><dd>{run.run_id}</dd>
              <dt>Tier</dt><dd>{run.tier}</dd>
              <dt>Tokens</dt><dd>{readerFigure(tokens.input ?? 0) + ' in, ' + readerFigure(tokens.output ?? 0) + ' out'}</dd>
              <dt>Sources</dt>
              <dd>
                <ul className="ask42-list">
                  {(run.source_status || []).map((source, index) => (
                    <li key={index}>{(platformLabel(source.platform) || source.platform) + ' · ' + source.route + ' · ' + why(source.status) + ' · ' + readerFigure(source.items ?? 0) + ' items'}</li>
                  ))}
                </ul>
              </dd>
            </dl>
          </details>
        </footer>
      </article>
      <SourcePanel evidence={pinned} quotes={pinned ? quotesFor(answer, pinned.id) : []} onClose={() => setPinnedId(null)} />
    </div>
  );
}
