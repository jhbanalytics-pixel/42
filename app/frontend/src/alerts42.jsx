/* Alerts on the 42 API (docs/full-42/EXPERIENCE.md, Alerts; core/api/
   contract.md section 10.5): watch a topic, sound, creator or brand. Lists
   today's alerts and the rules still waiting for their data, every current
   watch with Pause or Resume, and a form to add a watch. Nothing is deleted:
   a paused watch stays listed. */
import {useEffect, useId, useRef, useState} from 'react';
import {MarketScopeNote, inMarket, pickedMarket, useFollowedMarket} from './marketScope.jsx';
import {createWatch, fetchAlerts, listWatches, pauseWatch, resumeWatch} from './api42.js';
import {longDate, topicHref} from './ui/TrendCard.jsx';
import {FIRST_CHOICE, MARKET_WORDS, RuleChoice, heldWords, ruleFrom, ruleWords, waitingWords} from './ui/WatchDialog.jsx';
import './styles/today42.css';
import './styles/alerts42.css';

const KINDS = [
  {id: 'hashtag', label: 'Hashtag', field: 'Hashtag', hint: '#amapiano', empty: 'Type the hashtag to watch.'},
  {id: 'sound', label: 'Sound', field: 'Sound', hint: 'Sound name', empty: 'Type the sound to watch.'},
  {id: 'creator', label: 'Creator', field: 'Creator', hint: '@handle', empty: 'Type the creator to watch.'},
  {id: 'brand', label: 'Brand', field: 'Brand', hint: 'Brand name', empty: 'Type the brand to watch.'},
  {id: 'query', label: 'Query', field: 'Words', hint: 'Words to look for', empty: 'Type the words to watch.'},
];
const KIND_WORDS = {item: 'Trend', hashtag: 'Hashtag', sound: 'Sound', creator: 'Creator', brand: 'Brand', query: 'Query'};
const MARKETS = ['ZA', 'NG', 'KE', 'all'];
const NO_ANSWER = 'The 42 service did not answer. Try again.';
const list = (value) => (Array.isArray(value) ? value : []);
const marketWord = (market) => MARKET_WORDS[market] || market;
const heldOf = (alert) => (alert.card && alert.card.held_back ? alert.card.held_back : null);
const waitingOf = (alerts) => (alerts.state === 'ready'
  ? list(alerts.data.alerts).filter((a) => a.waiting).concat(list(alerts.data.waiting))
  : []);

export function AlertsPage42({onAuth, region}){
  const picked = pickedMarket(region);
  const [watches, setWatches] = useState({state: 'loading'});
  const [alerts, setAlerts] = useState({state: 'loading'});
  const [tick, setTick] = useState(0);
  const [alertsTick, setAlertsTick] = useState(0);
  const authRef = useRef(onAuth);
  authRef.current = onAuth;
  const asked = useRef(false);

  /* Both reads can meet the same 401; the passcode flow is asked for once. */
  const failed = (error) => {
    if (error && error.auth){
      if (!asked.current){
        asked.current = true;
        if (authRef.current) authRef.current();
      }
      return {state: 'auth'};
    }
    return {state: 'error'};
  };

  useEffect(() => {
    const ctrl = new AbortController();
    setWatches({state: 'loading'});
    listWatches({signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setWatches({state: 'ready', items: list(data && data.watches)}); })
      .catch((error) => { if (!ctrl.signal.aborted) setWatches(failed(error)); });
    return () => ctrl.abort();
  }, [tick]);

  useEffect(() => {
    const ctrl = new AbortController();
    setAlerts({state: 'loading'});
    fetchAlerts(undefined, {signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setAlerts({state: 'ready', data: data || {}}); })
      .catch((error) => { if (!ctrl.signal.aborted) setAlerts(failed(error)); });
    return () => ctrl.abort();
  }, [alertsTick]);

  const replace = (watch) => setWatches((prev) => (prev.state === 'ready'
    ? {...prev, items: prev.items.map((w) => (w.watch_id === watch.watch_id ? watch : w))}
    : prev));
  const add = (watch) => setWatches((prev) => (prev.state === 'ready' ? {...prev, items: [watch, ...prev.items]} : prev));
  const known = watches.state === 'ready' ? watches.items : [];

  return (
    <section className="page t42 a42">
      <header className="t42-head">
        <h1 className="t42-heading">Alerts</h1>
        <p className="t42-status">What are you watching, and has any of it moved? Watch a topic, sound, creator or brand and get an alert when it moves.</p>
      </header>
      {/* Layout pass, 4 October 2026: what moved and what is watched read
          down the left column and the add form fills the right, so the two
          columns end close together instead of leaving a hole under the
          shorter one. One column on a phone, in reading order. */}
      <div className="a42-board">
        <TodaysAlerts picked={picked} alerts={alerts} watches={known} onRetry={() => setAlertsTick((t) => t + 1)} />
        <Watches picked={picked} watches={watches} waiting={waitingOf(alerts)} onRetry={() => setTick((t) => t + 1)} onChange={replace} onAuth={onAuth} />
        <AddWatch onAdd={add} onAuth={onAuth} market={picked || (region === 'ALL' ? 'all' : 'ZA')} />
      </div>
    </section>
  );
}

