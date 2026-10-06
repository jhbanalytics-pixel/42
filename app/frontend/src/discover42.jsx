/* Discover on the 42 API: every live trend by market, kind, state and
   platform, plus Radar, growth against reach (docs/full-42/EXPERIENCE.md,
   Discover; core/api/contract.md section 10.2). explore.jsx's discovery
   modes become the kind tabs, read from the API's own filters. What the
   gate held back is listed with its reason and card, never dropped. */
import {Fragment, useEffect, useRef, useState} from 'react';
import {fetchDiscover, fetchRadar} from './api42.js';
import {readerFigure, sentenceCase} from './api.js';
import {human} from './model.js';
import {SearchingNow} from './ui/SearchingNow.jsx';
import {TrendCard, figureWords, longDate, platformWord, topicHref} from './ui/TrendCard.jsx';
import {Skeleton} from './ui/Skeleton.jsx';
import {useCardWatch} from './ui/WatchDialog.jsx';
import './styles/today42.css';
import './styles/discover42.css';

const MARKETS = ['ZA', 'NG', 'KE'];
const TABS = [
  {id: 'ZA', label: 'South Africa'},
  {id: 'NG', label: 'Nigeria'},
  {id: 'KE', label: 'Kenya'},
  {id: 'ALL', label: 'All'},
];
const KIND_WORDS = {
  topic: 'Topics', topics: 'Topics', hashtag: 'Hashtags', hashtags: 'Hashtags', sound: 'Sounds', sounds: 'Sounds',
  format: 'Formats', formats: 'Formats', creator: 'Creators', creators: 'Creators', meme: 'Memes', memes: 'Memes',
};
const STATE_WORDS = {
  new_to_42: 'First spotted', spike: 'Spike', on_the_boards: 'On the boards', emerging: 'Emerging', rising: 'Rising',
  peaking: 'Peaking', mainstream: 'Mainstream', fading: 'Fading', recurring: 'Recurring', seasonal: 'Seasonal',
};
const FLAG_WORDS = {
  check_pattern: 'Unusual posting pattern', likely_coordinated: 'Likely coordinated', paid_led: 'Paid-led',
  market_unconfirmed: 'Market unconfirmed', not_assessed: 'Not assessed (thin sample)', data_issue: 'Data issue',
};
const FEW_PLACED = 'Fewer than 8 posts in 7 days had a known place, so 42 has not confirmed the market';
const SORTS = [
  {id: 'order', label: 'Worth attention'},
  {id: 'velocity', label: 'Velocity'},
  {id: 'reach', label: 'Reach'},
  {id: 'new', label: 'Newest'},
];
const LIMIT = 50;
const SLOW_READ_MS = 5000;
const WARM_NOTE = 'Growth needs 14 days of data; showing reach only';
const SAYS_GROWTH_WAIT = /growth needs 14 days/i;
const hasField = (card, key) => Boolean(card) && Object.prototype.hasOwnProperty.call(card, key);

/* The not-yet states true of every card in the list, so the page says each
   once instead of on every card. One card keeps its own wording. */
function sharedNotYet(items){
  if (items.length < 2) return [];
  const states = [];
  if (items.every((card) => hasField(card, 'spread_line') && !card.spread_line)) states.push('spread');
  if (items.every((card) => hasField(card, 'growth') && !isFigure(card.growth))) states.push('growth');
  return states;
}

function notYetSentence(states, radarSaysGrowth){
  const parts = [];
  if (states.includes('spread')) parts.push('spread is not measured yet');
  if (states.includes('growth') && !radarSaysGrowth) parts.push('growth needs 14 days of data');
  return parts.length ? 'For every trend here, ' + parts.join(' and ') + '.' : '';
}
const SEARCH_MARKET_NAMES = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya'};

const codeWords = (code) => sentenceCase(String(code || '').replace(/_/g, ' '));
const kindWord = (kind) => KIND_WORDS[kind] || codeWords(kind);
const stateWord = (state) => STATE_WORDS[state] || codeWords(state);
const isFigure = (value) => value && typeof value === 'object' && typeof value.value === 'number' && Number.isFinite(value.value);
const list = (value) => (Array.isArray(value) ? value : []);
const globalSearchLabel = (card) => {
  if (!card || card.market_scope === 'market') return '';
  const searchMarket = SEARCH_MARKET_NAMES[String(card.market || '').toUpperCase()];
  // A missing or unknown scope still fails closed, but only a global scope claims reach outside the market.
  if (card.market_scope !== 'global') return searchMarket ? 'Not confirmed as local to ' + searchMarket : 'Not confirmed as local';
  return 'Also popular outside ' + (searchMarket || 'this market');
};
const emptyWords = (market, held) => {
  const where = SEARCH_MARKET_NAMES[market] || 'any market';
  const count = held && typeof held.count === 'number' ? held.count : list(held && held.items).length;
  const heldWords = count === 1 ? ' 1 is held back; see why below.' : count > 1 ? ' ' + count + ' are held back; see why below.' : '';
  return 'No trends have cleared the checks in ' + where + ' yet.' + heldWords;
};
/* Demo polish, 2 October 2026: a card that carries no market basis, or no
   share, gets no evidence line at all; its Global label already says where it
   was seen. Figures that are present but disagree still say so. */
