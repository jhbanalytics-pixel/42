/* Compare on the 42 API: two to five things side by side in the same units
   over the same window (docs/full-42/EXPERIENCE.md, Compare; core/api/
   contract.md section 11). Three modes: things in one market, one thing
   across markets, one thing across platforms. The URL holds the choice, so a
   comparison can be shared. Days without usable collection stay gaps, every
   figure keeps its query, and a held-back subject is compared with its
   reason shown. A measured day on a chart asks why that day jumped (section
   14.1), through the same confirm as the topic page. */
import {useEffect, useRef, useState} from 'react';
import {fetchCompare, fetchDiscover} from './api42.js';
import {readerFigure, sentenceCase, seriesFigure} from './api.js';
import {unitFor} from './readerUnits.js';
import {SpikeConfirm} from './ui/SpikeConfirm.jsx';
import {figureWords, longDate, platformWord, topicHref} from './ui/TrendCard.jsx';
import './styles/today42.css';
import './styles/compare42.css';

const MODES = [
  {id: 'items', label: 'Things in one market'},
  {id: 'markets', label: 'One thing across markets'},
  {id: 'platforms', label: 'One thing across platforms'},
];
const MARKETS = ['ZA', 'NG', 'KE'];
const MARKET_WORDS = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya', all: 'every market'};
const PLATFORMS = ['tiktok', 'instagram', 'youtube', 'x', 'reddit', 'facebook'];
const DAYS = [7, 14, 28];
const MOST = 5;
const SHOWN = 8;
const NO_ANSWER = 'The 42 service did not answer.';

const list = (value) => (Array.isArray(value) ? value : []);
const split = (value) => [...new Set(String(value || '').split(',').map((v) => v.trim()).filter(Boolean))];
const num = (value) => typeof value === 'number' && Number.isFinite(value);
const most = (mode) => (mode === 'items' ? MOST : 1);

/* The URL query of #/compare?... as a choice. Anything outside the contract
   falls back: an unknown mode to items, a market to the reader's region, a
   window to 28 days. */
export function parseCompareQuery(search, region){
  const query = new URLSearchParams(String(search || '').replace(/^\?/, ''));
  const mode = MODES.some((m) => m.id === query.get('mode')) ? query.get('mode') : 'items';
  const market = String(query.get('market') || '').toUpperCase();
  const markets = [...new Set(split(query.get('markets')).map((m) => m.toUpperCase()).filter((m) => MARKETS.includes(m)))];
  const days = Number(query.get('days'));
  return {
    mode,
    items: split(query.get('items')).slice(0, most(mode)),
    market: MARKETS.includes(market) ? market : MARKETS.includes(region) ? region : 'ZA',
    markets: markets.length > 0 ? markets : MARKETS.slice(),
    platforms: split(query.get('platforms')).filter((p) => PLATFORMS.includes(p)).slice(0, MOST),
    days: DAYS.includes(days) ? days : 28,
  };
}

function compareHash(choice){
  const join = (values) => values.map(encodeURIComponent).join(',');
  let hash = '#/compare?mode=' + choice.mode + '&items=' + join(choice.items);
  if (choice.mode === 'markets') hash += '&markets=' + choice.markets.join(',');
  else hash += '&market=' + choice.market;
  if (choice.mode === 'platforms') hash += '&platforms=' + choice.platforms.join(',');
  return hash + '&days=' + choice.days;
}

/* What the choice still needs before it can be read, in words, or null. */
function needs(choice){
  if (choice.mode === 'items'){
    if (choice.items.length === 0) return 'Add two to five things to compare.';
    if (choice.items.length === 1) return 'Add at least one more thing to compare.';
    return null;
  }
  if (choice.items.length === 0) return 'Add the thing to compare.';
  if (choice.mode === 'markets' && choice.markets.length < 2) return 'Pick two or three markets.';
  if (choice.mode === 'platforms' && choice.platforms.length < 2) return 'Pick two to five platforms.';
  return null;
}