function TodaysAlerts({picked = '', alerts, watches, onRetry}){
  let body;
  if (alerts.state === 'loading') body = <p className="t42-status" role="status">Loading alerts</p>;
  else if (alerts.state === 'auth') body = <p className="t42-status">Enter the passcode to read alerts.</p>;
  else if (alerts.state === 'error'){
    body = (
      <>
        <p className="t42-status" role="alert">The alerts could not load.</p>
        <button type="button" className="t42-button" onClick={onRetry}>Try again</button>
      </>
    );
  } else {
    const allFired = list(alerts.data.alerts).filter((a) => !a.waiting);
    const fired = allFired.filter((a) => inMarket(a.market, picked));
    const waiting = waitingOf(alerts);
    const labelOf = (entry) => {
      if (entry.label) return entry.label;
      const watch = watches.find((w) => w.watch_id === entry.watch_id);
      return watch && watch.label ? watch.label : 'A watch';
    };
    body = (
      <>
        <div className="a42-count" data-quiet={fired.length === 0 ? '' : undefined}>
          <p className="t42-line-text a42-count-line">
            {fired.length === 0 ? 'No alerts today.' : fired.length === 1 ? '1 alert today' : fired.length + ' alerts today'}
          </p>
          {alerts.data.date && <p className="a42-meta">Checked on {longDate(alerts.data.date)}.</p>}
        </div>
        {fired.length === 0 && waiting.length === 0 && (
          <p className="a42-explain">
            {watches.length === 0
              ? 'Nothing is watched yet, so there is nothing to check. Add a watch and its alerts land here and on Today.'
              : 'When a watch moves, it lands here and on Today with the reason it fired, its market and the day it started.'}
          </p>
        )}
        <MarketScopeNote picked={picked} hidden={allFired.length - fired.length} what={allFired.length - fired.length === 1 ? 'alert is' : 'alerts are'} />
        {fired.length > 0 && (
          <ul className="a42-list">
            {fired.map((a) => {
              const held = heldOf(a);
              return (
                <li key={a.watch_id + ':' + a.market + ':' + a.item_id} className="a42-alert" data-held={held ? '' : undefined}>
                  <a className="t42-link a42-name" href={topicHref(a.item_id, a.market)}>{a.label}</a>
                  {held && <span className="a42-held">{heldWords(held)}</span>}
                  <span className="a42-meta">{a.fired_because} · {marketWord(a.market)}{a.since ? ' · since ' + longDate(a.since) : ''}</span>
                </li>
              );
            })}
          </ul>
        )}
        {waiting.length > 0 && (
          <>
            <h3 className="a42-subtitle">Not checked yet</h3>
            <ul className="a42-waiting">
              {waiting.map((w) => <li key={w.watch_id}>{labelOf(w)}: {w.waiting}</li>)}
            </ul>
          </>
        )}
      </>
    );
  }
  return (
    <section className="t42-section a42-section" data-section="alerts" aria-labelledby="a42-alerts-title">
      <h2 className="a42-title" id="a42-alerts-title">Today's alerts</h2>
      {body}
    </section>
  );
}

