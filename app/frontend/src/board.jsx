/* PULSE · My Board. The watchlist, client-side only: topics pinned to
   pulse-watch and creators tracked in pulse-crm, resolved against the live desk
   and voices boards. The since-last-look move is a real stored snapshot: the
   last observed value for each pinned item, written to pulse-lastlook on every
   board visit, compared against the fresh value on the next one. First visit
   after pinning has nothing to compare against yet, so it shows no move
   rather than inventing one. Nothing here is a forecast. */
import {useState, useEffect, useRef} from 'react';
import {useApi} from './api.js';
import {MOM_META, Sparkline, SignalLoader, EmptyState} from './parts.jsx';
import {human, REGION_NAME, readerWord} from './model.js';
import {buildTopicHash} from './router.js';
import {readTopicPins, changeTopicPins, resolveTopicPin, topicPinKey, validTopicRows, clearTopicPrior} from './topicPins.js';
import './styles/boardviews.css';

const CRM_KEY = 'pulse-crm';
/* Demo polish, 2 October 2026: a pin with no stored reading yet says so in
   plain words, and a market is written as its name rather than its code. */
const FIRST_LOOK = 'First look, no change yet';
const marketName = (market) => REGION_NAME[String(market || '').toUpperCase()] || '';
const LASTLOOK_KEY = 'pulse-lastlook';

function sameHandle(a, b){
  const normalize = (value) => typeof value === 'string' ? value.trim().replace(/^@+/, '').toLowerCase() : '';
  return Boolean(normalize(a)) && normalize(a) === normalize(b);
}

function load(key){
  try { const v = JSON.parse(localStorage.getItem(key) || '[]'); return Array.isArray(v) ? v : []; }
  catch (e){ return []; }
}

function readLastLook(){
  try { const v = JSON.parse(localStorage.getItem(LASTLOOK_KEY) || '{}'); return v && typeof v === 'object' && !Array.isArray(v) ? v : null; }
  catch (e){ return null; }
}
function writeLastLook(v){
  try { localStorage.setItem(LASTLOOK_KEY, JSON.stringify(v)); return true; } catch (e){ return false; }
}

/* the observed value to snapshot per kind: a topic's latest daily volume, a
   voice's latest reach. Both are real backend fields, never derived. */
function seriesTail(series){
  return Array.isArray(series) && series.length ? series[series.length - 1] : null;
}

/* percent move against the last stored snapshot for this key. Returns null
   (no move line at all) when there is no prior snapshot to compare against,
   or when the current value cannot be read. */
function deltaFromStore(store, key, value){
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null;
  const prior = store?.[key];
  if (!validPrior(prior)) return null;
  if (prior.v === 0) return null;
  const change = (value - prior.v) / prior.v * 100;
  return Number.isFinite(change) ? change : null;
}

function validPrior(prior){
  return prior && typeof prior.v === 'number' && Number.isFinite(prior.v) && prior.v >= 0 && typeof prior.at === 'number' && Number.isFinite(prior.at) && prior.at > 0 && prior.at <= Date.now();
}

function comparisonNote(store, key, value){
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 'Current reading unavailable';
  if (!store) return 'Previous reading unavailable';
  const prior = store[key];
  if (!prior) return FIRST_LOOK;
  if (!validPrior(prior)) return 'Previous reading unavailable';
  return prior.v === 0 ? 'Percentage change unavailable from a zero baseline' : 'Percentage change unavailable';
}

function momLabel(m){
  return (MOM_META[m] || MOM_META.steady).label;
}

/* the move line, dash-free copy, color from direction */
function chgLine(v, missing = FIRST_LOOK){
  const flat = typeof v !== 'number';
  /* Demo polish, 2 October 2026: muted, not faint, so the first look line
     clears 4.5:1 on the midnight card. */
  const color = flat ? 'var(--muted)' : (v >= 0 ? 'var(--up)' : 'var(--down)');
  const text = flat
    ? missing
    : (v >= 0 ? '▲ ' : '▼ ') + Number(Math.abs(v).toPrecision(3)).toString() + '% since last look';
  return {color, text};
}