export function Compare42({search, region, onAuth}){
  const [choice, setChoice] = useState(() => parseCompareQuery(search, region));
  const [names, setNames] = useState({});
  const [load, setLoad] = useState({state: 'idle'});
  const [tick, setTick] = useState(0);
  const authRef = useRef(onAuth);
  authRef.current = onAuth;
  const asked = useRef(false);
  const hash = compareHash(choice);
  const waiting = needs(choice);

  /* Both reads can meet the same 401; the passcode flow is asked for once. */
  const failed = (error) => {
    if (error && error.auth){
      if (!asked.current){
        asked.current = true;
        if (authRef.current) authRef.current();
      }
      return {state: 'auth'};
    }
    return {state: 'error', message: error && error.status ? error.message : NO_ANSWER};
  };

  useEffect(() => {
    if (!String(window.location.hash).startsWith('#/compare')) return;
    if (window.location.hash !== hash) window.history.replaceState(window.history.state, '', hash);
  }, [hash]);

  useEffect(() => {
    if (waiting){ setLoad({state: 'idle'}); return undefined; }
    const ctrl = new AbortController();
    setLoad({state: 'loading'});
    fetchCompare(choice, {signal: ctrl.signal})
      .then((data) => {
        if (ctrl.signal.aborted) return;
        const body = data || {};
        setLoad({state: 'ready', data: body});
        setNames((prev) => ({...prev, ...Object.fromEntries(list(body.subjects).map((s) => [s.item_id, s.label]))}));
      })
      .catch((error) => { if (!ctrl.signal.aborted) setLoad(failed(error)); });
    return () => ctrl.abort();
  }, [hash, tick]);

  const update = (change) => setChoice((prev) => ({...prev, ...change(prev)}));
  const toggle = (key, order, value) => update((prev) => {
    const next = prev[key].includes(value) ? prev[key].filter((v) => v !== value) : prev[key].concat(value);
    return {[key]: order.filter((v) => next.includes(v))};
  });

  return (
    <section className="page t42 c42">
      <header className="t42-head">
        <h1 className="t42-heading">Compare</h1>
        <p className="t42-status">How do trends stack up against each other? Two to five things side by side, in the same units over the same window.</p>
      </header>
      <Picker
        choice={choice}
        names={names}
        waiting={waiting}
        failed={failed}
        onNames={(found) => setNames((prev) => ({...found, ...prev}))}
        onMode={(mode) => update((prev) => ({mode, items: prev.items.slice(0, most(mode))}))}
        onMarket={(market) => update(() => ({market}))}
        onMarkets={(market) => toggle('markets', MARKETS, market)}
        onPlatforms={(platform) => toggle('platforms', PLATFORMS, platform)}
        onDays={(days) => update(() => ({days}))}
        onAdd={(id) => update((prev) => ({items: prev.items.includes(id) ? prev.items : prev.items.concat(id).slice(0, most(prev.mode))}))}
        onRemove={(id) => update((prev) => ({items: prev.items.filter((v) => v !== id)}))}
      />
      <Result load={load} onRetry={() => setTick((t) => t + 1)} onAuth={onAuth} />
    </section>
  );
}

