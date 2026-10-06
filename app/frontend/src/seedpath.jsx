/* Seed path: trace one word, hashtag or slang term through the posts 42
   collected in one market (core/api/contract.md section 18). Which platform
   recorded it first, how many matching posts each platform saw a day over
   the last 28 days, the words that most often sit beside it and the 42 topics
   those posts belong to. Every number comes from GET /api/seed-path and keeps
   its query id; nothing is inferred on the page. */
import {useState, useEffect, useCallback, useRef} from 'react';
import {useApi, readerFigure, sentenceCase} from './api.js';
import {SignalLoader} from './parts.jsx';
import {REGION_NAME} from './model.js';
import {go, buildScopedReadHash} from './router.js';
import {PageHero, EmptyState, NextActions} from './ui/index.js';
import {longDate, topicHref} from './ui/TrendCard.jsx';
import {LoopRail} from './loopRail.jsx';
import './styles/seedpath.css';

const MARKETS = ['za', 'ng', 'ke'];
const STATUSES = ['ok', 'no_match', 'not_ready', 'too_large'];
const SUGGESTIONS = 6;

/* The term as the API matches it: trimmed, a leading # dropped, inner spaces
   collapsed, lower case. */
export function normaliseTerm(value){
  return String(value ?? '').trim().replace(/\s+/g, ' ').replace(/^#+/, '').trim().toLowerCase();
}

const record = (v) => !!v && typeof v === 'object' && !Array.isArray(v);
const isFigure = (v) => record(v) && v.value !== undefined && v.value !== null && typeof v.query_id === 'string';
const isDate = (v) => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v);
const count = (v) => Number.isSafeInteger(v) && v >= 0;

/* A response is shown only when it answers this term in this market and
   every number it carries is a Figure (or a daily block that names its
   query). Anything else is refused as incomplete, never patched. */
export function validSeedPathPayload(value, keyword, market){
  const term = normaliseTerm(keyword);
  const scope = String(market || '').toLowerCase();
  if (!record(value) || !term || !MARKETS.includes(scope)) return false;
  if (value.keyword !== term || String(value.market || '').toLowerCase() !== scope) return false;
  if (!STATUSES.includes(value.status) || typeof value.query_id !== 'string') return false;
  if (![value.platforms, value.side_words, value.topics].every(Array.isArray)) return false;
  if (value.status !== 'ok') return typeof value.message === 'string' && !!value.message.trim();
  if (!record(value.window) || !isDate(value.window.from) || !isDate(value.window.to) || !isFigure(value.matched_posts)) return false;
  if (!value.platforms.length) return false;
  const days = value.platforms[0]?.daily?.points?.length;
  return value.platforms.every((p) => record(p) && typeof p.platform === 'string' && typeof p.name === 'string'
      && isFigure(p.first_seen) && isDate(p.first_seen.value) && typeof p.before_window === 'boolean'
      && isFigure(p.posts_in_window) && count(p.posts_in_window.value)
      && record(p.daily) && typeof p.daily.query_id === 'string' && Array.isArray(p.daily.points)
      && p.daily.points.length === days && p.daily.points.every((d) => record(d) && isDate(d.date) && count(d.posts)))
    && value.side_words.every((w) => record(w) && typeof w.word === 'string' && !!w.word && isFigure(w.posts))
    && value.topics.every((t) => record(t) && typeof t.item_id === 'string' && !!t.item_id && typeof t.title === 'string' && isFigure(t.posts));
}

function plural(n, one, many){
  return readerFigure(n) + ' ' + (Number(n) === 1 ? one : many);
}

function shortDate(value){
  return longDate(value).replace(/ \d{4}$/, '');
}

/* A number with its query: the id rides on the element and in its title, as
   Compare carries it. */
function Fig({figure, children}){
  return <span className="sp-fig" data-query-id={figure.query_id} title={'Query ' + figure.query_id}>{children}</span>;
}

function askHref(term, market){
  const where = REGION_NAME[market.toUpperCase()] || market;
  const q = 'What is driving "' + term + '" in ' + where + ', and which words and topics sit next to it?';
  return '#/ask?q=' + encodeURIComponent(q) + '&market=' + market.toUpperCase() + '&draft=1';
}

/* One row per platform on one shared day axis. Bars are matching posts first
   seen that day, all on one scale; the platform's first sighting is the red
   bar, or a dashed red edge when it came before the window. */
