/* The one trend card of Today, Discover and topic pages (contract.md
   sections 4 and 10.1). A Stage 1 card shows what Stage 1 can fill; the
   Stage 2 fields show when the card carries them, and a field that is
   present but null says its data does not exist yet instead of hiding.
   Watch and the feedback taps show only when the page passes their calls. */
import {useEffect, useRef, useState} from 'react';
import {fetchTrend} from '../api42.js';
import {readerFigure, sentenceCase, seriesFigure} from '../api.js';
import {lifecycleRuleWords} from '../plainLabels.js';
import {safeUrl} from '../safeUrl.js';
import {AskAboutThis} from './AskAboutThis.jsx';
import {Facts} from './Facts.jsx';
import {UNNAMED_TOPIC_WORDS, isUnnamedTopic} from '../topicNames.js';
import '../styles/today42.css';
import '../styles/discover42.css';
import '../styles/alerts42.css';

const TAG_WORDS = {first_time: 'First time on Today', moved_up: 'Moved up', held_place: 'Held its place'};
export const PLATFORM_WORDS = {
  tiktok: 'TikTok', instagram: 'Instagram', youtube: 'YouTube', x: 'X', twitter: 'X', reddit: 'Reddit',
  facebook: 'Facebook', telegram: 'Telegram', apple_music: 'Apple Music', news: 'News',
  threads: 'Threads', spotify: 'Spotify', shazam: 'Shazam', audiomack: 'Audiomack', boomplay: 'Boomplay',
  app_store: 'App Store', google_play: 'Google Play', gdelt: 'GDELT',
  linkedin: 'LinkedIn', snapchat: 'Snapchat', pinterest: 'Pinterest', twitch: 'Twitch', kick: 'Kick',
};
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const EXPLANATION_WORDS = {
  not_run: 'No explanation yet',
  failed_checks: 'Explanation held back: it did not pass the checks',
  held: 'Explanation held back',
};
const NOVELTY_WORDS = {new: 'New', recurrence: 'Recurrence', variant: 'Variant of an earlier trend', ongoing: 'Ongoing'};

export function longDate(value){
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  if (!match) return String(value || '');
  return Number(match[3]) + ' ' + MONTHS[Number(match[2]) - 1] + ' ' + match[1];
}

export function platformWord(platform){
  /* A platform with no word of its own reads as words, "some_feed" as
     "Some feed", never with the producer's underscore. */
  return PLATFORM_WORDS[platform] || sentenceCase(String(platform ?? '').replace(/_/g, ' '));
}

/* A Figure as words: "31 creators in 3 days", and "1 creator", "1 post"
   for one (tester report, 5 October 2026: "1 creators and 1 posts"). */
const COUNT_NOUNS = ['creators', 'posts'];
export function figureWords(figure){
  const unit = String(figure.unit || '').trim();
  const noun = Number(figure.value) === 1 ? COUNT_NOUNS.find((word) => unit === word || unit.startsWith(word + ' ')) : null;
  const shownUnit = noun ? noun.slice(0, -1) + unit.slice(noun.length) : unit;
  return readerFigure(figure.value) + (shownUnit ? ' ' + shownUnit : '');
}

/* A stored count line as a reader says it: briefs written before 6 October
   2026 say "1 creators and 1 posts in 3 days". "21 posts" keeps its plural. */
const ONE_COUNT = /(^|[^\d.,\u00a0\u202f])1 (creator|post)s\b/gu;
export function countLineWords(line){
  return typeof line === 'string' ? line.replace(ONE_COUNT, (whole, before, noun) => before + '1 ' + noun) : line;
}

/* Model prose names days as ISO dates ("on 2026-10-01"); a reader sees
   "1 Oct", with the year only when it is not this year. The stored text is
   unchanged; only what is shown reads as a date. */
const ISO_DAY = /\b(\d{4})-(\d{2})-(\d{2})\b(?![T:\d])/gu;
export function proseDates(text, now = Date.now()){
  if (typeof text !== 'string') return text;
  const thisYear = new Date(now).getUTCFullYear();
  return text.replace(ISO_DAY, (whole, year, month, day) => {
    const m = Number(month), d = Number(day);
    if (m < 1 || m > 12 || d < 1 || d > 31) return whole;
    return d + ' ' + MONTHS[m - 1].slice(0, 3) + (Number(year) === thisYear ? '' : ' ' + year);
  });
}

export function topicHref(itemId, market){
  return '#/t/' + encodeURIComponent(itemId) + '?market=' + encodeURIComponent(market);
}

export function askHref(card, market, date, question){
  return '#/ask?q=' + encodeURIComponent(question || '')
    + '&market=' + encodeURIComponent(market)
    + '&item=' + encodeURIComponent(card.item_id)
    + '&date=' + encodeURIComponent(date);
}

const has = (card, key) => Object.prototype.hasOwnProperty.call(card, key);
export const isFigure = (value) => value && typeof value === 'object' && value.value !== undefined && value.value !== null;
/* "31 creators, 3 days" and "31 creators in 3 days" say one fact: compare
   them with digit grouping, commas and the word "in" set aside. */
