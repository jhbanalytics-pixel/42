/* A topic page on the 42 API: the story of one thing (docs/full-42/EXPERIENCE.md,
   topic and creator pages; core/api/contract.md section 10.3). The card
   leads, then how its state moved, each platform's daily series, how far it
   spread, where it started, the news stories linked to it, earlier waves,
   what the posts look like, and the posts themselves. A market is where 42
   collected the posts (its feeds), never where people live.
   A part whose data does not exist yet says so in words instead of hiding.
   A measured day on a platform series asks why that day jumped (section
   14.1), after a confirm that names the cost. */
import {useEffect, useRef, useState} from 'react';
import {fetchTopic} from './api42.js';
import {readerFigure, sentenceCase, seriesFigure} from './api.js';
import {safeUrl} from './safeUrl.js';
import {unitFor} from './readerUnits.js';
import {SpikeConfirm} from './ui/SpikeConfirm.jsx';
import {AskAboutThis} from './ui/AskAboutThis.jsx';
import {ExcerptNote, PostFull, TrendCard, askQuestion, longDate, platformWord, postTextView, writtenTitle} from './ui/TrendCard.jsx';
import {UNNAMED_TOPIC_WORDS, isUnnamedTopic} from './topicNames.js';
import {useCardWatch} from './ui/WatchDialog.jsx';
import './styles/topic42.css';

const MARKET_WORDS = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya'};
const TIERS = [
  {key: 'nano', word: 'Nano'},
  {key: 'micro', word: 'Micro'},
  {key: 'mid', word: 'Mid'},
  {key: 'macro', word: 'Macro'},
  {key: 'mega', word: 'Mega'},
];
/* Account age is a bot signal, never a person's age, so its words are fixed here. */
const SIGNAL_WORDS = {young_accounts: 'Accounts under 30 days old'};
/* The server's flag word, in the words a reader needs. */
const FLAG_WORDS = {'Check pattern': 'Unusual posting pattern'};
const NOT_CHECKED_WORDS = {
  not_assessed: 'Not checked: too few posts to judge.',
  not_stored: 'Not checked: no check of these posts is stored.',
};

const isFigure = (value) => value && typeof value === 'object' && typeof value.value === 'number';
const marketWord = (market) => MARKET_WORDS[market] || market;

function askHref(card, market, question){
  return '#/ask?q=' + encodeURIComponent(question || '')
    + '&market=' + encodeURIComponent(market)
    + '&item=' + encodeURIComponent(card.item_id)
    + (card.date ? '&date=' + encodeURIComponent(card.date) : '');
}

/* The day Try again on a failed spike ask sends back here
   (#/t/<item_id>?market=ZA&day=<date>&series=<series>), as the confirm's
   day. Null when the hash is for another topic or names no measured day:
   a gap cannot be asked about. Without a series, the first series measured
   that day is taken. */
function hashDay(hash, itemId, series){
  const text = String(hash || '');
  const at = text.indexOf('?');
  const path = at >= 0 ? text.slice(0, at) : text;
  if (!path.startsWith('#/t/')) return null;
  let id = '';
  try { id = decodeURIComponent(path.slice('#/t/'.length)); } catch (_error){ return null; }
  if (id !== itemId) return null;
  const query = new URLSearchParams(at >= 0 ? text.slice(at + 1) : '');
  const date = query.get('day');
  const wanted = query.get('series');
  if (!date) return null;
  for (const s of Array.isArray(series) ? series : []){
    if (wanted && s.series !== wanted) continue;
    const point = (Array.isArray(s.points) ? s.points : []).find((p) => p.date === date);
    if (point && typeof point.value === 'number') return {date, series: s.series, words: s.series_words || platformWord(s.platform)};
  }
  return null;
}

/* Once the confirm has opened from the URL, day and series come out of the
   hash by replacing the current entry, not adding one, so a refresh or Back
   does not open it again. The rest of the query stays. */
function forgetHashDay(){
  const hash = window.location.hash;
  const at = hash.indexOf('?');
  const query = new URLSearchParams(hash.slice(at + 1));
  query.delete('day');
  query.delete('series');
  const rest = query.toString();
  window.history.replaceState(window.history.state, '', hash.slice(0, at) + (rest ? '?' + rest : ''));
}

