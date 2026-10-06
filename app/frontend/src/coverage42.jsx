/* Coverage on the 42 API: what was collected, where, when, at what cost, and
   the gaps (docs/full-42/EXPERIENCE.md, Coverage; core/api/contract.md
   section 10.4). Every count shown as a finding is a Figure and carries the
   query that produced it. */
import React, {useEffect, useId, useRef, useState} from 'react';
import {fetchCoverage} from './api42.js';
import {readerFigure, sentenceCase, seriesFigure} from './api.js';
import {coverageHash, parseCoverageDate} from './router.js';
import {BarList, BulletBar} from './ui/Charts42.jsx';
import './styles/coverage42.css';

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const LANE_WORDS = {
  feed: 'Feeds and boards',
  panel: 'Panels',
  seed: 'Seeds',
  expansion: 'Expansion searches',
  exploration: 'Exploration searches',
  placebo: 'Placebo checks',
  anchor: 'Anchor searches',
  watchlist: 'Watchlist',
  confirm: 'Confirmation',
  agent_live: 'Ask live calls',
};
/* ENGINE.md section 6, in the order the scorecard is read. */
const SCORECARD_MEASURES = [
  {key: 'time_to_detect', words: 'Time to detect'},
  {key: 'lead_time', words: 'Lead time'},
  {key: 'precision', words: 'Precision'},
  {key: 'recall', words: 'Recall'},
  {key: 'breadth', words: 'Breadth'},
  {key: 'cost_per_confirmed_trend', words: 'Cost per confirmed trend'},
];
const STATUS_WORDS = {ok: 'Finished', failed: 'Failed', running: 'Reported running', skipped: 'Skipped'};
const SCORECARD_WAIT = 'No weekly scorecard yet. The learn job writes one every Monday at 07:30 SAST, for the week before';
const NO_MODEL_SPEND = 'Model spend is not recorded per run yet';
const CODE = /^[a-z]+(?:_[a-z]+)+$/;

const isFigure = (value) => value && typeof value === 'object' && typeof value.value === 'number';
const words = (code) => sentenceCase(String(code || '').replace(/_/g, ' '));
const laneWord = (lane) => LANE_WORDS[lane] || words(lane);
const statusWord = (status) => STATUS_WORDS[status] || words(status);
const usd = (value) => 'USD ' + value.toFixed(2);

function longDate(value){
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  if (!match) return String(value || '');
  return Number(match[3]) + ' ' + MONTHS[Number(match[2]) - 1] + ' ' + match[1];
}

/* Keep the service's clock time and make its supplied offset visible. */
function clock(value){
  const match = /T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/.exec(String(value || ''));
  if (!match) return null;
  const offset = match[2];
  let zone = 'timezone unknown';
  if (offset === 'Z' || offset === '+00:00' || offset === '+0000') zone = 'UTC';
  else if (offset === '+02:00' || offset === '+0200') zone = 'UTC+02:00';
  else if (offset && offset !== '-00:00' && offset !== '-0000') {
    zone = 'UTC' + offset.replace(/([+-]\d{2})(\d{2})$/, '$1:$2');
  }
  return {time: match[1], zone};
}

/* A table can be wider than its column, and from 1024 px the shell clips
   sideways overflow, so each table scrolls inside its own box. The box is a
   named region with a Tab stop so a keyboard reader can scroll it too. */
function Scroll({label, children}){
  return <div className="cv42-scroll" role="region" aria-label={label} tabIndex={0}>{children}</div>;
}

function Fig({figure, children}){
  return (
    <span className="cv42-fig" data-query-id={figure.query_id} title={'Query ' + figure.query_id + ', run ' + figure.run_id}>
      {children ?? readerFigure(figure.value)}
    </span>
  );
}

/* The day the page reads: a date prop wins (tests and embeds), else the
   hash, #/coverage?date=YYYY-MM-DD, which a hashchange re-reads. */
