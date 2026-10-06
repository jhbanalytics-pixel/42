/* Build, #/console: the place to make things in 42 from its own data.
   Three ways in (ask a question, draft an investigation, start a dossier
   from a finished answer) and the work already made, each list read from
   the 42 API (contract.md sections 12.4, 13.1, 13.2 and 14.2). The market
   the header holds shapes every list that has a market; questions carry
   none, so they are always shown. Every list says in plain words when it
   is loading, empty or could not be read. */
import {Fragment, useEffect, useRef, useState} from 'react';
import './styles/ask42.css';
import './styles/build42.css';
import {createInvestigation, createInvestigationDossier, createDossier, fetchHistoryAsks, fetchHistoryBriefs, listDossiers, listInvestigations, listSchedules} from './api42.js';
import {go} from './router.js';
import {postDay} from './ui/EvidenceChip.jsx';

const MARKETS = [
  {code: 'ZA', name: 'South Africa'},
  {code: 'NG', name: 'Nigeria'},
  {code: 'KE', name: 'Kenya'},
];
const MARKET_NAME = Object.fromEntries(MARKETS.map((m) => [m.code, m.name]));

const INVESTIGATION_WORD = {draft: 'Draft', running: 'Running', complete: 'Finished', stopped: 'Stopped', failed: 'Failed'};
const BRIEF_WORD = {published: 'Published', partial: 'Published, some trends not yet explained', data_issue: 'Data issue', not_ready: 'Not ready', failed: 'Failed'};
const CADENCE_WORD = {weekly_monday: 'Every Monday', daily: 'Every day'};
const RECENT = 5;

export const BUILD_API = Object.freeze({
  listInvestigations, listDossiers, fetchHistoryAsks, fetchHistoryBriefs, listSchedules,
  createInvestigation, createInvestigationDossier, createDossier,
});

const marketOf = (region) => (MARKET_NAME[region] ? region : '');

/* "in South Africa" when the header names one market, nothing for all three. */
const inMarket = (market) => (market ? ' in ' + MARKET_NAME[market] : '');

function failureWords(error, what){
  if (error && error.auth) return 'Enter the passcode to read ' + what + '.';
  /* A refusal speaks in the server's own words; a service that is down or
     out of reach gets the same plain line in every list. */
  if (error && error.status && error.status < 500 && error.message) return error.message;
  return 'Your ' + what + ' could not be read right now. Try again in a minute.';
}

function useAuthHandover(onAuth){
  const ref = useRef(onAuth);
  ref.current = onAuth;
  return (error) => { if (error && error.auth && ref.current) ref.current(); };
}

/* One read per list. Each list fails on its own, so one service that is
   down never blanks the rest of the page. */
function useList(read, pick, handover, deps){
  const [state, setState] = useState({phase: 'loading', items: [], error: null});
  const [round, setRound] = useState(0);
  useEffect(() => {
    const ctrl = new AbortController();
    setState({phase: 'loading', items: [], error: null});
    Promise.resolve()
      .then(() => read({signal: ctrl.signal}))
      .then((data) => {
        if (ctrl.signal.aborted) return;
        const items = pick(data);
        setState({phase: 'ready', items: Array.isArray(items) ? items : [], error: null});
      })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        setState({phase: 'error', items: [], error});
        handover(error);
      });
    return () => ctrl.abort();
  }, [...deps, round]);
  return [state, () => setRound((n) => n + 1)];
}

/* A row's details, each kept whole on its line, joined by middle dots. */
function Meta({parts, children}){
  const shown = parts.filter(Boolean);
  if (!children && shown.length === 0) return null;
  return (
    <span className="b42-meta">
      {children}
      {shown.map((part, index) => <Fragment key={index}>{(children || index > 0) ? ' ' : ''}<span className="b42-meta-part">{part}</span></Fragment>)}
    </span>
  );
}

/* A section of recent work: its heading, a link to the full page, and the
   loading, error, empty or filled list. */
function Recent({id, title, more, state, retry, what, empty, children}){
  const {phase, items, error} = state;
  return (
    <section className="b42-recent" aria-labelledby={id} aria-busy={phase === 'loading' ? 'true' : 'false'}>
      <div className="b42-recent-head">
        <h3 className="b42-list-title" id={id}>{title}</h3>
        {more && <a className="b42-more" href={more.href}>{more.label}</a>}
      </div>
      {phase === 'loading' && <p className="b42-quiet" role="status">{'Reading ' + what}</p>}
      {phase === 'error' && (
        <div className="b42-error" role="alert">
          <p>{failureWords(error, what)}</p>
          <button type="button" className="ask42-quiet b42-button" onClick={retry}>Try again</button>
        </div>
      )}
      {phase === 'ready' && items.length === 0 && <p className="b42-empty">{empty}</p>}
      {phase === 'ready' && items.length > 0 && <ol className="b42-list">{children}</ol>}
    </section>
  );
}