export const factWords = (value) => String(value).toLowerCase()
  .replace(/(\d)[\s\u00a0\u202f,](?=\d{3}\b)/gu, '$1')
  .replace(/,/g, ' ')
  .replace(/\bin\b/g, ' ')
  .replace(/\s+/g, ' ')
  .trim();
const sameWord = (a, b) => typeof a === 'string' && typeof b === 'string' && a.trim().toLowerCase() === b.trim().toLowerCase();
const YOUTUBE_CHANNEL_ID = /^UC[A-Za-z0-9_-]{22}$/i;
const isYoutubeChannelId = (value) => typeof value === 'string' && YOUTUBE_CHANNEL_ID.test(value.trim());
export const isYoutubeChannelAuthor = (item) => item.platform === 'youtube' && typeof item.handle === 'string'
  && isYoutubeChannelId(item.handle.trim().replace(/^@+/, ''));

/* Tester report, 6 October 2026: a card titled "northeast governors,
   northeast, governors" was about Independence Day reflections. An explained
   card whose brief wrote a title that passed its checks (title_written) leads
   with it, and the cluster label stays beside it as smaller text. A card
   without one, or whose written title only repeats the label, reads as before. */
const titleKey = (text) => String(text || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
export function writtenTitle(card, label){
  const written = card && card.explained === true && typeof card.title_written === 'string' ? card.title_written.trim() : '';
  return written && titleKey(written) !== titleKey(label) ? written : null;
}

/* The question an Ask link carries. The server words it from the cluster label, so a card that shows a written title asks about that title in the same sentence; a question that does not hold the label is left as the server wrote it. */
export function askQuestion(card, label, written){
  const base = typeof card.ask === 'string' && card.ask.trim() ? card.ask : '';
  if (!written) return base || card.title || '';
  return base ? (base.includes(label) ? base.split(label).join(written) : base) : written;
}

/* The posts behind a card, loaded when asked for and dropped when the card goes. */
export function useCardPosts(card, where, when, onAuth){
  const [posts, setPosts] = useState({state: 'closed'});
  const ctrl = useRef(null);
  useEffect(() => () => { if (ctrl.current) ctrl.current.abort(); }, []);
  const loadPosts = () => {
    if (ctrl.current) ctrl.current.abort();
    const c = new AbortController();
    ctrl.current = c;
    setPosts({state: 'loading'});
    fetchTrend(card.item_id, where, when, {signal: c.signal})
      .then((trend) => { if (!c.signal.aborted) setPosts({state: 'ready', evidence: trend.evidence || []}); })
      .catch((error) => {
        if (c.signal.aborted) return;
        if (error && error.auth){ setPosts({state: 'closed'}); if (onAuth) onAuth(); return; }
        setPosts({state: 'error'});
      });
  };
  const togglePosts = () => {
    if (posts.state !== 'closed'){
      if (ctrl.current) ctrl.current.abort();
      setPosts({state: 'closed'});
    } else loadPosts();
  };
  return {posts, open: posts.state !== 'closed', loadPosts, togglePosts};
}

/* scope: context nodes that belong in the facts row (Discover's search
   label). extraActions: links that sit with Ask, Watch and Posts. index: the
   card's place in its list, which the stylesheet turns into a capped entrance
   stagger. className: a class the list adds to the card itself. saidAbove:
   the not-yet states ('spread', 'growth') the page has already said once for
   every card, so this card does not repeat them; a field that has a value
   still shows. */
export function TrendCard({card, market, date, onAuth, onWatch, onFeedback, linkTopic = false, tapToOpen = false, posts: showPosts = true, todaySpecificity = null, scope = null, extraActions = null, index = null, className = '', sparkMinDays = MIN_CHART_DAYS, saidAbove = null}){
  const where = card.market || market;
  const when = card.date || date;
  const panelId = 't42-posts-' + where + '-' + card.item_id;
  const {posts, open, loadPosts, togglePosts} = useCardPosts(card, where, when, onAuth);
  const label = isYoutubeChannelId(card.title) ? 'YouTube channel' : card.title;
  const written = writtenTitle(card, label);
  const unnamed = !written && isUnnamedTopic(label);
  const title = written || (unnamed ? UNNAMED_TOPIC_WORDS : label);
  const question = askQuestion(card, label, written);

  const tag = TAG_WORDS[card.tag];
  const status = card.explanation_status || (card.explained ? 'explained' : 'failed_checks');
  const watching = Boolean(card.watch_id);
  const stateWord = card.state_word && !sameWord(card.state_word, card.lifecycle && card.lifecycle.step ? card.lifecycle.word : null)
    ? card.state_word : null;
  const hasMeta = Boolean(tag || stateWord || card.flag_word || card.news_driven === true);
  const hasLifecycle = Boolean(card.lifecycle && card.lifecycle.step);
  const said = (field) => Array.isArray(saidAbove) && saidAbove.includes(field);
  const showSpread = has(card, 'spread_line') && !(said('spread') && !card.spread_line);
  const showReach = has(card, 'reach');
  const showGrowth = has(card, 'growth') && !(said('growth') && !isFigure(card.growth));
  /* Reach is said once: a count line that says what the reach figure says
     gives way to the figure, which carries its query. */
  const countLine = countLineWords(card.count_line);
  const countSaysReach = Boolean(countLine) && isFigure(card.reach)
    && factWords(countLine) === factWords(figureWords(card.reach));
  /* Visual pass, 3 October 2026: the card's headline numbers are set large
     beside its trend line, so the figure is the first thing the eye lands
     on. A fact set large is not said again in the small print. */
  const big = bigFigures(card);
  const saysBig = (words) => big.some((figure) => factWords(figureWords(figure)) === factWords(words));
  const countShown = Boolean(countLine) && !countSaysReach && !saysBig(countLine);
  const reachShown = showReach && !(isFigure(card.reach) && big.includes(card.reach));

  /* Tester report, 5 October 2026: a tap anywhere on a Today card opens it,
     as the title link does. A tap on a control, a link, a disclosure or the
     open posts keeps its own job, and selecting text opens nothing. The
     title link stays the one keyboard stop, so keyboard reading is unchanged. */
  const openOnTap = tapToOpen && linkTopic ? (event) => {
    if (event.defaultPrevented || event.button > 0) return;
    const control = event.target.closest ? event.target.closest('a, button, input, select, textarea, label, summary, details, [role="button"], [tabindex], .t42-posts') : null;
    if (control && event.currentTarget.contains(control)) return;
    const selected = typeof window.getSelection === 'function' ? String(window.getSelection() || '') : '';
    if (selected) return;
    const link = event.currentTarget.querySelector('.tc-title-link');
    if (!link) return;
    if (event.metaKey || event.ctrlKey) window.open(link.href, '_blank', 'noopener');
    else link.click();
  } : undefined;

  return (
    <li className={'t42-card' + (openOnTap ? ' t42-card-tap' : '') + (className ? ' ' + className : '')} data-card="" style={Number.isInteger(index) ? {'--i': index} : undefined}
      onClick={openOnTap}>
      {/* Title first; the state words sit on the title's row as a quiet kicker. */}
      <div className="t42-card-head">
        <h3 className="t42-card-title">
          {linkTopic ? <a className="tc-title-link" href={topicHref(card.item_id, where)}>{title}</a> : title}
        </h3>
        {(written || unnamed) && <p className="t42-card-label" data-card-label="">{label}</p>}
        {(hasMeta || hasLifecycle) && (
          <div className="t42-card-kicker">
            {hasMeta && (
              <p className="t42-card-meta">
                {tag && <span className="t42-tag">{tag}</span>}
                {/* Demo polish, 2 October 2026 (QA item 14): the lifecycle marker
                    names the state, so the chip does not say the same word. */}
                {stateWord && <span className="t42-state" data-state={card.state || undefined}>{stateWord}</span>}
                {card.flag_word && <span className="t42-flag">{card.flag_word}</span>}
                {card.news_driven === true && <span className="t42-news">News-driven</span>}
              </p>
            )}
            {hasLifecycle ? <Lifecycle lifecycle={card.lifecycle} id={'tc-rule-' + where + '-' + card.item_id} /> : null}
          </div>
        )}
      </div>
      {todaySpecificity
        ? <TodaySpecificity specificity={todaySpecificity} date={when} />
        : status === 'explained'
        ? card.explanation ? <p className="t42-explanation">{proseDates(card.explanation)}</p> : null
        : <p className="t42-held-line">{EXPLANATION_WORDS[status] || EXPLANATION_WORDS.failed_checks}</p>}
      <div className="t42-card-metrics">
        {scope}
        {countShown && <p className="t42-count">{countLine}</p>}
        {showSpread && <p className="tc-spread">{card.spread_line || 'Spread is not measured yet'}</p>}
        <Novelty card={card} />
        {(reachShown || showGrowth) && (
          <p className="tc-figures">
            {reachShown && <Figure name="reach" label="Reach" figure={card.reach} missing="not measured yet" />}
            {showGrowth && <Figure name="growth" label="Growth" figure={card.growth} missing="needs 14 days of data" />}
          </p>
        )}
      </div>
      {/* The numbers, the trend line and the stills share one panel, which
          sits beside the words on a wide card and under them on a narrow
          one; an empty panel collapses. */}
      <div className="t42-card-aside">
        <BigFigures figures={big} reach={card.reach} reach7={isFigure(card.reach7) ? card.reach7 : null} />
        <div className="t42-card-media">
          <Sparkline sparkline={card.sparkline} minDays={sparkMinDays} />
          <Thumbnails card={card} />
        </div>
      </div>
      {/* One action region: Ask is the primary action; Watch, Posts and any
          extra links are quiet secondary actions of the same height. */}
      <div className="t42-card-foot">
        <div className="t42-actions">
          <AskAboutThis className="t42-action t42-action-primary" href={askHref(card, where, when, question)} question={question} />
          {onWatch
            ? <button type="button" className="t42-action" aria-pressed={watching ? 'true' : 'false'} onClick={() => onWatch(written ? {...card, title: written} : card, where)}>
                {watching ? 'Watching' : 'Watch'}
              </button>
            : watching && <span className="tc-watching">Watching</span>}
          {showPosts && <button type="button" className="t42-action" aria-expanded={open ? 'true' : 'false'} aria-controls={panelId} onClick={togglePosts}>Posts</button>}
          {extraActions}
        </div>
        {onFeedback && <Feedback card={card} title={title} market={where} date={when} onFeedback={onFeedback} onAuth={onAuth} />}
      </div>
      {showPosts && (
        <div id={panelId} className="t42-posts" aria-live="polite" aria-busy={posts.state === 'loading' ? 'true' : 'false'} hidden={!open}>
          {posts.state === 'loading' && <p className="t42-status">Loading posts</p>}
          {posts.state === 'error' && (
            <p className="t42-status">
              The posts could not load. <button type="button" className="t42-button" onClick={loadPosts}>Load again</button>
            </p>
          )}
          {posts.state === 'ready' && (posts.evidence.length > 0
            ? <><PostsShownNote shown={posts.evidence.length} numbers={card.numbers} /><EvidenceList items={posts.evidence} /></>
            : <p className="t42-status">No posts are stored for this trend.</p>)}
        </div>
      )}
    </li>
  );
}

/* The first day of a brief's 3-day counts ("31 creators in 3 days"): the
   brief day and the two before it, as YYYY-MM-DD, or null. */
export function countWindowStart(date){
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(date || ''));
  if (!match) return null;
  const day = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]) - 2));
  return Number.isFinite(day.getTime()) ? day.toISOString().slice(0, 10) : null;
}
export const BEFORE_COUNT = 'posted before the counted days';

