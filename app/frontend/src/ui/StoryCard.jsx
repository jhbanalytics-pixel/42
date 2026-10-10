/* The Today story card: one trend as a full-width card on the 12 column grid.
   The story is on the left (rank, headline, badges, why now, the quote as a
   pull quote, then the real example posts as tiles, two side by side and a
   swipeable strip on a phone), a live panel is on the right (creators and
   posts in the counted window, the posts a day chart, the platforms of the
   evidence, momentum when the payload sends it), and the actions and the
   feedback taps close the card across both.
   Only fields the Today payload sends are drawn; a field that is missing
   shrinks its zone and never leaves a blank box. The page reads the same
   facts as the plain TrendCard, so nothing here is a new claim. */
import {useMemo} from 'react';
import {Facts} from './Facts.jsx';
import {AskAboutThis} from './AskAboutThis.jsx';
import {PlatformLogo, brandOf, brandStyle, platformId} from './PlatformLogo.jsx';
import {SnapBand} from './SnapBand.jsx';
import {
  BEFORE_COUNT, BigFigures, Feedback, Figure, Lifecycle, Novelty, PostFull, PostsShownNote, EvidenceList, ExcerptNote, Sparkline, Thumbnails,
  accountsCard, askHref, askQuestion, bigFigures, countLineWords, countWindowStart, factWords, figureWords, isFigure, isYoutubeChannelAuthor, longDate, platformWord,
  postTextView, proseDates, topicHref, useCardPosts, writtenTitle,
} from './TrendCard.jsx';
import {UNNAMED_TOPIC_WORDS, isUnnamedTopic} from '../topicNames.js';
import {readerFigure} from '../api.js';
import {safeUrl} from '../safeUrl.js';
import '../styles/today-story.css';

const NOVELTY_WORDS = {new: 'New', recurrence: 'Recurrence', variant: 'Variant of an earlier trend', ongoing: 'Ongoing'};
const TAG_WORDS = {first_time: 'First time on Today', moved_up: 'Moved up', held_place: 'Held its place'};
const sameText = (a, b) => typeof a === 'string' && typeof b === 'string'
  && a.replace(/\s+/g, ' ').trim() === b.replace(/\s+/g, ' ').trim();
const text = (value) => (typeof value === 'string' && value.trim() !== '' ? value : '');

/* The creators the card counts in its window: the reach figure or the number
   whose unit is creators. null when the payload carries no such figure, so a
   card is only held back for a count that was sent and is zero. */
export function countedCreators(card){
  if (!card || typeof card !== 'object') return null;
  const figures = [card.reach, ...(Array.isArray(card.numbers) ? card.numbers : [])].filter(isFigure);
  const found = figures.find((figure) => /^(creators\b|accounts? posting\b)/i.test(String(figure.unit || '').trim()));
  return found ? Number(found.value) : null;
}

/* The question, the title the reader sees and the Ask address for one card. */
export function cardWords(card, market, date){
  const where = card.market || market;
  const when = card.date || date;
  const label = card.title;
  const written = writtenTitle(card, label);
  const unnamed = !written && isUnnamedTopic(label);
  const title = written || (unnamed ? UNNAMED_TOPIC_WORDS : label);
  const question = askQuestion(card, label, written);
  return {where, when, label, written, unnamed, title, question, href: askHref(card, where, when, question)};
}

/* The platforms of a card's evidence in the order they first appear, each with
   its official mark and its name as text. attribute names the data attribute
   a reader or a test finds one by. */
export function PlatformMarks({evidence, attribute, className = ''}){
  const seen = [];
  for (const item of Array.isArray(evidence) ? evidence : []){
    const id = item && typeof item.platform === 'string' && item.platform.trim() !== '' ? platformId(item.platform) : null;
    if (id && !seen.includes(id)) seen.push(id);
  }
  if (seen.length === 0) return null;
  return (
    <ul className={'sc-platforms' + (className ? ' ' + className : '')} data-platforms="" aria-label="Platforms of the evidence">
      {seen.map((id) => {
        const brand = brandOf(id);
        return (
          <li key={id} className="sc-platform" {...{[attribute]: id}} style={brand ? brandStyle(brand) : undefined}>
            <span className="sc-platform-mark"><PlatformLogo platform={id} size={16} /></span>
            <span className="sc-platform-name">{platformWord(id)}</span>
          </li>
        );
      })}
    </ul>
  );
}

