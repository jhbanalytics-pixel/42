/* PULSE · the lexicon.
   Page port, 3 October 2026: the words and hashtags 42 is recording in one
   market, read from GET /api/lexicon?market=XX (core/api/contract.md section
   20). Each term carries its posts over the last 28 days, the last 7 days
   against the 7 before (no percentage when the earlier week has no posts or
   collection is still warming up), and opens Seed path for its real posts.
   The old page read the desk's slang index and carrier posts, which f42-api
   never serves; that wall of posts is gone. A definition comes only from the
   written glossary, and the panel says so plainly when there is none. */
import {useEffect, useState} from 'react';
import {StateView} from 'ogilvy-intelligence-design-system';
import {useApi, readerFigure} from './api.js';
import {buildScopedReadHash, go} from './router.js';
import {GLOSSARY, REGION_NAME} from './model.js';
import {PageHero} from './ui/index.js';
import {longDate, topicHref} from './ui/TrendCard.jsx';
import {routeStateView} from './instrumentRouteModels.js';
import {Dumbbell} from './ui/Charts42.jsx';
import './styles/lexlisten.css';
import './styles/loop.css';

const MARKETS = ['ZA', 'NG', 'KE'];
const DAYS = 28;

export function wowParts(wow){
  if (typeof wow !== 'number' || !Number.isFinite(wow)) return null;
  return {
    text: (wow >= 0 ? '▲ ' : '▼ ') + Math.abs(wow) + '%',
    color: wow >= 0 ? 'var(--up)' : 'var(--accent-text)',
  };
}

/* The term as Seed path matches it: trimmed, a leading # dropped, inner
   spaces collapsed, lower case. */