/* Tester report, 5 October 2026: "1 creator in 3 days" sat above examples
   from two creators, one posted 29 September. The examples come from the
   brief's 7 days of posts and the counts from 3 days of trending boards and
   followed accounts, so the examples say their window and a post from before
   the counted days says so beside its date. Which posts are cited is the
   brief's; nothing here changes it. */
function TodaySpecificity({specificity, date}){
  return (
    <div className="t42-specificity" data-today-specificity="">
      <p className="t42-explanation t42-specificity-why-now">
        <span className="t42-specificity-label">Why now</span>
        {proseDates(specificity.whyNow)}
      </p>
      <blockquote className="t42-specificity-quote">
        <span className="t42-specificity-label">Quote</span>
        <p className="t42-post-text">{specificity.quote}</p>
      </blockquote>
      <div className="t42-specificity-examples" data-local-examples="">
        <p className="t42-specificity-label">Local examples</p>
        <p className="t42-specificity-window" data-examples-window="">From the last 7 days of posts. The counts above cover 3 days.</p>
        <EvidenceList items={specificity.examples} quoted={specificity.quote} countFrom={countWindowStart(date)} />
      </div>
    </div>
  );
}

const TAPS = [['real', 'Real'], ['not_real', 'Not real'], ['useful', 'Useful']];

