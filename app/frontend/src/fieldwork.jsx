/* Fieldwork: the source roster 42 collects from, per market, and how each
   source did on one collection day (core/api/contract.md section 17).

   Coverage is the daily scorecard of how much was seen. Fieldwork answers a
   different question: what 42 listens to, where, and whether each source
   delivered. The roster is read from the collection settings, so it is real
   even on a day nothing ran; how a source did comes from the day's
   collection record, and a source without one says so instead of showing a
   zero. Every figure on the page comes from GET /api/fieldwork. */

import {useEffect, useId, useRef, useState} from 'react';
import {getJson} from './api42.js';
import {readerFigure} from './api.js';
import {UnitRows} from './ui/Charts42.jsx';

import './styles/fieldwork.css';

export const FIELDWORK_PATH = '/api/fieldwork';
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const MARKET_ORDER = ['ZA', 'NG', 'KE', 'GLOBAL'];
const RUN_WORDS = {ok: 'Finished', failed: 'Failed', running: 'Reported running', skipped: 'Skipped'};
/* The states a source can be in on a day, in the order a reader scans for trouble. */
const TALLY = [
  ['failed', 'failed'],
  ['partial', 'partly delivered'],
  ['delivered', 'delivered'],
  ['no_record', 'with no record'],
  ['waiting', 'not recorded yet'],
  ['off', 'off'],
];
const MEMBERS_SHOWN = 6;

export function fieldworkPath(date){
  return FIELDWORK_PATH + (date && DATE.test(date) ? '?date=' + encodeURIComponent(date) : '');
}

function longDate(value, {weekday = false} = {}){
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  if (!match) return String(value || '');
  const words = Number(match[3]) + ' ' + MONTHS[Number(match[2]) - 1] + ' ' + match[1];
  if (!weekday) return words;
  const day = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))).getUTCDay();
  return WEEKDAYS[day] + ' ' + words;
}

/* The service's own clock time, with its zone made visible. */
function clock(value){
  const match = /T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/.exec(String(value || ''));
  if (!match) return null;
  const offset = match[2];
  let zone = '';
  if (offset === '+02:00' || offset === '+0200') zone = 'SAST';
  else if (offset === 'Z' || offset === '+00:00') zone = 'UTC';
  else if (offset) zone = 'UTC' + offset;
  return {time: match[1], zone};
}

const isFigure = (value) => value && typeof value === 'object' && typeof value.value === 'number';
const plural = (n, one, many) => readerFigure(n) + ' ' + (n === 1 ? one : many);
const percent = (share) => Math.round(share * 100) + '%';

/* The page's states. Loading and a refused passcode come first; a transport
   or service failure is an error and shows no roster; a payload whose
   markets cannot be read is an error too, never an empty roster. */
export function buildFieldworkState({loading = false, payload = null, error = null} = {}){
  if (loading) return {state: 'loading'};
  if (error){
    if (error.auth || error.status === 401) return {state: 'auth'};
    return {state: 'error', message: error.status ? (error.message || 'The 42 service refused the request.') : 'The 42 service did not answer.'};
  }
  if (!payload || typeof payload !== 'object' || !Array.isArray(payload.markets) || !DATE.test(String(payload.date || ''))){
    return {state: 'error', message: 'The 42 service answered, but not with a source list this page can read.'};
  }
  if (payload.plan_state === 'unavailable' || payload.markets.length === 0){
    return {state: 'unavailable', data: payload, message: payload.plan_note || 'The collection settings could not be read, so the source list is missing.'};
  }
  const markets = [...payload.markets].sort((a, b) => MARKET_ORDER.indexOf(a.market) - MARKET_ORDER.indexOf(b.market));
  return {state: 'ready', data: {...payload, markets}};
}

export function FieldworkWorkspace({onAuth}){
  const [date, setDate] = useState(null);
  const [tick, setTick] = useState(0);
  const [load, setLoad] = useState({loading: true});
  const authRef = useRef(onAuth);
  authRef.current = onAuth;

  useEffect(() => {
    const ctrl = new AbortController();
    /* A re-read (another day, or Try again) keeps the painted roster and
       marks it busy, so the page does not drop to the loading frame. */
    setLoad((prior) => (prior.payload ? {payload: prior.payload, busy: true} : {loading: true}));
    getJson(fieldworkPath(date), {signal: ctrl.signal})
      .then((payload) => { if (!ctrl.signal.aborted) setLoad({payload}); })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        setLoad({error: error || {}});
        if (error && error.auth && authRef.current) authRef.current();
      });
    return () => ctrl.abort();
  }, [date, tick]);

  return <FieldworkPage {...load} onDate={setDate} onRetry={() => setTick((t) => t + 1)} />;
}

