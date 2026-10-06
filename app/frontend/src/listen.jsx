/* PULSE · Listen. The live mention feed: real ingested posts for one market,
   filterable by sentiment and category. Quiet register, 23 Sept 2026: each
   mention is a row in a plain list, separated by space and a hairline, and
   its sentiment is a word in a state colour (positive green, negative amber,
   anything else muted), never brand red, because red on this screen marks
   only the current filter. Recency outranks raw engagement; every quote
   carries its age in words. */
import {useState, useEffect, useRef, useId} from 'react';
import {FAILURE_ORIGIN_LABEL, failureOrigin, useApi} from './api.js';
import {StateView} from 'ogilvy-intelligence-design-system';
import {Icon, EmptyState} from './parts.jsx';
import {listenEmptyFacts, routeStateView} from './instrumentRouteModels.js';
import {REGION_NAME, readerWord, webHref} from './model.js';
import {PageHero} from './ui/index.js';
import './styles/today42.css';
import './styles/lexlisten.css';
import './styles/workspaces.css';

const MARKETS = ['ZA', 'NG', 'KE', 'ALL'];
/* Demo polish, 2 October 2026: the market tabs are written as names, as the
   header writes them, not as codes. */
const marketTab = (mk) => (mk === 'ALL' ? 'All' : REGION_NAME[mk] || mk);
/* The first page of mentions is 30 rows, and the read lands through the
   package loading frame inside a wrapper that reserves the page's geometry,
   so main does not grow by the page when the rows land. The geometry is
   read from the last settled render of the same market in this session, the
   row count at its mean row height with the gaps between rows, because a
   fixture constant reserved 30 rows at 133px while the desk landed rows of
   140 to 258px. With nothing remembered the wrapper reserves only the
   package frame's default band rather than a guess. */
const MENTIONS_PAGE = 30;
const LISTEN_WAIT = Object.freeze({
  state: 'loading',
  title: 'Tuning into the market',
  task: 'Reading the most recent mentions first',
  body: 'The feed opens once the first page of mentions has been read. Recency outranks raw engagement here, and the sentiment shares are worked out from the loaded posts that carry a sentiment.',
});
const RESERVE_KEY = 'listen-reserve:';

function reserveStorage(){
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage; } catch (e){ return null; }
}

export function listenReserve(market, storage = reserveStorage()){
  try {
    const raw = storage ? storage.getItem(RESERVE_KEY + market) : null;
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    const rows = Number(parsed && parsed.rows);
    const rowHeight = Number(parsed && parsed.rowHeight);
    return Number.isInteger(rows) && rows > 0 && rowHeight > 0 ? {rows, rowHeight} : null;
  } catch (e){
    return null;
  }
}

export function rememberListenReserve(market, rows, blockHeight, storage = reserveStorage()){
  if (!storage || !Number.isInteger(rows) || rows < 1 || !(blockHeight > 0)) return;
  try { storage.setItem(RESERVE_KEY + market, JSON.stringify({rows, rowHeight: Math.round(blockHeight / rows)})); } catch (e){}
}
const SENTIMENTS = [['', 'All'], ['positive', 'Positive'], ['negative', 'Negative']];

/* The age of a quote in the words a reader would say: "12 days ago". The
   clock is a parameter so the words can be checked against a fixed time. */
export function mentionAge(d, now = Date.now()){
  if (!d) return '';
  const then = Date.parse(d);
  if (!then) return '';
  /* Whole units elapsed, as a person counts them: 30 seconds is just now
     and 23 hours is still hours. */
  const mins = Math.floor((now - then) / 6e4);
  if (mins < 1) return 'just now';
  const unit = (n, word) => n + ' ' + word + (n === 1 ? '' : 's') + ' ago';
  if (mins < 60) return unit(mins, 'minute');
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return unit(hrs, 'hour');
  return unit(Math.floor(hrs / 24), 'day');
}

/* Sentiment is a state written as a word, so it takes a state colour and
   never the brand red: red on Listen marks only the current filter, and a
   red "Negative" also read as a fall. Positive is the ready green, negative
   the amber, and anything else stays muted. */
function toneOf(s){
  if (s === 'positive') return 'var(--green)';
  if (s === 'negative') return 'var(--amber)';
  return 'var(--muted)';
}

/* Demo polish, 2 October 2026: the filter box takes a search glass, not a
   microphone, because it filters text and listens to nothing. */
function SearchIcon(p){
  return <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="var(--muted)" strokeWidth="2" strokeLinecap="round" aria-hidden="true" {...p}><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>;
}