/* One-tap feedback (contract.md section 10.6). It tells reviewers where to
   look and never feeds the trust numbers. A tap never waits on the one
   before it; the latest tap's answer is the one shown. */
export function Feedback({card, title, market, date, onFeedback, onAuth}){
  const [note, setNote] = useState({value: null, status: 'idle'});
  const latest = useRef(0);
  useEffect(() => () => { latest.current = -1; }, []);
  const tap = (value) => {
    const turn = ++latest.current;
    setNote({value, status: 'sending'});
    new Promise((resolve) => resolve(onFeedback({target: {kind: 'card', item_id: card.item_id, market, date}, value})))
      .then(() => { if (latest.current === turn) setNote({value, status: 'thanks'}); })
      .catch((error) => {
        if (latest.current !== turn) return;
        if (error && error.auth){ setNote({value: null, status: 'idle'}); if (onAuth) onAuth(); return; }
        setNote({value, status: 'error'});
      });
  };
  return (
    <div className="f42-feedback" role="group" aria-label={'Feedback on ' + title}>
      {/* A run-in label says what the taps answer, so the row reads as a
          question after the actions rather than a second cluster of them. */}
      <span className="f42-label">Was this right?</span>
      {TAPS.map(([value, label]) => (
        <button key={value} type="button" className="f42-tap" aria-pressed={note.status === 'thanks' && note.value === value ? 'true' : 'false'} onClick={() => tap(value)}>{label}</button>
      ))}
      <span className="f42-note" role="status">
        {note.status === 'thanks' ? 'Thanks' : note.status === 'error' ? 'That did not save. Tap again to retry.' : ''}
      </span>
    </div>
  );
}

/* The five-step neutral marker: Emerging, Rising, Peaking, Mainstream,
   Fading. Its rule shows on focus and hover and is its description. */
export function Lifecycle({lifecycle, id}){
  const step = Math.max(1, Math.min(5, Number(lifecycle.step) || 1));
  const rule = lifecycleRuleWords(lifecycle.rule);
  return (
    <p className="tc-lifecycle-row">
      <span
        className="tc-lifecycle"
        tabIndex={0}
        role="img"
        aria-label={'Lifecycle: ' + lifecycle.word + ', step ' + step + ' of 5'}
        aria-describedby={rule ? id : undefined}
      >
        <span className="tc-steps" aria-hidden="true">
          {[1, 2, 3, 4, 5].map((n) => <span key={n} className={'tc-step' + (n <= step ? ' tc-step-on' : '')} />)}
        </span>
        <span className="tc-word">{lifecycle.word}</span>
        {rule && <span className="tc-rule" id={id} role="tooltip">{rule}</span>}
      </span>
    </p>
  );
}