/* rough age of the earliest stored snapshot across the pinned set, for the
   "since X ago" strap. null when there is nothing stored yet (first visit). */
function ageLabel(ms){
  if (!ms) return null;
  const mins = Math.max(1, Math.round((Date.now() - ms) / 60000));
  if (mins < 60) return mins + (mins === 1 ? ' minute ago' : ' minutes ago');
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return hrs + (hrs === 1 ? ' hour ago' : ' hours ago');
  const days = Math.round(hrs / 24);
  return days + (days === 1 ? ' day ago' : ' days ago');
}

const dividerWrap = {display: 'flex', alignItems: 'center', gap: '14px', margin: '34px 0 0', flexWrap: 'wrap'};
/* Demo polish, 2 October 2026: the section title is a heading in sentence
   case, the sans at 16px and 600 with no tracking, as the 42 pages title
   their sections. */
const dividerLabel = {
  fontFamily: 'var(--sans)', fontSize: 'var(--type-4)', fontWeight: 600, color: 'var(--ink)', margin: 0,
};
const dividerTick = {
  display: 'inline-block', width: '16px', height: '2px', background: 'var(--accent)', marginRight: '12px', verticalAlign: 'middle',
};
const dividerRule = {flex: 1, height: '1px', background: 'var(--line)', minWidth: '20px'};
const dividerMeta = {fontFamily: 'var(--sans)', fontSize: 'var(--type-2)', color: 'var(--muted)'};
const grid = {display: 'grid', gridTemplateColumns: 'repeat(2,1fr)', gap: '13px', marginTop: '18px'};
const sparkWrap = {margin: '10px 0 2px'};
const missLine = {fontFamily: 'var(--sans)', fontSize: 'var(--type-2)', color: 'var(--muted)', marginTop: '8px', display: 'block'};
const moveLine = {display: 'inline-block', fontFamily: 'var(--sans)', fontSize: 'var(--type-2)', marginTop: '8px'};