function Tile({item, quoted, countFrom}){
  const views = item.engagement && typeof item.engagement.views === 'number' ? item.engagement.views : null;
  const channel = isYoutubeChannelAuthor(item);
  const postedDay = /^\d{4}-\d{2}-\d{2}/.test(String(item.posted_at || '')) ? String(item.posted_at).slice(0, 10) : null;
  const before = Boolean(countFrom && postedDay && postedDay < countFrom);
  const when = item.posted_at ? longDate(item.posted_at) : null;
  const meta = [channel ? 'YouTube channel' : platformWord(item.platform), channel ? null : item.handle, when, before ? BEFORE_COUNT : null, views !== null ? readerFigure(views) + ' views' : null];
  const url = safeUrl(item.url);
  const view = postTextView(item);
  const who = channel ? 'YouTube channel' : text(item.handle) || platformWord(item.platform);
  const quotedAbove = view.preview && sameText(text(item.text) || view.preview, quoted);
  return (
    <li className="t42-post sc-tile" data-evidence-id={item.id}>
      <p className="t42-post-meta sr-only"><Facts parts={meta} /></p>
      <div className="sc-tile-head">
        <span className="sc-tile-mark" aria-hidden="true"><PlatformLogo platform={item.platform} size={18} /></span>
        <span className="sc-tile-who" aria-hidden="true">{who}</span>
        {quotedAbove && <span className="sc-tile-tag" data-quoted="">quoted</span>}
      </div>
      <p className="sc-tile-when" aria-hidden="true">{[when, views !== null ? readerFigure(views) + ' views' : null].filter(Boolean).join(' · ')}</p>
      {view.preview && <p className="t42-post-text sc-tile-text" data-shortened={view.shortened ? '' : undefined}>{view.preview}</p>}
      {view.full && <PostFull text={view.full} />}
      {before && <p className="sc-tile-note" aria-hidden="true">{BEFORE_COUNT}</p>}
      {url && <a className="t42-link sc-tile-open" href={url} target="_blank" rel="noopener noreferrer">Open the post</a>}
      {view.excerpt && <ExcerptNote />}
    </li>
  );
}

function Strip({specificity, date, title}){
  const examples = specificity.examples;
  const countFrom = countWindowStart(date);
  return (
    <div className="sc-evidence" data-local-examples="">
      <SnapBand label={'Example posts for ' + title} prevLabel="Show earlier posts" nextLabel="Show later posts" itemSelector=".sc-tile" className="sc-strip"
        before={(
          <div className="sc-strip-head">
            <p className="t42-specificity-label sc-strip-title">Local examples</p>
            <p className="t42-specificity-window sc-strip-window" data-examples-window="">From the last 7 days of posts. The counts above cover 3 days.</p>
          </div>
        )}>
        <ul className="t42-evidence sc-tiles">
          {examples.map((item) => <Tile key={item.id} item={item} quoted={specificity.quote} countFrom={countFrom} />)}
        </ul>
      </SnapBand>
    </div>
  );
}

const hasChart = (sparkline) => Boolean(sparkline && Array.isArray(sparkline.points) && sparkline.points.length > 0);