export function Novelty({card}){
  const word = NOVELTY_WORDS[card.novelty];
  if (!word) return null;
  const wave = card.novelty === 'recurrence' && card.last_wave && card.last_wave.peak_date ? card.last_wave : null;
  const peak = wave && isFigure(wave.peak_posts) ? ' at ' + figureWords(wave.peak_posts) : '';
  return (
    <p className="tc-novelty" data-query-id={wave && isFigure(wave.peak_posts) ? wave.peak_posts.query_id : undefined}>
      {wave ? word + ': the last wave peaked on ' + longDate(wave.peak_date) + peak : word}
    </p>
  );
}

/* The one or two figures a card sets large: its reach when it has one,
   else its first number, then a second number that says something else. */
export function bigFigures(card){
  const numbers = Array.isArray(card.numbers) ? card.numbers.filter(isFigure) : [];
  const lead = isFigure(card.reach) ? card.reach : numbers[0];
  if (!lead) return [];
  /* Two figures in one unit side by side would read as a contradiction, so
     the second must measure something else. */
  const unitOf = (figure) => String(figure.unit || '').trim().toLowerCase();
  const second = numbers.find((figure) => unitOf(figure) !== unitOf(lead));
  return second ? [lead, second] : [lead];
}

/* A big figure that is the card's reach keeps the reach field's name and
   words for a screen reader ("Reach: 31 creators in 3 days"), so the fact is
   said once, large, and still carries its query. */
/* reach7 (Discover, 5 October 2026): the creators over 7 days from every
   source, set beside the 3-day reach so a small measured count is not read
   as the whole picture. The 3-day reach counts only trending boards and
   followed accounts, which is what the quality checks use; both say so on
   hover and to a screen reader. */
export const REACH_MEASURED_NOTE = 'Counts only trending boards and followed accounts, which is what the quality checks use.';
export const REACH_EVERY_NOTE = 'Every collection lane 42 reads, over 7 days, with flagged accounts left out. The 3-day reach counts only trending boards and followed accounts, which is what the quality checks use.';

export function BigFigures({figures, reach, reach7 = null}){
  if (figures.length === 0 && !reach7) return null;
  const sevenValue = reach7 ? readerFigure(reach7.value) : '';
  return (
    <div className="tc-big">
      {figures.map((figure, index) => {
        const words = figureWords(figure);
        const value = readerFigure(figure.value);
        const isReach = figure === reach;
        const note = isReach && reach7 ? REACH_MEASURED_NOTE : '';
        return (
          <p key={index} className={'tc-big-item' + (index === 0 ? ' tc-big-lead' : '')}
            data-figure={isReach ? 'reach' : undefined} data-query-id={figure.query_id} title={'From query ' + figure.query_id + (note ? '. ' + note : '')}>
            {isReach ? <span className="sr-only">Reach: </span> : null}
            <span className="tc-big-value">{value}</span>
            <span className="tc-big-unit">{words.slice(value.length)}</span>
            {note ? <span className="sr-only">{' ' + note}</span> : null}
          </p>
        );
      })}
      {reach7 && (
        <p className={'tc-big-item' + (figures.length === 0 ? ' tc-big-lead' : '')} data-figure="reach7"
          data-query-id={reach7.query_id} title={'From query ' + reach7.query_id + '. ' + REACH_EVERY_NOTE}>
          <span className="sr-only">Reach from every source: </span>
          <span className="tc-big-value">{sevenValue}</span>
          <span className="tc-big-unit">{figureWords(reach7).slice(sevenValue.length) + ', every source'}</span>
          <span className="sr-only">{' ' + REACH_EVERY_NOTE}</span>
        </p>
      )}
    </div>
  );
}

export function Figure({name, label, figure, missing}){
  const present = isFigure(figure);
  return (
    <span className="tc-figure" data-figure={name} data-query-id={present ? figure.query_id : undefined}>
      {label + ': ' + (present ? figureWords(figure) : missing)}
    </span>
  );
}

/* A day per point. A null value is a day with no valid collection, so the
   line breaks there instead of dropping to zero. The expected band is drawn
   only over days that carry both edges (none in warm-up). */
const MIN_CHART_DAYS = 3;
const num = (v) => typeof v === 'number' && Number.isFinite(v);

/* A date's day and short month for the trend line's time scale, or nothing
   when the date cannot be read. */
function dayWord(date){
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(date || ''));
  const month = m ? MONTHS[Number(m[2]) - 1] : null;
  return month ? Number(m[3]) + ' ' + month.slice(0, 3) : '';
}

/* The date span of a series in words: "24 to 30 September", or with both
   months when it crosses one. */
function spanWords(first, last){
  const a = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(first || ''));
  const b = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(last || ''));
  if (!a || !b) return '';
  if (a[1] === b[1] && a[2] === b[2]) return Number(a[3]) + ' to ' + Number(b[3]) + ' ' + MONTHS[Number(b[2]) - 1];
  return longDate(first).replace(/ \d{4}$/, '') + ' to ' + longDate(last).replace(/ \d{4}$/, '');
}

/* With fewer than three measured days the line would be a lone dash at the
   edge, so the card says so in words instead. No series draws nothing.
   A drawn series always carries its own caption (unit, days, latest value
   and missing days), a zero baseline and a mark on each measured day, so a
   gap reads as a day not collected rather than a broken line. */