/* ---------------- ask a question ---------------- */

function AskStart({market}){
  const [question, setQuestion] = useState('');
  const [chosen, setChosen] = useState(market);
  useEffect(() => { setChosen(market); }, [market]);
  const words = question.trim();
  const submit = (event) => {
    event.preventDefault();
    if (words.length < 3) return;
    /* draft=1 fills Ask with the question; the reader asks there, beside its cost, so nothing spends from here. */
    const query = new URLSearchParams({q: words});
    if (chosen) query.set('market', chosen);
    query.set('draft', '1');
    go('/ask?' + query.toString());
  };
  return (
    <section className="b42-start" aria-labelledby="b42-ask-title">
      <h2 className="b42-section-title" id="b42-ask-title">Ask a question</h2>
      <p className="b42-help">A quick answer with its sources.</p>
      <form className="b42-form" onSubmit={submit}>
        <label className="b42-label" htmlFor="b42-ask-question">Your question</label>
        <textarea id="b42-ask-question" className="ask42-input" rows={3} maxLength={500} value={question} onChange={(event) => setQuestion(event.target.value)} />
        <div className="b42-actions">
          <MarketField id="b42-ask-market" value={chosen} onChange={setChosen} />
          <button type="submit" className="ask42-primary b42-button" disabled={words.length < 3}>Ask</button>
        </div>
        <div className="b42-notes" />
      </form>
    </section>
  );
}

/* The market a question is about; the header's market until changed here. */
function MarketField({id, value, onChange}){
  return (
    <span className="b42-field">
      <label className="b42-label" htmlFor={id}>Market</label>
      <select id={id} className="ask42-select" value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">From the question</option>
        {MARKETS.map((m) => <option key={m.code} value={m.code}>{m.name}</option>)}
      </select>
    </span>
  );
}

/* ---------------- draft an investigation ---------------- */

function InvestigationStart({market, api, handover}){
  const [question, setQuestion] = useState('');
  const [chosen, setChosen] = useState(market);
  const [state, setState] = useState({busy: false, error: null});
  useEffect(() => { setChosen(market); }, [market]);
  const words = question.trim();

  async function draft(event){
    event.preventDefault();
    if (words.length < 3 || state.busy) return;
    setState({busy: true, error: null});
    try {
      const made = await api.createInvestigation({question: words, market: chosen});
      go('/investigations/' + encodeURIComponent(made.investigation_id));
    } catch (error){
      setState({busy: false, error});
      handover(error);
    }
  }

  return (
    <section className="b42-start" aria-labelledby="b42-inv-title">
      <h2 className="b42-section-title" id="b42-inv-title">Start an investigation</h2>
      <p className="b42-help">A research plan you check before it runs.</p>
      <form className="b42-form" onSubmit={draft}>
        <label className="b42-label" htmlFor="b42-inv-question">What should 42 find out?</label>
        <textarea id="b42-inv-question" className="ask42-input" rows={3} maxLength={2000} value={question} onChange={(event) => setQuestion(event.target.value)} />
        <div className="b42-actions">
          <MarketField id="b42-inv-market" value={chosen} onChange={setChosen} />
          <button type="submit" className="ask42-primary b42-button" disabled={state.busy || words.length < 3} aria-busy={state.busy ? 'true' : 'false'}>Draft a plan</button>
        </div>
        <div className="b42-notes">
          {state.busy && <p className="b42-quiet" role="status">Drafting the plan</p>}
          {state.error && <p className="b42-error" role="alert">{state.error.auth ? 'Enter the passcode to draft an investigation.' : (state.error.message || 'The plan could not be drafted.')}</p>}
        </div>
      </form>
    </section>
  );
}

/* ---------------- start a dossier ---------------- */

/* The answers a dossier can be made from: finished investigations in the
   market, then answered questions, newest first. */
function dossierSources(investigations, asks){
  const fromInvestigations = investigations
    .filter((inv) => inv.status === 'complete')
    .map((inv) => ({key: 'i:' + inv.investigation_id, kind: 'investigation', id: inv.investigation_id, title: inv.question, at: inv.updated_at || inv.created_at, market: inv.market}));
  const fromAsks = asks
    .filter((ask) => ask.status === 'complete' && (ask.answer_status === 'complete' || ask.answer_status === 'partial'))
    .map((ask) => ({key: 'a:' + ask.ask_id, kind: 'ask', id: ask.ask_id, title: ask.question, at: ask.at, partial: ask.answer_status === 'partial'}));
  return [...fromInvestigations, ...fromAsks].slice(0, RECENT);
}

