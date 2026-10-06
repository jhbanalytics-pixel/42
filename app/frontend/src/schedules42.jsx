/* Scheduled questions on the 42 API (core/api/contract.md section 14.2): a
   saved question re-asked on a cadence, every answer kept. Lists each
   schedule with its last run and, when that run was skipped, why; Pause and
   Resume append a row, so nothing is deleted; a form saves a new one. Ask
   hands a finished question over as #/schedules?q=<question>&market=ZA. */
import {useEffect, useId, useRef, useState} from 'react';
import {MarketScopeNote, inMarket, pickedMarket, useFollowedMarket} from './marketScope.jsx';
import {createSchedule, listSchedules, pauseSchedule, resumeSchedule, scheduledRunWords} from './api42.js';
import {longDate} from './ui/TrendCard.jsx';
import {MARKET_WORDS} from './ui/WatchDialog.jsx';
import './styles/today42.css';
import './styles/alerts42.css';

const MARKETS = ['ZA', 'NG', 'KE'];
const TIERS = [
  {id: 'T0', label: 'Lookup', cost: 'the lightest read, up to 10 credits each time it runs'},
  {id: 'T1', label: 'Quick scan', cost: 'a wider read of fresh posts, up to 60 credits each time it runs'},
];
const CADENCES = [
  {id: 'weekly_monday', label: 'Every Monday'},
  {id: 'daily', label: 'Every day'},
];
const DELIVERY = [
  {id: 'in_app', label: 'In the app'},
  {id: 'channel', label: 'Team channel (not sent yet)'},
];
/* The scheduled job keeps every answer in the app but sends nothing to a channel yet (core/api/scheduled.py reads
   no delivery), so the form offers only the app; older rows that chose the channel say it was not sent. */
const OFFERED = DELIVERY.filter((d) => d.id === 'in_app');
const MAX_QUESTION = 500;
const NO_ANSWER = 'The 42 service did not answer. Try again.';
const list = (value) => (Array.isArray(value) ? value : []);
const wordOf = (options, id) => (options.find((o) => o.id === id) || {}).label || id;
const marketWord = (market) => MARKET_WORDS[market] || market;

export const runWords = scheduledRunWords;
const askFollowHref = (askId) => '#/ask?follow=' + encodeURIComponent(askId);

function deliverWords(deliver){
  const words = list(deliver).map((d) => wordOf(DELIVERY, d));
  return words.length > 1 ? words.slice(0, -1).join(', ') + ' and ' + words[words.length - 1] : words.join('');
}

/* A link's market wins; with none the form starts on the header's market,
   and on South Africa when the header reads All markets. */
function prefillOf(search, picked){
  const query = new URLSearchParams(String(search || ''));
  const market = String(query.get('market') || '').toUpperCase();
  return {question: String(query.get('q') || '').slice(0, MAX_QUESTION), market: MARKETS.includes(market) ? market : '',
    fallback: MARKETS.includes(picked) ? picked : 'ZA'};
}

export function SchedulesPage42({search, onAuth, region}){
  const picked = pickedMarket(region);
  const [schedules, setSchedules] = useState({state: 'loading'});
  const [tick, setTick] = useState(0);
  const authRef = useRef(onAuth);
  authRef.current = onAuth;

  useEffect(() => {
    const ctrl = new AbortController();
    setSchedules({state: 'loading'});
    listSchedules({signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setSchedules({state: 'ready', items: list(data && data.schedules)}); })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){
          setSchedules({state: 'auth'});
          if (authRef.current) authRef.current();
          return;
        }
        setSchedules({state: 'error'});
      });
    return () => ctrl.abort();
  }, [tick]);

  /* A pause or resume answers with the schedule row alone, so its last run
     stays as it was read. */
  const replace = (next) => setSchedules((prev) => (prev.state === 'ready'
    ? {...prev, items: prev.items.map((s) => (s.schedule_id === next.schedule_id ? {...s, ...next, last_run: s.last_run, skip_reason: s.skip_reason} : s))}
    : prev));
  /* A save made before the list arrived reads the list again, since the
     read in flight was taken before the save. */
  const add = (made) => {
    if (schedules.state !== 'ready'){ setTick((t) => t + 1); return; }
    setSchedules((prev) => (prev.state === 'ready'
      ? {...prev, items: [{last_run: null, skip_reason: null, ...made}, ...prev.items]}
      : prev));
  };

  return (
    <section className="page t42 a42">
      <header className="t42-head">
        {/* Shell consistency, 2 October 2026: the title is the menu's noun. */}
        <h1 className="t42-heading">Schedules</h1>
        <p className="t42-status">Save a question and 42 asks it again on the days you choose, keeping every answer.</p>
      </header>
      {/* Layout pass, 4 October 2026: the saved list sits beside how a
          schedule runs, and the form lays the question beside its settings. */}
      <div className="a42-pair">
        <Schedules picked={picked} schedules={schedules} onRetry={() => setTick((t) => t + 1)} onChange={replace} onAuth={onAuth} />
        <section className="t42-section a42-section" data-section="how" aria-labelledby="s42-how-title">
          <h2 className="a42-title" id="s42-how-title">How a schedule runs</h2>
          <ol className="a42-steps">
            <li><span className="a42-step-n" aria-hidden="true">1</span><span className="a42-step-text"><strong>Save the question.</strong> Pick its market, how often it runs and how deep it reads.</span></li>
            {/* Demo polish, 2 October 2026: still true (a schedule waits for
                its share of the daily questions), but no longer read as a
                feature that does not run. */}
            <li><span className="a42-step-n" aria-hidden="true">2</span><span className="a42-step-text"><strong>42 asks it again.</strong> Scheduled questions run at 07:00 South Africa time (06:00 in Lagos, 08:00 in Nairobi). Saved schedules start running once their share of the daily questions is set.</span></li>
            <li><span className="a42-step-n" aria-hidden="true">3</span><span className="a42-step-text"><strong>Every answer is kept.</strong> Open the latest from its row here or in History.</span></li>
          </ol>
        </section>
      </div>
      <AddSchedule prefill={prefillOf(search, picked)} onAdd={add} onAuth={onAuth} />
    </section>
  );
}