function Watches({picked = '', watches, waiting, onRetry, onChange, onAuth}){
  const [rows, setRows] = useState({});
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);

  const toggle = (watch) => {
    const id = watch.watch_id;
    setRows((r) => ({...r, [id]: {busy: true}}));
    (watch.status === 'paused' ? resumeWatch : pauseWatch)(id)
      .then((next) => {
        if (!live.current) return;
        onChange(next && next.watch_id ? next : {...watch, status: watch.status === 'paused' ? 'active' : 'paused'});
        setRows((r) => ({...r, [id]: {}}));
      })
      .catch((error) => {
        if (!live.current) return;
        if (error && error.auth){ setRows((r) => ({...r, [id]: {}})); if (onAuth) onAuth(); return; }
        setRows((r) => ({...r, [id]: {error: error && error.status ? error.message : NO_ANSWER}}));
      });
  };

  const waitingFor = (watch) => {
    const entry = waiting.find((w) => w.watch_id === watch.watch_id);
    return entry ? entry.waiting : waitingWords(watch.rule);
  };

  let body;
  if (watches.state === 'loading') body = <p className="t42-status" role="status">Loading watches</p>;
  else if (watches.state === 'auth') body = <p className="t42-status">Enter the passcode to read the watches.</p>;
  else if (watches.state === 'error'){
    body = (
      <>
        <p className="t42-status" role="alert">The watches could not load.</p>
        <button type="button" className="t42-button" onClick={onRetry}>Try again</button>
      </>
    );
  } else if (watches.items.length === 0){
    body = (
      <div className="a42-start">
        <p className="t42-line-text a42-empty">No watches yet. Add one and 42 checks it each time the trends update.</p>
        <ol className="a42-steps">
          <li><span className="a42-step-n" aria-hidden="true">1</span><span className="a42-step-text"><strong>Name it.</strong> A hashtag, sound, creator, brand or any words.</span></li>
          <li><span className="a42-step-n" aria-hidden="true">2</span><span className="a42-step-text"><strong>Pick the moment.</strong> Rising, growth or reach past a number, a breakout or a tone flip.</span></li>
          <li><span className="a42-step-n" aria-hidden="true">3</span><span className="a42-step-text"><strong>Read the alert.</strong> It shows on this page and on Today, with why it fired.</span></li>
        </ol>
      </div>
    );
  }
  else {
    body = (
      <>
      <MarketScopeNote picked={picked} hidden={watches.items.filter((w) => !inMarket(w.market, picked)).length} what="watches are" />
      <ul className="a42-list">
        {watches.items.filter((w) => inMarket(w.market, picked)).map((w) => {
          const target = w.target || {};
          const row = rows[w.watch_id] || {};
          const paused = w.status === 'paused';
          return (
            <li key={w.watch_id} className="a42-watch" data-watch={w.watch_id}>
              <span className="a42-name">
                {target.kind === 'item' ? <a className="t42-link" href={topicHref(target.item_id, w.market)}>{w.label}</a> : w.label}
              </span>
              <span className="a42-meta">{KIND_WORDS[target.kind] || 'Watch'} · {marketWord(w.market)} · {ruleWords(w.rule)}</span>
              <span className="a42-state">{paused ? 'Paused' : waitingFor(w) || 'Active'}</span>
              <button type="button" className="t42-button" disabled={Boolean(row.busy)} onClick={() => toggle(w)}>{paused ? 'Resume' : 'Pause'}</button>
              {row.error && <p className="w42-error" role="alert">{row.error}</p>}
            </li>
          );
        })}
      </ul>
      </>
    );
  }
  return (
    <section className="t42-section a42-section" data-section="watches" aria-labelledby="a42-watches-title">
      <h2 className="a42-title" id="a42-watches-title">Watches</h2>
      {/* Demo polish, 2 October 2026: who sees the list, in plain words. */}
      <p className="t42-line-text a42-note">This list is shared with your team.</p>
      {body}
    </section>
  );
}