function Picker({choice, names, waiting, failed, onNames, onMode, onMarket, onMarkets, onPlatforms, onDays, onAdd, onRemove}){
  const [find, setFind] = useState('');
  const [pool, setPool] = useState({state: 'loading', items: []});
  const [tick, setTick] = useState(0);
  const [more, setMore] = useState('idle');
  const moreCtrl = useRef(null);
  const from = choice.mode === 'markets' ? 'all' : choice.market;

  /* One page of Discover, named for the picker. A trend already offered is
     not offered twice. */
  const pageOf = (data, prev = {items: [], held: []}) => {
    const body = data || {};
    const seen = new Set(prev.items.concat(list(prev.held)).map((c) => c.item_id));
    const named = (rows) => rows
      .filter((c) => c && c.item_id && !seen.has(c.item_id) && seen.add(c.item_id))
      .map((c) => ({item_id: c.item_id, title: c.title || c.item_id, reason: c.reason, reason_text: c.reason_text}));
    /* Demo polish, 2 October 2026 (QA item 11): held-back trends can be
       compared by design, so they are offered apart from the ones that
       cleared the gate, each with its reason, never as a plain Add. */
    const items = named(list(body.items));
    const held = named(list(body.held_back && body.held_back.items));
    onNames(Object.fromEntries(items.concat(held).map((c) => [c.item_id, c.title])));
    return {state: 'ready', items: prev.items.concat(items), held: list(prev.held).concat(held), cursor: body.next_cursor || null};
  };

  useEffect(() => {
    const ctrl = new AbortController();
    setPool({state: 'loading', items: []});
    setMore('idle');
    fetchDiscover({market: from, limit: 50}, {signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setPool(pageOf(data)); })
      .catch((error) => { if (!ctrl.signal.aborted) setPool({...failed(error), items: []}); });
    return () => { ctrl.abort(); if (moreCtrl.current) moreCtrl.current.abort(); };
  }, [from, tick]);

  /* Discover answers a page at a time; the rest is read only when asked
     for, so the picker never says nothing matches while pages remain. */
  const loadMore = () => {
    if (pool.state !== 'ready' || !pool.cursor) return;
    const ctrl = new AbortController();
    moreCtrl.current = ctrl;
    setMore('loading');
    fetchDiscover({market: from, limit: 50, cursor: pool.cursor}, {signal: ctrl.signal})
      .then((data) => {
        if (ctrl.signal.aborted) return;
        /* pageOf names the new trends to the parent, so it runs here and
           not inside a state updater; the button is disabled while this
           read is on its way, so pool is still the page it continues. */
        setPool(pageOf(data, pool));
        setMore('idle');
      })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){ failed(error); setMore('idle'); return; }
        setMore('error');
      });
  };

  const full = choice.items.length >= most(choice.mode);
  const words = find.trim().toLowerCase();
  const open = (c) => !choice.items.includes(c.item_id) && (!words || c.title.toLowerCase().includes(words));
  const matches = pool.items.filter(open).slice(0, SHOWN);
  const heldMatches = list(pool.held).filter(open).slice(0, SHOWN);
  const nameOf = (id) => names[id] || id;

  let options;
  if (full){
    options = <p className="t42-line-text">{choice.mode === 'items' ? 'Five is the most one comparison holds.' : 'One thing at a time in this mode. Remove it to pick another.'}</p>;
  } else if (pool.state === 'loading'){
    options = <p className="t42-line-text" role="status">Loading trends to add</p>;
  } else if (pool.state === 'error'){
    options = (
      <p className="t42-line-text">
        The trends to add could not load. <button type="button" className="t42-button" onClick={() => setTick((t) => t + 1)}>Load again</button>
      </p>
    );
  } else if (pool.state === 'ready'){
    options = matches.length > 0 || heldMatches.length > 0
      ? <>
          {matches.length > 0 && (
            <ul className="c42-options">
              {matches.map((c) => (
                <li key={c.item_id}>
                  <span className="c42-option-name">{c.title}</span>
                  <button type="button" className="t42-button c42-add" onClick={() => onAdd(c.item_id)}>Add<span className="sr-only"> {c.title}</span></button>
                </li>
              ))}
            </ul>
          )}
          {heldMatches.length > 0 && (
            <>
              <h3 className="c42-held-intro">Held back by checks</h3>
              <ul className="c42-options c42-options-held" data-held-options="">
                {heldMatches.map((c) => {
                  const why = 'c42-held-why-' + c.item_id;
                  return (
                    <li key={c.item_id}>
                      <span className="c42-option-name">{c.title}</span>
                      <span className="c42-held" id={why}><span className="sr-only">Held back: </span>{heldReason(c)}</span>
                      <button type="button" className="t42-button c42-add" aria-describedby={why} onClick={() => onAdd(c.item_id)}>Add<span className="sr-only"> {c.title}</span></button>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </>
      : pool.cursor
        ? <p className="t42-line-text">{words ? 'No trend read so far matches those words.' : 'No trend read so far is left to add.'}</p>
        : <p className="t42-line-text">{words ? 'No trend matches those words.' : 'No more trends to add.'}</p>;
    if (pool.cursor){
      options = (
        <>
          {options}
          <button type="button" className="t42-button" onClick={loadMore} disabled={more === 'loading'}>
            {more === 'loading' ? 'Loading more' : 'Load more trends'}
          </button>
          {more === 'error' && <p className="t42-line-text" role="alert">More trends could not load. Press Load more trends to try again.</p>}
        </>
      );
    }
  }

  return (
    <section className="c42-part" data-section="picker" aria-labelledby="c42-pick-title">
      <h2 className="c42-title" id="c42-pick-title">What to compare</h2>
      {/* What the comparison still needs leads the picker, so the instruction
          is read before the choices it governs. */}
      {/* An empty picker would only repeat the subtitle ("two to five
          things"), so the line stays empty until there is something to say. */}
      <p className="c42-need" role="status">{choice.mode === 'items' && choice.items.length === 0 ? '' : waiting || ''}</p>
      <div className="c42-pick-grid">
      <div className="c42-settings">
      <fieldset className="c42-set">
        <legend className="c42-legend">Compare</legend>
        {MODES.map((m) => (
          <label key={m.id} className="c42-option">
            <input type="radio" name="mode" value={m.id} checked={choice.mode === m.id} onChange={() => onMode(m.id)} />{m.label}
          </label>
        ))}
      </fieldset>
      {choice.mode !== 'markets' && (
        <span className="c42-field">
          <label className="c42-label" htmlFor="c42-market">Market</label>
          <select id="c42-market" name="market" className="c42-control" value={choice.market} onChange={(e) => onMarket(e.target.value)}>
            {MARKETS.map((m) => <option key={m} value={m}>{MARKET_WORDS[m]}</option>)}
          </select>
        </span>
      )}
      {choice.mode === 'markets' && (
        <fieldset className="c42-set">
          <legend className="c42-legend">Markets, two or three</legend>
          {MARKETS.map((m) => (
            <label key={m} className="c42-option">
              <input type="checkbox" name="markets" value={m} checked={choice.markets.includes(m)} onChange={() => onMarkets(m)} />{MARKET_WORDS[m]}
            </label>
          ))}
        </fieldset>
      )}
      {choice.mode === 'platforms' && (
        <fieldset className="c42-set">
          <legend className="c42-legend">Platforms, two to five</legend>
          {PLATFORMS.map((p) => {
            const on = choice.platforms.includes(p);
            return (
              <label key={p} className="c42-option">
                <input type="checkbox" name="platforms" value={p} checked={on} disabled={!on && choice.platforms.length >= MOST} onChange={() => onPlatforms(p)} />{platformWord(p)}
              </label>
            );
          })}
        </fieldset>
      )}
      <fieldset className="c42-set">
        <legend className="c42-legend">Window</legend>
        {DAYS.map((d) => (
          <label key={d} className="c42-option">
            <input type="radio" name="days" value={d} checked={choice.days === d} onChange={() => onDays(d)} />{d} days
          </label>
        ))}
      </fieldset>
      </div>
      <div className="c42-pick">
      {choice.items.length > 0 && (
        <ul className="c42-chosen" aria-label="Chosen to compare">
          {choice.items.map((id) => (
            <li key={id}>
              <span>{nameOf(id)}</span>
              <button type="button" className="t42-button" aria-label={'Remove ' + nameOf(id)} onClick={() => onRemove(id)}>Remove</button>
            </li>
          ))}
        </ul>
      )}
      {!full && (
        <span className="c42-field">
          <label className="c42-label" htmlFor="c42-find">Find a trend in {MARKET_WORDS[from]}</label>
          <input id="c42-find" name="find" type="search" className="c42-control" placeholder="Type a hashtag or topic" value={find} onChange={(e) => setFind(e.target.value)} />
        </span>
      )}
      {options}
      </div>
      </div>
    </section>
  );
}

/* Before the choice is complete, the space the comparison will fill says
   what it will hold, so the page reads whole while trends are picked. */
const RESULT_PARTS = [
  ['One chart per thing', 'Every chart on the same scale, so a taller line is a bigger number.'],
  ['Side by side', 'A table of each measure, with its unit, for every thing chosen.'],
  ['Notes', 'What to keep in mind when reading the comparison.'],
];

function ResultPreview(){
  return (
    <div className="c42-preview" data-part="result-preview">
      <h2 className="c42-preview-title">The comparison appears here</h2>
      <dl className="c42-preview-parts">
        {RESULT_PARTS.map(([term, line]) => (
          <div key={term} className="c42-preview-part"><dt>{term}</dt><dd>{line}</dd></div>
        ))}
      </dl>
    </div>
  );
}

function Result({load, onRetry, onAuth}){
  if (load.state === 'idle') return <ResultPreview />;
  let body;
  if (load.state === 'loading') body = <p className="t42-status" role="status">Reading the comparison</p>;
  else if (load.state === 'auth') body = <p className="t42-status">Enter the passcode to compare.</p>;
  else if (load.state === 'error'){
    body = (
      <>
        <h2 className="c42-title">The comparison could not load</h2>
        <p className="t42-status" role="alert">{load.message}</p>
        <button type="button" className="t42-button" onClick={onRetry}>Try again</button>
      </>
    );
  } else body = <Comparison data={load.data} onAuth={onAuth} />;
  return <div className="c42-result" data-section="result" aria-busy={load.state === 'loading' ? 'true' : 'false'}>{body}</div>;
}

const heldOf = (subject) => (subject.card && subject.card.held_back ? subject.card.held_back : null);
const heldReason = (held) => held.reason_text || sentenceCase(String(held.reason || 'by 42\'s checks').replace(/_/g, ' '));
const heldWords = (held) => 'Held back: ' + heldReason(held);

function scopeOf(subject, mode){
  if (mode === 'markets') return MARKET_WORDS[subject.market] || subject.market;
  if (mode === 'platforms') return platformWord(subject.platform);
  return null;
}

function Comparison({data, onAuth}){
  const subjects = list(data.subjects);
  const series = list(data.series);
  const rows = list(data.rows);
  const notes = list(data.notes);
  const win = data.window || {};
  return (
    <>
      {win.from && win.to && (
        <p className="t42-line-text c42-window">
          {win.days} days, {longDate(win.from)} to {longDate(win.to)}{data.mode !== 'markets' && subjects[0] ? ', ' + (MARKET_WORDS[subjects[0].market] || subjects[0].market) : ''}
        </p>
      )}
      <section className="c42-part" data-section="notes" aria-labelledby="c42-notes-title">
        <h2 className="c42-title" id="c42-notes-title">Read with these notes</h2>
        {notes.length > 0
          ? <ul className="c42-notes">{notes.map((n) => <li key={n}>{n}</li>)}</ul>
          : <p className="t42-line-text">No notes came with this comparison.</p>}
      </section>
      {rows.length > 0 && <Chart subjects={subjects} series={series} mode={data.mode} onAuth={onAuth} />}
      {rows.length > 0 && <Table subjects={subjects} rows={rows} mode={data.mode} />}
    </>
  );
}

function Chart({subjects, series, mode, onAuth}){
  const [spike, setSpike] = useState(null);
  const values = series.flatMap((s) => list(s.points).map((p) => p.value)).filter(num);
  const top = Math.max(1, ...values);
  const unit = (series[0] && series[0].unit) || 'posts a day';
  const gaps = series.some((s) => list(s.points).some((p) => !num(p.value)));
  return (
    <section className="c42-part" data-section="chart" aria-labelledby="c42-chart-title">
      <h2 className="c42-title" id="c42-chart-title">{sentenceCase(unit)}</h2>
      <p className="t42-line-text">Same scale on every chart: 0 to {readerFigure(top)} {unit}.</p>
      {gaps && <p className="t42-line-text">Shaded days had no usable collection, so they are gaps, not zeros.</p>}
      <ul className="c42-multiples">
        {subjects.map((s) => {
          const line = series.find((x) => x.key === s.key);
          const held = heldOf(s);
          const scope = scopeOf(s, mode);
          const label = s.label + (scope ? ', ' + scope : '');
          return (
            <li key={s.key} className="c42-multiple" data-subject={s.key}>
              <p className="c42-multiple-title">{label}</p>
              {held && <p className="c42-held">{heldWords(held)}</p>}
              {line && list(line.points).length > 0
                ? <Multiple points={line.points} top={top} unit={line.unit || unit} label={label}
                    onDay={(p) => setSpike({subject: s, day: {date: p.date, words: label}})} />
                : <p className="t42-line-text">No days to draw.</p>}
            </li>
          );
        })}
      </ul>
      {spike && <SpikeConfirm day={spike.day} itemId={spike.subject.item_id} market={spike.subject.market} onAuth={onAuth} onClose={() => setSpike(null)} />}
    </section>
  );
}

/* One small multiple. Every chart shares the same top, so heights compare
   across subjects. A null day is a gap: the line breaks and the day is
   shaded, never drawn as zero. Each measured day is a button over its point;
   a gap has none, since there is nothing to ask about. */
function Multiple({points, top, unit, label, onDay}){
  const W = 240, H = 64, pad = 4;
  const n = points.length;
  const step = n > 1 ? (W - 2 * pad) / (n - 1) : W - 2 * pad;
  const x = (i) => (n === 1 ? W / 2 : pad + i * step);
  const y = (v) => H - pad - (Math.max(0, v) / top) * (H - 2 * pad);
  const fix = (v) => v.toFixed(1);
  let line = '';
  let open = false;
  points.forEach((p, i) => {
    if (!num(p.value)){ open = false; return; }
    line += (open ? ' L ' : (line ? ' M ' : 'M ')) + fix(x(i)) + ' ' + fix(y(p.value));
    open = true;
  });
  line = line.replace(/M (\S+ \S+)(?= M |$)/g, 'M $1 h 0.1');
  const values = points.map((p) => p.value).filter(num);
  const aria = label + ': ' + unit + ' over ' + n + ' days'
    + (values.length ? ', highest ' + seriesFigure(Math.max(...values)) : ', no usable days')
    + (values.length < n ? ', with days missing' : '');
  const svg = (
    <svg className="c42-spark" viewBox={'0 0 ' + W + ' ' + H} width={W} height={H} role="img" aria-label={aria} data-y-max={top}>
      {points.map((p, i) => {
        if (num(p.value)) return null;
        const left = Math.max(0, x(i) - step / 2);
        const right = Math.min(W, x(i) + step / 2);
        return <rect key={p.date || i} className="c42-gap" x={fix(left)} y="0" width={fix(right - left)} height={H} />;
      })}
      <line className="c42-base" x1="0" y1={H - pad} x2={W} y2={H - pad} />
      {line && <path className="c42-line" d={line} fill="none" strokeLinecap="round" strokeLinejoin="round" />}
    </svg>
  );
  const pct = (v) => (100 * v / W).toFixed(2) + '%';
  return (
    <span className="c42-spark-days">
      {svg}
      {points.map((p, i) => {
        if (!num(p.value) || !p.date) return null;
        const left = Math.max(0, x(i) - step / 2);
        const right = Math.min(W, x(i) + step / 2);
        const words = longDate(p.date) + ', ' + seriesFigure(p.value) + ' ' + unitFor(p.value, unit) + '. Ask why this day jumped';
        return (
          <button key={p.date} type="button" className="c42-day" data-day={p.date}
            style={{left: pct(left), width: pct(right - left)}}
            aria-label={label + ', ' + words} title={words} onClick={() => onDay(p)} />
        );
      })}
    </span>
  );
}

function cellWords(metric, figure){
  if (figure.value === null || figure.value === undefined) return metric === 'first_seen' ? 'Not seen' : 'None';
  if (metric === 'first_seen') return longDate(figure.value);
  if (metric === 'state') return figure.word || sentenceCase(String(figure.value).replace(/_/g, ' '));
  return figureWords(figure);
}

function Table({subjects, rows, mode}){
  return (
    <section className="c42-part" data-section="table" aria-labelledby="c42-table-title">
      <h2 className="c42-title" id="c42-table-title">Side by side</h2>
      {/* Wider than its column with five subjects, and from 1024 px the shell
          clips sideways overflow, so the table scrolls inside a named region
          with a Tab stop that a keyboard reader can scroll. */}
      <div className="c42-scroll" role="region" aria-label="Side by side table" tabIndex={0}>
        <table className="c42-table" aria-labelledby="c42-table-title">
          <thead>
            <tr>
              <th scope="col">Measure</th>
              {subjects.map((s) => {
                const held = heldOf(s);
                const scope = scopeOf(s, mode);
                return (
                  <th key={s.key} scope="col">
                    <span className="c42-head">
                      <a className="t42-link c42-subject" href={topicHref(s.item_id, s.market)}>{s.label}</a>
                      {scope && <span className="c42-scope">{scope}</span>}
                      {s.matched_by === 'keyword' && <span className="c42-matched">Matched by name in post text</span>}
                      {held && <span className="c42-held">{heldWords(held)}</span>}
                    </span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.metric}>
                <th scope="row">{r.words}</th>
                {subjects.map((s) => {
                  const figure = r.values ? r.values[s.key] : null;
                  const id = 'c42-q-' + r.metric + '-' + s.key;
                  return (
                    <td key={s.key}>
                      {figure
                        ? <>
                            <span className="c42-fig" tabIndex={0} data-query-id={figure.query_id} aria-describedby={id}>{cellWords(r.metric, figure)}</span>
                            <span className="c42-tip" role="tooltip" id={id}>Query {figure.query_id}, run {figure.run_id}</span>
                          </>
                        : <span className="c42-none">Not measured</span>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