function DossierStart({investigations, asks, api, handover}){
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const loading = investigations.phase === 'loading' || asks.phase === 'loading';
  const bothFailed = investigations.phase === 'error' && asks.phase === 'error';
  const sources = dossierSources(investigations.items, asks.items);

  async function start(source){
    if (busy) return;
    setBusy(source.key);
    setError(null);
    try {
      const made = source.kind === 'investigation'
        ? await api.createInvestigationDossier(source.id)
        : await api.createDossier(source.id);
      go('/dossiers/' + encodeURIComponent(made.dossier_id));
    } catch (failure){
      setBusy(null);
      setError({key: source.key, failure});
      handover(failure);
    }
  }

  return (
    <section className="b42-dossier-start" aria-labelledby="b42-dossier-title">
      <h2 className="b42-section-title" id="b42-dossier-title">Start a dossier</h2>
      <p className="b42-help">Made from a finished answer. Check its claims, then freeze and export it.</p>
      {loading && <p className="b42-quiet" role="status">Reading finished answers</p>}
      {!loading && bothFailed && <p className="b42-error" role="alert">Finished answers could not be read. Try again in a minute.</p>}
      {!loading && !bothFailed && sources.length === 0 && (
        <p className="b42-empty">No finished answers yet. Ask a question or run an investigation, then start a dossier from it here.</p>
      )}
      {!loading && sources.length > 0 && (
        <ul className="b42-sources" aria-label="Finished answers">
          {sources.map((source) => (
            <li className="b42-source" key={source.key}>
              <span className="b42-source-title">{source.title}</span>
              <Meta parts={[source.kind === 'investigation' ? 'Investigation' : (source.partial ? 'Question, partly answered' : 'Question'), MARKET_NAME[source.market], postDay(source.at)]} />
              <button
                type="button"
                className="ask42-quiet b42-button b42-source-action"
                onClick={() => start(source)}
                disabled={Boolean(busy)}
                aria-busy={busy === source.key ? 'true' : 'false'}
                aria-label={'Start a dossier from: ' + source.title}
              >{busy === source.key ? 'Starting' : 'Start dossier'}</button>
              {error && error.key === source.key && (
                <p className="b42-error b42-source-error" role="alert">{error.failure.auth ? 'Enter the passcode to start a dossier.' : (error.failure.message || 'The dossier could not be started.')}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/* ---------------- the page ---------------- */

export function Build42({region, onAuth, api = BUILD_API}){
  const market = marketOf(region);
  const handover = useAuthHandover(onAuth);
  const sameMarket = (row) => !market || row.market === market;

  const [investigations, retryInvestigations] = useList(
    (o) => api.listInvestigations({}, o), (d) => (d && d.investigations) || [], handover, [api]);
  const [dossiers, retryDossiers] = useList(
    (o) => api.listDossiers({limit: 20}, o), (d) => (d && d.dossiers) || [], handover, [api]);
  const [asks, retryAsks] = useList(
    (o) => api.fetchHistoryAsks(market ? {limit: 20, market} : {limit: 20}, o), (d) => (d && d.asks) || [], handover, [api, market]);
  const [briefs, retryBriefs] = useList(
    (o) => api.fetchHistoryBriefs({limit: 7}, o), (d) => (d && d.dates) || [], handover, [api]);
  const [schedules, retrySchedules] = useList(
    (o) => api.listSchedules(o), (d) => (d && d.schedules) || [], handover, [api]);

  const scoped = (state, filter) => ({...state, items: state.items.filter(filter)});
  const invHere = scoped(investigations, sameMarket);
  const dossiersHere = scoped(dossiers, sameMarket);
  const schedulesHere = scoped(schedules, sameMarket);
  const briefsHere = {
    ...briefs,
    items: briefs.items
      .map((day) => ({...day, markets: (Array.isArray(day.markets) ? day.markets : []).filter(sameMarket)}))
      .filter((day) => day.markets.length > 0),
  };

  return (
    <main className="ask42 b42" aria-labelledby="b42-title">
      <h1 className="ask42-title b42-title" id="b42-title">Build</h1>
      <p className="b42-lede">
        {'Make something from what 42 has collected' + inMarket(market) + ': a quick answer, a researched investigation or a dossier you can check and export.'}
      </p>

      <div className="b42-starts">
        <AskStart market={market} />
        <InvestigationStart market={market} api={api} handover={handover} />
      </div>
      <DossierStart investigations={invHere} asks={asks} api={api} handover={handover} />

      <section className="b42-band" aria-labelledby="b42-recent-title">
      <h2 className="b42-band-title" id="b42-recent-title">Your recent work</h2>
      <div className="b42-recents">
        <Recent
          id="b42-recent-inv" title="Investigations" what="investigations"
          more={{href: '#/investigations', label: 'All investigations'}}
          state={invHere} retry={retryInvestigations}
          empty={'No investigations' + inMarket(market) + ' yet. Start one above.'}
        >
          {invHere.items.slice(0, RECENT).map((inv) => (
            <li className="b42-row" key={inv.investigation_id}>
              <a className="b42-row-title" href={'#/investigations/' + encodeURIComponent(inv.investigation_id)}>{inv.question}</a>
              <Meta parts={[MARKET_NAME[inv.market], postDay(inv.updated_at || inv.created_at)]}>
                <span className="b42-meta-part b42-state" data-state={inv.status}>{INVESTIGATION_WORD[inv.status] || inv.status}</span>
              </Meta>
            </li>
          ))}
        </Recent>

        <Recent
          id="b42-recent-dossiers" title="Dossiers" what="dossiers"
          more={{href: '#/dossiers', label: 'All dossiers'}}
          state={dossiersHere} retry={retryDossiers}
          empty={'No dossiers' + inMarket(market) + ' yet. Start one from a finished answer above.'}
        >
          {dossiersHere.items.slice(0, RECENT).map((d) => (
            <li className="b42-row" key={d.dossier_id}>
              <a className="b42-row-title" href={'#/dossiers/' + encodeURIComponent(d.dossier_id)}>{d.title || 'Untitled dossier'}</a>
              <Meta parts={[MARKET_NAME[d.market], typeof d.kept === 'number' ? d.kept + (d.kept === 1 ? ' claim kept' : ' claims kept') : '', postDay(d.created_at)]}>
                <span className="b42-meta-part b42-state" data-state={d.state}>{d.state === 'frozen' ? 'Frozen' : 'Draft, continue checking'}</span>
              </Meta>
            </li>
          ))}
        </Recent>

        <Recent
          id="b42-recent-asks" title="Questions" what="past questions"
          more={{href: '#/history', label: 'All questions'}}
          state={asks} retry={retryAsks}
          empty="No questions asked yet. Ask one above."
        >
          {asks.items.slice(0, RECENT).map((ask) => (
            <li className="b42-row" key={ask.ask_id}>
              <a className="b42-row-title" href={'#/ask?follow=' + encodeURIComponent(ask.ask_id)}>{ask.question}</a>
              <Meta parts={[askWords(ask), postDay(ask.at)]} />
            </li>
          ))}
        </Recent>

        <Recent
          id="b42-recent-briefs" title="Morning briefs" what="briefs"
          more={{href: '#/history?tab=briefs', label: 'All briefs'}}
          state={briefsHere} retry={retryBriefs}
          empty={'No briefs' + inMarket(market) + ' yet. A brief is published after each morning collection.'}
        >
          {briefsHere.items.slice(0, RECENT).map((day) => (
            <li className="b42-row" key={day.date}>
              <a className="b42-row-title" href={'#/today?date=' + encodeURIComponent(day.date)}>{postDay(day.date) || day.date}</a>
              <Meta parts={day.markets.map((m) => (MARKET_NAME[m.market] || m.market) + ': ' + (BRIEF_WORD[m.status] || m.status))} />
            </li>
          ))}
        </Recent>

        <Recent
          id="b42-recent-schedules" title="Scheduled questions" what="scheduled questions"
          more={{href: '#/schedules', label: 'All schedules'}}
          state={schedulesHere} retry={retrySchedules}
          empty={'No scheduled questions' + inMarket(market) + ' yet. Set one up on Schedules to have a question asked again each week.'}
        >
          {schedulesHere.items.slice(0, RECENT).map((s) => (
            <li className="b42-row" key={s.schedule_id}>
              <a className="b42-row-title" href="#/schedules">{s.question}</a>
              <Meta parts={[s.status === 'paused' ? 'Paused' : 'Active', CADENCE_WORD[s.cadence] || s.cadence, MARKET_NAME[s.market]]} />
            </li>
          ))}
        </Recent>
      </div>
      </section>
    </main>
  );
}

function askWords(ask){
  if (ask.status === 'failed') return 'Did not finish';
  if (ask.status === 'running' || ask.status === 'queued') return 'Still running';
  if (ask.answer_status === 'complete') return 'Answered';
  if (ask.answer_status === 'partial') return 'Partly answered';
  if (ask.answer_status === 'refused') return 'Declined';
  return '';
}