export function FieldworkPage({loading, busy = false, payload, error, onDate, onRetry}){
  const view = buildFieldworkState({loading, payload, error});
  const rereading = busy && (view.state === 'ready' || view.state === 'unavailable');
  return (
    <section className="page fieldwork-page" data-fieldwork-state={view.state} aria-busy={view.state === 'loading' || rereading ? 'true' : undefined}>
      <header className="fieldwork-lead">
        <h1 className="fieldwork-lead__title" tabIndex={-1}>Fieldwork</h1>
        <p className="fieldwork-lead__standfirst">The sources 42 listens to in each market, and whether each one delivered on the collection day.</p>
        <p className="fieldwork-lead__kicker">For how much was seen that day, open <a href="#/coverage">Coverage</a>.</p>
      </header>
      {rereading && <p className="fieldwork-state__note fieldwork-state__note--busy" role="status">Reading the day again</p>}
      <FieldworkBody view={view} onDate={onDate} onRetry={onRetry} />
    </section>
  );
}

function FieldworkBody({view, onDate, onRetry}){
  if (view.state === 'loading'){
    return <p className="fieldwork-state__note" role="status">Reading the source list</p>;
  }
  if (view.state === 'auth'){
    return <p className="fieldwork-state__note" role="status">Enter the passcode to read Fieldwork.</p>;
  }
  if (view.state === 'error'){
    return (
      <div className="fieldwork-problem">
        <h2 className="fieldwork-section__title">Fieldwork could not load</h2>
        <p className="fieldwork-state__note" role="alert">{view.message}</p>
        {onRetry && <button type="button" className="fieldwork-button" onClick={onRetry}>Try again</button>}
      </div>
    );
  }
  const data = view.data;
  return (
    <>
      <DaySummary data={data} onDate={onDate} />
      {data.health_note && <p className="fieldwork-state__note fieldwork-state__note--notice" role="note">{data.health_note}</p>}
      {view.state === 'unavailable'
        ? <div className="fieldwork-problem">
            <p className="fieldwork-state__note" role="alert">{view.message}</p>
            {onRetry && <button type="button" className="fieldwork-button" onClick={onRetry}>Try again</button>}
          </div>
        : <><SourceStates markets={data.markets} /><Roster data={data} /></>}
      <OffList rows={data.off} />
      <HowToRead />
    </>
  );
}

function runTimes(run){
  const from = clock(run.started_at);
  const to = clock(run.finished_at);
  if (from && to) return from.time + ' to ' + to.time + (to.zone ? ' ' + to.zone : '');
  if (from) return 'From ' + from.time + (from.zone ? ' ' + from.zone : '');
  return null;
}

function DaySummary({data, onDate}){
  const inputId = useId();
  const run = data.collect_run;
  const next = data.next_collection;
  return (
    <dl className="fieldwork-day">
      <div className="fieldwork-day__item">
        <dt id={inputId + '-label'}>Collection day</dt>
        <dd>
          {onDate
            ? <span className="fieldwork-date">
                <span className="fieldwork-day__value" aria-hidden="true">{longDate(data.date)}</span>
                <span className="fieldwork-date__hint" aria-hidden="true">Change day</span>
                {/* The native picker stays the control: it lies over the long
                    date, so a click or a key opens the browser's own calendar
                    while the reader sees the day in words. */}
                <input
                  id={inputId}
                  className="fieldwork-date__input"
                  type="date"
                  value={data.date}
                  max={data.today || undefined}
                  aria-label={'Collection day, ' + longDate(data.date) + '. Choose another day'}
                  onClick={(event) => { try { event.currentTarget.showPicker && event.currentTarget.showPicker(); } catch (_error){ /* the browser opens its own */ } }}
                  onChange={(event) => { if (DATE.test(event.target.value)) onDate(event.target.value); }}
                />
              </span>
            : <span className="fieldwork-day__value">{longDate(data.date)}</span>}
          {onDate && data.latest_day && data.latest_day !== data.date && (
            <button type="button" className="fieldwork-link-button" onClick={() => onDate(data.latest_day)} aria-label={'Back to ' + longDate(data.latest_day) + ', the latest recorded day'}>
              Back to {longDate(data.latest_day)}
            </button>
          )}
        </dd>
      </div>
      <div className="fieldwork-day__item">
        <dt>Collection run</dt>
        <dd>
          <span className="fieldwork-day__value">{run ? RUN_WORDS[run.status] || 'Recorded' : 'None recorded'}</span>
          {run && runTimes(run) && <span className="fieldwork-day__note">{runTimes(run)}</span>}
          {run && run.error && <span className="fieldwork-day__note">{run.error}</span>}
        </dd>
      </div>
      <div className="fieldwork-day__item">
        <dt>Collection credits</dt>
        <dd><CreditWords credits={data.credits} /></dd>
      </div>
      <div className="fieldwork-day__item">
        <dt>Next collection</dt>
        <dd>
          {next
            ? <>
                <span className="fieldwork-day__value">{next.time} {next.zone}</span>
                <span className="fieldwork-day__note">{longDate(next.date, {weekday: true})}</span>
              </>
            : <span className="fieldwork-day__value">Not known</span>}
        </dd>
      </div>
    </dl>
  );
}