/* wide: the line fills the width of the panel it sits in. The drawing is laid
   out in that width's own pixels, so its dates and figures stay the size of the
   page's small type at any width, and its height is fixed, so the page does not
   move when the width is measured. Until it is measured the drawing is laid out
   at WIDE_WIDTH. */
const WIDE_WIDTH = 480;
const WIDE_HEIGHT = 112;
const WIDE_FOOT = 24;
function useMeasuredWidth(active){
  const holder = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = holder.current;
    if (!active || !el || typeof ResizeObserver !== 'function') return undefined;
    const read = () => {
      const next = Math.round(el.getBoundingClientRect().width);
      setWidth((now) => (next >= 200 && Math.abs(next - now) >= 2 ? next : now));
    };
    read();
    const watch = new ResizeObserver(read);
    watch.observe(el);
    return () => watch.disconnect();
  }, [active]);
  return [holder, width];
}

export function Sparkline({sparkline, minDays = MIN_CHART_DAYS, wide = false}){
  /* Night Desk, 4 October 2026: the line answers a pointer with the nearest
     measured day's figure (styles/nightdesk.css). The caption still carries
     the latest figure for readers who never point. */
  const [probe, setProbe] = useState(null);
  const [holder, fitted] = useMeasuredWidth(wide);
  /* A missing entry reads as a day with no value, never as a crash. */
  const points = sparkline && Array.isArray(sparkline.points) ? sparkline.points.map((p) => p || {}) : [];
  if (points.length === 0) return null;
  const measuredDays = points.filter((p) => p && num(p.value)).length;
  /* A list that asks for a longer series (Discover asks for 14 days) draws
     nothing until it has one, rather than a line too short to read. */
  if (minDays > MIN_CHART_DAYS && measuredDays < minDays) return null;
  if (measuredDays < MIN_CHART_DAYS) return <p className="t42-status t42-spark-empty">Not enough measured days yet</p>;
  /* Visual pass, 3 October 2026: the first and last day sit under the
     baseline, so the line has a time scale as well as a value scale. */
  const W = wide ? (fitted || WIDE_WIDTH) : 264, H = wide ? WIDE_HEIGHT : 64, pad = wide ? 8 : 4, gutter = wide ? 32 : 28, foot = wide ? WIDE_FOOT : 16;
  const peak = Math.max(1, ...points.flatMap((p) => [p.value, p.expected_high].filter(num)));
  const x = (i) => (points.length === 1 ? (gutter + W) / 2 : gutter + pad + (i * (W - gutter - 2 * pad)) / (points.length - 1));
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

  /* A quiet dotted bridge between the measured days either side of a gap. */
  const measured = points.map((p, i) => (num(p.value) ? i : null)).filter((i) => i !== null);
  const bridge = measured.slice(1).map((i, k) => [measured[k], i]).filter(([from, to]) => to - from > 1)
    .map(([from, to]) => 'M ' + fix(x(from)) + ' ' + fix(y(points[from].value)) + ' L ' + fix(x(to)) + ' ' + fix(y(points[to].value)))
    .join(' ');

  /* A soft fill under each run of measured days, so the shape reads at a
     glance; a gap is never filled. */
  const area = [];
  let runStart = null;
  points.forEach((p, i) => {
    const end = i === points.length - 1 || !num(points[i + 1] && points[i + 1].value);
    if (!num(p.value)){ runStart = null; return; }
    if (runStart === null) runStart = i;
    if (end && i > runStart){
      const top = [];
      for (let k = runStart; k <= i; k++) top.push(fix(x(k)) + ' ' + fix(y(points[k].value)));
      area.push('M ' + top.join(' L ') + ' L ' + fix(x(i)) + ' ' + fix(H - pad) + ' L ' + fix(x(runStart)) + ' ' + fix(H - pad) + ' Z');
    }
    if (end) runStart = null;
  });
  const areaPath = area.join(' ');

  const bands = [];
  let run = [];
  const flush = () => { if (run.length > 1) bands.push(run); run = []; };
  points.forEach((p, i) => {
    if (num(p.expected_low) && num(p.expected_high)) run.push(i); else flush();
  });
  flush();
  const bandPath = bands.map((r) => {
    const upper = r.map((i) => fix(x(i)) + ' ' + fix(y(points[i].expected_high)));
    const lower = r.slice().reverse().map((i) => fix(x(i)) + ' ' + fix(y(points[i].expected_low)));
    return 'M ' + upper.join(' L ') + ' L ' + lower.join(' L ') + ' Z';
  }).join(' ');

  const values = points.map((p) => p.value).filter(num);
  const missing = points.length - values.length;
  const span = spanWords(points[0] && points[0].date, points[points.length - 1] && points[points.length - 1].date);
  const caption = [
    sentenceCase(sparkline.unit || 'value') + (span ? ', ' + span : ', last ' + points.length + ' days'),
    values.length ? 'latest ' + seriesFigure(values[values.length - 1]) : null,
    missing > 0 ? (missing === 1 ? '1 day not collected' : missing + ' days not collected') : null,
  ];
  const onProbe = (event) => {
    const box = event.currentTarget.getBoundingClientRect();
    const at = box.width > 0 ? ((event.clientX - box.left) * W) / box.width : event.clientX;
    let best = null;
    for (const i of measured) if (best === null || Math.abs(x(i) - at) < Math.abs(x(best) - at)) best = i;
    setProbe(best);
  };
  const probeWords = probe === null ? '' : dayWord(points[probe].date) + ': ' + seriesFigure(points[probe].value);
  const probeX = probe === null ? 0 : Math.min(W - 2, Math.max(gutter + 2, x(probe)));
  return (
    <figure className="t42-trend" ref={holder}>
      <figcaption className="t42-trend-caption"><Facts parts={caption} /></figcaption>
      <svg className="t42-spark" viewBox={'0 0 ' + W + ' ' + (H + foot)} width={wide ? '100%' : W} height={H + foot} aria-hidden="true" data-wide={wide ? '' : undefined} preserveAspectRatio={wide ? "xMinYMid meet" : undefined}
        onPointerMove={onProbe} onPointerDown={onProbe} onPointerLeave={() => setProbe(null)}>
        {/* The caption above says what the line shows, so the drawing is not read twice. */}
        {/* The scale: zero on the baseline and the top of the range above it. */}
        <text className="t42-tick" x={gutter - 6} y={H - pad} textAnchor="end" dominantBaseline="middle" aria-hidden="true">0</text>
        <text className="t42-tick" x={gutter - 6} y={pad} textAnchor="end" dominantBaseline="middle" aria-hidden="true">{seriesFigure(peak)}</text>
        <line className="t42-axis" x1={gutter} y1={H - pad} x2={W} y2={H - pad} />
        {dayWord(points[0].date) && <text className="t42-tick t42-tick-day" x={x(0)} y={H + foot - 3} textAnchor="start">{dayWord(points[0].date)}</text>}
        {points.length > 1 && dayWord(points[points.length - 1].date) && <text className="t42-tick t42-tick-day" x={W} y={H + foot - 3} textAnchor="end">{dayWord(points[points.length - 1].date)}</text>}
        {bandPath && <path className="t42-band" d={bandPath} />}
        {areaPath && <path className="t42-area" d={areaPath} />}
        {bridge && <path className="t42-gap" d={bridge} fill="none" />}
        {line && <path className="t42-line" d={line} fill="none" strokeLinecap="round" strokeLinejoin="round" pathLength="1" />}
        {measured.map((i, k) => <circle key={i} className={k === measured.length - 1 ? 't42-dot t42-dot-now' : 't42-dot'} cx={fix(x(i))} cy={fix(y(points[i].value))} r={k === measured.length - 1 ? '3.5' : '2'} />)}
        {wide && measured.length > 0 && <text className="t42-now-label" x={fix(Math.min(W - 2, x(measured[measured.length - 1])))} y={fix(Math.max(pad + 10, y(points[measured[measured.length - 1]].value) - 10))} textAnchor="end">{seriesFigure(points[measured[measured.length - 1]].value)}</text>}
        {probe !== null && (
          <g className="t42-probe">
            <line className="t42-probe-rule" x1={fix(x(probe))} x2={fix(x(probe))} y1={pad} y2={H - pad} />
            <text className="t42-probe-words" x={fix(probeX)} y={pad + 8} textAnchor={x(probe) > W * 0.6 ? 'end' : 'start'} dx={x(probe) > W * 0.6 ? -6 : 6}>{probeWords}</text>
          </g>
        )}
      </svg>
    </figure>
  );
}