/* A Figure's value with the query that produced it on the element. */
function Fig({figure, children}){
  return (
    <span className="tp42-fig" data-query-id={figure.query_id} title={'Query ' + figure.query_id + ', run ' + figure.run_id}>
      {children ?? readerFigure(figure.value)}
    </span>
  );
}

export function TopicPage42({itemId, market, onAuth, onCreateWatch}){
  const [load, setLoad] = useState({state: 'loading'});
  const [tick, setTick] = useState(0);
  const authRef = useRef(onAuth);
  authRef.current = onAuth;
  const watch = useCardWatch(onCreateWatch, onAuth);
  const [spike, setSpike] = useState(null);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoad({state: 'loading'});
    fetchTopic(itemId, market, {signal: ctrl.signal})
      .then((data) => {
        if (ctrl.signal.aborted) return;
        setLoad({state: 'ready', data});
        const day = hashDay(window.location.hash, itemId, data && data.series);
        setSpike(day);
        if (day) forgetHashDay();
      })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){
          setLoad({state: 'auth'});
          if (authRef.current) authRef.current();
        } else if (error && error.status === 404){
          setLoad({state: 'missing'});
        } else {
          setLoad({state: 'error', message: error && error.status ? error.message : 'The 42 service did not answer.'});
        }
      });
    return () => ctrl.abort();
  }, [itemId, market, tick]);

  if (load.state === 'loading'){
    return (
      <section className="page tp42" aria-busy="true">
        <p className="tp42-status" role="status">Loading the topic</p>
      </section>
    );
  }
  if (load.state === 'auth'){
    return (
      <section className="page tp42">
        <p className="tp42-status" role="status">Enter the passcode to read this topic.</p>
      </section>
    );
  }
  if (load.state === 'missing'){
    return (
      <section className="page tp42">
        <h1 className="tp42-heading">Topic not found</h1>
        <p className="tp42-status">42 has no topic with this id in {marketWord(market)}.</p>
      </section>
    );
  }
  if (load.state === 'error'){
    return (
      <section className="page tp42">
        <h1 className="tp42-heading">The topic could not load</h1>
        <p className="tp42-status" role="alert">{load.message}</p>
        <button type="button" className="tp42-button" onClick={() => setTick((t) => t + 1)}>Try again</button>
      </section>
    );
  }

  const data = load.data || {};
  const card = data.card || {};
  const where = card.market || market;
  const aliases = Array.isArray(data.aliases) ? data.aliases : [];
  /* The heading is the title the card leads with on Today; the cluster label it replaces sits in the line under it. */
  const written = writtenTitle(card, card.title);
  const unnamed = !written && isUnnamedTopic(card.title);
  const heading = written || (unnamed ? UNNAMED_TOPIC_WORDS : card.title);
  const question = askQuestion(card, card.title, written);

  return (
    <section className="page tp42">
      <header className="tp42-head">
        <h1 className="tp42-heading">{heading}</h1>
        <p className="tp42-sub">
          {(written || unnamed) ? card.title + ' · ' : ''}{marketWord(where)}
          {aliases.length > 0 ? ' · also seen as ' + aliases.join(', ') : ''}
        </p>
        {card.held_back && card.held_back.reason_text && (
          <p className="tp42-held">Held back: {card.held_back.reason_text}</p>
        )}
      </header>

      {/* The page heading already names the topic, so the card drops its own
          title here. A held topic's card is what we saw, not what we published:
          its tag and lifecycle are muted and it says so. */}
      <div className="tp42-card" data-section="card" data-held={card.held_back ? '' : undefined}>
        {card.held_back && <p className="tp42-card-label">What we saw (not published)</p>}
        <ul className="tp42-card-list">
          <TrendCard card={watch.mark(card, where)} market={where} date={card.date} onAuth={onAuth} onWatch={watch.onWatch} posts={false} />
        </ul>
      </div>

      <div className="tp42-grid">
        <History history={data.history} />
        <Spread spread={data.spread} line={card.spread_line} />
        <Origin origin={data.origin} market={where} />
        <News news={data.news} />
        <Series series={data.series} onDay={setSpike} />
        <Waves waves={data.waves} />
        <Authenticity authenticity={data.authenticity} />
      </div>

      {/* Demo polish, 2 October 2026 (QA item 13): when the topic read holds no
          posts but the trend's own card carries its stored posts, as Today
          shows them, those are this topic's posts. */}
      <Evidence items={Array.isArray(data.evidence) && data.evidence.length > 0
        ? data.evidence
        : Array.isArray(card.evidence) && card.evidence.length > 0 ? card.evidence : data.evidence} />

      <p className="tp42-actions" style={{display: 'flex', flexWrap: 'wrap', gap: 'var(--s-4)'}}>
        <AskAboutThis className="tp42-link" href={askHref(card, where, question)} question={question} />
        <a className="tp42-link" href={'#/compare?mode=items&items=' + encodeURIComponent(card.item_id || itemId) + '&market=' + encodeURIComponent(where)}>Compare with</a>
        <a className="tp42-link" href={'#/history/items/' + encodeURIComponent(card.item_id || itemId) + '?market=' + encodeURIComponent(where)}>History of this item</a>
      </p>
      {watch.dialog}
      {spike && <SpikeConfirm day={spike} itemId={card.item_id || itemId} market={where} onAuth={onAuth} onClose={() => setSpike(null)} />}
    </section>
  );
}