/* One line on what pressing Watch will do, in the reader's own words. */
function summaryOf(spec, value, market, choice, creator){
  const {rule} = ruleFrom(choice, {creator});
  const when = rule ? ruleWords(rule).replace(/^When/, 'when') : 'when the rule you pick is met';
  const words = value.trim();
  const where = market === 'all' ? 'any market' : marketWord(market);
  return words
    ? 'Alerts you about ' + words + ' in ' + where + ' ' + when + '.'
    : 'Name the ' + spec.label.toLowerCase() + ' and 42 alerts you in ' + where + ' ' + when + '.';
}

function AddWatch({onAdd, onAuth, market: headerMarket = 'ZA'}){
  const [kind, setKind] = useState('hashtag');
  const [value, setValue] = useState('');
  const [market, setMarket] = useFollowedMarket(headerMarket);
  const [choice, setChoice] = useState(FIRST_CHOICE);
  const [note, setNote] = useState({status: 'idle'});
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const id = useId();
  const spec = KINDS.find((k) => k.id === kind) || KINDS[0];

  const submit = (event) => {
    event.preventDefault();
    const words = value.trim();
    if (!words){ setNote({status: 'error', message: spec.empty}); return; }
    const {rule, error} = ruleFrom(choice, {creator: kind === 'creator'});
    if (error){ setNote({status: 'error', message: error}); return; }
    setNote({status: 'saving'});
    createWatch({target: {kind, value: words}, market, rule, label: words.slice(0, 120)})
      .then((watch) => {
        if (!live.current) return;
        onAdd(watch);
        setValue('');
        setNote({status: 'saved', message: 'Watching ' + (watch.label || words) + '.'});
      })
      .catch((failure) => {
        if (!live.current) return;
        if (failure && failure.auth){ setNote({status: 'idle'}); if (onAuth) onAuth(); return; }
        setNote({status: 'error', message: failure && failure.status ? failure.message : NO_ANSWER});
      });
  };

  return (
    <section className="t42-section a42-section" data-section="add" aria-labelledby="a42-add-title">
      <h2 className="a42-title" id="a42-add-title">Add a watch</h2>
      <form className="a42-form a42-form-wide" onSubmit={submit} noValidate>
        <div className="a42-fields">
          <span className="a42-field">
            <label className="a42-label" htmlFor={id + '-kind'}>What to watch</label>
            <select id={id + '-kind'} name="kind" className="a42-control" value={kind} onChange={(e) => setKind(e.target.value)}>
              {KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
            </select>
          </span>
          <span className="a42-field">
            <label className="a42-label" htmlFor={id + '-value'}>{spec.field}</label>
            <input id={id + '-value'} name="value" type="text" className="a42-control" maxLength={200} placeholder={spec.hint}
              value={value} onChange={(e) => setValue(e.target.value)} />
          </span>
          <span className="a42-field">
            <label className="a42-label" htmlFor={id + '-market'}>Market</label>
            <select id={id + '-market'} name="market" className="a42-control" value={market} onChange={(e) => setMarket(e.target.value)}>
              {MARKETS.map((m) => <option key={m} value={m}>{marketWord(m)}</option>)}
            </select>
          </span>
        </div>
        <RuleChoice choice={choice} onChange={setChoice} creator={kind === 'creator'} />
        {note.status === 'error' && <p className="w42-error" role="alert">{note.message}</p>}
        <p className="a42-saved" role="status">{note.status === 'saved' ? note.message : ''}</p>
        {/* The button sits beside the sentence it carries out, so it reads as
            the end of the form rather than a lone red block under it. */}
        <div className="a42-submit">
          <p className="a42-summary">{summaryOf(spec, value, market, choice, kind === 'creator')}</p>
          <button type="submit" className="w42-primary" disabled={note.status === 'saving'} aria-busy={note.status === 'saving' ? 'true' : 'false'}>Watch</button>
        </div>
      </form>
    </section>
  );
}