/* Up to two stills. A post with no usable image, or one that fails to load,
   leaves no frame behind: an empty box says nothing a reader can use. */
export function Thumbnails({card}){
  const [failed, setFailed] = useState([]);
  const ids = (card.thumbnails || []).slice(0, 2);
  const evidence = card.evidence || [];
  const stills = ids.map((id) => {
    const post = evidence.find((e) => e.id === id);
    const src = post && safeUrl(post.thumbnail_url);
    if (!src || failed.includes(src)) return null;
    const label = isYoutubeChannelAuthor(post) ? 'Post on YouTube, channel name unavailable'
      : post.handle ? 'Post by ' + post.handle + ' on ' + platformWord(post.platform)
      : 'Post on ' + platformWord(post.platform);
    return {id, src, label};
  }).filter(Boolean);
  if (stills.length === 0) return null;
  return (
    <div className="t42-thumbs">
      {stills.map(({id, src, label}) => (
        <img key={id} className="t42-thumb" src={src} alt={label} width="72" height="96" loading="lazy"
          onError={() => setFailed((current) => current.includes(src) ? current : [...current, src])} />
      ))}
    </div>
  );
}

const sameText = (a, b) => typeof a === 'string' && typeof b === 'string'
  && a.replace(/\s+/g, ' ').trim() === b.replace(/\s+/g, ' ').trim();

/* Post text that was cut never ends mid-word without a mark (audit TOD-01
   and TOP-01, 4 October 2026). Stored evidence keeps text as the first 280
   characters of a post and, in the brief pack, quote_text as the whole post.
   When the whole post is longer than the preview, the preview ends at a
   whole word with an ellipsis and a "Read full post" disclosure shows every
   stored word. When only a cut preview is stored, the preview keeps its
   words, gains an ellipsis, and a note says the full text is on the post.
   excerptAtLeast: true for the topic read, whose excerpt has no flag, so 280
   characters or more counts as cut; Today counts exactly 280. */