function LastRun({schedule}){
  const run = schedule.last_run;
  if (!run) return <span className="a42-meta">Not run yet</span>;
  return (
    <span className="a42-meta">
      {'Last run ' + longDate(run.date) + ': ' + (run.status === 'skipped' && schedule.skip_reason ? 'Skipped, ' + schedule.skip_reason : runWords(run))}
      {run.ask_id && <>{' · '}<a className="t42-link" href={askFollowHref(run.ask_id)}>Read the answer</a></>}
    </span>
  );
}

function Schedules({picked = '', schedules, onRetry, onChange, onAuth}){
  const [rows, setRows] = useState({});
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);

  const toggle = (schedule) => {
    const id = schedule.schedule_id;
    const paused = schedule.status === 'paused';
    setRows((r) => ({...r, [id]: {busy: true}}));
    (paused ? resumeSchedule : pauseSchedule)(id)
      .then((next) => {
        if (!live.current) return;
        onChange(next && next.schedule_id ? next : {...schedule, status: paused ? 'active' : 'paused'});
        setRows((r) => ({...r, [id]: {}}));
      })
      .catch((error) => {
        if (!live.current) return;
        if (error && error.auth){ setRows((r) => ({...r, [id]: {}})); if (onAuth) onAuth(); return; }
        setRows((r) => ({...r, [id]: {error: error && error.status ? error.message : NO_ANSWER}}));
      });
  };

  let body;
  if (schedules.state === 'loading') body = <p className="t42-status" role="status">Loading scheduled questions</p>;
  else if (schedules.state === 'auth') body = <p className="t42-status">Enter the passcode to read the scheduled questions.</p>;
  else if (schedules.state === 'error'){
    body = (
      <>
        <p className="t42-status" role="alert">The scheduled questions could not load.</p>
        <button type="button" className="t42-button" onClick={onRetry}>Try again</button>
      </>
    );
  } else if (schedules.items.length === 0){
    body = (
      <div className="a42-start">
        <p className="t42-line-text a42-empty"><strong className="a42-empty-lead">No scheduled questions yet.</strong> Schedule one below, or choose Ask every Monday under an answer in Ask. Each one shows here with its market, how often it runs and its last answer.</p>
      </div>
    );
  }
  else {
    body = (
      <>
      <MarketScopeNote picked={picked} hidden={schedules.items.filter((s) => !inMarket(s.market, picked)).length} what="scheduled questions are" />
      <ul className="a42-list">
        {schedules.items.filter((s) => inMarket(s.market, picked)).map((s) => {
          const row = rows[s.schedule_id] || {};
          const paused = s.status === 'paused';
          return (
            <li key={s.schedule_id} className="a42-watch" data-schedule={s.schedule_id}>
              <span className="a42-name">{s.question}</span>
              <span className="a42-meta">{[marketWord(s.market), wordOf(TIERS, s.tier), wordOf(CADENCES, s.cadence), deliverWords(s.deliver)].join(' · ')}</span>
              <span className="a42-state">{paused ? 'Paused' : 'Active'}</span>
              <LastRun schedule={s} />
              <button type="button" className="t42-button" disabled={Boolean(row.busy)} onClick={() => toggle(s)}>{paused ? 'Resume' : 'Pause'}</button>
              {row.error && <p className="w42-error" role="alert">{row.error}</p>}
            </li>
          );
        })}
      </ul>
      </>
    );
  }
  return (
    <section className="t42-section a42-section" data-section="schedules" aria-labelledby="s42-list-title">
      <h2 className="a42-title" id="s42-list-title">Saved questions</h2>
      <p className="t42-line-text a42-note">Everyone with the passcode shares this list.</p>
      {body}
    </section>
  );
}