function CreditWords({credits}){
  if (!credits) return <span className="fieldwork-day__value">Not known</span>;
  const cap = typeof credits.cap === 'number' ? readerFigure(credits.cap) : null;
  const planned = typeof credits.planned === 'number' ? readerFigure(credits.planned) + ' planned' : null;
  if (credits.state === 'unavailable'){
    return <><span className="fieldwork-day__value">Spend could not be read</span>{cap && <span className="fieldwork-day__note">Daily cap {cap}</span>}</>;
  }
  if (!isFigure(credits.spent)){
    return <><span className="fieldwork-day__value">None charged</span>{cap && <span className="fieldwork-day__note">Daily cap {cap}</span>}{planned && <span className="fieldwork-day__note">{planned} for the day</span>}</>;
  }
  const spent = credits.spent;
  const over = typeof credits.cap === 'number' && spent.value > credits.cap;
  return (
    <>
      <span className="fieldwork-day__value">
        <span className="fieldwork-fig" data-query-id={spent.query_id} title={'Query ' + spent.query_id + ', run ' + spent.run_id}>{readerFigure(spent.value)}</span>
        {cap && <> of {cap}</>}
      </span>
      <span className="fieldwork-day__note">{over ? 'Over the daily cap' : 'Spent of the daily cap'}</span>
      {planned && <span className="fieldwork-day__note">{planned} for the day</span>}
    </>
  );
}

/* Charts, 3 October 2026: every source in every market as one square,
   grouped by how it did on the day, so trouble shows before any table. */
const STATE_TONES = {delivered: 'ink', partial: 'soft', failed: 'alert', no_record: 'open', waiting: 'open', off: 'open'};
function SourceStates({markets}){
  const order = ['delivered', 'partial', 'failed', 'no_record', 'waiting', 'off'];
  const words = Object.fromEntries(TALLY);
  return (
    <UnitRows title="How every source did" data="data-fieldwork-states" unit="sources"
      caption="One square per source, by market."
      states={order.map((key) => ({key, label: words[key], tone: STATE_TONES[key]}))}
      rows={(Array.isArray(markets) ? markets : []).map((m) => ({key: m.market, label: m.label || m.market, counts: m.counts || {}}))} />
  );
}

function marketTally(market){
  const counts = market.counts || {};
  const parts = TALLY.filter(([key]) => counts[key] > 0).map(([key, words]) => readerFigure(counts[key]) + '\u00a0' + words).join(', ');
  if (!parts) return '';
  /* The tabs drop their source counts at phone width, so the tally says it. */
  return plural((market.sources || []).length, 'source', 'sources') + ': ' + parts;
}