function useCoverageDay(date){
  const [hashDay, setHashDay] = useState(() => (typeof window === 'undefined' ? null : parseCoverageDate(window.location.hash)));
  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const onHash = () => setHashDay(parseCoverageDate(window.location.hash));
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return date || hashDay || undefined;
}

export function CoveragePage42({date, onAuth}){
  const day = useCoverageDay(date);
  const [load, setLoad] = useState({state: 'loading'});
  const [tick, setTick] = useState(0);
  const authRef = useRef(onAuth);
  authRef.current = onAuth;

  useEffect(() => {
    const ctrl = new AbortController();
    setLoad({state: 'loading'});
    fetchCoverage(day, {signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setLoad({state: 'ready', data}); })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){
          setLoad({state: 'auth'});
          if (authRef.current) authRef.current();
        } else {
          setLoad({state: 'error', message: error && error.status ? error.message : 'The 42 service did not answer.'});
        }
      });
    return () => ctrl.abort();
  }, [day, tick]);

  if (load.state === 'loading'){
    return (
      <section className="page cv42" aria-busy="true">
        <p className="cv42-status" role="status">Loading coverage</p>
      </section>
    );
  }
  if (load.state === 'auth'){
    return (
      <section className="page cv42">
        <p className="cv42-status" role="status">Enter the passcode to read Coverage.</p>
      </section>
    );
  }
  if (load.state === 'error'){
    return (
      <section className="page cv42">
        <h1 className="cv42-heading">Coverage could not load</h1>
        <p className="cv42-status" role="alert">{load.message}</p>
        <div className="cv42-actions">
          <button type="button" className="cv42-button" onClick={() => setTick((t) => t + 1)}>Try again</button>
          {day && <a className="cv42-link" href={coverageHash(null)}>Open today instead</a>}
        </div>
      </section>
    );
  }

  const data = load.data || {};
  const shown = data.date || day;
  const markets = Array.isArray(data.markets) ? data.markets : [];
  const anyRows = markets.some((m) => Array.isArray(m.series) && m.series.length > 0);
  const collected = data.collection ? data.collection === 'collected' : anyRows;
  const scale = niceCeiling(Math.max(0, ...markets.flatMap((m) => (Array.isArray(m.series) ? m.series : []).map((r) => r.items || 0))));
  return (
    <section className="page cv42">
      <header className="cv42-head">
        <h1 className="cv42-heading">Coverage, <span className="cv42-nowrap">{longDate(shown)}</span></h1>
        <DayPicker date={shown} today={data.today} />
      </header>
      {/* The data leads. How each figure is counted stays one tap away, in
          a closed disclosure, instead of four paragraphs above the tables. */}
      <details className="cv42-method" data-section="method">
        <summary>How to read these numbers</summary>
        <div className="cv42-method-body">
          <p className="cv42-line">Successful calls are shown against total attempts. A successful call can return zero items.</p>
          <p className="cv42-line">For each series, the denominator is its distinct collected posts. The percentage shows how many have a location confidence of at least 70%, based on an external-region, home-market, or place-mention signal.</p>
          <p className="cv42-line">This is a post-level location signal, not proof of where people live or of general public or TikTok activity in the market.</p>
          <p className="cv42-line">The summary above each market counts items collected and platforms from usable sources only, as Today does. A post found by two searches counts twice.</p>
          <p className="cv42-line">Last successful collection times are unavailable for individual sources.</p>
          <p className="cv42-line">Run statuses are recorded by the pipeline; a running record does not confirm the job is still active.</p>
        </div>
      </details>
      {!collected && <DayNotice data={data} date={shown} />}
      {anyRows && markets.map((m) => <MarketSeries key={m.market} market={m} scale={scale} />)}
      <div className="cv42-sources" data-section="sources">
        <a className="cv42-link" href="#/fieldwork">See every source in Fieldwork</a>
      </div>
      <div className="cv42-grid">
        <Credits credits={data.credits} />
        <ModelSpend spend={data.model_spend} />
        <Runs runs={data.runs} />
      </div>
      <Scorecard scorecard={data.scorecard} wait={data.scorecard_text} />
      <NotSeen items={data.not_seen} />
    </section>
  );
}