const noValue = (value) => value === null || value === undefined;
const marketEvidenceLabel = (card) => {
  if (!card || card.market_scope === 'market') return '';
  const marketPosts = card.market_posts7;
  const totalPosts = card.total_posts7;
  const share = card.market_share7;
  if (noValue(marketPosts) || noValue(totalPosts)) return '';
  if (noValue(share) && totalPosts !== 0) return '';
  if (!Number.isSafeInteger(marketPosts) || !Number.isSafeInteger(totalPosts)
    || marketPosts < 0 || totalPosts < marketPosts || totalPosts > 12){
    return 'Market evidence share unavailable';
  }
  if (totalPosts === 0){
    return marketPosts === 0 && share === null
      ? 'Market evidence: 0 of 0 selected posts, no measured share over 7 days'
      : 'Market evidence share unavailable';
  }
  if (typeof share !== 'number' || !Number.isFinite(share) || share < 0 || share > 1){
    return 'Market evidence share unavailable';
  }

  const shareTenths = Math.round(share * 1000);
  const countTenths = Math.round((marketPosts / totalPosts) * 1000);
  if (shareTenths !== countTenths) return 'Market evidence share unavailable';
  const percent = shareTenths / 10;
  const percentText = Number.isInteger(percent) ? String(percent) : percent.toFixed(1);
  return 'Market evidence: ' + marketPosts + ' of ' + totalPosts + ' selected posts (' + percentText + '%) over 7 days';
};

function tabForRegion(region){
  const value = String(region || '').toUpperCase();
  return MARKETS.includes(value) ? value : 'ALL';
}

/* The filters ride in the #/explore query (kind, state, platform, sort), so
   Back from a topic page lands on the same filtered list. The page replaces
   the hash as filters change, the way Compare does, and never pushes. */
const SORT_IDS = new Set(SORTS.map((s) => s.id));
export function parseDiscoverQuery(hash){
  const text = String(hash || '');
  const at = text.indexOf('?');
  const query = new URLSearchParams(at >= 0 ? text.slice(at + 1) : '');
  const sort = query.get('sort') || '';
  return {
    kind: query.get('kind') || '',
    state: query.get('state') || '',
    platform: query.get('platform') || '',
    sort: SORT_IDS.has(sort) ? sort : 'order',
  };
}
export function discoverHash({kind, state, platform, sort}){
  const query = new URLSearchParams();
  if (kind) query.set('kind', kind);
  if (state) query.set('state', state);
  if (platform) query.set('platform', platform);
  if (sort && sort !== 'order') query.set('sort', sort);
  const qs = query.toString();
  return '#/explore' + (qs ? '?' + qs : '');
}
const onExplore = () => /^#\/?explore(?:[?/]|$)/.test(String(window.location.hash || ''));

const apiMarket = (tab) => (tab === 'ALL' ? 'all' : tab);
const compareHref = (itemId, market) => '#/compare?mode=items&items=' + encodeURIComponent(itemId) + '&market=' + encodeURIComponent(market);