export const EXCERPT_CHARS = 280;
export const EXCERPT_NOTE = 'Excerpt; the full text is on the post';
const charCount = (value) => [...value].length;
const storedText = (value) => typeof value === 'string' && value.trim() !== '' ? value : '';

function wordPreview(text, full){
  const cutInsideWord = /\S$/u.test(text)
    && (!full.startsWith(text) || /\S/u.test(full.charAt(text.length)));
  let shown = text;
  if (cutInsideWord){
    const lastSpace = text.search(/\s\S*$/u);
    if (lastSpace > 0) shown = text.slice(0, lastSpace);
  }
  return shown.trimEnd() + '\u2026';
}

export function postTextView(item, {excerptAtLeast = false} = {}){
  const text = storedText(item && item.text);
  const full = storedText(item && item.quote_text);
  if (!text) return {preview: full, full: '', shortened: false, excerpt: false};
  if (full && charCount(full.trim()) > charCount(text.trim())){
    return {preview: wordPreview(text, full), full, shortened: true, excerpt: false};
  }
  const length = charCount(text);
  const cut = !full && (excerptAtLeast ? length >= EXCERPT_CHARS : length === EXCERPT_CHARS);
  return cut
    ? {preview: text.trimEnd() + '\u2026', full: '', shortened: true, excerpt: true}
    : {preview: text, full: '', shortened: false, excerpt: false};
}

/* The whole stored post behind a native disclosure, so Enter or Space opens
   it. Blank lines in the post stay paragraph breaks. */
export function PostFull({text}){
  const paragraphs = text.split(/\n[^\S\n]*\n\s*/u).map((part) => part.trim()).filter(Boolean);
  return (
    <details className="t42-post-full">
      <summary className="t42-disclose">Read full post</summary>
      <div className="t42-post-full-text" data-post-full="">
        {paragraphs.map((part, index) => <p key={index}>{part}</p>)}
      </div>
    </details>
  );
}

export function ExcerptNote(){
  return <span className="t42-post-note" data-excerpt-note="">{EXCERPT_NOTE}</span>;
}

/* quoted: the quote the card already shows above the list. A post whose
   text is that quote word for word keeps its source line and link, and says
   "Quoted above" instead of printing the same words twice. */
/* Tester report, 5 October 2026: "21 posts in 3 days" sat over 12 posts
   with nothing saying the list was a sample. The brief keeps a capped set of
   example posts from its 7 days, so when the figures count more posts than
   the list holds, the list says it does not show them all. */
export function postsShownWords(shown, numbers){
  const counted = (Array.isArray(numbers) ? numbers : []).find((number) => isFigure(number)
    && typeof number.unit === 'string' && /^posts in \S/.test(number.unit.trim()));
  if (!counted || !Number.isFinite(Number(counted.value)) || Number(counted.value) <= shown) return null;
  const window = counted.unit.trim().slice('posts '.length);
  return 'Showing ' + readerFigure(shown) + (shown === 1 ? ' example post' : ' example posts')
    + ', not all ' + readerFigure(counted.value) + ' posts counted ' + window + '.';
}

export function PostsShownNote({shown, numbers}){
  const words = postsShownWords(shown, numbers);
  return words ? <p className="t42-status t42-posts-shown" data-posts-shown="">{words}</p> : null;
}

/* countFrom: the first counted day (YYYY-MM-DD); a post dated before it
   says "posted before the counted days" after its date. The counts go by the
   day a trend was first seen and the label by the day a post went up, so it
   names only what the post's own date shows. */
export function EvidenceList({items, quoted = null, countFrom = null}){
  return (
    <ul className="t42-evidence">
      {items.map((e) => {
        const views = e.engagement && typeof e.engagement.views === 'number' ? e.engagement.views : null;
        const youtubeChannelAuthor = isYoutubeChannelAuthor(e);
        const postedDay = /^\d{4}-\d{2}-\d{2}/.test(String(e.posted_at || '')) ? String(e.posted_at).slice(0, 10) : null;
        const beforeCount = Boolean(countFrom && postedDay && postedDay < countFrom);
        const meta = [youtubeChannelAuthor ? 'YouTube channel' : platformWord(e.platform), youtubeChannelAuthor ? null : e.handle, e.posted_at ? longDate(e.posted_at) : null, beforeCount ? BEFORE_COUNT : null, views !== null ? readerFigure(views) + ' views' : null];
        const url = safeUrl(e.url);
        const view = postTextView(e);
        return (
          <li key={e.id} className="t42-post" data-evidence-id={e.id}>
            <p className="t42-post-meta"><Facts parts={meta} /></p>
            {view.preview && (sameText(storedText(e.text) || view.preview, quoted)
              ? <p className="t42-post-quoted" data-quoted-above="">Quoted above</p>
              : <p className="t42-post-text" data-shortened={view.shortened ? '' : undefined}>{view.preview}</p>)}
            {view.full && <PostFull text={view.full} />}
            {url && <a className="t42-link" href={url} target="_blank" rel="noopener noreferrer">Open the post</a>}
            {view.excerpt && <ExcerptNote />}
          </li>
        );
      })}
    </ul>
  );
}