export function PlatformTimeline({platforms, window: win, market}){
  const days = platforms[0].daily.points.map((p) => p.date);
  const top = Math.max(1, ...platforms.flatMap((p) => p.daily.points.map((d) => d.posts)));
  const midAt = Math.floor((days.length - 1) / 2);
  const earlier = platforms.some((p) => p.before_window);
  const where = REGION_NAME[market.toUpperCase()] || market;
  return (
    <figure className="sp-chart">
      <div className="sp-grid">
        <ol className="sp-rows">
          {platforms.map((p, n) => {
            const firstAt = days.indexOf(p.first_seen.value);
            const busiest = Math.max(...p.daily.points.map((d) => d.posts));
            const label = p.name + ': first seen ' + longDate(p.first_seen.value)
              + (p.before_window ? ', before these 28 days' : '') + '. '
              + plural(p.posts_in_window.value, 'matching post', 'matching posts') + ' in the 28 days'
              + (busiest ? ', at most ' + plural(busiest, 'post', 'posts') + ' in a day.' : '.');
            return (
              <li key={p.platform} className="sp-row" data-platform-row={n + 1}>
                <div className="sp-row-name">
                  <span className="sp-platform">{p.name}</span>
                  <span className="sp-first">
                    First seen <Fig figure={p.first_seen}><time dateTime={p.first_seen.value}>{longDate(p.first_seen.value)}</time></Fig>
                    {p.before_window ? <span className="sp-before">Before these 28 days</span> : null}
                  </span>
                </div>
                <div className="sp-track" role="img" aria-label={label} style={{'--sp-days': days.length}}>
                  {p.before_window && <span className="sp-earlier" aria-hidden="true" />}
                  {p.daily.points.map((d, i) => (
                    <span key={d.date} className={'sp-bar' + (d.posts ? '' : ' is-zero') + (i === firstAt ? ' is-first' : '')} style={{gridColumn: i + 1, '--sp-h': d.posts / top}} data-date={d.date} data-posts={d.posts} />
                  ))}
                </div>
                <div className="sp-row-count">
                  <Fig figure={p.posts_in_window}>{plural(p.posts_in_window.value, 'post', 'posts')}</Fig>
                </div>
              </li>
            );
          })}
        </ol>
        <div className="sp-axis" aria-hidden="true">
          <div className="sp-axis-line">
            {[0, midAt, days.length - 1].map((i, k) => (
              <span key={days[i]} className={'sp-axis-tick' + ['', ' is-mid', ' is-end'][k]} style={{'--sp-at': (i + 0.5) / days.length}}>{shortDate(days[i])}</span>
            ))}
          </div>
        </div>
      </div>
      <figcaption className="sp-caption">
        <details className="sp-how">
          <summary>How to read this chart</summary>
          <p>Each bar is one day in {where}, {longDate(win.from)} to {longDate(win.to)}. Its height is the matching posts 42 first saw that day, on one scale for every row (the tallest bar is {plural(top, 'post', 'posts')}). A red bar is the platform's first sighting{earlier ? '; a dashed red edge means it came before these 28 days' : ''}. No bar means no matching post that day, or little collected there.</p>
        </details>
      </figcaption>
    </figure>
  );
}

function SideWords({words, minPosts, market, onTrace}){
  if (!words.length){
    return <p className="sp-quiet">No other word appears in {minPosts} or more of these posts yet.</p>;
  }
  return (
    <ol className="sp-words">
      {words.map((w) => (
        <li key={w.word}>
          <button type="button" className="sp-word" onClick={() => onTrace(w.word, market)} title={'Trace ' + w.word}>{w.word}</button>
          <Fig figure={w.posts}>{plural(w.posts.value, 'post', 'posts')}</Fig>
        </li>
      ))}
    </ol>
  );
}

function Topics({topics, market}){
  const where = REGION_NAME[market.toUpperCase()] || market;
  if (!topics.length){
    return <p className="sp-quiet">None of these posts belongs to a topic 42 is following in {where} today.</p>;
  }
  return (
    <ul className="sp-topics">
      {topics.map((t) => (
        <li key={t.item_id}>
          <a className="sp-topic" href={topicHref(t.item_id, market.toUpperCase())}>{t.title}</a>
          <span className="sp-topic-meta">{[t.kind ? sentenceCase(t.kind) : null, t.state_word, t.held_back ? 'Held back' : null].filter(Boolean).join(', ')}</span>
          <Fig figure={t.posts}>{plural(t.posts.value, 'post', 'posts')}</Fig>
        </li>
      ))}
    </ul>
  );
}