/* Each share is counted from the label the producer sent and rounded on its
   own, so four neutral rows of nine read 44% and the shares can sum to 99 or
   101 by rounding alone. A row that carries no sentiment is not called
   neutral: it is counted apart and said apart. Demo polish, 2 October 2026:
   the shares are taken over the rated rows only, so one positive and two
   negative of four rows, one unrated, read 33% and 67% rather than 25% and
   50%, which summed to 75% on screen. */
export function sentimentSplit(rows){
  const total = rows.length;
  const count = (label) => rows.filter((r) => r.sentiment === label).length;
  const pos = count('positive'), neu = count('neutral'), neg = count('negative');
  const rated = pos + neu + neg;
  const share = (n) => (rated ? Math.round((n / rated) * 100) : 0);
  return {total, positive: share(pos), neutral: share(neu), negative: share(neg), unrated: total - rated};
}

/* Quiet register, 23 Sept 2026: the split is one sentence with its figures,
   not an unlabelled three colour bar whose negative share was painted red.
   Demo polish, 2 October 2026: the sentence says how many posts carry a
   sentiment, so a reader can see what the shares are of. */
export function SentimentSplit({rows}){
  const split = sentimentSplit(rows);
  if (!split.total) return null;
  const rated = split.total - split.unrated;
  const posts = (n) => (n === 1 ? 'post' : 'posts');
  if (!rated){
    return (
      <p className="listen-split">
        {split.total === 1
          ? <>The <span className="tnum">1</span> post loaded has no sentiment.</>
          : <>None of the <span className="tnum">{split.total}</span> posts loaded has a sentiment.</>}
      </p>
    );
  }
  const shares = <><span className="tnum">{split.positive}%</span> positive, <span className="tnum">{split.neutral}%</span> neutral and <span className="tnum">{split.negative}%</span> negative.</>;
  if (!split.unrated){
    return (
      <p className="listen-split">
        <span className="tnum">{split.total}</span> {posts(split.total)} loaded{split.total === 1 ? '' : ', all with a sentiment'}: {shares}
      </p>
    );
  }
  return (
    <p className="listen-split">
      <span className="tnum">{split.total}</span> {posts(split.total)} loaded. Of the <span className="tnum">{rated}</span> with a sentiment, <span className="tnum">{split.positive}%</span> are positive, <span className="tnum">{split.neutral}%</span> neutral and <span className="tnum">{split.negative}%</span> negative.
      {' '}<span className="tnum">{split.unrated}</span> {split.unrated === 1 ? 'has' : 'have'} none.
    </p>
  );
}

/* Quiet register, 23 Sept 2026: a label on Listen is the sans at 14px in
   muted, sentence case written at the source, with no tracking. */
/* Shell consistency, 2 October 2026: each filter is the Today tab row, an ink
   underline under the chosen word, with its name as a muted label above it.
   The buttons keep aria-pressed: they are toggles that filter one list, and
   the row is a group named by its label. */

function CategoryFilters({categories, active, onPick}){
  const labelId = useId();
  if (!categories.length) return null;
  const opts = [['', 'All categories'], ...categories.map((c) => [c, readerWord(c)])];
  return (
    <div className="listen-filter-row listen-filter-row--refine">
      <span className="listen-filter-label" id={labelId}>Category</span>
      <div className="t42-tabs" role="group" aria-labelledby={labelId}>
        {opts.map(([id, label]) => (
          <button
            key={id || 'all'}
            className="listen-filter-chip t42-tab"
            onClick={() => onPick(id)}
            aria-pressed={active === id}
          >{label}</button>
        ))}
      </div>
    </div>
  );
}

/* Quiet register, 23 Sept 2026: a mention is a row in a list, not a framed
   card with a sentiment stroke. The quote leads; the meta line under it is
   the sans at 14px with the sentiment word in its state colour, and the
   source link is a quiet ink link rather than a red one on every row. */
export function MentionRow({r}){
  const age = mentionAge(r.date);
  const href = webHref(r.url);
  return (
    <li className="listen-mention-row">
      {(r.title || r.content) ? (
        <p className="listen-mention-quote">{r.title || r.content}</p>
      ) : (
        <p className="listen-mention-empty">No preview text for this {(r.category || 'social').toLowerCase()} post. Read it at the source.</p>
      )}
      {r.title && r.content && (
        <p className="listen-mention-body">{r.content}</p>
      )}
      <p className="listen-mention-meta">
        {r.sentiment && (
          <span className="listen-mention-sentiment" style={{color: toneOf(r.sentiment)}}>{readerWord(r.sentiment)}</span>
        )}
        {r.market && <span>{REGION_NAME[String(r.market).toUpperCase()] || String(r.market).toUpperCase()}</span>}
        {r.category && <span>{readerWord(r.category)}</span>}
        <span>{r.host || 'web'}{age ? ' · ' + age : ''}</span>
      </p>
      {href ? (
        <a href={href} target="_blank" rel="noreferrer" className="listen-source">
          Read the source<Icon.arrRight aria-hidden="true" style={{width: '14px', height: '14px'}} />
        </a>
      ) : (
        <span className="listen-source listen-source--none">Source not available</span>
      )}
    </li>
  );
}