export function Discover42({region, onAuth, onCreateWatch, onRegionChange}){
  const [market, setMarket] = useState(() => tabForRegion(region));
  const [initial] = useState(() => parseDiscoverQuery(onExplore() ? window.location.hash : ''));
  const [kind, setKind] = useState(initial.kind);
  const [stateFilter, setStateFilter] = useState(initial.state);
  const [platform, setPlatform] = useState(initial.platform);
  const [sort, setSort] = useState(initial.sort);
  const [feedTick, setFeedTick] = useState(0);
  const [load, setLoad] = useState({state: 'loading'});
  const [more, setMore] = useState('idle');
  const [filters, setFilters] = useState({kinds: [], states: [], platforms: []});
  const [radarNote, setRadarNote] = useState('');
  const authRef = useRef(onAuth);
  authRef.current = onAuth;
  const moreCtrl = useRef(null);
  const watch = useCardWatch(onCreateWatch, onAuth);

  useEffect(() => { setMarket(tabForRegion(region)); }, [region]);

  useEffect(() => {
    if (!onExplore()) return;
    const hash = discoverHash({kind, state: stateFilter, platform, sort});
    if (window.location.hash !== hash) window.history.replaceState(window.history.state, '', hash);
  }, [kind, stateFilter, platform, sort]);

  const params = {market: apiMarket(market), kind, state: stateFilter, platform, sort, limit: LIMIT};

  const failed = (error, fallback) => {
    if (error && error.auth){
      if (authRef.current) authRef.current();
      return {state: 'auth'};
    }
    return {state: 'error', message: error && error.status ? error.message : fallback};
  };

  useEffect(() => {
    const ctrl = new AbortController();
    if (moreCtrl.current) moreCtrl.current.abort();
    setLoad({state: 'loading'});
    setMore('idle');
    const slowTimer = setTimeout(() => {
      if (!ctrl.signal.aborted){
        setLoad((current) => current.state === 'loading' ? {...current, slow: true} : current);
      }
    }, SLOW_READ_MS);
    fetchDiscover(params, {signal: ctrl.signal})
      .then((data) => {
        clearTimeout(slowTimer);
        if (ctrl.signal.aborted) return;
        const body = data || {};
        setLoad({state: 'ready', data: body, items: list(body.items), cursor: body.next_cursor || null});
        if (body.filters) setFilters({kinds: list(body.filters.kinds), states: list(body.filters.states), platforms: list(body.filters.platforms)});
      })
      .catch((error) => {
        clearTimeout(slowTimer);
        if (!ctrl.signal.aborted) setLoad(failed(error, 'The 42 service did not answer.'));
      });
    return () => {
      clearTimeout(slowTimer);
      ctrl.abort();
    };
  }, [market, kind, stateFilter, platform, sort, feedTick]);

  useEffect(() => () => { if (moreCtrl.current) moreCtrl.current.abort(); }, []);

  const loadMore = () => {
    if (load.state !== 'ready' || !load.cursor) return;
    const ctrl = new AbortController();
    moreCtrl.current = ctrl;
    setMore('loading');
    fetchDiscover({...params, cursor: load.cursor}, {signal: ctrl.signal})
      .then((data) => {
        if (ctrl.signal.aborted) return;
        const body = data || {};
        setLoad((prev) => ({...prev, items: prev.items.concat(list(body.items)), cursor: body.next_cursor || null}));
        setMore('idle');
      })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){ if (authRef.current) authRef.current(); setMore('idle'); return; }
        setMore('error');
      });
  };

  const kinds = filters.kinds;

  return (
    <section className="page t42 d42">
      <header className="t42-head">
        <h1 className="t42-heading">Discover</h1>
        <p className="t42-status">What else is 42 following, beyond today’s picks? Filter by market, kind, state and platform.</p>
      </header>
      <div className="t42-tabs" role="tablist" aria-label="Markets">
        {TABS.map((t) => (
          <button key={t.id} type="button" role="tab" id={'d42-tab-' + t.id} aria-selected={market === t.id ? 'true' : 'false'} aria-controls="d42-panel" className="t42-tab" onClick={() => {
            setMarket(t.id);
            if (onRegionChange) onRegionChange(t.id);
          }}>{t.label}</button>
        ))}
      </div>
      <div id="d42-panel" role="tabpanel" aria-labelledby={'d42-tab-' + market}>
        {kinds.length > 0 && (
          <div className="t42-tabs d42-kinds" role="tablist" aria-label="Kinds">
            {[''].concat(kinds).map((k) => (
              <button key={k || 'all'} type="button" role="tab" aria-selected={kind === k ? 'true' : 'false'} aria-controls="d42-feed" className="t42-tab" onClick={() => setKind(k)}>
                {k ? kindWord(k) : 'All kinds'}
              </button>
            ))}
          </div>
        )}
        <div className="d42-filters">
          <Choice id="d42-state" name="state" label="State" value={stateFilter} onChange={setStateFilter}
            options={[{id: '', label: 'Every state'}].concat(filters.states.map((s) => ({id: s, label: stateWord(s)})))} />
          <Choice id="d42-platform" name="platform" label="Platform" value={platform} onChange={setPlatform}
            options={[{id: '', label: 'Every platform'}].concat(filters.platforms.map((p) => ({id: p, label: platformWord(p)})))} />
          <Choice id="d42-sort" name="sort" label="Sort by" value={sort} onChange={setSort} options={SORTS} />
        </div>
        <Radar market={market} kind={kind} onAuth={onAuth} onNote={setRadarNote} />
        <Feed load={load} market={market} filtered={Boolean(kind || stateFilter || platform)} more={more} onMore={loadMore} onRetry={() => setFeedTick((t) => t + 1)} onAuth={onAuth} watch={watch}
          radarSaysGrowth={SAYS_GROWTH_WAIT.test(radarNote)} />
        {load.state === 'ready' && <HeldBack held={load.data.held_back} market={market} date={load.data.date} onAuth={onAuth} watch={watch} />}
      </div>
      {watch.dialog}
    </section>
  );
}

function Choice({id, name, label, value, options, onChange}){
  return (
    <span className="d42-choice">
      <label className="d42-label" htmlFor={id}>{label}</label>
      <select id={id} name={name} className="d42-select" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => <option key={o.id || 'every'} value={o.id}>{o.label}</option>)}
      </select>
    </span>
  );
}