export function SeedPathResult({data, market, onTrace}){
  const where = REGION_NAME[market.toUpperCase()] || market;
  const first = data.platforms[0];
  const n = data.platforms.length;
  return (
    <div className="sp-result">
      <header className="sp-summary">
        <h2 className="sp-term">&ldquo;{data.keyword}&rdquo; in {where}</h2>
        <p className="sp-lead">
          Recorded on <span className="sp-keep">{plural(n, 'platform', 'platforms')}</span>. {first.name} saw it first, on <Fig figure={first.first_seen}>{longDate(first.first_seen.value)}</Fig>.
          {' '}<span className="sp-keep"><Fig figure={data.matched_posts}>{readerFigure(data.matched_posts.value)}</Fig> {Number(data.matched_posts.value) === 1 ? 'matching post' : 'matching posts'}</span> {Number(data.matched_posts.value) === 1 ? 'was' : 'were'} first seen in the 28 days to {longDate(data.window.to)}.
        </p>
        <a className="legacy-action" href={askHref(data.keyword, market)}>Ask about this</a>
      </header>

      <section className="sp-section" aria-labelledby="sp-where">
        <h3 id="sp-where" className="sp-section-title">Where it was recorded first</h3>
        <PlatformTimeline platforms={data.platforms} window={data.window} market={market} />
      </section>

      <div className="sp-pair">
        <section className="sp-section" aria-labelledby="sp-words">
          <h3 id="sp-words" className="sp-section-title">Words beside it</h3>
          <p className="sp-section-sub">Words in at least {data.side_words_min_posts} of the matching posts, most often first. Choose one to trace it.</p>
          <SideWords words={data.side_words} minPosts={data.side_words_min_posts} market={market} onTrace={onTrace} />
        </section>
        <section className="sp-section" aria-labelledby="sp-topics">
          <h3 id="sp-topics" className="sp-section-title">Topics it touches</h3>
          <p className="sp-section-sub">42 topics these posts belong to, with how many of the matching posts each holds.</p>
          <Topics topics={data.topics} market={market} />
        </section>
      </div>

      {data.notes && data.notes.length > 0 && (
        <ul className="sp-notes">
          {data.notes.map((note) => <li key={note}>{note}</li>)}
        </ul>
      )}
    </div>
  );
}

const STATUS_TITLES = {
  no_match: (term, where) => 'No posts use “' + term + '” in ' + where,
  not_ready: () => 'Seed path is waiting for posts',
  too_large: () => 'That term is too broad to trace',
};

/* Real terms to try: the names of things Discover is following in this
   market today. No item, no suggestions; none are made up. */
export function suggestionTerms(data){
  const items = record(data) && Array.isArray(data.items) ? data.items : [];
  const out = [];
  for (const item of items){
    const title = record(item) && item.kind !== 'creator' && typeof item.title === 'string' ? item.title.trim() : '';
    if (title && title.length <= 60 && !out.includes(title)) out.push(title);
    if (out.length === SUGGESTIONS) break;
  }
  return out;
}