export function BoardPage({session, onAuth}){
  const [watch, setWatch] = useState(readTopicPins);
  const [crm, setCrm] = useState(() => load(CRM_KEY));
  const [desk, retryDesk] = useApi('/api/desk?region=all', session, onAuth);
  const [voices, retryVoices] = useApi('/api/voices?region=all', session, onAuth);
  const [historyError, setHistoryError] = useState(false);
  const [trackingError, setTrackingError] = useState(false);
  /* the last-look snapshot read once per mount, so every card on this visit
     compares against the same prior state rather than a store that mutates
     mid-render as later cards are written. */
  const lastLookRef = useRef(readLastLook());
  const snapshotted = useRef(new Set());
  const pendingHistoryCleanup = useRef(new Map());

  const deskReady = desk.state === 'ready' && validTopicRows(desk.data?.topics);
  const voicesReady = voices.state === 'ready' && Array.isArray(voices.data?.creators) && voices.data.creators.every(row => row && typeof row.handle === 'string');
  const deskTopics = deskReady ? desk.data.topics : null;
  const allCreators = voicesReady ? voices.data.creators : [];

  const unwatch = (pin) => {
    const key = topicPinKey(pin);
    const next = changeTopicPins(pins => pins.filter(saved => topicPinKey(saved) !== key));
    setWatch(next);
    if (next.status !== 'ready') return;
    const priorKey = typeof pin === 'string' ? 'topic:' + pin : key;
    if (lastLookRef.current) delete lastLookRef.current[priorKey];
    snapshotted.current.delete(key);
    if (!clearTopicPrior(pin)) {pendingHistoryCleanup.current.set(key, () => clearTopicPrior(pin)); setHistoryError(true);}
    else pendingHistoryCleanup.current.delete(key);
  };
  const resolveLegacy = (id, market) => setWatch(changeTopicPins(pins => {
    const pin = {id, market}, key = topicPinKey(pin);
    const retained = pins.filter(saved => saved !== id);
    return retained.some(saved => topicPinKey(saved) === key) ? retained : [...retained, pin];
  }));
  const clearVoiceHistory = (handle) => {
    const store = readLastLook();
    if (!store) return false;
    for (const key of Object.keys(store)) if (key.startsWith('voice:') && sameHandle(key.slice(6), handle)) delete store[key];
    return writeLastLook(store);
  };
  const untrack = (handle) => {
    const next = crm.filter((x) => !sameHandle(x?.name, handle));
    try { localStorage.setItem(CRM_KEY, JSON.stringify(next)); } catch (e){setTrackingError(true); return;}
    setCrm(next); setTrackingError(false);
    const key = 'voice:' + handle;
    if (!clearVoiceHistory(handle)) {pendingHistoryCleanup.current.set(key, () => clearVoiceHistory(handle)); setHistoryError(true);}
    else pendingHistoryCleanup.current.delete(key);
  };
  const retryHistory = () => {
    for (const [key, clear] of pendingHistoryCleanup.current) if (clear()) pendingHistoryCleanup.current.delete(key);
    setHistoryError(pendingHistoryCleanup.current.size > 0);
  };

  const topics = watch.pins.map((pin) => {
    const topic = resolveTopicPin(pin, deskTopics);
    const value = seriesTail(topic.series);
    return {...topic, move: topic.found ? deltaFromStore(lastLookRef.current, topic.key, value) : null, comparisonNote: comparisonNote(lastLookRef.current, topic.key, value)};
  });
  const creators = crm.filter((c, index, rows) => typeof c?.name === 'string' && !rows.slice(0, index).some((prior) => sameHandle(prior?.name, c.name))).map((c) => {
    const v = allCreators.find((x) => sameHandle(x.handle, c.name));
    if (!v) return {handle: c.name, found: false, unavailable: !voicesReady};
    const value = typeof v.reach === 'number' ? v.reach : seriesTail(v.series);
    const move = deltaFromStore(lastLookRef.current, 'voice:' + c.name, value);
    return {handle: c.name, found: true, platform: v.platform, market: v.market, reach: v.reach, posts: v.posts, series: v.series, move, comparisonNote: comparisonNote(lastLookRef.current, 'voice:' + c.name, value)};
  });

  const loading = desk.state === 'loading' || voices.state === 'loading';
  const empty = watch.status === 'ready' && !watch.pins.length && !crm.length;

  /* once the live data is in, write today's snapshot for next time. Runs
     once per successful load, after render has already used the prior
     snapshot for this visit's delta. */
  useEffect(() => {
    if (loading) return;
    const store = readLastLook();
    if (!store) {setHistoryError(true); return;}
    const now = Date.now();
    const recorded = [];
    topics.forEach((t) => {
      if (!t.found || snapshotted.current.has(t.key)) return;
      const value = seriesTail(t.series);
      if (typeof value === 'number' && Number.isFinite(value)) { store[t.key] = {v: value, at: now}; recorded.push(t.key); }
    });
    creators.forEach((c) => {
      if (!c.found || snapshotted.current.has('voice:' + c.handle)) return;
      const value = typeof c.reach === 'number' ? c.reach : seriesTail(c.series);
      if (typeof value === 'number' && Number.isFinite(value)) { store['voice:' + c.handle] = {v: value, at: now}; recorded.push('voice:' + c.handle); }
    });
    if (recorded.length) {
      if (writeLastLook(store)) recorded.forEach(key => snapshotted.current.add(key));
      else setHistoryError(true);
    }
  });

  /* the oldest prior snapshot among today's pinned set: the honest "since"
     label. Falls back to nothing when this is the first look. */
  const priorAts = [];
  topics.forEach((t) => { const p = t.market && lastLookRef.current?.[t.key]; if (validPrior(p)) priorAts.push(p.at); });
  creators.forEach((c) => { const p = lastLookRef.current?.['voice:' + c.handle]; if (validPrior(p)) priorAts.push(p.at); });
  const sinceLabel = priorAts.length ? ageLabel(Math.min(...priorAts)) : null;

  const goTopics = () => { window.location.hash = '#/pulse'; };

  return (
    /* Demo polish, 2 October 2026: the page is no longer the .board card (a
       box drawn around the whole page); it sits on the canvas as the 42 pages
       do. Shell consistency, 2 October 2026: it takes the shared shell gutter,
       the menu's noun is the title, and the old headline leads the sentence
       under it. Each card names its own market. */
    <div className="page board-page">
      <div className="page-shell">
        <div className="reveal">
          <h1 className="page-title">Board</h1>
          <p className="page-sub">What you are tracking: the topics and voices you starred, with what changed since you last looked. Stored on this device.</p>
        </div>
        {watch.status === 'unavailable' && <p role="alert">Topic pins could not be saved or read. Existing saved data has been preserved. <button className="legacy-action" onClick={() => setWatch(readTopicPins())}>Retry saved pins</button></p>}
        {historyError && <p role="alert">Device storage could not read or save this change. Existing data has been preserved. <button className="legacy-action" onClick={retryHistory}>Retry history</button></p>}
        {trackingError && <p role="alert">The tracked handle could not be removed from device storage. Try Untrack again.</p>}
        {!loading && (!deskReady || !voicesReady) && <p role="alert">Some Board records are unavailable. Saved pins and their previous readings remain. <button className="legacy-action" onClick={() => {retryDesk(); retryVoices();}}>Retry Board reads</button></p>}

        {empty && (
          <div className="reveal">
            <EmptyState
              loader="ring"
              isLoading={false}
              title="Nothing pinned yet"
              body="Star a topic or a voice anywhere in 42 and it lands here, with how it moved since your last visit."
              actions={[{label: 'Open Today', primary: true, onClick: goTopics}]}
            />
          </div>
        )}

        {!empty && loading && (
          <div style={{display: 'flex', alignItems: 'center', gap: '14px', marginTop: '28px'}}>
            <SignalLoader />
            <span style={{fontFamily: 'var(--sans)', fontSize: 'var(--type-2)', color: 'var(--muted)'}}>Loading your board</span>
          </div>
        )}

        {!empty && !loading && !!topics.length && (
          <>
            <div style={dividerWrap}>
              <h2 style={dividerLabel}><span style={dividerTick} />Tracked topics</h2>
              <span style={dividerRule} />
              <span style={dividerMeta}>{topics.length} pinned{sinceLabel ? ' · since ' + sinceLabel : ''}</span>
            </div>
            <div className="board-grid" style={grid}>
              {topics.map((t) => {
                const color = t.found ? (MOM_META[t.momentum] || MOM_META.steady).color : 'var(--line)';
                const move = chgLine(t.found ? t.move : null, t.comparisonNote);
                return (
                  <div key={t.key} data-topic-pin-state={t.state} data-topic-pin-market={t.market || undefined} className="board-card board-card--topic" style={{'--board-momentum': color}}>
                    <button onClick={() => unwatch(t.pin)} aria-label="Unpin" className="board-close">×</button>
                    {t.found ? (
                      <a href={buildTopicHash(t.id, t.market)} className="board-card-link">
                        <span className="board-card-heading">
                          <span className="board-topic-name">
                            <span className="board-title">{t.name}</span>
                          </span>
                          <span className="board-metadata">{marketName(t.market)}</span>
                          <span className="board-momentum">{momLabel(t.momentum)}</span>
                        </span>
                        {Array.isArray(t.series) && t.series.length > 1 && (
                          <span style={{...sparkWrap, display: 'block'}}>
                            <Sparkline data={t.series} color={color} height={32} dot={false} strokeWidth={1.8} animate={false} />
                          </span>
                        )}
                        <span style={{...moveLine, color: move.color}}>{move.text}</span>
                      </a>
                    ) : t.state === 'legacy' ? (
                      <div className="board-legacy-pin">
                        <span className="board-title">{t.id.replace(/_/g, ' ')}</span>
                        <p>The market was not stored with this pin.</p>
                        {t.choices.length ? <div className="board-market-choices">{t.choices.map(market => <button key={market} className="legacy-action" onClick={() => resolveLegacy(t.id, market)}>Use {({za: 'South Africa', ng: 'Nigeria', ke: 'Kenya'})[market]}</button>)}</div> : <><p>No market choices are available from this read. Retry the topic read or remove this pin.</p><button className="legacy-action" onClick={retryDesk}>Retry topic records</button></>}
                      </div>
                    ) : t.state === 'unavailable' ? <p>Topic records are unavailable. Retry the Board read.</p> : (
                      <a href={buildTopicHash(t.id, t.market)} className="board-card-link">
                        <span style={{display: 'flex', alignItems: 'center', gap: '10px'}}>
                          <span className="board-title board-identifier">{t.id}</span>
                        </span>
                        <span className="board-metadata">{marketName(t.market)}</span>
                        <span style={missLine}>{t.state === 'ambiguous' ? 'Multiple records were returned for this topic and market. Open the latest story.' : 'No matching record was returned for this market. Open the latest story.'}</span>
                      </a>
                    )}
                  </div>
                );
              })}
            </div>
          </>
        )}

        {!empty && !loading && !!creators.length && (
          <>
            <div style={dividerWrap}>
              <h2 style={dividerLabel}><span style={dividerTick} />Tracked voices</h2>
              <span style={dividerRule} />
              <span style={dividerMeta}>{creators.length} tracked</span>
            </div>
            <p style={{fontSize: 'var(--type-2)', color: 'var(--muted)'}}>Handle profiles can combine records from different markets and platforms.</p>
            <div className="board-grid" style={grid}>
              {creators.map((c) => {
                const move = chgLine(c.found ? c.move : null, c.comparisonNote);
                return (
                  <div key={c.handle} className="board-card">
                    <button onClick={() => untrack(c.handle)} aria-label="Untrack" className="board-close">×</button>
                    {c.found ? (
                      <a href={'#/creator/' + encodeURIComponent(c.handle)} className="board-card-link">
                        <span className="board-card-heading">
                          <span className="board-handle board-identifier" data-identifier={'@' + c.handle}>@{c.handle.split(/([._-])/).flatMap((part, index) => /^[._-]$/.test(part) ? [part, <wbr key={index} />] : [part])}</span>
                          <span className="board-metadata">{[marketName(c.market), readerWord(c.platform)].filter(Boolean).join(' · ')}</span>
                        </span>
                        <span style={{display: 'block', fontFamily: 'var(--sans)', fontSize: 'var(--type-2)', color: 'var(--muted)', marginTop: '8px'}}>
                          <span className="board-reach">{human(c.reach)}</span> engagement · {c.posts} posts · last 30 days
                        </span>
                        {Array.isArray(c.series) && c.series.length > 1 && (
                          <span style={{...sparkWrap, display: 'block'}}>
                            <Sparkline data={c.series} color="var(--accent)" height={32} dot={false} strokeWidth={1.8} animate={false} />
                          </span>
                        )}
                        <span style={{...moveLine, color: move.color}}>{move.text}</span>
                      </a>
                    ) : (
                      <a href={'#/creator/' + encodeURIComponent(c.handle)} className="board-card-link">
                        <span className="board-card-heading">
                          <span className="board-handle board-identifier" data-identifier={'@' + c.handle}>@{c.handle.split(/([._-])/).flatMap((part, index) => /^[._-]$/.test(part) ? [part, <wbr key={index} />] : [part])}</span>
                        </span>
                        <span style={missLine}>{c.unavailable ? 'Voice records are unavailable. Retry the Board read.' : 'No matching handle was returned. Open the profile for the latest.'}</span>
                      </a>
                    )}
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