/* "30 Sept 2026", for the day field on a phone. */
function shortDate(value){
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  if (!match) return String(value || '');
  const month = MONTHS[Number(match[2]) - 1];
  return Number(match[3]) + ' ' + (month.length > 4 ? month.slice(0, month === 'September' ? 4 : 3) : month) + ' ' + match[1];
}

/* ISO day arithmetic in UTC, so no local clock shifts a day. */
function shiftDay(iso, days){
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  if (!match) return null;
  const d = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]) + days));
  return d.toISOString().slice(0, 10);
}

/* Previous day, a date field and next day. Each day is its own link, so a
   day can be shared; the next day stops at today, the service's own date. */
function DayPicker({date, today}){
  const prev = shiftDay(date, -1);
  const next = shiftDay(date, 1);
  const canNext = next && (!today || next <= today);
  /* The long date is what shows; the native field over it does the picking. */
  const open = (e) => { try { if (e.currentTarget.showPicker) e.currentTarget.showPicker(); } catch { /* the field still takes typing */ } };
  return (
    <nav className="cv42-days" aria-label="Choose a day">
      {prev
        ? <a className="cv42-day-step" href={coverageHash(prev)} aria-label={'Previous day, ' + longDate(prev)}><span aria-hidden="true">‹</span><span className="cv42-day-long" aria-hidden="true"> Previous day</span></a>
        : <span />}
      <span className="cv42-day-field">
        <span className="cv42-day-shown" aria-hidden="true"><span className="cv42-day-full">{longDate(date)}</span><span className="cv42-day-short">{shortDate(date)}</span></span>
        <input type="date" className="cv42-day-input" value={date || ''} max={today || undefined}
          aria-label={'Choose a day, showing ' + longDate(date)}
          onClick={open}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(e); } }}
          onChange={(e) => { if (parseCoverageDate('?date=' + e.target.value)) window.location.hash = coverageHash(e.target.value); }} />
      </span>
      {canNext
        ? <a className="cv42-day-step" href={coverageHash(next)} aria-label={'Next day, ' + longDate(next)}><span className="cv42-day-long" aria-hidden="true">Next day </span><span aria-hidden="true">›</span></a>
        : <span className="cv42-day-step" aria-disabled="true" aria-label="Next day, not yet available"><span className="cv42-day-long" aria-hidden="true">Next day </span><span aria-hidden="true">›</span></span>}
    </nav>
  );
}

/* What happened to collection on a day with no source recorded, in the
   store's own terms, and the way back to the latest day that has data. */
function collectionWords(state, date, today){
  if (state === 'running') return 'Collection is reported running for this day. Sources show here once it records them.';
  if (state === 'failed') return 'Collection ran on this day and failed, so no source was recorded. The failed run is listed under Runs of the day.';
  if (state === 'empty') return 'Collection finished on this day but recorded no source.';
  if (today && date > today) return 'This day has not happened yet.';
  if (today && date === today) return 'No collection recorded for today yet.';
  return 'No collection recorded for this day yet.';
}

function DayNotice({data, date}){
  const latest = data.latest_day_with_data;
  return (
    <div className="cv42-notice" role="status" data-section="day-notice" data-state={data.collection || 'not_recorded'}>
      <p className="cv42-notice-title">{collectionWords(data.collection, date, data.today)}</p>
      {latest && latest !== date
        ? <p className="cv42-notice-next">The latest day with collected sources is {longDate(latest)}. <a className="cv42-link" href={coverageHash(latest)}>Open {longDate(latest)}</a></p>
        : !latest && <p className="cv42-notice-next">No day has collected sources yet. Coverage fills after the first collection run.</p>}
    </div>
  );
}