function Roster({data}){
  const markets = data.markets;
  const [selected, setSelected] = useState(markets[0] && markets[0].market);
  const tabs = useRef({});
  const base = useId();
  const current = markets.find((m) => m.market === selected) || markets[0];
  const groups = Array.isArray(data.groups) ? data.groups : [];

  function onKey(event){
    const index = markets.findIndex((m) => m.market === current.market);
    let next = null;
    if (event.key === 'ArrowRight') next = (index + 1) % markets.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + markets.length) % markets.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = markets.length - 1;
    if (next === null) return;
    event.preventDefault();
    const code = markets[next].market;
    setSelected(code);
    if (tabs.current[code]) tabs.current[code].focus();
  }

  return (
    <section className="fieldwork-roster" aria-labelledby={base + '-title'}>
      <h2 className="fieldwork-section__title" id={base + '-title'}>Sources by market</h2>
      {/* Below 520px four market names do not fit on one row, so a labelled
          select stands in for the tabs; CSS shows one or the other. */}
      <div className="fieldwork-market-select">
        <label className="fieldwork-market-select__label" htmlFor={base + '-select'}>Market</label>
        <select
          id={base + '-select'}
          className="fieldwork-market-select__control"
          value={current.market}
          aria-controls={base + '-panel'}
          onChange={(event) => setSelected(event.target.value)}
        >
          {markets.map((m) => <option key={m.market} value={m.market}>{m.label}</option>)}
        </select>
      </div>
      <div className="fieldwork-tabs" role="tablist" aria-label="Market" onKeyDown={onKey}>
        {markets.map((m) => {
          const active = m.market === current.market;
          return (
            <button
              key={m.market}
              ref={(node) => { tabs.current[m.market] = node; }}
              type="button"
              role="tab"
              id={base + '-tab-' + m.market}
              aria-selected={active ? 'true' : 'false'}
              aria-controls={base + '-panel'}
              tabIndex={active ? 0 : -1}
              className="fieldwork-tab"
              onClick={() => setSelected(m.market)}
            >
              <span className="fieldwork-tab__name">{m.label}</span>
              <span className="fieldwork-tab__count">{plural((m.sources || []).length, 'source', 'sources')}</span>
            </button>
          );
        })}
      </div>
      <div className="fieldwork-panel" role="tabpanel" id={base + '-panel'} aria-labelledby={base + '-tab-' + current.market} data-market={current.market}>
        {/* The chart above draws this tally per market with its own key, so it
            is said here for screen readers only, as the panel's summary. */}
        <p className="fieldwork-panel__tally sr-only">{marketTally(current) || 'No sources listed'}</p>
        {groups.map((g) => {
          const rows = (current.sources || []).filter((s) => s.group === g.key);
          if (rows.length === 0) return null;
          return (
            <section key={g.key} className="fieldwork-group" data-group={g.key} aria-label={g.label}>
              <h3 className="fieldwork-group__title" id={base + '-group-' + g.key}>{g.label}</h3>
              {/* A status table, not prose: one column per fact so the
                  figures line up from row to row (Tufte, The Visual Display
                  of Quantitative Information, on tables). The group heading
                  names the table. */}
              <table className="fieldwork-sources" aria-labelledby={base + '-group-' + g.key}>
                <thead>
                  <tr>
                    <th scope="col" className="fieldwork-col-source">Source</th>
                    <th scope="col" className="fieldwork-col-status">Status</th>
                    <th scope="col" className="fieldwork-num">Calls came back</th>
                    <th scope="col" className="fieldwork-num">Items</th>
                    <th scope="col" className="fieldwork-num">Location known</th>
                    <th scope="col" className="fieldwork-num">Planned</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((s) => <SourceRow key={s.key} source={s} />)}
                </tbody>
              </table>
            </section>
          );
        })}
      </div>
    </section>
  );
}

function Members({names}){
  const list = Array.isArray(names) ? names.filter(Boolean) : [];
  if (list.length === 0) return null;
  if (list.length <= MEMBERS_SHOWN) return <p className="fieldwork-source__members">{list.join(', ')}</p>;
  const shown = MEMBERS_SHOWN - 2;
  return (
    <details className="fieldwork-source__more">
      <summary>{list.slice(0, shown).join(', ')} and {list.length - shown} more</summary>
      <p className="fieldwork-source__members">{list.slice(shown).join(', ')}</p>
    </details>
  );
}

function planWords(source){
  if (!source.planned_calls) return 'Not in the collection plan for this day';
  const reads = plural(source.planned_calls, 'read', 'reads') + ' planned';
  if (source.free) return reads + ', free';
  return reads + ', ' + plural(source.planned_credits || 0, 'credit', 'credits');
}

/* The share of posts with a known location, or the measured range when a
   source has several parts. The column heading carries the words. */