function Feed({load, market, filtered, more, onMore, onRetry, onAuth, watch, radarSaysGrowth = false}){
  if (load.state === 'loading'){
    return (
      <>
        <p className="t42-status" role="status" aria-live="polite" data-feed-status="">
          {load.slow ? 'Discover is taking longer than usual.' : 'Loading Discover'}
        </p>
        <section className="d42-feed" id="d42-feed" aria-busy="true">
          <h2 className="d42-title">Trends</h2>
          <div className="d42-loading-grid" aria-hidden="true">
            <Skeleton variant="card" className="d42-loading-card" />
          </div>
        </section>
      </>
    );
  }
  if (load.state === 'auth'){
    return (
      <>
        <p className="t42-status" role="status" data-feed-status="">Enter the passcode to read Discover.</p>
        <section className="d42-feed" id="d42-feed"><h2 className="d42-title">Trends</h2></section>
      </>
    );
  }
  if (load.state === 'error'){
    return (
      <section className="d42-feed" id="d42-feed">
        <h2 className="d42-title">Discover could not load</h2>
        <p className="t42-status" role="alert">{load.message}</p>
        <button type="button" className="t42-button" onClick={onRetry}>Try again</button>
      </section>
    );
  }
  const data = load.data;
  const items = load.items;
  const notYet = sharedNotYet(items);
  const notYetLine = notYetSentence(notYet, radarSaysGrowth);
  const moreBusy = more === 'loading';
  const moreStatus = data.date
    ? 'Loading more trends. Current results are from ' + longDate(data.date) + '.'
    : 'Loading more trends. Current results stay visible while more load.';
  return (
    <>
      {moreBusy && <p className="t42-status" role="status" aria-live="polite" data-feed-status="">{moreStatus}</p>}
      <section className="d42-feed" id="d42-feed" aria-label="Trends" aria-busy={moreBusy ? 'true' : undefined}>
        <h2 className="d42-title">Trends</h2>
        {data.date && <p className="t42-status">Updated {longDate(data.date)}</p>}
        {notYetLine && <p className="t42-status d42-not-yet" data-feed-not-yet="">{notYetLine}</p>}
        <SearchingNow signals={Array.isArray(data.searching_now) ? data.searching_now : []} market={market} />
        {items.length > 0
          ? <ol className="t42-cards">
              {items.map((card, index) => {
                const label = globalSearchLabel(card);
                const evidence = marketEvidenceLabel(card);
                const where = card.market || apiMarket(market);
                /* Only the not-run line is hidden on Discover; a held-back
                   explanation (failed checks) stays visible. */
                const notRun = card.explanation_status === 'not_run';
                return (
                  <TrendCard key={where + ':' + card.item_id} index={index}
                    className={'d42-cell' + (label ? ' d42-cell-global' : '') + (notRun ? ' d42-cell-not-run' : '')}
                    card={watch.mark(card, apiMarket(market))} market={where} date={card.date || data.date}
                    onAuth={onAuth} onWatch={watch.onWatch} linkTopic posts={false} sparkMinDays={14} saidAbove={notYet}
                    scope={label ? <>
                      <p className="d42-global-scope">{label}</p>
                      {evidence && <p className="d42-market-evidence">{evidence}</p>}
                    </> : null}
                    extraActions={<a className="t42-action" href={compareHref(card.item_id, where)}>Compare with</a>} />
                );
              })}
            </ol>
          : <p className="t42-status">{filtered ? 'No trends match these filters.' : emptyWords(market, data.held_back)}</p>}
        {load.cursor && (
          <button type="button" className="t42-button" onClick={onMore} disabled={more === 'loading'}>
            {more === 'loading' ? 'Loading more' : 'Load more'}
          </button>
        )}
        {more === 'error' && <p className="t42-status" role="alert">More trends could not load. Press Load more to try again.</p>}
      </section>
    </>
  );
}