/* A round top for the posts axis: 1, 2, 2.5 or 5 times a power of ten. */
function niceCeiling(value){
  if (!(value > 0)) return 0;
  const power = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((m) => m * power >= value);
  return step * power;
}

function Stat({value, words}){
  return <li className="cv42-stat"><span className="cv42-stat-value">{value}</span> <span className="cv42-stat-words">{words}</span></li>;
}

function Summary({summary}){
  if (!summary) return null;
  const share = typeof summary.located_share === 'number' ? Math.round(summary.located_share * 100) + '%' : null;
  return (
    <ul className="cv42-stats" aria-label="The day at a glance">
      {Number.isFinite(summary.posts)
        ? <Stat value={readerFigure(summary.posts)} words={summary.posts === 1 ? 'item collected' : 'items collected'} />
        : <li className="cv42-stat cv42-stat-words">Items collected: not measured</li>}
      <Stat value={readerFigure(summary.platforms ?? 0)} words={summary.platforms === 1 ? 'platform' : 'platforms'} />
      {share
        ? <Stat value={share} words="with a confident location" />
        : <li className="cv42-stat cv42-stat-words">Location not measured yet</li>}
      <Stat value={readerFigure(summary.sources_usable ?? 0) + ' of ' + readerFigure(summary.sources ?? 0)} words="sources usable" />
    </ul>
  );
}

/* The sources table in parts (owner, 5 October 2026: "can we not clean this
   up and display this better?"). The service names each source once and
   puts it in a part; inside a part the usable sources lead, then the most
   posts, then the name. A source with no call on the day folds into one line
   under the table and still counts in the summary above. */
const PART_ORDER = ['charts', 'boards', 'posts', 'news', 'other'];
const PART_WORDS = {
  charts: 'Charts and app stores',
  boards: 'Trending boards and feeds',
  posts: 'Posts from platforms and followed accounts',
  news: 'News feeds',
  other: 'Other sources',
};
const byRank = (a, b) => (Number(Boolean(b.row.valid)) - Number(Boolean(a.row.valid)))
  || ((b.row.items || 0) - (a.row.items || 0))
  || String(a.row.series_words || '').localeCompare(String(b.row.series_words || ''));
const rowKey = ({row, index}) => [row.platform, row.series, row.series_words, index].join(':');

function sourceParts(rows){
  const grouped = rows.some((r) => r.group);
  const parts = new Map();
  rows.forEach((row, index) => {
    if (row.calls === 0) return;
    const key = grouped ? (PART_ORDER.includes(row.group) ? row.group : 'other') : 'all';
    if (!parts.has(key)) parts.set(key, {key, words: grouped ? (row.group_words || PART_WORDS[key]) : null, rows: []});
    parts.get(key).rows.push({row, index});
  });
  return [...parts.values()]
    .sort((a, b) => PART_ORDER.indexOf(a.key) - PART_ORDER.indexOf(b.key))
    .map((part) => ({...part, rows: part.rows.slice().sort(byRank)}));
}

function PartHead({part}){
  const usable = part.rows.filter(({row}) => row.valid);
  const posts = usable.reduce((sum, {row}) => sum + (row.items || 0), 0);
  return (
    <tr className="cv42-group-row">
      <th scope="rowgroup" colSpan={6}>
        <span className="cv42-group-name">{part.words}</span>
        <span className="cv42-group-total">
          {readerFigure(usable.length) + ' of ' + readerFigure(part.rows.length) + ' usable, '
            + readerFigure(posts) + (posts === 1 ? ' post' : ' posts') + ' from usable sources'}
        </span>
      </th>
    </tr>
  );
}