function located(health){
  if (typeof health.located_share === 'number') return percent(health.located_share);
  if (Array.isArray(health.located_range) && health.located_range.length === 2){
    const [low, high] = health.located_range;
    return low === high ? percent(low) : percent(low) + ' to ' + percent(high);
  }
  return null;
}

/* A figure the collection record does not hold: the row's status already
   says there is no record, so the cell stays visually empty and a screen
   reader still hears that the value is unknown, never zero. */
const NOT_RECORDED = <span className="fieldwork-source__none sr-only">Not recorded</span>;

function SourceRow({source}){
  const health = source.health;
  const place = health ? located(health) : null;
  const [open, setOpen] = useState(false);
  return (
    <tr className="fieldwork-source" data-status={source.status} data-series={source.series} data-recorded={health ? 'true' : 'false'} data-open={open ? 'true' : undefined}>
      <th scope="row" className="fieldwork-source__what">
        {/* On a phone a row shows the source and its status; the figures
            open under it on demand. The control is hidden on wider screens,
            where every figure has its own column. */}
        <p className="fieldwork-source__name">{source.name}</p>
        {source.detail && <p className="fieldwork-source__detail">{source.detail}</p>}
        <Members names={source.members} />
        <button type="button" className="fieldwork-source__toggle" aria-expanded={open ? 'true' : 'false'} aria-label={'Figures for ' + source.name} onClick={() => setOpen((v) => !v)}>
          Figures
        </button>
      </th>
      <td className="fieldwork-source__how" data-label="Status">
        <span className="fieldwork-source__status"><svg className="fieldwork-status-mark" viewBox="0 0 10 10" aria-hidden="true" focusable="false"><circle cx="5" cy="5" r="4" /></svg>{source.status_words}</span>
      </td>
      <td className="fieldwork-num" data-label="Calls came back">
        {health ? <>{readerFigure(health.calls_ok ?? 0)} of {readerFigure(health.calls ?? 0)}</> : NOT_RECORDED}
      </td>
      <td className="fieldwork-num" data-label="Items">
        {health && isFigure(health.items)
          ? <span className="fieldwork-fig" data-query-id={health.items.query_id}>{readerFigure(health.items.value)}</span>
          : NOT_RECORDED}
      </td>
      <td className="fieldwork-num" data-label="Location known">
        {!health ? NOT_RECORDED : place || <span className="fieldwork-source__none">Not measured</span>}
      </td>
      <td className="fieldwork-num fieldwork-source__plan" data-label="Planned">{planWords(source).split(', ').map((part, i, all) => <span key={i} className="fieldwork-source__plan-part">{part}{i < all.length - 1 ? ',' : ''}</span>).flatMap((node, i) => (i > 0 ? [' ', node] : [node]))}</td>
    </tr>
  );
}

const OFF_LABELS = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya', GLOBAL: 'All markets'};

function OffList({rows}){
  const list = Array.isArray(rows) ? rows : [];
  if (list.length === 0) return null;
  return (
    <section className="fieldwork-off" aria-labelledby="fieldwork-off-title">
      <h2 className="fieldwork-section__title" id="fieldwork-off-title">Switched off or retired</h2>
      <p className="fieldwork-off__intro">Sources the collection settings name but do not read, and why.</p>
      <ul className="fieldwork-off__list">
        {list.map((o) => (
          <li key={o.market + ':' + o.name} className="fieldwork-off__row">
            <p className="fieldwork-off__market">{OFF_LABELS[o.market] || o.market}</p>
            <p className="fieldwork-off__name">{o.name}</p>
            <p className="fieldwork-off__why">{o.why}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}

function HowToRead(){
  return (
    <details className="fieldwork-method">
      <summary>How to read this page</summary>
      <div className="fieldwork-method__body">
        <p>The source list is what the 02:00 collection is set to read on that day. It comes from 42's own settings, so it shows even before anything ran.</p>
        <p>Delivered means every part of a source came back usable. A source fails when too few of its calls came back, when its post count is far from its usual level, when its mix of posts is far from usual, or when too few accounts were checked.</p>
        <p>Location known is the share of a source's posts with at least 70% confidence in a location signal. It does not check that the place is in this market, and it describes posts, not where people live. A range spans the source's parts.</p>
        <p>Google Trends searches only guide what 42 looks into. They are never evidence, never a count of posts and never proof of place.</p>
        <p>Credits are the collection share of the day's spend against its daily cap. Spend on questions and briefs is on Coverage.</p>
      </div>
    </details>
  );
}