export function ListenPage({region, setRegion, session, onAuth, initialQuery}){
  const [sentiment, setSentiment] = useState('');
  const [category, setCategory] = useState('');
  const [q, setQ] = useState(initialQuery || '');
  useEffect(() => { setQ(initialQuery || ''); }, [initialQuery]);
  return <ListenFeed key={region} region={region} setRegion={setRegion} session={session} onAuth={onAuth} sentiment={sentiment} setSentiment={setSentiment} category={category} setCategory={setCategory} q={q} setQ={setQ} />;
}

function ListenFeed({region, setRegion, session, onAuth, sentiment, setSentiment, category, setCategory, q, setQ}){
  const marketLabelId = useId();
  const sentimentLabelId = useId();
  // The global masthead region is the single source of truth so the market nav
  // and the in-page chips can never diverge. ALL fans out across markets.
  const regUp = String(region || 'ZA').toUpperCase();
  const market = MARKETS.includes(regUp) ? regUp : 'ZA';
  const pickMarket = (mk) => setRegion && setRegion(mk.toLowerCase());
  const [rows, setRows] = useState([]);
  const listRef = useRef(null);

  const basePath = '/api/intel/mentions?market=' + market.toLowerCase()
    + (sentiment ? '&sentiment=' + sentiment : '')
    + (category ? '&category=' + encodeURIComponent(category) : '');
  const [first, retryFirst] = useApi(basePath, session, onAuth, undefined, 30000);
  const unavailable = first.state === 'error' && first.code === 'http_404' && first.message === 'No such API route.';
  const timedOut = first.state === 'error' && first.code === 'request_timeout';
  const readFailed = first.state === 'error';

  /* The feed shows the one page the producer serves. bq.py listen_feed returns
     has_more false unconditionally and main.py never forwards a cursor, so
     there is no second page to load and no load-more control is drawn.
     Cursor pagination is tracked as a separate producer item, not stubbed
     here with a control that could never fetch more. */
  useEffect(() => {
    if (first.state === 'ready' && first.data) setRows(first.data.results || []);
    if (first.state === 'loading') setRows([]);
  }, [first.state, first.data]);

  const needle = q.trim().toLowerCase();
  const shown = needle
    ? rows.filter((r) => (r.title || '').toLowerCase().includes(needle) || (r.content || '').toLowerCase().includes(needle))
    : rows;
  const categories = [...new Set(rows.map((r) => r.category).filter(Boolean))].sort();
  const reserve = first.state === 'loading' ? listenReserve(market) : null;

  /* Once the first page has settled, its first-page rows are measured as
     one block, gaps included, and remembered for the next load of this
     market. A filtered list, by search, sentiment or category, is not the
     page, so it is not remembered. */
  useEffect(() => {
    if (first.state !== 'ready' || needle || sentiment || category || !listRef.current) return;
    const page = [...listRef.current.querySelectorAll('.listen-mention-row')].slice(0, MENTIONS_PAGE);
    if (!page.length) return;
    const top = page[0].getBoundingClientRect().top;
    const bottom = page[page.length - 1].getBoundingClientRect().bottom;
    rememberListenReserve(market, page.length, bottom - top);
  }, [first.state, rows, needle, sentiment, category, market]);

  return (
    <div className="page listen">
      <div className="listen-page">
        {/* Quiet register, 23 Sept 2026: a page title at the page title size
            with a one sentence lead, and no eyebrow above it. Shell
            consistency, 2 October 2026: the title is the menu's noun and the
            old headline leads the sentence. */}
        <PageHero
          title="Listen"
          sub="Hear it in their words: real posts from one market, newest first, each with its age and a link to where it was said."
        />

        <div className="listen-filter-row">
          <span className="listen-filter-label" id={marketLabelId}>Market</span>
          <div className="t42-tabs" role="group" aria-labelledby={marketLabelId}>
            {MARKETS.map((mk) => (
              <button
                key={mk}
                className="listen-filter-chip t42-tab"
                onClick={() => pickMarket(mk)}
                aria-pressed={market === mk}
              >{marketTab(mk)}</button>
            ))}
          </div>
        </div>

        {/* Design audit, 2 October 2026: a control that can do nothing is
            not shown (NN/g, empty states; Nielsen heuristic 8, aesthetic and
            minimalist design). When the feed is unavailable or the read
            failed there are no posts to filter, so the filter box and the
            sentiment tabs are left out. The market tabs above stay, because
            the market is the page's context and a way out. */}
        {!readFailed && (
          <>
            <div className="listen-search" style={{display: 'flex', alignItems: 'center', gap: '12px', minHeight: '48px', padding: '0 0 0 16px', maxWidth: '560px', margin: '24px 0 0'}}>
              <SearchIcon />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter these posts…" spellCheck="false" aria-label="Filter mentions" />
              {first.state === 'ready' && q && <span className="listen-search-count tnum">{shown.length} of {rows.length}</span>}
              {q && <button onClick={() => setQ('')} aria-label="Clear filter" className="listen-clear">&times;</button>}
            </div>

            <SentimentSplit rows={rows} />
          </>
        )}
        {/* A chosen sentiment that then fails to read keeps its row, so the
            reader can go back to All without reloading the page. */}
        {(!readFailed || Boolean(sentiment)) && (
          <>
            <div className="listen-filter-row listen-filter-row--refine">
              <span className="listen-filter-label" id={sentimentLabelId}>Sentiment</span>
              <div className="t42-tabs" role="group" aria-labelledby={sentimentLabelId}>
                {SENTIMENTS.map(([id, label]) => (
                  <button
                    key={id || 'all'}
                    className="listen-filter-chip t42-tab"
                    onClick={() => setSentiment(id)}
                    aria-pressed={sentiment === id}
                  >{label}</button>
                ))}
              </div>
            </div>
          </>
        )}

        <CategoryFilters categories={categories} active={category} onPick={setCategory} />

        {first.state === 'loading' && (
          <div
            className="listen-wait"
            data-reserved-mentions={reserve ? reserve.rows : undefined}
            style={reserve ? {'--reserved-mentions': reserve.rows, '--reserved-mention-height': reserve.rowHeight + 'px'} : undefined}
          >
            <StateView {...routeStateView({...LISTEN_WAIT, title: 'Tuning into ' + (REGION_NAME[market] || market)})} />
          </div>
        )}
        {first.state === 'error' && (
          <div className="intel-down" role="alert">
            <p className="id-head">{unavailable ? 'Listen is not available yet.' : timedOut ? 'The mention feed timed out.' : 'The mention feed could not be read for ' + (REGION_NAME[market] || market) + '.'}</p>
            <p className="id-msg">{unavailable ? 'The mention feed is not available in this version. No mention data was read.' : timedOut ? 'The feed did not answer within 30 seconds. No mention data was read.' : <>Nothing was read, so no mention count and no sentiment split are shown. This does not establish that the market is quiet. {first.message || ''}</>}</p>
            {/* Shell consistency, 2 October 2026: the failed read is the one
                empty state pattern, a bold title, one sentence and one
                bordered action, with the origin label and code waiting under a
                closed Details line after it, as on Ask, Fieldwork and
                Historical. A read that may answer again offers Try again; a
                feature this version does not have offers Today's posts. */}
            {unavailable
              ? <a className="legacy-action" href="#/pulse">Open Today</a>
              : <button type="button" className="legacy-action" onClick={retryFirst}>Try again</button>}
            <details className="workspace-reason"><summary>Details</summary><p className="id-msg">{unavailable ? <>The app returned <code>{first.code}</code>: {first.message}</> : timedOut ? <>This page stopped waiting after 30 seconds: <code>{first.code}</code></> : <>{FAILURE_ORIGIN_LABEL[failureOrigin(first.code)]} <code>{first.code || 'unstated'}</code></>}</p></details>
          </div>
        )}
        {first.state === 'ready' && !rows.length && (
          <EmptyState
            loader="drum"
            isLoading={false}
            title={'Nothing heard in ' + (REGION_NAME[market] || market) + ' yet'}
            body="No mentions for this market in the current window. The drum keeps listening; try another market or clear the sentiment and category filters."
            status={listenEmptyFacts({market, sentiment, category, loaded: 0})}
            actions={[
              {label: 'Try ' + (REGION_NAME[MARKETS.find((m) => m !== market && m !== 'ALL')] || 'another market') + ' instead →', primary: true, onClick: () => pickMarket(MARKETS.find((m) => m !== market && m !== 'ALL'))},
              (sentiment || category) ? {label: 'Clear filters', onClick: () => { setSentiment(''); setCategory(''); }} : null,
            ]}
          />
        )}
        {first.state === 'ready' && rows.length > 0 && !shown.length && (
          <p className="listen-no-match">No loaded mentions match "{q.trim()}".</p>
        )}

        {shown.length > 0 && (
          <ul ref={listRef} className="listen-mentions">
            {shown.map((r, i) => <MentionRow key={r.url || r.title || i} r={r} />)}
          </ul>
        )}
      </div>
    </div>
  );
}