function SourceRow({row, scale}){
  const r = row;
  return (
    <tr className={r.valid ? '' : 'cv42-invalid'}>
      <th scope="row">{r.series_words}</th>
      <td className="cv42-num" data-label="Posts">{readerFigure(r.items ?? 0)}</td>
      <td className="cv42-bar-col" aria-hidden="true">
        <span className="cv42-track"><span className="cv42-bar" style={{'--cv42-w': (scale > 0 ? Math.min(1, (r.items || 0) / scale) : 0)}} /></span>
      </td>
      <td className="cv42-num" data-label="Located">{typeof r.located_share === 'number'
        ? Math.round(r.located_share * 100) + '%'
        : <span className="cv42-muted">{r.located_words || 'Not measured'}</span>}</td>
      <td className="cv42-num" data-label="Calls worked">{readerFigure(r.calls_ok ?? 0)} of {readerFigure(r.calls ?? 0)}</td>
      <td className="cv42-state">
        <span className="cv42-state-word">{r.valid ? 'Usable' : 'Not usable'}</span>
        {!r.valid && <span className="cv42-state-reason">{sentenceCase(String(r.invalid_words || 'not usable'))}</span>}
      </td>
    </tr>
  );
}

function NotCollected({rows}){
  if (rows.length === 0) return null;
  const names = rows.map((row, index) => ({row, index}))
    .sort((a, b) => String(a.row.series_words || '').localeCompare(String(b.row.series_words || '')));
  return (
    <details className="cv42-idle" data-not-collected="">
      <summary>Not collected on this day: {readerFigure(rows.length)} {rows.length === 1 ? 'source' : 'sources'}</summary>
      <div className="cv42-idle-body">
        <p className="cv42-line">No calls were recorded for {rows.length === 1 ? 'this source' : 'these sources'} on this day. The summary above counts {rows.length === 1 ? 'it' : 'them'} as not usable.</p>
        <ul className="cv42-rows">
          {names.map((n) => <li key={rowKey(n)}>{n.row.series_words}</li>)}
        </ul>
      </div>
    </details>
  );
}

function MarketSeries({market, scale}){
  const rows = Array.isArray(market.series) ? market.series : [];
  const parts = sourceParts(rows);
  const idle = rows.filter((r) => r.calls === 0);
  return (
    <section className="cv42-part" data-market={market.market} aria-label={market.label}>
      <h2 className="cv42-part-title">{market.label}</h2>
      <Summary summary={market.summary} />
      {parts.length > 0 && (
        <Scroll label={market.label + ' series table'}>
          <table className="cv42-table cv42-sources-table cv42-stack">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col" className="cv42-num">Posts</th>
                <th scope="col" className="cv42-bar-col">
                  <span className="cv42-axis"><span>0</span><span>{readerFigure(scale)}</span></span>
                </th>
                <th scope="col" className="cv42-num">Located</th>
                <th scope="col" className="cv42-num">Calls worked</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            {parts.map((part) => (
              <tbody key={part.key} data-part={part.key}>
                {part.words && <PartHead part={part} />}
                {part.rows.map((entry) => <SourceRow key={rowKey(entry)} row={entry.row} scale={scale} />)}
              </tbody>
            ))}
          </table>
        </Scroll>
      )}
      <NotCollected rows={idle} />
      {rows.length === 0 && <p className="cv42-line">Nothing was collected for {market.label} on this day.</p>}
    </section>
  );
}