export function StoryCard({card: stored, market, date, onAuth, onWatch, onFeedback, todaySpecificity = null, index = null, className = ''}){
  /* The card is read through its measured accounts figure, as Discover does. */
  const card = useMemo(() => accountsCard(stored), [stored]);
  const words = cardWords(card, market, date);
  const {where, when, label, written, unnamed, title, question, href} = words;
  const {posts, open, loadPosts, togglePosts} = useCardPosts(card, where, when, onAuth);
  /* A card whose counted creators is 0 says nothing a reader can use, so it is
     never drawn, whatever reached the page. */
  if (countedCreators(card) === 0) return null;
  const panelId = 't42-posts-' + where + '-' + card.item_id;
  const tag = TAG_WORDS[card.tag];
  const watching = Boolean(card.watch_id);
  const momentum = text(card.state_word) || (card.lifecycle && text(card.lifecycle.word)) || '';
  const hasLifecycle = Boolean(card.lifecycle && card.lifecycle.step);
  const big = bigFigures(card);
  const reach7 = isFigure(card.reach7) ? card.reach7 : null;
  const chart = hasChart(card.sparkline);
  /* The facts the large figures do not already say: a count line that adds to them, spread, novelty, reach and growth. */
  const countLine = countLineWords(card.count_line);
  const countSaysReach = Boolean(countLine) && isFigure(card.reach) && factWords(countLine) === factWords(figureWords(card.reach));
  const saysBig = (words) => big.some((figure) => factWords(figureWords(figure)) === factWords(words));
  const countShown = Boolean(countLine) && !countSaysReach && !saysBig(countLine);
  const showSpread = Object.prototype.hasOwnProperty.call(card, 'spread_line');
  const showReach = Object.prototype.hasOwnProperty.call(card, 'reach') && !(isFigure(card.reach) && big.includes(card.reach));
  const showGrowth = Object.prototype.hasOwnProperty.call(card, 'growth');
  const facts = countShown || showSpread || showReach || showGrowth || Boolean(NOVELTY_WORDS[card.novelty]);
  const rank = String((Number.isInteger(index) ? index : 0) + 1).padStart(2, '0');
  const badges = [
    tag ? <span key="tag" className="sc-badge sc-badge-tag">{tag}</span> : null,
    card.flag_word ? <span key="flag" className="sc-badge">{card.flag_word}</span> : null,
    card.news_driven === true ? <span key="news" className="sc-badge">News-driven</span> : null,
  ].filter(Boolean);

  /* A tap anywhere on the card opens it, as the title link does. A control, a
     link, the strip of posts or a disclosure keeps its own job, and selecting
     text opens nothing. The title link stays the one keyboard stop. */
  const openOnTap = (event) => {
    if (event.defaultPrevented || event.button > 0) return;
    const control = event.target.closest ? event.target.closest('a, button, input, select, textarea, label, summary, details, [role="button"], [tabindex], .t42-posts') : null;
    if (control && event.currentTarget.contains(control)) return;
    const selected = typeof window.getSelection === 'function' ? String(window.getSelection() || '') : '';
    if (selected) return;
    const link = event.currentTarget.querySelector('.tc-title-link');
    if (!link) return;
    if (event.metaKey || event.ctrlKey) window.open(link.href, '_blank', 'noopener');
    else link.click();
  };

  return (
    <li className={'t42-card t42-card-tap sc-card' + (className ? ' ' + className : '')} data-card="" data-story="" style={Number.isInteger(index) ? {'--i': index} : undefined} onClick={openOnTap}>
      <div className="t42-specificity sc-body" data-today-specificity="">
        <div className="sc-story">
          <div className="sc-top">
            <span className="sc-rank" aria-hidden="true" data-lead={index === 0 ? '' : undefined}>{rank}</span>
            <div className="sc-heading">
              <h3 className="t42-card-title sc-title">
                <a className="tc-title-link" href={topicHref(card.item_id, where)}>{title}</a>
              </h3>
              {(written || unnamed) && <p className="t42-card-label sc-label" data-card-label="">{label}</p>}
            </div>
          </div>
          {badges.length > 0 && <p className="sc-badges">{badges}</p>}
          {todaySpecificity && (
            <>
              <p className="t42-explanation t42-specificity-why-now sc-why">
                <span className="t42-specificity-label">Why now</span>
                {proseDates(todaySpecificity.whyNow)}
              </p>
              <blockquote className="t42-specificity-quote sc-pull">
                <span className="t42-specificity-label">Quote</span>
                <p className="t42-post-text">{todaySpecificity.quote}</p>
              </blockquote>
            </>
          )}
          {todaySpecificity && todaySpecificity.examples.length > 0 && <Strip specificity={todaySpecificity} date={when} title={title} />}
        </div>
        <div className="sc-live">
          <BigFigures figures={big} reach={card.reach} reach7={reach7} />
          {facts && (
            <div className="t42-card-metrics sc-facts">
              {countShown && <p className="t42-count">{countLine}</p>}
              {showSpread && <p className="tc-spread">{card.spread_line || 'Spread is not measured yet'}</p>}
              <Novelty card={card} />
              {(showReach || showGrowth) && (
                <p className="tc-figures">
                  {showReach && <Figure name="reach" label="Reach" figure={card.reach} missing="not measured yet" />}
                  {showGrowth && <Figure name="growth" label="Growth" figure={card.growth} missing="needs 14 days of data" />}
                </p>
              )}
            </div>
          )}
          {momentum && (
            <p className="sc-momentum-row">
              <span className="sc-momentum" data-momentum="" data-state={card.state || undefined}>{momentum}</span>
            </p>
          )}
          {hasLifecycle && <Lifecycle lifecycle={card.lifecycle} id={'tc-rule-' + where + '-' + card.item_id} />}
          {chart && <div className="sc-chart"><Sparkline sparkline={card.sparkline} wide fill /></div>}
          <Thumbnails card={card} />
          <PlatformMarks evidence={card.evidence} attribute="data-card-platform" />
        </div>
      </div>
      <div className="t42-card-foot sc-foot">
        <div className="t42-actions">
          <AskAboutThis className="t42-action t42-action-primary" href={href} question={question} />
          {onWatch
            ? <button type="button" className="t42-action" aria-pressed={watching ? 'true' : 'false'} onClick={() => onWatch(written ? {...card, title: written} : card, where)}>
                {watching ? 'Watching' : 'Watch'}
              </button>
            : watching && <span className="tc-watching">Watching</span>}
          <button type="button" className="t42-action" aria-expanded={open ? 'true' : 'false'} aria-controls={panelId} onClick={togglePosts}>Posts</button>
        </div>
        {onFeedback && <Feedback card={card} title={title} market={where} date={when} onFeedback={onFeedback} onAuth={onAuth} />}
      </div>
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
    </li>
  );
}