function bare(value){
  return String(value ?? '').trim().replace(/\s+/g, ' ').replace(/^#+/, '').trim().toLowerCase();
}

const record = (v) => !!v && typeof v === 'object' && !Array.isArray(v);
const isFigure = (v) => record(v) && Number.isSafeInteger(v.value) && v.value >= 0 && typeof v.query_id === 'string';

/* A term is shown only when its numbers are Figures from the read; a row of
   any other shape is left out, never patched. */
function validTerm(t){
  return record(t) && typeof t.item_id === 'string' && !!t.item_id && typeof t.label === 'string' && !!t.label.trim()
    && t.kind !== 'creator' && isFigure(t.posts) && isFigure(t.week_posts) && isFigure(t.prior_week_posts)
    && record(t.change);
}

function plural(n, one, many){
  return readerFigure(n) + ' ' + (Number(n) === 1 ? one : many);
}

function Fig({figure, children}){
  return <span className="lex-fig tnum" data-query-id={figure.query_id} title={'Query ' + figure.query_id}>{children}</span>;
}

function matches(entry, term){
  const want = bare(term);
  return !!want && (bare(entry.seed_term) === want || bare(entry.label) === want);
}

function ChangeMark({entry}){
  const wp = wowParts(entry.change.percent);
  if (wp) return <span className="lex-change tnum" style={{color: wp.color}} title={entry.change.text}>{wp.text}</span>;
  /* New only when every post in the window is from this week; a term with
     older posts and a quiet week before is not new. */
  if (entry.change.reason === 'no_earlier_posts' && entry.week_posts.value > 0 && entry.week_posts.value === entry.posts.value) return <span className="lex-change lex-change--new">New this week</span>;
  return null;
}

/* Charts, 3 October 2026: the last 7 days against the 7 before for the first
   12 terms the page lists, in its order. The largest rise is the focus row. A
   term missing either figure stays as a row that says so. */
const CHART_TERMS = 12;
const figValue = (f) => (isFigure(f) ? f.value : null);

function WeekChart({terms}){
  /* Before two full weeks of collection the earlier week is not a real
     comparison (the API says so on each term), so no chart is drawn. */
  if (terms.some((t) => t.change && t.change.reason === 'warming_up')) return null;
  const rows = terms.slice(0, CHART_TERMS).map((t) => ({key: t.item_id, label: t.label, from: figValue(t.prior_week_posts), to: figValue(t.week_posts), queryId: t.week_posts && t.week_posts.query_id}));
  let best = null;
  rows.forEach((r) => {
    if (r.from !== null && r.to !== null && r.to > r.from && (!best || r.to - r.from > best.to - best.from)) best = r;
  });
  if (best) best.focus = true;
  return (
    <Dumbbell
      data="data-lex-week-chart"
      title="This week against last week"
      caption={'Posts in the last 7 days against the 7 days before, ' + (rows.length < terms.length ? 'for the first ' + rows.length + ' terms listed' : 'for every term listed') + ', on one scale.'}
      rows={rows}
      fromLabel="Last week"
      toLabel="This week"
    />
  );
}

export function DecodePanel({region, term, entry, window: win}){
  /* Demo polish, 2 October 2026: no empty box repeats "Tap a term" before a
     term is picked; the page lead already says it. */
  if (!term) return null;
  const market = MARKETS.includes(String(region || '').toUpperCase()) ? String(region).toUpperCase()
    : String(entry && entry.market || '').toUpperCase();
  const where = REGION_NAME[market] || 'this market';
  const days = win && Number.isSafeInteger(win.days) ? win.days : DAYS;
  const known = entry && validTerm(entry) ? entry : null;
  const seedTerm = known ? known.seed_term : bare(term);
  const def = GLOSSARY[bare(known ? known.label : term)] || null;

  return (
    <aside className="lex-decode" tabIndex={-1} aria-label={'About ' + (known ? known.label : term)}>
      <div className="lex-decode-head">
        <div className="lex-meta">{where} · last {days} days</div>
        <h2 className="lex-decode-title">{known ? known.label : term}</h2>
        {known && <div className="lex-meta">{known.kind_word || (known.kind === 'hashtag' ? 'Hashtag' : 'Word or phrase')}</div>}
      </div>
      <div className="lex-decode-body">
        {known ? (
          <>
            <p className="lex-fact"><Fig figure={known.posts}>{plural(known.posts.value, 'post', 'posts')}</Fig> in the last {days} days.</p>
            <p className="lex-fact"><Fig figure={known.week_posts}>{readerFigure(known.week_posts.value)}</Fig> in the last 7 days, <Fig figure={known.prior_week_posts}>{readerFigure(known.prior_week_posts.value)}</Fig> in the 7 days before. {known.change.text}{/[.!?]$/.test(known.change.text || '') ? '' : '.'}</p>
            {known.first_seen && <p className="lex-fact">First recorded by 42 on {longDate(known.first_seen)}, in any market.</p>}
          </>
        ) : (
          <p className="lex-fact">&ldquo;{term}&rdquo; is not among the words and hashtags listed for {where} in the last {days} days. Seed path can still look for it in the posts.</p>
        )}
        <div className="lex-meta lex-meta--section">What it means</div>
        {def
          ? <p className="lex-fact">{def}. <span className="lex-quiet">From 42's written glossary.</span></p>
          : <p className="lex-fact lex-quiet">42 has no written definition for this term yet.</p>}
        <div className="lex-links">
          {seedTerm && MARKETS.includes(market) && <a className="lex-link" href={buildScopedReadHash('seedpath', seedTerm, market.toLowerCase())}>See the posts</a>}
          {known && MARKETS.includes(market) && <a className="lex-link" href={topicHref(known.item_id, market)}>Open its topic page</a>}
        </div>
      </div>
    </aside>
  );
}

/* Before a term is picked, the place the term panel will take says what it
   will hold, so the page reads whole. It is not the panel itself: no empty
   box repeats "Tap a term". */
function LexGuide({days}){
  return (
    <aside className="lex-guide" aria-labelledby="lex-guide-title">
      <h2 className="lex-guide-title" id="lex-guide-title">Pick a term to see</h2>
      <dl className="lex-guide-parts">
        <div className="lex-guide-part"><dt>How often</dt><dd>Its posts over the last {days} days, and the last 7 days against the 7 before.</dd></div>
        <div className="lex-guide-part"><dt>Since when</dt><dd>The day 42 first recorded it, in any market.</dd></div>
        <div className="lex-guide-part"><dt>What it means</dt><dd>The meaning from 42's written glossary, when there is one.</dd></div>
        <div className="lex-guide-part"><dt>Where to go next</dt><dd>The posts that use it in Seed path, and its topic page.</dd></div>
      </dl>
    </aside>
  );
}

function MarketPicker({market, term}){
  return (
    <div className="lex-markets loop-tabs" role="group" aria-label="Market">
      {MARKETS.map((mk) => (
        <button
          key={mk}
          type="button"
          className="legacy-chip loop-tab"
          aria-pressed={market === mk}
          onClick={() => { window.location.hash = buildScopedReadHash('lexicon', term || '', mk.toLowerCase()); }}
        >{REGION_NAME[mk]}</button>
      ))}
    </div>
  );
}

export function LexiconPage({region, term, session, onAuth}){
  const reg = String(region || 'ALL').toLowerCase();
  const market = reg.toUpperCase();
  const readable = MARKETS.includes(market);
  const [res, retry] = useApi('/api/lexicon?market=' + market, session, onAuth, readable);
  /* Page data audit, 3 October 2026: a service without this route answers
     with its "No such API route." 404, which Try again cannot fix. The page
     says the lexicon is not in this version and offers Today. */
  const unavailable = res.state === 'error' && res.code === 'http_404' && res.message === 'No such API route.';
  const data = res.state === 'ready' && record(res.data) ? res.data : null;
  const notReady = (res.state === 'error' && (res.code === 'not_ready' || res.code === 'http_409')) || (data && data.status === 'not_ready');
  const terms = data && !notReady && Array.isArray(data.terms) ? data.terms.filter(validTerm) : [];
  const win = data && record(data.window) ? data.window : null;
  const days = win && Number.isSafeInteger(win.days) ? win.days : DAYS;
  const [sel, setSel] = useState(term || null);
  useEffect(() => { setSel(term || null); }, [term]);
  const selTerm = sel || term || null;
  const entry = selTerm ? terms.find((e) => matches(e, selTerm)) || null : null;
  const where = REGION_NAME[market] || market;

  const pick = (e) => {
    setSel(e.seed_term || e.label);
    const destination = buildScopedReadHash('lexicon', e.seed_term || bare(e.label), reg);
    if (window.location.hash !== destination) {
      window.location.hash = destination;
    }
    requestAnimationFrame(() => {
      const panel = document.querySelector('.lexicon-page .lex-decode');
      if (!panel) return;
      panel.focus({preventScroll: true});
      if (window.matchMedia('(max-width: 1200px)').matches){
        panel.scrollIntoView({block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
      }
    });
  };

  return (
    <div className="lexicon-page" data-lexicon-state={readable ? res.state : 'choose'}>
      <div className="reveal">
        {/* Shell consistency, 2 October 2026: the menu's noun is the title and
            the old headline leads the sentence under it. */}
        <PageHero
          title="Lexicon"
          sub="Which words and hashtags are people using? Counted in one market over the last 28 days. Pick one to see the posts that use it."
        />
      </div>

      <MarketPicker market={readable ? market : null} term={selTerm} />

      {!readable && (
        <div className="lex-state" data-state-frame="choose">
          <StateView {...routeStateView({state: 'empty', title: 'Choose one market', body: 'The lexicon reads one country at a time. Choose South Africa, Nigeria or Kenya above.'})} />
        </div>
      )}
      {readable && res.state === 'loading' && (
        <div className="lex-state" data-state-frame="loading">
          <StateView {...routeStateView({state: 'loading', title: 'Reading the lexicon', task: 'Reading term counts', body: 'Counting the words and hashtags for this market.', reservedRows: 3})} />
        </div>
      )}
      {readable && notReady && (
        <div className="lex-state" data-state-frame="not-ready">
          <StateView {...routeStateView({state: 'empty', title: 'The lexicon is not ready yet', body: (data && data.message) || res.message || '42 has not finished its first daily count of posts.', nextAction: 'Open Today', actions: [{id: 'today', label: 'Open Today', onClick: () => go('/pulse')}]})} />
        </div>
      )}
      {readable && data && !notReady && !terms.length && (
        <div className="lex-state" data-state-frame="empty">
          <StateView {...routeStateView({state: 'empty', title: 'No words or hashtags recorded here yet', body: data.message || ('42 has not recorded posts for any word or hashtag in ' + where + ' in the last ' + days + ' days.'), nextAction: 'Open Discover', actions: [{id: 'discover', label: 'Open Discover', onClick: () => go('/explore')}]})} />
        </div>
      )}
      {unavailable && <div className="lex-state" data-state-frame="unavailable"><StateView {...routeStateView({state: 'error', title: 'The lexicon is not available yet', body: 'The lexicon is not served in this version, so no term counts were read.', code: res.code, nextAction: 'Open Today', actions: [{id: 'today', label: 'Open Today', onClick: () => go('/pulse')}]})} /></div>}
      {res.state === 'error' && !unavailable && !notReady && <div className="lex-state" data-state-frame="error"><StateView {...routeStateView({state: 'error', title: 'The lexicon could not be read', body: 'Try again in a moment.', code: res.code, actions: [{id: 'retry', kind: 'button', label: 'Try again', onClick: retry}]})} /></div>}

      {terms.length > 0 && (
        <div className={'lex-shell' + (selTerm ? '' : ' lex-shell--solo')}>
          <div className="lex-terms">
            <div className="lex-group-head">
              <span className="lex-group-name">{where}</span>
              {win && <span className="lex-meta">{longDate(win.from)} to {longDate(win.to)}</span>}
            </div>
            <WeekChart terms={terms} />
            <div className="lex-grid">
              {terms.map((e) => {
                const on = !!selTerm && matches(e, selTerm);
                return (
                  <button key={e.item_id} type="button" onClick={() => pick(e)} className="lex-term-card" aria-pressed={on}>
                    <span className="lex-term-top">
                      <span className="lex-term-name">{e.label}</span>
                      <ChangeMark entry={e} />
                    </span>
                    <span className="lex-count">
                      <Fig figure={e.posts}>{plural(e.posts.value, 'post', 'posts')}</Fig>
                      <span className="lex-kind"> · {e.kind_word || (e.kind === 'hashtag' ? 'Hashtag' : 'Word or phrase')}</span>
                    </span>
                  </button>
                );
              })}
            </div>
            {Array.isArray(data.notes) && data.notes.length > 0 && (
              <p className="lex-notes">{data.notes.join(' ')}</p>
            )}
          </div>

          <DecodePanel region={market} term={selTerm} entry={entry} window={win} />
          {!selTerm && <LexGuide days={days} />}
        </div>
      )}

      {/* Page port, 3 October 2026: Listen is leaving the More menu, so the
          next steps are Seed path, where a word's posts are, and Discover. */}
      <nav className="legacy-next" aria-label="Next">
        <span className="legacy-next-label">Next</span>
        <a className="legacy-action" href={buildScopedReadHash('seedpath', selTerm ? bare(selTerm) : '', reg)}>{selTerm ? 'Trace this term in Seed path' : 'Trace a term in Seed path'}</a>
        <a className="legacy-action" href="#/explore">Open Discover</a>
      </nav>
    </div>
  );
}