function Credits({credits}){
  const rows = credits && Array.isArray(credits.rows) ? credits.rows : [];
  return (
    <section className={'cv42-part' + (rows.length > 0 ? '' : ' cv42-part-quiet')} data-section="credits">
      <h2 className="cv42-part-title">Credits</h2>
      {rows.length > 0
        ? <>
            <BarList title="Credits by job" caption="Credits charged on this day, by market, job and search" data="data-cv42-credits-chart"
              rows={rows.map((r) => ({
                key: r.market + ':' + r.job + ':' + r.lane,
                label: [r.market_label, words(r.job), laneWord(r.lane)].join(' · '),
                value: isFigure(r.credits) ? r.credits.value : null,
                queryId: isFigure(r.credits) ? r.credits.query_id : undefined,
              }))} />
            <Scroll label="Credits table">
              <table className="cv42-table cv42-stack cv42-credits">
                <thead><tr><th scope="col">Market</th><th scope="col">Job</th><th scope="col">Searched as</th><th scope="col">Credits charged</th></tr></thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.market + ':' + r.job + ':' + r.lane}>
                      <th scope="row">{r.market_label}</th>
                      <td data-label="Job">{words(r.job)}</td>
                      <td data-label="Searched as">{laneWord(r.lane)}</td>
                      <td className="cv42-num" data-label="Credits">{isFigure(r.credits) ? <Fig figure={r.credits} /> : 'Not recorded'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Scroll>
            {isFigure(credits.total) && <p className="cv42-line">Total: <Fig figure={credits.total} /> credits charged</p>}
          </>
        : <p className="cv42-line">No credits charged on this day.</p>}
    </section>
  );
}

function ModelSpend({spend}){
  const spent = spend && isFigure(spend.usd) ? spend.usd : null;
  const cap = spend && typeof spend.cap_usd === 'number' ? spend.cap_usd : null;
  const stages = spend && Array.isArray(spend.stages) ? spend.stages.filter((s) => isFigure(s.usd)) : [];
  return (
    <section className={'cv42-part' + (stages.length > 0 ? '' : ' cv42-part-quiet')} data-section="model-spend">
      <h2 className="cv42-part-title">Model spend</h2>
      {spent
        ? <>
            {/* The figure leads at display size inside the one sentence; the
                bar below draws it against the cap without repeating it. */}
            <p className="cv42-line cv42-spend-line">
              <Fig figure={spent}><span className="cv42-spend-value">{usd(spent.value)}</span></Fig>
              {cap
                ? <> of the {usd(cap)} daily cap ({Math.round((spent.value / cap) * 100)}%){spent.value > cap ? ', over the cap' : ''}</>
                : ' today'}
            </p>
            <BulletBar data="data-cv42-spend-chart"
              value={spent.value} cap={cap} format={usd} />
            {stages.length > 0 && (
              <ul className="cv42-rows">
                {stages.map((s) => <li key={s.stage}>{words(s.stage)}: <Fig figure={s.usd}>{usd(s.usd.value)}</Fig></li>)}
              </ul>
            )}
          </>
        : <p className="cv42-line">{(spend && spend.text) || NO_MODEL_SPEND}.</p>}
    </section>
  );
}