function AddSchedule({prefill, onAdd, onAuth}){
  const [question, setQuestion] = useState(prefill.question);
  const [market, setMarket] = useFollowedMarket(prefill.fallback, prefill.market);
  const [tier, setTier] = useState('T1');
  const [cadence, setCadence] = useState('weekly_monday');
  const [deliver, setDeliver] = useState(['in_app']);
  const [note, setNote] = useState({status: 'idle'});
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const id = useId();

  const flip = (value) => setDeliver((d) => (d.includes(value) ? d.filter((x) => x !== value) : DELIVERY.map((o) => o.id).filter((x) => x === value || d.includes(x))));

  const submit = (event) => {
    event.preventDefault();
    const words = question.trim();
    if (words.length < 3 || words.length > MAX_QUESTION){ setNote({status: 'error', message: 'Type a question of 3 to 500 characters.'}); return; }
    if (deliver.length === 0){ setNote({status: 'error', message: 'Choose at least one place for the answer.'}); return; }
    setNote({status: 'saving'});
    createSchedule({question: words, market, tier, cadence, deliver})
      .then((made) => {
        if (!live.current) return;
        onAdd(made);
        setQuestion('');
        setNote({status: 'saved', message: 'Saved. 42 asks it ' + (cadence === 'daily' ? 'every day' : 'every Monday') + '.'});
      })
      .catch((failure) => {
        if (!live.current) return;
        if (failure && failure.auth){ setNote({status: 'idle'}); if (onAuth) onAuth(); return; }
        setNote({status: 'error', message: failure && failure.status ? failure.message : NO_ANSWER});
      });
  };

  return (
    <section className="t42-section a42-section" data-section="add" aria-labelledby="s42-add-title">
      <h2 className="a42-title" id="s42-add-title">Schedule a question</h2>
      <form className="a42-form s42-form" onSubmit={submit} noValidate>
        <span className="a42-field">
          <label className="a42-label" htmlFor={id + '-question'}>Question</label>
          <textarea id={id + '-question'} name="question" className="a42-control" rows={3} maxLength={MAX_QUESTION} style={{padding: 'var(--s-2) var(--s-3)', resize: 'vertical'}}
            value={question} onChange={(e) => setQuestion(e.target.value)} />
        </span>
        <div className="a42-fields">
          <span className="a42-field">
            <label className="a42-label" htmlFor={id + '-market'}>Market</label>
            <select id={id + '-market'} name="market" className="a42-control" value={market} onChange={(e) => setMarket(e.target.value)}>
              {MARKETS.map((m) => <option key={m} value={m}>{marketWord(m)}</option>)}
            </select>
          </span>
          <span className="a42-field">
            <label className="a42-label" htmlFor={id + '-cadence'}>How often</label>
            <select id={id + '-cadence'} name="cadence" className="a42-control" value={cadence} onChange={(e) => setCadence(e.target.value)}>
              {CADENCES.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
          </span>
        </div>
        <fieldset className="w42-rules">
          <legend className="w42-legend">How deep</legend>
          {TIERS.map((t) => (
            <label key={t.id} className="w42-rule">
              <input type="radio" name="tier" value={t.id} checked={tier === t.id} onChange={() => setTier(t.id)} />
              {t.label} <span className="a42-meta">{t.cost}</span>
            </label>
          ))}
        </fieldset>
        <fieldset className="w42-rules">
          <legend className="w42-legend">Where the answer goes</legend>
          {OFFERED.map((d) => (
            <label key={d.id} className="w42-rule">
              <input type="checkbox" name="deliver" value={d.id} checked={deliver.includes(d.id)} onChange={() => flip(d.id)} />
              {d.label}
            </label>
          ))}
          <p className="a42-meta">Sending answers to the team channel is not connected yet.</p>
        </fieldset>
        {note.status === 'error' && <p className="w42-error" role="alert">{note.message}</p>}
        <p className="a42-saved" role="status">{note.status === 'saved' ? note.message : ''}</p>
        <button type="submit" className="w42-primary" disabled={note.status === 'saving'} aria-busy={note.status === 'saving' ? 'true' : 'false'}>Schedule</button>
      </form>
    </section>
  );
}