export function SeedPathPage({session, onAuth, initialKeyword, initialMarket, setRegion, scopeError}){
  const [keyword, setKeyword] = useState(initialKeyword || '');
  const [problem, setProblem] = useState(null);
  const market = String(initialMarket || 'za').toLowerCase();
  const q = (initialKeyword || '').trim();
  const keywordInputRef = useRef(null);
  const where = REGION_NAME[market.toUpperCase()] || market;

  const traceTerm = useCallback((term, mk) => {
    const t = String(term || '').trim();
    if (!t || !MARKETS.includes(mk)) return;
    setKeyword(t);
    go(buildScopedReadHash('seedpath', t, mk).slice(1));
  }, []);

  useEffect(() => {
    setKeyword((initialKeyword || '').trim());
    setProblem(null);
  }, [initialKeyword]);

  const readable = MARKETS.includes(market) && !scopeError;
  const tooShort = !!q && normaliseTerm(q).length < 2;
  /* Cold load reads nothing for the trace: the hook idles until a term is
     traced. */
  const path = '/api/seed-path?keyword=' + encodeURIComponent(q) + '&market=' + encodeURIComponent(market.toUpperCase());
  const [res, retryPath] = useApi(path, session, onAuth, !!q && readable && !tooShort);
  const [discoverRes] = useApi('/api/discover?market=' + market.toUpperCase() + '&limit=12', session, onAuth, readable && !q);
  const examples = suggestionTerms(discoverRes.state === 'ready' ? discoverRes.data : null);

  /* The service traces 2 to 60 characters with at least one letter or
     number; a draft it would refuse says why here instead of doing nothing. */
  const submit = (e) => {
    e.preventDefault();
    const t = keyword.trim();
    if (!t) { setProblem('Type a word, hashtag or slang term to trace.'); focusField(); return; }
    if (normaliseTerm(t).length < 2 || !/[\p{L}\p{N}]/u.test(t)) {
      setProblem('Type at least two characters, with a letter or number among them.'); focusField(); return;
    }
    setProblem(null);
    traceTerm(t, market);
  };

  const ready = res.state === 'ready' && q && readable && !tooShort;
  const invalid = ready && !validSeedPathPayload(res.data, q, market);
  const d = ready && !invalid ? res.data : null;
  const focusField = () => keywordInputRef.current && keywordInputRef.current.focus();

  return (
    <div className="page seedpath">
      <div className="page-shell">
        <div className="loop-hero-band">
          <PageHero
            title="Seed path"
            sub="Where did 42 first record a word, and which words appear with it? Type a word, hashtag or slang term to see where it was first recorded and the words beside it."
          />
          <LoopRail active="explorer" />
        </div>

        <form onSubmit={submit} className="sp-form">
          <input
            ref={keywordInputRef}
            value={keyword}
            onChange={(e) => { setKeyword(e.target.value); if (problem) setProblem(null); }}
            aria-invalid={problem ? 'true' : undefined}
            aria-describedby={problem ? 'sp-problem' : undefined}
            placeholder="Word, hashtag or slang"
            aria-label="Keyword to trace"
            className="seed-field sp-input"
            maxLength={60}
          />
          <div className="sp-markets loop-tabs" role="group" aria-label="Market">
            {MARKETS.map((mk) => (
              <button
                key={mk}
                type="button"
                onClick={() => {
                  setRegion?.(mk);
                  go(buildScopedReadHash('seedpath', q, mk).slice(1));
                }}
                className="legacy-chip loop-tab"
                aria-pressed={market === mk}
              >{REGION_NAME[mk.toUpperCase()]}</button>
            ))}
          </div>
          <button type="submit" disabled={!readable} className="legacy-action legacy-action--primary">Trace</button>
        </form>
        {problem && <p className="sp-problem" id="sp-problem" role="alert" data-seedpath-problem="">{problem}</p>}

        {!readable && <EmptyState title="Choose one market" body="Seed path reads one country at a time. Choose South Africa, Nigeria or Kenya above to continue." />}

        {!q && readable && (
          <div className="sp-idle">
            <div className="sp-idle-main">
              <EmptyState
                title="Nothing traced yet"
                body="Type a word above to see which platforms recorded it first, the words beside it and the topics it touches."
              />
              {examples.length > 0 && (
                <div className="sp-try">
                  <div className="sp-label">Things 42 is following in <span className="sp-keep">{where} today</span></div>
                  <div className="sp-try-list">
                    {examples.map((ex) => (
                      <button key={ex} type="button" onClick={() => traceTerm(ex, market)} className="sp-chip">{ex}</button>
                    ))}
                  </div>
                </div>
              )}
            </div>
            <aside className="sp-idle-aside" aria-labelledby="sp-idle-title">
              <h2 className="sp-idle-title" id="sp-idle-title">What a trace shows</h2>
              <dl className="sp-idle-parts">
                <div className="sp-idle-part"><dt>Where it was recorded first</dt><dd>Each platform that recorded the term, the day it first did, and its posts day by day over 28 days.</dd></div>
                <div className="sp-idle-part"><dt>Words beside it</dt><dd>Words that keep turning up in the same posts. Choose one to trace it in turn.</dd></div>
                <div className="sp-idle-part"><dt>Topics it touches</dt><dd>The 42 topics those posts belong to, each a link to the topic.</dd></div>
              </dl>
            </aside>
          </div>
        )}

        {tooShort && readable && (
          <EmptyState title="Type a longer term" body="Seed path needs at least two letters or numbers to trace." cta={{label: 'Edit the term', onClick: focusField}} />
        )}

        {res.state === 'loading' && q && readable && !tooShort && (
          <div className="sp-loading">
            <SignalLoader />
            <span className="sp-quiet">Reading the posts 42 collected in {where}</span>
          </div>
        )}

        {(res.state === 'error' || invalid) && q && readable && !tooShort && (
          <EmptyState
            title="Could not trace this term"
            body={invalid ? 'The answer was incomplete or was for another term or market, so nothing is shown. Try again.' : (res.message || 'The 42 service did not answer.') + ' Try again in a moment.'}
            cta={{label: 'Try again', onClick: retryPath}}
          />
        )}

        {d && d.status !== 'ok' && (
          <EmptyState
            title={STATUS_TITLES[d.status](d.keyword, where)}
            body={d.status === 'no_match'
              ? d.message + ' Seed path looks in post text, video transcripts and hashtags. Try another spelling or another market.'
              : d.message}
            cta={{label: 'Try another term', onClick: focusField}}
          />
        )}

        {d && d.status === 'ok' && <SeedPathResult data={d} market={market} onTrace={traceTerm} />}

        {(!q || ['ready', 'error'].includes(res.state)) && readable && (
          <NextActions actions={[{label: 'Open Seeds', route: '/seeds'}, {label: 'Open Discover', route: '/explore'}]} />
        )}
      </div>
    </div>
  );
}

export default SeedPathPage;