function Part({name, title, children}){
  return (
    <section className="tp42-part" data-section={name}>
      <h2 className="tp42-part-title">{title}</h2>
      {children}
    </section>
  );
}

function History({history}){
  const rows = (Array.isArray(history) ? history : []).slice().sort((a, b) => String(a.date).localeCompare(String(b.date)));
  return (
    <Part name="history" title="How it developed">
      {rows.length > 0
        ? <ol className="tp42-timeline">
            {rows.map((h) => <li key={h.date + h.state_word}>{longDate(h.date)} · {h.state_word}</li>)}
          </ol>
        : <p className="tp42-line">No state recorded yet.</p>}
    </Part>
  );
}

/* A series with no usable day says nothing on the page, so it is left out. */
function Series({series, onDay}){
  const rows = (Array.isArray(series) ? series : [])
    .filter((s) => (Array.isArray(s.points) ? s.points : []).some((p) => typeof p.value === 'number'));
  return (
    <Part name="series" title="Daily posts by platform">
      {series === null ? <p className="tp42-line">Platform series are not measured yet.</p> : rows.length > 0
        ? <ul className="tp42-series">
            {rows.map((s) => {
              const points = Array.isArray(s.points) ? s.points : [];
              const words = s.series_words || platformWord(s.platform);
              return (
                <li key={s.platform + ':' + s.series} className="tp42-series-row">
                  <span className="tp42-series-name">{words}</span>
                  {measured(points).length < 3
                    ? <span className="tp42-series-young">{youngSeries(points, s.unit)}</span>
                    : <Sparkline points={points} unit={s.unit} onDay={onDay && ((p) => onDay({date: p.date, series: s.series, words}))} />}
                </li>
              );
            })}
          </ul>
        : <p className="tp42-line">{Array.isArray(series) && series.length > 0 ? 'No platform has a usable day for this topic yet.' : 'No platform series for this topic yet.'}</p>}
    </Part>
  );
}

/* Under three measured days a line chart draws a slope out of almost
   nothing, so the readings are given in words instead. */
const measured = (points) => points.filter((p) => typeof p.value === 'number' && Number.isFinite(p.value));

function youngSeries(points, unit){
  const days = measured(points);
  const noun = unit && unit !== 'posts' ? unit : 'posts';
  const values = days.map((p) => seriesFigure(p.value)).join(' and ');
  /* One reading of exactly 1 reads "1 post a day", not "1 posts a day". */
  if (days.length === 1) return '1 day of readings so far: ' + values + ' ' + unitFor(seriesFigure(days[0].value), noun);
  return (days.length === 1 ? '1 day of readings so far: ' : days.length + ' days of readings so far: ') + values + ' ' + noun;
}