function Runs({runs}){
  const rows = Array.isArray(runs) ? runs : [];
  /* When every run carries the same zone, it is said once under the table. */
  const zones = new Set(rows.flatMap((r) => [clock(r.started_at), clock(r.finished_at)].filter(Boolean).map((c) => c.zone)));
  const shared = zones.size === 1 ? [...zones][0] : null;
  const zoneNote = shared === 'UTC+02:00' ? 'Times are South African time, UTC+02:00.'
    : shared === 'UTC' ? 'Times are UTC.'
    : shared === 'timezone unknown' ? 'No time zone was recorded for these times.'
    : shared ? 'Times are ' + shared + '.' : null;
  return (
    <section className={'cv42-part' + (rows.length > 0 ? ' cv42-part-wide' : ' cv42-part-quiet')} data-section="runs">
      <h2 className="cv42-part-title">Runs of the day</h2>
      {rows.length > 0
        ? <Scroll label="Runs of the day table">
            <table className="cv42-table cv42-stack cv42-runs">
              <thead><tr><th scope="col">Stage</th><th scope="col">Status</th><th scope="col">Time</th></tr></thead>
              <tbody>
                {rows.map((r) => {
                  const start = clock(r.started_at);
                  const end = clock(r.finished_at);
                  let time = '';
                  const z = (c) => (shared ? '' : ' ' + c.zone);
                  if (start && end) {
                    time = start.zone === end.zone
                      ? start.time + ' to ' + end.time + z(start)
                      : start.time + ' ' + start.zone + ' to ' + end.time + ' ' + end.zone;
                  } else if (start) {
                    time = 'from ' + start.time + z(start);
                  } else if (end) {
                    time = 'to ' + end.time + z(end);
                  }
                  /* A run that finished ok but could not make some writes (contract 10.4, degraded) says which. */
                  const gaps = Array.isArray(r.degraded) ? r.degraded.filter(Boolean) : [];
                  const degraded = r.status === 'ok' && gaps.length > 0;
                  const error = CODE.test(r.error || '') ? words(r.error) : r.error || '';
                  const note = [degraded ? 'Did not complete: ' + gaps.join('; ') : '', error].filter(Boolean).join('. ');
                  const failed = r.status === 'failed' ? ' cv42-invalid' : '';
                  return (
                    <React.Fragment key={r.run_id}>
                      <tr className={(note ? 'cv42-has-note' : '') + failed} data-degraded={degraded ? 'true' : undefined}>
                        <th scope="row">{words(r.stage)}</th>
                        <td data-label="Status">{degraded ? 'Finished, degraded' : statusWord(r.status)}</td>
                        <td className="cv42-time" data-label="Time">{time}</td>
                      </tr>
                      {note && <tr className="cv42-note-row"><td colSpan={3}>{note}</td></tr>}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </Scroll>
        : <p className="cv42-line">No runs recorded for this day.</p>}
      {rows.length > 0 && zoneNote && <p className="cv42-line cv42-zone-note">{zoneNote}</p>}
    </section>
  );
}

/* A measure reads number first: the value with its first unit word ("2
   days", "50 credits", "60%"), and the rest of the unit once, muted, under
   the measure's name. A market whose unit differs keeps its own words. */
function splitUnit(figure){
  const unit = String(figure.unit || '').trim();
  if (/^share of /.test(unit)){
    return {lead: Math.round(figure.value * 100) + '%', unit: sentenceCase(unit)};
  }
  const [first, ...rest] = unit.split(/\s+/);
  const shown = seriesFigure(figure.value);
  const word = first && shown === '1' && /s$/.test(first) ? first.slice(0, -1) : first;
  /* Visual QA, 5 October 2026: a mean cost printed 225.83333333333334 and
     pushed Kenya's column off the table. One decimal, whole numbers whole. */
  return {lead: shown + (word ? ' ' + word : ''), unit: unit ? sentenceCase(unit) : '', rest: rest.join(' ')};
}

/* "21 to 27 September 2026" inside one month, else two whole dates. */
function WeekSpan({from, to}){
  const a = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(from || ''));
  const b = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(to || ''));
  if (!b) return <span className="cv42-nowrap">{longDate(from)}</span>;
  if (a && a[1] === b[1] && a[2] === b[2]) return <>{Number(a[3])} to <span className="cv42-nowrap">{longDate(to)}</span></>;
  return <><span className="cv42-nowrap">{longDate(from)}</span> to <span className="cv42-nowrap">{longDate(to)}</span></>;
}

const MARKET_NAMES = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya'};
const scoreMarkets = (scorecard) => (Array.isArray(scorecard.markets) ? scorecard.markets : []);

function Scorecard({scorecard, wait}){
  const markets = scorecard ? scoreMarkets(scorecard) : [];
  const notesId = useId();
  /* Why a measure has no value, once per cell, in table order, under the table. */
  const notes = SCORECARD_MEASURES.flatMap((measure) => markets
    .filter((m) => !isFigure(m[measure.key]) && m.reasons && m.reasons[measure.key])
    .map((m) => ({market: m.market, key: measure.key, words: measure.words, reason: m.reasons[measure.key]})));
  return (
    <section className="cv42-part" data-section="scorecard">
      <h2 className="cv42-part-title">Weekly scorecard</h2>
      {scorecard
        ? <>
            {scorecard.week && <p className="cv42-line">Week of <WeekSpan from={scorecard.week} to={scorecard.week_end} /></p>}
            {/* One row per market per week (engine_scorecard); a measure without a value says why when L2 gave a reason. */}
            <Scroll label="Weekly scorecard table">
              <table className="cv42-table cv42-score cv42-stack">
                <thead>
                  <tr>
                    <th scope="col">Measure</th>
                    {markets.map((m) => <th scope="col" key={m.market}>{MARKET_NAMES[m.market] || m.market}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {SCORECARD_MEASURES.map((measure) => {
                    const first = markets.map((m) => m[measure.key]).find(isFigure);
                    const unit = first ? splitUnit(first).unit : '';
                    /* "Credits per confirmed trend" under "Cost per confirmed trend" says nothing new. */
                    const tail = (text) => text.toLowerCase().split(/\s+/).slice(-3).join(' ');
                    const rowUnit = unit && tail(unit) !== tail(measure.words) ? unit : '';
                    /* Night Desk: a meter per figure on the row's own scale. A share
                       reads on 0 to 100; any other measure against the row's largest. */
                    const share = first ? /^share of /.test(String(first.unit || '').trim()) : false;
                    const rowMax = Math.max(0, ...markets.map((m) => m[measure.key]).filter(isFigure).map((f) => f.value));
                    const meter = (fig) => share ? Math.min(1, Math.max(0, fig.value)) : (rowMax > 0 ? fig.value / rowMax : 0);
                    return (
                      <tr key={measure.key}>
                        <th scope="row">
                          <span className="cv42-measure">{measure.words}</span>
                          {rowUnit && <span className="cv42-measure-unit">{rowUnit}</span>}
                        </th>
                        {markets.map((m) => {
                          const fig = m[measure.key];
                          const reason = m.reasons && m.reasons[measure.key];
                          if (!isFigure(fig)){
                            const n = reason ? notes.findIndex((x) => x.market === m.market && x.key === measure.key) + 1 : 0;
                            return (
                              <td className="cv42-score-cell cv42-unmeasured" key={m.market} data-label={MARKET_NAMES[m.market] || m.market}>
                                <span className="cv42-score-value">Not measured yet{n > 0 && <sup className="cv42-note-mark"><a href={'#' + notesId + '-' + n} aria-label={'Why, note ' + n} onClick={(e) => { e.preventDefault(); const el = document.getElementById(notesId + '-' + n); if (el) el.focus(); }}>{n}</a></sup>}</span>
                              </td>
                            );
                          }
                          const parts = splitUnit(fig);
                          return (
                            <td className="cv42-score-cell" key={m.market} data-label={MARKET_NAMES[m.market] || m.market}>
                              <Fig figure={fig}><span className="cv42-score-value">{parts.lead}</span></Fig>
                              {parts.unit !== unit && <span className="cv42-score-reason">{parts.unit}</span>}
                              <span className="cv42-score-meter" aria-hidden="true" data-market={m.market} style={{'--v': String(Math.round(meter(fig) * 1000) / 1000)}}></span>
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </Scroll>
            {notes.length > 0 && (
              <ol className="cv42-notes" aria-label="Why some measures have no value">
                {notes.map((x, i) => (
                  <li key={x.market + x.key} id={notesId + '-' + (i + 1)} tabIndex={-1}>
                    {(MARKET_NAMES[x.market] || x.market) + ', ' + x.words.toLowerCase() + ': ' + String(x.reason).replace(/\.$/, '') + '.'}
                  </li>
                ))}
              </ol>
            )}
          </>
        : <p className="cv42-line">{wait || SCORECARD_WAIT}.</p>}
    </section>
  );
}

function NotSeen({items}){
  const rows = Array.isArray(items) ? items : [];
  if (rows.length === 0) return null;
  return (
    <section className="cv42-part" data-section="not-seen">
      <h2 className="cv42-part-title">What 42 does not see</h2>
      <ul className="cv42-rows">
        {rows.map((line) => <li key={line}>{line}</li>)}
      </ul>
    </section>
  );
}