function HeldBack({held, market, date, onAuth, watch}){
  const [open, setOpen] = useState(null);
  const items = held ? list(held.items) : [];
  const count = held && typeof held.count === 'number' ? held.count : items.length;
  return (
    <section className="t42-section d42-held" data-section="held-back">
      <h2 className="d42-title">Held back</h2>
      <p className="t42-line-text">{count > 0 ? count + ' held back' : 'Nothing held back.'}</p>
      {items.length > 0 && (
        <ul className="t42-rows">
          {items.map((h) => {
            const isOpen = open === h.item_id;
            const id = 'd42-held-' + h.item_id;
            const label = globalSearchLabel(h.card);
            const evidence = marketEvidenceLabel(h.card);
            return (
              <li key={h.item_id}>
                <button type="button" className="t42-disclose" aria-expanded={isOpen ? 'true' : 'false'} aria-controls={id} onClick={() => setOpen(isOpen ? null : h.item_id)}>{h.title}</button>
                {/* The reason shows in the row, so the list is readable without opening each. */}
                <p className="t42-line-text d42-held-reason" data-held-row-reason="">{h.reason_text || 'No specific held reason was provided.'}</p>
                {isOpen && (
                  <div id={id} className="t42-held-detail">
                    <p className="t42-line-text"><a className="t42-link" href={compareHref(h.item_id, (h.card && h.card.market) || apiMarket(market))}>Compare with</a></p>
                    {label && <p className="d42-global-scope">{label}</p>}
                    {label && evidence && <p className="d42-market-evidence">{evidence}</p>}
                    {h.card && (
                      <ol className="t42-cards">
                        <TrendCard card={watch.mark(h.card, apiMarket(market))} market={h.card.market || apiMarket(market)} date={h.card.date || date} onAuth={onAuth} onWatch={watch.onWatch} linkTopic posts={false} />
                      </ol>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/* Radar reads one market at a time: the contract gives no all-market
   Radar, so All says so instead of asking for one. */
function Radar({market, kind, onAuth, onNote}){
  const [load, setLoad] = useState({state: 'loading'});
  const authRef = useRef(onAuth);
  authRef.current = onAuth;

  useEffect(() => {
    if (market === 'ALL') return undefined;
    const ctrl = new AbortController();
    setLoad({state: 'loading'});
    const slowTimer = setTimeout(() => {
      if (!ctrl.signal.aborted){
        setLoad((current) => current.state === 'loading' ? {...current, slow: true} : current);
      }
    }, SLOW_READ_MS);
    fetchRadar(market, kind, {signal: ctrl.signal})
      .then((data) => {
        clearTimeout(slowTimer);
        if (!ctrl.signal.aborted) setLoad({state: 'ready', data: data || {}});
      })
      .catch((error) => {
        clearTimeout(slowTimer);
        if (ctrl.signal.aborted) return;
        if (error && error.auth){ setLoad({state: 'auth'}); if (authRef.current) authRef.current(); return; }
        setLoad({state: 'error'});
      });
    return () => {
      clearTimeout(slowTimer);
      ctrl.abort();
    };
  }, [market, kind]);

  /* The note Radar shows, so the feed below does not say it again. */
  const shownNote = radarNote(market, load);
  useEffect(() => { if (onNote) onNote(shownNote); }, [shownNote, onNote]);

  let body;
  if (market === 'ALL') body = <p className="t42-status">Pick one market to see Radar.</p>;
  else if (load.state === 'loading') body = <p className="t42-status" role="status">{load.slow ? 'Radar is taking longer than usual.' : 'Loading Radar'}</p>;
  else if (load.state === 'auth') body = null;
  else if (load.state === 'error') body = <p className="t42-status">Radar could not load.</p>;
  else if (list(load.data.points).filter((p) => isFigure(p.reach)).length === 0) return null;
  else body = <RadarBody data={load.data} market={market} />;

  return (
    <section className="t42-section d42-radar" data-section="radar" aria-label="Radar">
      <h2 className="d42-title">Radar</h2>
      {body}
    </section>
  );
}

function radarNote(market, load){
  if (market === 'ALL' || load.state !== 'ready') return '';
  const points = list(load.data.points).filter((p) => isFigure(p.reach));
  if (points.length === 0) return '';
  const plotted = points.filter((p) => isFigure(p.growth));
  return plotted.length === 0 ? load.data.note || WARM_NOTE : load.data.note || '';
}

function RadarBody({data, market}){
  const points = list(data.points).filter((p) => isFigure(p.reach));
  const plotted = points.filter((p) => isFigure(p.growth));
  const heldCount = typeof data.held_back_count === 'number' ? data.held_back_count : 0;
  const held = <p className="t42-line-text d42-radar-held">{heldCount > 0 ? heldCount + ' held back, not plotted' : 'Nothing held back'}</p>;
  if (points.length === 0){
    return <>{data.note && <p className="t42-line-text">{data.note}</p>}<p className="t42-status">No trends to plot.</p>{held}</>;
  }
  if (plotted.length === 0){
    if (points.some((p) => p.measures && typeof p.measures === 'object')){
      return <MeasuredStrip data={data} points={points} market={market} held={held} />;
    }
    /* No measures read: the bars fall back to the gate's 3-day creator count. */
    const ranked = points.slice().sort((a, b) => b.reach.value - a.reach.value);
    const top = Math.max(1, ranked[0].reach.value);
    const unit = String(ranked[0].reach.unit || '').trim();
    const sameUnit = Boolean(unit) && ranked.every((p) => String(p.reach.unit || '').trim() === unit);
    return (
      <>
        <p className="t42-line-text d42-note">{data.note || WARM_NOTE}</p>
        {/* One scale for every bar: a fixed track, the unit said once in the
            caption, and each value right-aligned in tabular figures. */}
        {sameUnit && <p className="d42-strip-caption" data-strip-caption="">{sentenceCase(unit) + '; bars run from 0 to ' + readerFigure(top)}</p>}
        <ol className="d42-strip" aria-label="Trends ranked by reach">
          {ranked.map((p) => (
            <li key={p.item_id} data-reach={p.reach.value}>
              <a className="t42-link" href={topicHref(p.item_id, p.market || market)}>{p.label}</a>
              <span className="d42-bar" aria-hidden="true"><span style={{width: Math.max(2, (p.reach.value / top) * 100) + '%'}} /></span>
              <span className="d42-bar-value">{sameUnit ? readerFigure(p.reach.value) : figureWords(p.reach)}</span>
              <span className="d42-bar-flag">{p.flag && FLAG_WORDS[p.flag] ? FLAG_WORDS[p.flag] : ''}</span>
            </li>
          ))}
        </ol>
        {held}
      </>
    );
  }
  return (
    <>
      {data.note && <p className="t42-line-text d42-note">{data.note}</p>}
      <details className="d42-radar-chart">
        <summary>Open growth against reach chart</summary>
        <Scatter points={plotted} market={market} />
      </details>
      {held}
      {plotted.length < points.length && <p className="t42-line-text">{points.length - plotted.length} without a growth figure yet, not plotted</p>}
    </>
  );
}

/* Below this many posts in 7 days a row shows its count in words and no bar,
   the floor the gate's market check uses (G6): a bar on a handful of posts
   would read as a measured level. */
const BAR_FLOOR = 8;

const val = (f) => (isFigure(f) ? f.value : null);

function windowWords(win){
  if (!win || !win.from || !win.to) return '';
  return longDate(win.from).replace(/ \d{4}$/, '') + ' to ' + longDate(win.to);
}

/* Platforms that lost a day in the window: "YouTube 6 of 7 days". */
function shortPlatforms(win){
  if (!win || !Array.isArray(win.platforms)) return [];
  const days = win.days || 7;
  const valid = (p) => p.days_ok;
  return win.platforms.filter((p) => valid(p) < days)
    .map((p) => `${platformWord(p.platform)} ${valid(p)} of ${days} days`);
}

function listWords(words){
  if (words.length < 2) return words.join('');
  return words.slice(0, -1).join(', ') + ' and ' + words[words.length - 1];
}

/* A view count for a table cell: up to 99 999 in full, above that the
   compact form the cards print (183M, 11.9M, 880.4k), so the Views column
   stays one short figure wide and never breaks across lines. The full
   figure stays in the cell's title for the reader who wants it. */
function viewFigure(value){
  if (value < 100000) return readerFigure(value);
  /* Rounding never shows "1000k" or "1000M": it moves up a unit instead. */
  if (value >= 999500 && value < 1e6) return '1M';
  if (value >= 999.5e6 && value < 1e9) return '1B';
  return human(value);
}

/* A Radar row's facts, one per column. Each cell is the reader's words for
   one measure, or null when the measure is not recorded, so the table shows
   a muted word there rather than leaving a gap or a dash. Each figure is one
   unbroken token; its qualifier (posts with a view count, posts with a known
   place) is a second, muted line in the same cell. */
function rowCells(p){
  const m = p.measures;
  const posts = val(m.posts7);
  const measured = val(m.measured_creators7);
  const views = val(m.views7);
  const viewPosts = val(m.views_posts7);
  const located = val(m.located7);
  const share = val(m.local_share7);
  /* Two keys can name one platform (x and twitter), so names are unique. */
  const platforms = Array.isArray(m.platforms) ? [...new Set(m.platforms.map(platformWord))] : [];
  return {
    posts: posts === null ? null : readerFigure(posts),
    boards: measured === null ? null : readerFigure(measured),
    views: views === null ? null : {
      figure: viewFigure(views),
      full: readerFigure(views) + ' views' + (viewPosts !== null && posts !== null && viewPosts < posts ? ' on ' + readerFigure(viewPosts) + ' of ' + readerFigure(posts) + ' posts' : ''),
      note: viewPosts !== null && posts !== null && viewPosts < posts ? 'on ' + readerFigure(viewPosts) + ' of ' + readerFigure(posts) : null,
    },
    platforms: platforms.length ? platforms : null,
    place: located && share !== null ? {figure: Math.round(share * 100) + '%', note: 'of ' + readerFigure(located), full: Math.round(share * 100) + '% of ' + readerFigure(located) + ' posts with a known place are local'} : null,
    /* "none known" only when the count of placed posts is a known zero. */
    placeMissing: posts && located === 0 ? 'none known' : null,
    flag: p.flag && p.flag !== 'market_unconfirmed' && FLAG_WORDS[p.flag] ? FLAG_WORDS[p.flag] : null,
    /* Checked with nothing raised; a market_unconfirmed row is not this. */
    noFlag: !p.flag,
    /* market_unconfirmed: fewer than 8 posts in 7 days had a known place
       (state.sql geo_status). It was recorded, so never "not recorded". */
    fewPlaced: p.flag === 'market_unconfirmed',
  };
}

/* Up to three platforms by name; the rest as "+2", named in its title, so a
   row never wraps into a ragged paragraph of platform names. */
const PLATFORMS_SHOWN = 3;
function PlatformList({names}){
  const shown = names.slice(0, PLATFORMS_SHOWN);
  const rest = names.slice(PLATFORMS_SHOWN);
  return (
    <span className="d42-platforms">
      {shown.map((name, index) => <Fragment key={name}>{index > 0 && ', '}<span className="d42-platform">{name}</span></Fragment>)}
      {rest.length > 0 && <>{' '}<span className="d42-platform-more" title={listWords(rest)}><span aria-hidden="true">{'+' + rest.length}</span><span className="sr-only">{'and ' + listWords(rest)}</span></span></>}
    </span>
  );
}

/* Warm-up Radar with measures: one table, one row per trend, one labelled
   column per measure. Creators over 7 days (every lane, first sighting
   dated, flagged accounts left out) share one bar scale, drawn beside the
   count in the same cell so the bar reads with its trend; rows under the
   floor show their figures and no bar; a point with no measures says so
   once across its row. Columns no row has a value for are left out. */
const RADAR_ROWS = 20;

function MeasuredStrip({data, points, market, held}){
  const [all, setAll] = useState(false);
  const posts = (p) => val(p.measures && p.measures.posts7);
  /* An unknown creator count stays null: it reads "not recorded" and sorts
     last, never as a zero. */
  const creators = (p) => val(p.measures && p.measures.creators7);
  const barred = points.filter((p) => posts(p) !== null && posts(p) >= BAR_FLOOR)
    .sort((a, b) => (creators(b) ?? -1) - (creators(a) ?? -1) || posts(b) - posts(a));
  const thin = points.filter((p) => !barred.includes(p))
    .sort((a, b) => (posts(b) ?? -1) - (posts(a) ?? -1));
  const top = Math.max(1, ...barred.map((p) => creators(p) || 0));
  const span = windowWords(data.window);
  const short = shortPlatforms(data.window);
  const rows = barred.concat(thin).map((p) => ({p, cells: p.measures ? rowCells(p) : null}));
  /* Radar is read before the cards, so it shows its top rows and the rest
     on request rather than pushing the cards far down the page. */
  const capped = rows.length > RADAR_ROWS;
  const shownRows = capped && !all ? rows.slice(0, RADAR_ROWS) : rows;
  const any = (key) => rows.some((r) => r.cells && r.cells[key] !== null);
  const columns = [
    ['posts', 'Posts', 'Posts in 7 days'],
    ['boards', 'On boards', 'Creators on boards and followed accounts'],
    ...(any('views') ? [['views', 'Views', 'Views in 7 days']] : []),
    ['platforms', 'Platforms', null],
    ['place', 'Local', 'Share of posts with a known place that are local'],
    ...(any('flag') ? [['flag', 'Flag', null]] : []),
  ];
  const bars = barred.length > 0;
  const width = columns.length + (bars ? 1 : 0);
  const cell = (cells, key) => {
    const value = cells[key];
    if (value !== null){
      if (key === 'platforms') return <PlatformList names={value} />;
      if (key === 'views' || key === 'place'){
        return (
          <span className="d42-figure" title={value.full}>
            <span className="d42-figure-value">{value.figure}</span>
            {value.note && <span className="d42-figure-note">{' ' + value.note}</span>}
          </span>
        );
      }
      return value;
    }
    if (key === 'place' && cells.placeMissing) return <span className="d42-unknown">{cells.placeMissing}</span>;
    /* A row with no flag was checked and raised none: "not recorded" there
       read as if every trend went unchecked. */
    if (key === 'flag' && cells.noFlag) return <span className="d42-unknown">no flag</span>;
    if (key === 'flag' && cells.fewPlaced) return <span className="d42-unknown" title={FEW_PLACED}>few placed posts</span>;
    return <span className="d42-unknown">not recorded</span>;
  };
  return (
    <>
      <p className="t42-line-text d42-note">{data.note || WARM_NOTE}</p>
      <p className="d42-strip-caption" data-strip-caption="">
        {'Creators in 7 days' + (span ? ', ' + span : '') + (bars ? '; bars run from 0 to ' + readerFigure(top) : '')
          + '. Under ' + BAR_FLOOR + ' posts a row shows its count only.'}
      </p>
      {short.length > 0 && <p className="t42-line-text" data-window-short="">{'Short collection days: ' + listWords(short)}</p>}
      {/* With no row over the floor there is no scale to draw, so the table
          drops its bar column and the facts take the width. */}
      <div className="d42-table-wrap" id="d42-radar-table" role="region" aria-label="Radar table, scrolls sideways" tabIndex={0}>
        <table className={'d42-table' + (bars ? '' : ' d42-strip-words')} aria-label="Trends ranked by creators in 7 days">
          <thead>
            <tr>
              <th scope="col">Trend</th>
              {bars && <th scope="col" className="d42-creators-head">Creators in 7 days</th>}
              {columns.map(([key, label, title]) => <th scope="col" key={key} data-col={key} title={title || undefined} aria-label={title || undefined}>{label}</th>)}
            </tr>
          </thead>
          <tbody>
            {shownRows.map(({p, cells}) => {
              const bar = barred.includes(p);
              const count = creators(p);
              return (
                <tr key={p.item_id} data-creators={bar && count !== null ? count : undefined} data-thin={bar ? undefined : ''}>
                  <th scope="row"><a className="t42-link" href={topicHref(p.item_id, p.market || market)} title={p.label}>{p.label}</a></th>
                  {bars && cells && (
                    <td className="d42-creators" data-label="Creators in 7 days"><span className="d42-creators-in">
                      <span className="d42-bar-value">{!bar ? <span className="d42-unknown">few posts</span>
                        : count === null ? <span className="d42-unknown">not recorded</span> : readerFigure(count)}</span>
                      {bar && count !== null && <span className="sr-only"> creators in 7 days</span>}
                      {/* A zero or unknown count draws no bar at all. */}
                      <span className="d42-bar" aria-hidden="true">{bar && count > 0 && <span style={{width: Math.max(2, (count / top) * 100) + '%'}} />}</span>
                    </span></td>
                  )}
                  {cells
                    ? columns.map(([key, label]) => <td key={key} data-col={key} data-label={label}>{cell(cells, key)}</td>)
                    : <td className="d42-unmeasured" colSpan={width}>Not measured</td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {capped && (
        <button type="button" className="t42-button d42-radar-more" aria-controls="d42-radar-table" aria-expanded={all ? 'true' : 'false'} onClick={() => setAll(!all)}>
          {all ? 'Show fewer' : 'Show all ' + rows.length}
        </button>
      )}
      {held}
    </>
  );
}

function layoutScatterLabels(points, x, y, width, height, plot){
  const maxCharacters = 26;
  const characterWidth = 13;
  const lineHeight = 18;
  const gap = 5;
  const markerClearance = 6 + gap + 1;
  const markerBoxes = points.map((point) => {
    const px = x(point.reach.value);
    const py = y(point.growth.value);
    return {left: px - 6, right: px + 6, top: py - 6, bottom: py + 6};
  });
  const boxes = [];
  const labels = [];
  let overflowTop = height + 32;
  let usedOverflow = false;
  const overlaps = (a, b) => a.left < b.right + gap && a.right + gap > b.left
    && a.top < b.bottom + gap && a.bottom + gap > b.top;

  points.forEach((point) => {
    const characters = Array.from(String(point.label == null ? '' : point.label));
    const lines = [];
    while (characters.length > 0){
      let count = Math.min(maxCharacters, characters.length);
      if (count < characters.length){
        for (let i = count; i > 1; i--){
          if (/\s/.test(characters[i - 1])) { count = i; break; }
        }
      }
      lines.push(characters.splice(0, count).join(''));
    }
    if (lines.length === 0) lines.push('');
    const textWidth = Math.max(1, ...lines.map((line) => Array.from(line).length)) * characterWidth;
    const boxWidth = textWidth + 4;
    const boxHeight = lines.length * lineHeight;
    const px = x(point.reach.value);
    const py = y(point.growth.value);
    const candidates = [
      {left: px + markerClearance, top: py - boxHeight / 2, anchor: 'start'},
      {left: px - boxWidth - markerClearance, top: py - boxHeight / 2, anchor: 'end'},
      {left: px - boxWidth / 2, top: py + markerClearance, anchor: 'middle'},
      {left: px - boxWidth / 2, top: py - boxHeight - markerClearance, anchor: 'middle'},
    ];
    let box = candidates.find((candidate) => {
      const rect = {...candidate, right: candidate.left + boxWidth, bottom: candidate.top + boxHeight};
      const withinPlot = rect.left >= plot.left && rect.right <= width - plot.right
        && rect.top >= plot.top && rect.bottom <= height - plot.bottom;
      return withinPlot
        && !boxes.some((placed) => overlaps(rect, placed))
        && !markerBoxes.some((marker) => overlaps(rect, marker));
    });
    if (!box){
      box = {left: plot.left, top: overflowTop, anchor: 'start'};
      overflowTop += boxHeight + gap;
      usedOverflow = true;
    }
    const rect = {left: box.left, top: box.top, right: box.left + boxWidth, bottom: box.top + boxHeight};
    boxes.push(rect);
    const textX = box.anchor === 'end' ? rect.right : box.anchor === 'middle' ? (rect.left + rect.right) / 2 : rect.left;
    labels.push({
      lines,
      top: rect.top,
      textX,
      anchor: box.anchor,
      targetX: textX,
      targetY: Math.max(rect.top, Math.min(py, rect.bottom)),
    });
  });
  return {labels, viewBoxHeight: usedOverflow ? overflowTop + 16 : height};
}

function Scatter({points, market}){
  const W = 640, H = 360, left = 56, right = 24, top = 16, bottom = 48;
  const maxX = Math.max(1, ...points.map((p) => p.reach.value));
  const maxY = Math.max(1, ...points.map((p) => p.growth.value)) * 1.1;
  const x = (v) => left + (Math.max(0, v) / maxX) * (W - left - right);
  const y = (v) => H - bottom - (Math.max(0, v) / maxY) * (H - top - bottom);
  const fix = (n) => Number(n.toFixed(1));
  const reachUnit = points[0].reach.unit || 'reach';
  const growthUnit = points[0].growth.unit || 'growth';
  const layout = layoutScatterLabels(points, (value) => fix(x(value)), (value) => fix(y(value)), W, H, {left, right, top, bottom});
  return (
    <svg className="d42-scatter" viewBox={'0 0 ' + W + ' ' + layout.viewBoxHeight} role="group" aria-label={'Growth against reach for ' + points.length + ' trends'}>
      <g aria-hidden="true">
        {points.map((point, index) => <path key={point.item_id} className="d42-leader" d={'M ' + fix(x(point.reach.value)) + ' ' + fix(y(point.growth.value)) + ' L ' + layout.labels[index].targetX + ' ' + layout.labels[index].targetY} />)}
      </g>
      <line className="d42-axis" x1={left} y1={H - bottom} x2={W - right} y2={H - bottom} />
      <line className="d42-axis" x1={left} y1={top} x2={left} y2={H - bottom} />
      <line className="d42-usual" x1={left} y1={fix(y(1))} x2={W - right} y2={fix(y(1))} />
      <text className="d42-tick" x={W - right} y={fix(y(1)) - 4} textAnchor="end">Usual level</text>
      <text className="d42-tick" x={left} y={H - bottom + 16} textAnchor="middle">0</text>
      <text className="d42-tick" x={W - right} y={H - bottom + 16} textAnchor="end">{figureWords({value: maxX})}</text>
      <text className="d42-axis-title" x={(left + W - right) / 2} y={H - 8} textAnchor="middle">{'Reach, ' + reachUnit}</text>
      <text className="d42-axis-title" x={14} y={(top + H - bottom) / 2} textAnchor="middle" transform={'rotate(-90 14 ' + (top + H - bottom) / 2 + ')'}>{'Growth, ' + growthUnit}</text>
      {points.map((p, index) => {
        const px = fix(x(p.reach.value));
        const py = fix(y(p.growth.value));
        const flag = p.flag && FLAG_WORDS[p.flag];
        const label = p.label + ': reach ' + figureWords(p.reach) + ', growth ' + figureWords(p.growth) + (flag ? ', ' + flag : '');
        const labelLayout = layout.labels[index];
        return (
          <a key={p.item_id} className="d42-point" href={topicHref(p.item_id, p.market || market)} aria-label={label}>
            {flag
              ? <rect className="d42-mark d42-mark-flagged" x={px - 5} y={py - 5} width="10" height="10" data-x={px} data-y={py} />
              : <circle className="d42-mark" cx={px} cy={py} r="5" data-x={px} data-y={py} />}
            <text className="d42-point-label" textAnchor={labelLayout.anchor}>
              {labelLayout.lines.map((line, lineIndex) => (
                <tspan key={lineIndex} x={labelLayout.textX} y={labelLayout.top + 14 + lineIndex * 18}>{line}</tspan>
              ))}
            </text>
          </a>
        );
      })}
    </svg>
  );
}