/* A null value is a day with no valid collection, so the line breaks there
   instead of dropping to zero. Each measured day is a button over its point;
   a gap has none, since there is nothing to ask about. */
function Sparkline({points, unit, onDay}){
  const W = 160, H = 32, pad = 2;
  const num = (v) => typeof v === 'number' && Number.isFinite(v);
  const peak = Math.max(1, ...points.map((p) => p.value).filter(num));
  const x = (i) => (points.length === 1 ? W / 2 : pad + (i * (W - 2 * pad)) / (points.length - 1));
  const y = (v) => H - pad - (Math.max(0, v) / peak) * (H - 2 * pad);
  const fix = (n) => n.toFixed(1);
  let line = '';
  let open = false;
  points.forEach((p, i) => {
    if (!num(p.value)){ open = false; return; }
    line += (open ? ' L ' : (line ? ' M ' : 'M ')) + fix(x(i)) + ' ' + fix(y(p.value));
    open = true;
  });
  line = line.replace(/M (\S+ \S+)(?= M |$)/g, 'M $1 h 0.1');
  const values = points.map((p) => p.value).filter(num);
  const label = sentenceCase(unit || 'value') + ' over ' + points.length + ' days'
    + ', latest ' + seriesFigure(values[values.length - 1])
    + (values.length < points.length ? ', with days missing' : '');
  const svg = (
    <svg className="tp42-spark" viewBox={'0 0 ' + W + ' ' + H} width={W} height={H} role="img" aria-label={label}>
      <path className="tp42-line" d={line} fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
  if (!onDay) return svg;
  const step = points.length > 1 ? (W - 2 * pad) / (points.length - 1) : W;
  const pct = (n) => (100 * n / W).toFixed(2) + '%';
  return (
    <span className="tp42-spark-days" style={{width: W, height: H}}>
      {svg}
      {points.map((p, i) => (num(p.value) && p.date
        ? <button key={p.date} type="button" className="tp42-day" data-day={p.date}
            style={{left: pct(Math.max(0, x(i) - step / 2)), width: pct(Math.min(step, W))}}
            aria-label={longDate(p.date) + ', ' + seriesFigure(p.value) + ' ' + unitFor(p.value, unit || 'a day') + '. Ask why this day jumped'}
            title={longDate(p.date) + ': ' + seriesFigure(p.value) + '. Ask why this day jumped'}
            onClick={() => onDay(p)} />
        : null))}
    </span>
  );
}

function Spread({spread, line}){
  if (!spread){
    return (
      <Part name="spread" title="Spread">
        {line && <p className="tp42-line">{line}</p>}
        <p className="tp42-line">Spread is not measured yet.</p>
      </Part>
    );
  }
  const platforms = Array.isArray(spread.platforms) ? spread.platforms : [];
  const markets = Array.isArray(spread.markets) ? spread.markets : [];
  const tiers = spread.tiers || {};
  const shown = TIERS.filter((t) => isFigure(tiers[t.key]));
  /* The engine counts measured posts per creator tier, so its unit names posts; the label follows the unit. */
  const units = [...new Set(shown.map((t) => tiers[t.key].unit))];
  const postUnit = units.length === 1 && typeof units[0] === 'string' && units[0].startsWith('posts ') ? units[0] : null;
  const tierLabel = postUnit ? 'Posts by creator size, ' + postUnit.slice('posts '.length) : 'Creators by size';
  return (
    <Part name="spread" title="Spread">
      {line && <p className="tp42-lead">{line}</p>}
      {platforms.length > 0 && (
        <ul className="tp42-rows">
          {platforms.map((p) => <li key={p.platform}>{platformWord(p.platform)}, first seen {longDate(p.first_seen)}</li>)}
        </ul>
      )}
      {shown.length > 0 && (
        <p className="tp42-line">
          {tierLabel}:{' '}
          {shown.map((t, i) => (
            <span key={t.key}>{i > 0 ? ', ' : ''}{t.word} <Fig figure={tiers[t.key]} /></span>
          ))}
        </p>
      )}
      {markets.length > 0 && (
        <ul className="tp42-rows">
          {markets.map((m) => (
            <li key={m.market} data-market={m.market}>
              {feedsWord(m.market)}{m.first_seen ? ' from ' + longDate(m.first_seen) : ''}
              {isFigure(m.posts) && <>, <Fig figure={m.posts} /> {unitFor(m.posts.value, m.posts.unit)}</>}
            </li>
          ))}
        </ul>
      )}
    </Part>
  );
}

const feedsWord = (market) => 'Seen in ' + marketWord(market) + ' feeds';
const days = (n) => n + (Math.abs(n) === 1 ? ' day' : ' days');

/* The lag between a news day and social, in words; nothing when 42 has no lag. */
function lagWords(lag){
  if (typeof lag !== 'number') return null;
  if (lag === 0) return 'reached social the same day as the news';
  return lag > 0 ? 'reached social ' + days(lag) + ' after the news' : 'on social ' + days(-lag) + ' before the news';
}

/* Where the topic started (v_item_origin). The first measured day and the
   first state day are measured; whether it started in the news or on social
   stays unknown until 42 records a news source and its date. */
function Origin({origin, market}){
  if (!origin){
    return (
      <Part name="origin" title="Where it started">
        <p className="tp42-line">Where it started is not measured yet.</p>
      </Part>
    );
  }
  const where = origin.market || market;
  const lead = origin.origin === 'news_led'
    ? 'Started in the news' + (origin.lead_news_day ? ' on ' + longDate(origin.lead_news_day) : '')
      + (typeof origin.lag_days === 'number' ? ', reaching social ' + days(origin.lag_days) + ' later' : '') + '.'
    : origin.origin === 'native' ? 'Started on social.' : 'Whether it started in the news or on social is not known yet.';
  return (
    <Part name="origin" title="Where it started">
      {origin.first_measured && (
        <p className="tp42-lead">
          First seen in {marketWord(where)} feeds on {longDate(origin.first_measured)}.{' '}
          {origin.after_collection_began
            ? '42 was already collecting there before then.'
            : 'It may be older: 42 began collecting there that day or has no earlier record.'}
        </p>
      )}
      {origin.first_state_day && <p className="tp42-line">First flagged by 42 on {longDate(origin.first_state_day)}.</p>}
      <p className="tp42-line">{lead}</p>
    </Part>
  );
}

/* News stories 42 searched for that name this topic (v_news_followthrough),
   newest first. A news day and a lag show only once 42 records them. */
function News({news}){
  const rows = Array.isArray(news) ? news : [];
  return (
    <Part name="news" title="From the news">
      {rows.length > 0
        ? <ul className="tp42-rows">
            {rows.map((n) => {
              const parts = [
                n.news_day ? 'In the news ' + longDate(n.news_day) : null,
                n.first_measured ? 'On social from ' + longDate(n.first_measured) : null,
                lagWords(n.lag_days),
                n.seed_date ? '42 searched for it from ' + longDate(n.seed_date) : null,
                n.reached_state && n.first_state_day ? 'flagged by 42 on ' + longDate(n.first_state_day) : null,
              ].filter(Boolean);
              return (
                <li key={n.seed_date + ':' + n.title}>
                  <strong>{n.title}</strong>{parts.length > 0 ? ' · ' + parts.join(' · ') : ''}
                </li>
              );
            })}
          </ul>
        : <p className="tp42-line">No news story is linked to this topic yet.</p>}
    </Part>
  );
}

function Waves({waves}){
  const rows = Array.isArray(waves) ? waves : [];
  return (
    <Part name="waves" title="Earlier waves">
      {waves === null ? <p className="tp42-line">Earlier waves are not measured yet.</p> : rows.length > 0
        ? <ul className="tp42-rows">
            {rows.map((w) => (
              <li key={w.peak_date}>
                Peaked {longDate(w.peak_date)}
                {isFigure(w.peak_posts) && <> at <Fig figure={w.peak_posts} /> {unitFor(w.peak_posts.value, w.peak_posts.unit)}</>}
              </li>
            ))}
          </ul>
        : <p className="tp42-line">No earlier waves.</p>}
    </Part>
  );
}

function Authenticity({authenticity}){
  const auth = authenticity || {};
  const signals = Array.isArray(auth.signals) ? auth.signals : [];
  // Posts that were never checked say so once, in place of the flag word. The reassurance shows only when a
  // stored check ran and flagged nothing (flag null); not_stored means no check is stored for the item.
  const notChecked = NOT_CHECKED_WORDS[auth.flag] || '';
  const noSignals = auth.flag === null ? 'No unusual patterns in the posts 42 read.' : '';
  return (
    <Part name="authenticity" title="What the posts look like">
      {notChecked
        ? <p className="tp42-line" data-not-checked="">{notChecked}</p>
        : auth.flag_word && <p className="tp42-flag">{FLAG_WORDS[auth.flag_word] || auth.flag_word}</p>}
      {signals.length > 0
        ? <ul className="tp42-rows">
            {signals.map((s) => {
              const words = SIGNAL_WORDS[s.signal] || s.words || sentenceCase(String(s.signal || '').replace(/_/g, ' '));
              return (
                <li key={s.signal}>
                  {words}
                  {isFigure(s.share) && <>: <Fig figure={s.share}>{Math.round(s.share.value * 100) + '%'}</Fig></>}
                </li>
              );
            })}
          </ul>
        : noSignals && <p className="tp42-line">{noSignals}</p>}
    </Part>
  );
}

/* Visual QA, 5 October 2026: one Instagram post showed as two identical
   cards, read twice under two record ids. A post is shown once: the same
   link, or with no link the same platform, author, day and text, is the
   same post. The first record read stays. */
export function distinctPosts(items){
  const seen = new Set();
  return items.filter((e) => {
    const url = safeUrl(e && e.url);
    const key = url ? 'url:' + url
      : 'post:' + [e && e.platform, e && e.handle, e && e.posted_at, e && e.text].map((v) => String(v ?? '')).join('|');
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function Evidence({items}){
  const [failed, setFailed] = useState([]);
  const posts = distinctPosts(Array.isArray(items) ? items : []);
  return (
    <section className="tp42-part tp42-evidence" data-section="evidence">
      <h2 className="tp42-part-title">Posts</h2>
      {items === null ? <p className="tp42-line">Posts for this topic are not stored yet.</p> : posts.length > 0
        ? <ul className="tp42-posts">
            {posts.map((e) => {
              const views = e.engagement && typeof e.engagement.views === 'number' ? e.engagement.views : null;
              const meta = [platformWord(e.platform), e.handle, e.posted_at ? longDate(e.posted_at) : null, views !== null ? readerFigure(views) + ' views' : null]
                .filter(Boolean).join(' · ');
              const name = e.handle ? 'Post by ' + e.handle + ' on ' + platformWord(e.platform) : 'Post on ' + platformWord(e.platform);
              const url = safeUrl(e.url);
              /* The topic read's excerpt is cut at 280 characters with no flag,
                 so a post that long reads as an excerpt (audit TOP-01). */
              const view = postTextView(e, {excerptAtLeast: true});
              const src = safeUrl(e.thumbnail_url);
              /* A missing or failed still leaves no empty frame; the post reads as text. */
              const shown = src && !failed.includes(src) ? src : null;
              return (
                <li key={e.id} className="tp42-post">
                  {shown && <img className="tp42-thumb" src={shown} alt={name} width="72" height="96" loading="lazy"
                    onError={() => setFailed((current) => current.includes(shown) ? current : [...current, shown])} />}
                  <div className="tp42-post-body">
                    <p className="tp42-post-meta">{meta}</p>
                    {view.preview && <p className="tp42-post-text" data-shortened={view.shortened ? '' : undefined}>{view.preview}</p>}
                    {view.full && <PostFull text={view.full} />}
                    {url && <a className="tp42-link" href={url} target="_blank" rel="noopener noreferrer">Open the post</a>}
                    {view.excerpt && <ExcerptNote />}
                  </div>
                </li>
              );
            })}
          </ul>
        : <p className="tp42-line">No posts are stored for this topic.</p>}
    </section>
  );
}
