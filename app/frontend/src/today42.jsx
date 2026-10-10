/* Today on the 42 API: the morning's key trends per market, explained and
   proven (docs/full-42/EXPERIENCE.md, Today; core/api/contract.md section 4).
   Every market's cards, what left the list, what was held back and why,
   and what was collected, so nothing disappears without a line saying so.
   The app passes the alerts read and the watch and feedback writes (contract
   section 10.5 and 10.6), the finished investigations read (section 13.1)
   and the scheduled questions read (section 14.2); without them this is
   the Stage 1 page. */
import {headlineParts} from './nightdesk.js';
import {createContext, useContext, useEffect, useRef, useState} from 'react';
import {fetchToday, scheduledRunWords} from './api42.js';
import {readerFigure} from './api.js';
import {snapshotTime} from './plainLabels.js';
import {safeUrl} from './safeUrl.js';
import {SearchingNow} from './ui/SearchingNow.jsx';
import {TodayBoards} from './ui/TodayBoards.jsx';
import {EvidenceList, PostsShownNote, countLineWords, longDate, proseDates, topicHref} from './ui/TrendCard.jsx';
import {StoryCard, countedCreators} from './ui/StoryCard.jsx';
import {LeadPanel} from './ui/LeadPanel.jsx';
import {PartsBar, StepMeter} from './ui/Charts42.jsx';
import {FigureLine} from './ui/FigureLine.jsx';
import {Facts} from './ui/Facts.jsx';
import {MARKET_WORDS, heldWords, useCardWatch} from './ui/WatchDialog.jsx';
import './styles/today42.css';
import './styles/today-story.css';
import './styles/alerts42.css';

const MARKETS = ['ZA', 'NG', 'KE'];
const TABS = [
  {id: 'ZA', label: 'South Africa'},
  {id: 'NG', label: 'Nigeria'},
  {id: 'KE', label: 'Kenya'},
  {id: 'ALL', label: 'All'},
];
const FINISHED_DAYS = 7;
const TODAY_SLOW_READ_MS = 5000;
const MOMENT_WORDS = {holiday: 'Holiday', festival: 'Festival', fixture: 'Fixture', release: 'Release', seasonal_item: 'Seasonal'};

/* The SAST day, which is the day Today is published for. */
function sastToday(){
  return new Date(Date.now() + 2 * 3600000).toISOString().slice(0, 10);
}

/* The header line from run_receipt (contract section 4), or null when any
   count is missing or not a whole number, so the page never guesses one. */
function runReceiptLine(receipt){
  if (!receipt || typeof receipt !== 'object') return null;
  const {posts, shown, held, markets} = receipt;
  const whole = (n) => Number.isInteger(n) && n >= 0;
  if (![posts, shown, held].every(whole) || !Array.isArray(markets) || markets.length === 0
    || !markets.every((m) => typeof m === 'string' && m.trim())) return null;
  const grouped = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const count = (n, one, many) => `${grouped(n)} ${n === 1 ? one : many}`;
  const where = markets.length === 1 ? markets[0] : `${markets.slice(0, -1).join(', ')} and ${markets[markets.length - 1]}`;
  const heldPart = held === 0 ? 'held none back' : `held ${grouped(held)} back with reasons`;
  return `42 read ${count(posts, 'post', 'posts')} for ${where}, showed ${count(shown, 'trend', 'trends')} and ${heldPart}.`;
}

/* The heading ends in a date ("Taking off, 30 September 2026"). The date is
   one thought, so it never splits across lines: a narrow screen breaks before
   it, never inside it. The text reads exactly as the API sent it. */
const HEADING_DATE = /^(.*?)(\d{1,2} [A-Z][a-z]+ \d{4})(.*)$/su;
function headingWords(text){
  const match = typeof text === 'string' ? HEADING_DATE.exec(text) : null;
  if (!match) return text;
  return <>{match[1]}<span className="t42-nowrap">{match[2]}</span>{match[3]}</>;
}

/* A market with a real data problem: no brief, a failed stage, failed
   sources or invalid days (status data_issue, or a data_issue banner). An
   explanation that failed its checks is not one. */
function hasDataIssue(market){
  return Boolean(market) && (market.status === 'data_issue'
    || (Array.isArray(market.banners) && market.banners.some((banner) => banner && banner.kind === 'data_issue')));
}

function hasIncompleteRun(market){
  return hasDataIssue(market) || Boolean(market && Array.isArray(market.held_back?.items)
    && market.held_back.items.some((item) => item?.reason === 'explanation_failed'
      && typeof item.failed_reason === 'string' && item.failed_reason.startsWith('Model busy:')));
}

/* The day the brief is worded for: 'today' for the current SAST day, otherwise 'on 30 September 2026'. */
const BriefDay = createContext('today');
const briefDayWords = (briefDate) => (briefDate && briefDate < sastToday() ? 'on ' + longDate(briefDate) : 'today');

const andJoin = (names) => (names.length > 1 ? names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1] : names.join(''));

function tabForRegion(region){
  const value = String(region || '').toUpperCase();
  return MARKETS.includes(value) ? value : 'ALL';
}

const nonEmptyString = (value) => typeof value === 'string' && value.trim().length > 0;
const WORD_CHARACTER = /^[\p{L}\p{N}_]$/u;

function normalizeQuoteText(value){
  return value.normalize('NFKC')
    .replace(/[\u2018-\u201b]/gu, "'")
    .replace(/[\u201c-\u201f]/gu, '"')
    .replace(/\s+/gu, ' ')
    .trim();
}

function hasWordBoundedOccurrence(source, quote){
  const quoteCharacters = [...quote];
  const needsStartBoundary = WORD_CHARACTER.test(quoteCharacters[0]);
  const needsEndBoundary = WORD_CHARACTER.test(quoteCharacters[quoteCharacters.length - 1]);
  let start = source.indexOf(quote);
  while (start !== -1){
    const before = [...source.slice(0, start)].at(-1);
    const after = [...source.slice(start + quote.length)][0];
    if ((!needsStartBoundary || !WORD_CHARACTER.test(before || ''))
      && (!needsEndBoundary || !WORD_CHARACTER.test(after || ''))) return true;
    start = source.indexOf(quote, start + 1);
  }
  return false;
}

function quoteMatchesSource(source, quote){
  if (!source.includes(quote)) return false;
  return hasWordBoundedOccurrence(normalizeQuoteText(source), normalizeQuoteText(quote));
}

function usableLocalEvidence(item){
  return Boolean(item && (
    nonEmptyString(item.text)
    || nonEmptyString(item.quote_text)
    || safeUrl(item.url)
  ));
}

/* The server counts a quote's words with Python str.split(), which also splits
   on U+001C to U+001F and U+0085. JavaScript's \s does not, so the same quote
   would count one word here and two there. */
const SERVER_SPACE = /[\t-\r\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/u;

function todaySpecificityView(card){
  if (!card || typeof card !== 'object' || Array.isArray(card)
    || card.explained !== true || !nonEmptyString(card.explanation)
    || (card.explanation_status != null && card.explanation_status !== 'explained')) return null;

  const specificity = card.specificity;
  if (!specificity || typeof specificity !== 'object' || Array.isArray(specificity)
    || specificity.status !== 'pass' || specificity.reason !== null
    || specificity.why_now !== card.explanation.trim()
    || !Array.isArray(specificity.local_evidence_ids)
    || !specificity.local_evidence_ids.every(nonEmptyString)
    || !specificity.quote || typeof specificity.quote !== 'object' || Array.isArray(specificity.quote)
    || !nonEmptyString(specificity.quote.evidence_id) || !nonEmptyString(specificity.quote.text)) return null;

  const explanationClaimIds = card.explanation_claim_ids;
  if (!Array.isArray(explanationClaimIds) || explanationClaimIds.length === 0
    || !explanationClaimIds.every(nonEmptyString) || !Array.isArray(card.claims) || card.claims.length === 0) return null;

  const claimsById = new Map();
  for (const claim of card.claims){
    if (!claim || typeof claim !== 'object' || Array.isArray(claim) || !nonEmptyString(claim.id)
      || claimsById.has(claim.id) || !Array.isArray(claim.evidence_ids)
      || !claim.evidence_ids.every(nonEmptyString)) return null;
    claimsById.set(claim.id, claim);
  }

  const citedEvidenceIds = new Set();
  const citedQuotePairs = new Set();
  for (const claimId of new Set(explanationClaimIds)){
    const claim = claimsById.get(claimId);
    if (!claim) return null;
    const claimEvidenceIds = new Set(claim.evidence_ids);
    for (const id of claim.evidence_ids) citedEvidenceIds.add(id);
    if (Array.isArray(claim.quotes)){
      for (const quote of claim.quotes){
        if (quote && typeof quote === 'object' && !Array.isArray(quote)
          && typeof quote.evidence_id === 'string' && typeof quote.text === 'string'
          && claimEvidenceIds.has(quote.evidence_id)){
          citedQuotePairs.add(JSON.stringify([quote.evidence_id, quote.text]));
        }
      }
    }
  }
  if (!Array.isArray(card.evidence)
    || card.evidence.some((item) => !item || typeof item !== 'object' || Array.isArray(item))) return null;

  const evidenceById = new Map();
  const duplicateEvidenceIds = new Set();
  for (const item of card.evidence){
    if (item.platform != null && typeof item.platform !== 'string') return null;
    if (nonEmptyString(item.id)){
      if (evidenceById.has(item.id)) duplicateEvidenceIds.add(item.id);
      else evidenceById.set(item.id, item);
    }
  }
  const localIds = [...new Set(specificity.local_evidence_ids)];
  if (localIds.length < 2 || localIds.some((id) => !citedEvidenceIds.has(id))) return null;
  const availableLocalIds = localIds.filter((id) => !duplicateEvidenceIds.has(id)
    && evidenceById.has(id) && usableLocalEvidence(evidenceById.get(id)));
  const quoteId = specificity.quote.evidence_id;
  const quoteText = specificity.quote.text;
  const quoteSource = duplicateEvidenceIds.has(quoteId) ? null : evidenceById.get(quoteId);
  if (!availableLocalIds.includes(quoteId) || availableLocalIds.length < 2 || !quoteSource) return null;

  if (!citedQuotePairs.has(JSON.stringify([quoteId, quoteText]))) return null;
  const sourceText = nonEmptyString(quoteSource.quote_text) ? quoteSource.quote_text : quoteSource.text;
  const wordCount = quoteText.split(SERVER_SPACE).filter(Boolean).length;
  if (!nonEmptyString(sourceText) || wordCount < 2 || wordCount > 25
    || [...quoteText].length > 160 || !quoteMatchesSource(sourceText, quoteText)) return null;

  const secondId = availableLocalIds.find((id) => id !== quoteId);
  if (!secondId) return null;
  const examples = [quoteSource, evidenceById.get(secondId)].map((item) => (
    nonEmptyString(item.text) ? item : {...item, text: item.quote_text}
  ));
  return {
    whyNow: card.explanation,
    quote: quoteText,
    examples,
  };
}

function prepareTodayMarket(market){
  /* A published card whose counted creators is 0 is never drawn. */
  const cards = (Array.isArray(market.cards) ? market.cards : [])
    .concat(Array.isArray(market.more) ? market.more : [])
    .filter((card) => countedCreators(card) !== 0);
  const admitted = cards.flatMap((card) => {
    const specificity = todaySpecificityView(card);
    return specificity ? [{card, specificity}] : [];
  });
  return {
    market: {...market, cards: admitted.slice(0, 5).map(({card}) => card), more: admitted.slice(5).map(({card}) => card)},
    admitted,
    rejectedCards: cards.length - admitted.length,
    specificityByItemId: new Map(admitted.map(({card, specificity}) => [card.item_id, specificity])),
  };
}

export function TodayPage42({region, date, onAuth, loadAlerts, loadInvestigations, loadSchedules, onCreateWatch, onFeedback, onRegionChange}){
  const [load, setLoad] = useState({state: 'loading'});
  const [slowRead, setSlowRead] = useState(false);
  const [tick, setTick] = useState(0);
  const [tab, setTab] = useState(() => tabForRegion(region));
  const [alerts, setAlerts] = useState({state: 'loading'});
  const [finished, setFinished] = useState([]);
  const [scheduled, setScheduled] = useState([]);
  const authRef = useRef(onAuth);
  authRef.current = onAuth;
  const watch = useCardWatch(onCreateWatch, onAuth);

  useEffect(() => {
    if (!loadAlerts) return undefined;
    const ctrl = new AbortController();
    setAlerts({state: 'loading'});
    loadAlerts(date, {signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setAlerts({state: 'ready', data: data || {}}); })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){ setAlerts({state: 'auth'}); if (authRef.current) authRef.current(); return; }
        setAlerts({state: 'error'});
      });
    return () => ctrl.abort();
  }, [date, loadAlerts]);

  /* Investigations finished in the last 7 days. The notice is a quiet
     extra: a read that fails leaves it off and holds nothing up. */
  useEffect(() => {
    if (!loadInvestigations) return undefined;
    const ctrl = new AbortController();
    loadInvestigations({status: 'complete'}, {signal: ctrl.signal})
      .then((data) => {
        if (ctrl.signal.aborted) return;
        const since = Date.now() - FINISHED_DAYS * 86400000;
        const items = data && Array.isArray(data.investigations) ? data.investigations : [];
        setFinished(items.filter((item) => (Date.parse(item.updated_at) || 0) >= since));
      })
      .catch(() => {});
    return () => ctrl.abort();
  }, [loadInvestigations]);

  /* Scheduled answers of the last 7 days, read from each schedule's last
     run: only runs that made an answer, never a skip. Quiet the same way. */
  useEffect(() => {
    if (!loadSchedules) return undefined;
    const ctrl = new AbortController();
    loadSchedules({signal: ctrl.signal})
      .then((data) => {
        if (ctrl.signal.aborted) return;
        const since = new Date(Date.now() - FINISHED_DAYS * 86400000).toISOString().slice(0, 10);
        const items = data && Array.isArray(data.schedules) ? data.schedules : [];
        setScheduled(items.filter((item) => item.last_run && item.last_run.ask_id && String(item.last_run.date || '') >= since));
      })
      .catch(() => {});
    return () => ctrl.abort();
  }, [loadSchedules]);

  useEffect(() => { setTab(tabForRegion(region)); }, [region]);

  useEffect(() => {
    const ctrl = new AbortController();
    let slowTimer = setTimeout(() => {
      if (!ctrl.signal.aborted) setSlowRead(true);
    }, TODAY_SLOW_READ_MS);
    const clearSlowTimer = () => {
      if (slowTimer !== null) {
        clearTimeout(slowTimer);
        slowTimer = null;
      }
    };
    setLoad({state: 'loading'});
    setSlowRead(false);
    fetchToday(date, {signal: ctrl.signal})
      .then((data) => {
        clearSlowTimer();
        if (!ctrl.signal.aborted) {
          setSlowRead(false);
          setLoad({state: 'ready', data});
        }
      })
      .catch((error) => {
        clearSlowTimer();
        if (ctrl.signal.aborted) return;
        setSlowRead(false);
        if (error && error.auth){
          setLoad({state: 'auth'});
          if (authRef.current) authRef.current();
        } else if (error && (error.code === 'not_ready' || error.status === 409)){
          setLoad({state: 'not_ready'});
        } else {
          setLoad({state: 'error', message: error && error.status ? error.message : 'The 42 service did not answer.'});
        }
      });
    return () => {
      clearSlowTimer();
      ctrl.abort();
    };
  }, [date, tick]);

  const data = load.state === 'ready' ? load.data || {} : {};
  const briefDate = typeof data.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(data.date) ? data.date : null;
  const dayWords = briefDayWords(briefDate || (/^\d{4}-\d{2}-\d{2}$/.test(date || '') ? date : null));
  const earlierDefaultBrief = !date && briefDate && briefDate < sastToday();
  const publicationTime = typeof data.published_at === 'string'
    && /(?:Z|[+-]\d{2}:\d{2})$/i.test(data.published_at)
    && Number.isFinite(Date.parse(data.published_at))
    ? snapshotTime(data.published_at)
    : null;
  const markets = (Array.isArray(data.markets) ? data.markets : []).map(prepareTodayMarket);
  const warmup = data.warmup && data.warmup.active ? data.warmup : null;
  const shown = tab === 'ALL' ? markets : markets.filter((entry) => entry.market.market === tab);
  const allMarketsEmpty = markets.length > 0 && markets.every(({market}) => {
    const cards = Array.isArray(market.cards) ? market.cards.length : 0;
    const more = Array.isArray(market.more) ? market.more.length : 0;
    return cards + more === 0;
  });
  /* Tester report, 5 October 2026: a brief is "partial" when any held
     topic's explanation failed its checks, which read as a broken market.
     A data problem or an explicit model interruption remains incomplete.
     Fully checked holds are named separately, with their reasons below. */
  const incompleteMarkets = markets.map(({market}) => market).filter(hasIncompleteRun);
  const partialCopy = incompleteMarkets.length > 0
    ? 'Some markets are incomplete: ' + incompleteMarkets.map((market) => nonEmptyString(market.label) ? market.label : market.market).join(', ') + '.'
    : null;
  const checkedHeldMarkets = shown.filter(({rejectedCards}) => rejectedCards === 0).map(({market}) => market).filter((market) => {
    const held = market.held_back;
    return !hasIncompleteRun(market) && market.cards.length + market.more.length === 0
      && Array.isArray(held?.items) && held.items.length > 0 && held.count === held.items.length
      && held.items.every((item) => item && (['likely_coordinated', 'political_unconfirmed', 'paid_led',
        'not_local', 'too_few_creators', 'not_confirmed'].includes(item.reason)
        || (item.reason === 'explanation_failed' && nonEmptyString(item.failed_reason)
          && !item.failed_reason.startsWith('Model busy:'))));
  });
  const checkedHeldCopy = checkedHeldMarkets.length > 0
    ? 'All the topics checked in ' + andJoin(checkedHeldMarkets.map((market) => nonEmptyString(market.label) ? market.label : market.market)) + ' were held back. See their reasons below.'
    : null;
  /* The server words the heading from every market. A market tab with nothing cleared is not taking off. */
  const selectedHasCards = shown.some(({market}) => market.cards.length + market.more.length > 0);
  const headingText = typeof data.heading === 'string' && tab !== 'ALL' && !selectedHasCards
    ? data.heading.replace(/^Taking off/, 'Today') : data.heading;
  const selectedLead = tab === 'ALL' ? null : markets.find((entry) => entry.market.market === tab)?.admitted[0];
  const headline = data.headline && nonEmptyString(data.headline.text)
    && (tab === 'ALL' || data.headline.market === tab)
    && markets.some((entry) => entry.market.market === data.headline.market
      && entry.admitted.some(({card}) => card.item_id === data.headline.item_id))
    ? data.headline
    : selectedLead
      ? {text: selectedLead.specificity.whyNow, market: tab, item_id: selectedLead.card.item_id,
        claim_ids: selectedLead.card.explanation_claim_ids}
      : null;
  const receiptLine = runReceiptLine(data.run_receipt);
  const isLoading = load.state === 'loading';

  return (
    <BriefDay.Provider value={dayWords}>
    <section className="page t42">
      <header className="t42-head">
        <h1 className="t42-heading" {...(load.state === 'ready' ? {'data-today-loaded': ''} : {})}>{load.state === 'ready' && headingText ? headingWords(headingText) : 'Today'}</h1>
        {headline && (
          <div className="t42-lead" data-today-lead="">
            <p className="t42-headline"><HeadlineText text={headline.text} term={headlineTerm(markets, headline)} /></p>
            <LeadPanel {...headlineLead(markets, headline)} date={briefDate || date} watch={watch} />
          </div>
        )}
        {isLoading && (
          <div className="t42-loading-structure sc-loading-hero" aria-hidden="true">
              <div className="sc-skeleton-hero">
                <div className="sc-skeleton-hero-text">
                  <span className="t42-loading-shape sc-skeleton-headline" />
                  <span className="t42-loading-shape sc-skeleton-headline" />
                  <span className="t42-loading-shape sc-skeleton-headline sc-skeleton-headline-short" />
                </div>
                <div className="sc-skeleton-panel">
                  <span className="t42-loading-shape sc-skeleton-number" />
                  <span className="t42-loading-shape sc-skeleton-chart" />
                  <span className="t42-loading-shape t42-loading-card-line" />
                </div>
              </div>
          </div>
        )}
        {/* One status area: a warning that changes how the page is read leads
            it and keeps role=status; the rest is one quiet line of facts. */}
        <div className="t42-status-area" data-today-status-area="">
          {earlierDefaultBrief && <p className="t42-notice" role="status" data-today-earlier="">Earlier brief: <span className="t42-nowrap">{longDate(briefDate)}</span>. This is not today’s brief.</p>}
          {data.status === 'data_issue' && <p className="t42-notice t42-banner-data_issue" role="status" data-today-status="data_issue">Some data for this brief was incomplete, so the topics it affects are held back below with their reasons.</p>}
          {(receiptLine || (date && briefDate) || (data.status === 'partial' && partialCopy) || checkedHeldCopy || publicationTime || warmup) && (
            <p className="t42-facts" data-today-facts="">
              {date && briefDate && <span className="t42-fact" data-today-date-context="">Brief for {longDate(briefDate)}</span>}
              {publicationTime && <span className="t42-fact" data-today-published-at="">Published at {publicationTime}</span>}
              {warmup && <span className="t42-fact t42-fact-warming_up">{warmup.text}</span>}
              {data.status === 'partial' && partialCopy && <span className="t42-fact" role="status" data-today-status="partial">{partialCopy}</span>}
              {checkedHeldCopy && <span className="t42-fact" role="status" data-today-held-status="">{checkedHeldCopy}</span>}
              {receiptLine && <span className="t42-fact" data-today-run-receipt="">{receiptLine}</span>}
            </p>
          )}
          {warmup && <StepMeter value={warmup.day} of={warmup.of} data="data-today-warmup-meter" />}
        </div>
        <FirstVisitGuide />
      </header>
      {allMarketsEmpty && <p className="t42-line-text" data-today-history-link=""><a className="t42-link" href="#/history">Choose a past brief in History</a></p>}
      {/* Fired alerts lead the page. A read that is still loading or failed
          shows nothing (UX pass, 3 October 2026): Alerts is in the menu, and a
          side module's state should not take the place of the brief. */}
      {loadAlerts && alerts.state === 'ready' && <AlertsStrip alerts={alerts} />}
      {finished.length > 0 && <FinishedInvestigations items={finished} />}
      {scheduled.length > 0 && <ScheduledAnswers items={scheduled} />}
      {load.state === 'ready' && markets.length > 1 && (
        <MarketGlance markets={markets.map((entry) => entry.market)} date={briefDate} tab={tab} onPick={(id) => {
          setTab(id);
          if (onRegionChange) onRegionChange(id);
        }} />
      )}
      <div className="t42-tabs" role="tablist" aria-label="Markets">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={'t42-tab-' + t.id}
            aria-selected={tab === t.id ? 'true' : 'false'}
            aria-controls="t42-panel"
            className="t42-tab"
            onClick={() => {
              setTab(t.id);
              if (onRegionChange) onRegionChange(t.id);
            }}
          >{t.label}</button>
        ))}
      </div>
      {isLoading && <p className="t42-status" role="status" aria-live="polite" data-today-load-status="">Loading Today{slowRead ? '. The Today read is still in progress.' : ''}</p>}
      <div id="t42-panel" role="tabpanel" aria-labelledby={'t42-tab-' + tab} aria-busy={isLoading ? 'true' : undefined}>
        {isLoading && (
          <div className="t42-loading-structure" aria-hidden="true">
            <div className="sc-skeleton-card">
              <div className="sc-skeleton-story">
                <span className="t42-loading-shape t42-loading-card-meta" />
                <span className="t42-loading-shape t42-loading-card-title" />
                <span className="t42-loading-shape t42-loading-card-line" />
                <span className="t42-loading-shape t42-loading-card-line-short" />
              </div>
              <div className="sc-skeleton-live">
                <span className="t42-loading-shape sc-skeleton-number" />
                <span className="t42-loading-shape sc-skeleton-chart" />
              </div>
              <div className="sc-skeleton-tiles">
                <span className="t42-loading-shape sc-skeleton-tile" />
                <span className="t42-loading-shape sc-skeleton-tile" />
              </div>
            </div>
          </div>
        )}
        {load.state === 'auth' && <p className="t42-status" role="status">Enter the passcode to read Today.</p>}
        {load.state === 'not_ready' && (
          <>
            <h2 className="t42-heading">Today is not published yet</h2>
            <p className="t42-status">There is no brief for {longDate(date || sastToday())} yet.</p>
          </>
        )}
        {load.state === 'error' && (
          <>
            <h2 className="t42-heading">Today could not load</h2>
            <p className="t42-status" role="alert">{load.message}</p>
            <button type="button" className="t42-button" onClick={() => setTick((t) => t + 1)}>Try again</button>
          </>
        )}
        {load.state === 'ready' && (
          <>
            <BreakingStrip markets={shown.map((m) => m.market)} />
            {shown.map((m) => (
              <MarketBlock
                key={m.market.market + ':' + tab}
                market={m.market}
                rejected={m.rejectedCards}
                specificityByItemId={m.specificityByItemId}
                headline={headline}
                compact={tab === 'ALL'}
                showHistoryLink={!allMarketsEmpty}
                onOpenMarket={(id) => {
                  setTab(id);
                  if (onRegionChange) onRegionChange(id);
                }}
                date={data.date}
                dataIssue={data.status === 'data_issue'}
                searchingNow={Array.isArray(data.searching_now)
                  ? data.searching_now.filter((signal) => signal && signal.market === m.market.market)
                  : []}
                skipBanner={warmup ? warmup.text : null}
                onAuth={onAuth}
                watch={watch}
                onFeedback={onFeedback}
              />
            ))}
            {tab === 'ALL' && shown.length > 0 && (
              <TodayBoards
                groups={shown.map(({market}) => ({market: market.market, label: market.label, boards: market.boards}))}
                day={dayWords}
              />
            )}
            {shown.length === 0 && <p className="t42-status">This market is not in this brief.</p>}
          </>
        )}
      </div>
      {/* UX pass, 3 October 2026: an alerts read that is still loading or
          failed no longer adds a section at the foot; Alerts is in the menu. */}
      {watch.dialog}
      <BackToTop />
    </section>
    </BriefDay.Provider>
  );
}

/* Tester report, 5 October 2026: a long Today page had no way back to the
   top. Once the reader is more than a screen and a half down, a quiet button
   returns to the page heading and moves focus there, so a keyboard or screen
   reader user lands where a sighted reader does. */
const TOP_AFTER_SCREENS = 1.5;
function BackToTop(){
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const onScroll = () => {
      const y = window.scrollY || (document.documentElement && document.documentElement.scrollTop) || 0;
      setShown(y > (window.innerHeight || 0) * TOP_AFTER_SCREENS);
    };
    onScroll();
    window.addEventListener('scroll', onScroll, {passive: true});
    return () => window.removeEventListener('scroll', onScroll);
  }, []);
  if (!shown) return null;
  const toTop = () => {
    const still = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({top: 0, behavior: still ? 'auto' : 'smooth'});
    const heading = document.querySelector('.t42 .t42-heading');
    if (heading){
      if (!heading.hasAttribute('tabindex')) heading.setAttribute('tabindex', '-1');
      heading.focus({preventScroll: true});
    }
  };
  return <button type="button" className="t42-top" data-today-top="" onClick={toTop}>Back to top</button>;
}

/* Visual pass, 3 October 2026: the day across markets at a glance, before
   any card. Each market is one column: how many items 42 collected, set large
   (every item its searches, lists and boards returned, so a post found twice
   counts twice), then a bar for the share of them with a known location (labelled, on a
   0 to 100% scale), then how many trends cleared the checks and how many
   were held back. A column opens its market's tab. Only figures the brief
   already carries are drawn; one that is missing says so. */
function MarketGlance({markets, date, tab, onPick}){
  const posts = (market) => (market.coverage && Number.isFinite(market.coverage.posts) ? market.coverage.posts : null);
  const located = (market) => (market.coverage && Number.isFinite(market.coverage.located_share)
    ? Math.min(1, Math.max(0, market.coverage.located_share)) : null);
  return (
    <section className="t42-glance" aria-labelledby="t42-glance-title" data-today-glance="">
      <h2 className="sr-only" id="t42-glance-title">Today across markets</h2>
      {/* The figures name their window: the collection for this brief's day. */}
      {date && <p className="t42-glance-window" data-glance-window="">Items collected for {longDate(date)}. Each market counts every item its searches, lists and boards returned, so a post found twice counts twice.</p>}
      <ul className="t42-glance-list">
        {markets.map((market) => {
          const cleared = (Array.isArray(market.cards) ? market.cards.length : 0) + (Array.isArray(market.more) ? market.more.length : 0);
          const read = posts(market);
          const share = located(market);
          const pct = share === null ? null : Math.round(share * 100);
          const held = market.held_back && Number.isInteger(market.held_back.count) ? market.held_back.count
            : market.held_back && Array.isArray(market.held_back.items) ? market.held_back.items.length : 0;
          const name = nonEmptyString(market.label) ? market.label : market.market;
          const here = tab === market.market;
          const noBrief = hasNoBrief(market);
          const trendWords = readerFigure(cleared) + (cleared === 1 ? ' trend' : ' trends') + ' cleared';
          const label = 'Show ' + name + ': '
            + (read === null ? 'items collected not measured' : readerFigure(read) + ' items collected')
            + (pct === null ? '' : ', ' + pct + '% with a known location')
            + ', ' + (noBrief ? 'no brief' : trendWords) + (held > 0 ? ', ' + readerFigure(held) + ' held back' : '');
          return (
            <li key={market.market} data-glance-market={market.market}>
              <button type="button" className="t42-glance-market" aria-pressed={here ? 'true' : 'false'} aria-label={label} onClick={() => onPick(market.market)}>
                <span className="t42-glance-name"><span className="t42-glance-label">{name}</span>{hasIncompleteRun(market) ? <span className="t42-glance-note"> · incomplete</span> : null}</span>
                <span className="t42-glance-figure">
                  {read === null ? null : <span className="t42-glance-value">{readerFigure(read)}</span>}
                  <span className="t42-glance-unit">{read === null ? 'Items collected: not measured' : 'items collected'}</span>
                </span>
                {pct !== null && (
                  <span className="t42-glance-located">
                    <span className="t42-glance-bar" aria-hidden="true">
                      <span className="t42-glance-fill" style={{'--share': share}} />
                    </span>
                    <span className="t42-glance-bar-label">{pct}%<span className="t42-glance-long"> with a known location</span></span>
                  </span>
                )}
                <span className="t42-glance-facts">
                  <span className="t42-glance-cleared">{noBrief ? 'No brief' : readerFigure(cleared) + (cleared === 1 ? ' trend' : ' trends')}{noBrief ? null : <span className="t42-glance-long"> cleared</span>}</span>
                  {held > 0 ? <><span className="t42-glance-sep" aria-hidden="true">{' · '}</span><span>{readerFigure(held) + ' held back'}</span></> : null}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/* UX pass, 3 October 2026: one line that teaches the path through 42 to
   someone who has not used it before, with a way to put it away. It is
   remembered in this browser only; with no storage it simply shows. */
const GUIDE_KEY = 'f42_today_guide_closed';
function guideClosed(){
  try { return localStorage.getItem(GUIDE_KEY) === '1'; } catch (e){ return false; }
}
function FirstVisitGuide(){
  const [closed, setClosed] = useState(guideClosed);
  if (closed) return null;
  const close = () => {
    try { localStorage.setItem(GUIDE_KEY, '1'); } catch (e){}
    setClosed(true);
  };
  return (
    <aside className="t42-guide" aria-label="How to use 42" data-today-guide="">
      <ol className="t42-guide-steps">
        <li><span className="t42-guide-n">1</span> Read what is moving below.</li>
        <li><span className="t42-guide-n">2</span> Open a trend to see why, or ask 42.</li>
        <li><span className="t42-guide-n">3</span> Watch it, or save it to a dossier.</li>
      </ol>
      <button type="button" className="t42-guide-close" onClick={close}>Got it</button>
    </aside>
  );
}

/* Today's alerts from the watches (contract section 10.5): the count and
   each alert, linked to its topic. An alert on a held-back item says so
   with its reason. Waiting rules are not alerts; they are listed on Alerts. */
function AlertsStrip({alerts}){
  const day = useContext(BriefDay);
  const fired = Array.isArray(alerts.data.alerts) ? alerts.data.alerts.filter((a) => !a.waiting) : [];
  if (fired.length === 0) return null;
  const body = (
    <>
      <p className="t42-line-text">{fired.length === 1 ? '1 alert ' + day : fired.length + ' alerts ' + day}</p>
      <ul className="t42-rows a42-strip-rows">
        {fired.map((a) => {
          const held = a.card && a.card.held_back;
          return (
            <li key={a.watch_id + ':' + a.market + ':' + a.item_id} data-held={held ? '' : undefined}>
              <a className="t42-link" href={topicHref(a.item_id, a.market)}>{a.label}</a>:{' '}
              {held && <><span className="a42-held">{heldWords(held)}</span> · </>}
              {a.fired_because} · {MARKET_WORDS[a.market] || a.market}
            </li>
          );
        })}
      </ul>
    </>
  );
  return (
    <section className="t42-section a42-strip" data-section="alerts" aria-labelledby="t42-alerts-title">
      <h2 className="t42-section-title" id="t42-alerts-title">Alerts</h2>
      {body}
      <a className="t42-link" href="#/alerts">All alerts and watches</a>
    </section>
  );
}

/* Breaking in the last few hours (contract section 4, breaking): the
   hourly Breaking rule's items for the shown markets, kept apart from the
   checked morning cards. No lines, no strip: not even its heading. */
function BreakingStrip({markets}){
  const lines = markets.flatMap((market) => (Array.isArray(market.breaking) ? market.breaking : []))
    .filter((line) => line && typeof line === 'object' && nonEmptyString(line.item_id) && nonEmptyString(line.title)
      && nonEmptyString(line.market));
  if (lines.length === 0) return null;
  return (
    <section className="t42-section a42-strip" data-section="breaking" aria-labelledby="t42-breaking-title">
      <h2 className="t42-section-title" id="t42-breaking-title">Breaking in the last few hours</h2>
      <ul className="t42-rows a42-strip-rows t42-breaking-rows">
        {lines.map((line) => (
          <li key={line.market + ':' + line.item_id}>
            <a className="t42-link" href={topicHref(line.item_id, line.market)}>{line.title}</a>
            {' · ' + (nonEmptyString(line.market_label) ? line.market_label : MARKET_WORDS[line.market] || line.market)}
            {nonEmptyString(line.ago_text) ? ' · ' + line.ago_text : ''}
            {nonEmptyString(line.note) ? ' · ' + line.note : ''}
          </li>
        ))}
      </ul>
    </section>
  );
}

function FinishedInvestigations({items}){
  return (
    <p className="t42-line-text" data-section="investigations-done">
      {(items.length === 1 ? '1 investigation finished' : items.length + ' investigations finished') + ' in the last 7 days: '}
      {items.map((item, index) => (
        <span key={item.investigation_id}>
          {index > 0 && ' · '}
          <a className="t42-link" href={'#/investigations/' + encodeURIComponent(item.investigation_id)}>{item.question}</a>
        </span>
      ))}
    </p>
  );
}

/* The question, how its answer ended and a link; the answer text stays on
   the Ask page. */
function ScheduledAnswers({items}){
  return (
    <p className="t42-line-text" data-section="scheduled-done">
      {(items.length === 1 ? '1 scheduled answer' : items.length + ' scheduled answers') + ' in the last 7 days: '}
      {items.map((item, index) => (
        <span key={item.schedule_id}>
          {index > 0 && ' · '}
          <a className="t42-link" href={'#/ask?follow=' + encodeURIComponent(item.last_run.ask_id)}>{item.question}</a>
          {' (' + scheduledRunWords(item.last_run) + ')'}
        </span>
      ))}
    </p>
  );
}

/* The poster marks the trend it names (styles/nightdesk.css), so the eye
   lands on the name first. The words stay exactly the brief's. */
function headlineTerm(markets, headline){
  for (const entry of markets){
    if (entry.market.market !== headline.market) continue;
    const hit = entry.admitted.find(({card}) => card.item_id === headline.item_id);
    if (hit) return hit.card.title;
  }
  return '';
}

function headlineLead(markets, headline){
  for (const entry of markets){
    if (entry.market.market !== headline.market) continue;
    const hit = entry.admitted.find(({card}) => card.item_id === headline.item_id);
    if (hit) return {card: hit.card, specificity: hit.specificity, market: entry.market.market};
  }
  return {card: null, specificity: null, market: headline.market};
}

function HeadlineText({text, term}){
  const parts = headlineParts(proseDates(text), term);
  if (!parts.term) return parts.before;
  return <>{parts.before}<span className="t42-headline-term">{parts.term}</span>{parts.after}</>;
}

/* The page headline and a card's Why now can be the same sentence. When the
   headline already says it above, the card does not say it again. */
const sentenceKey = (value) => String(value || '').toLowerCase().replace(/[\s.,;:!]+/g, ' ').trim();
function saysHeadline(headline, card, specificity){
  return Boolean(headline && specificity && headline.item_id === card.item_id
    && sentenceKey(headline.text) === sentenceKey(specificity.whyNow));
}

/* A market the brief has no row for. The server says so in its own banner,
   "Data issue: no brief was published for ..."; a brief that was published
   with a data-issue status has its own wording and its own held count. */
const NO_BRIEF_BANNER = /^Data issue: no brief was published for /;
function hasNoBrief(market){
  const held = market.held_back;
  return market.status === 'data_issue' && Boolean(held) && held.count === 0
    && (!Array.isArray(held.items) || held.items.length === 0)
    && Array.isArray(market.banners) && market.banners.some((banner) => banner && banner.kind === 'data_issue' && NO_BRIEF_BANNER.test(String(banner.text || '')));
}

function emptyMarketWords(market, day){
  const held = market.held_back;
  const items = held && Array.isArray(held.items) ? held.items : null;
  const counted = held && Number.isInteger(held.count) && held.count >= 0;
  const count = counted ? held.count : items ? items.length : null;
  const name = nonEmptyString(market.label) ? market.label : market.market;
  if (hasNoBrief(market)) return {lead: '42 has no brief for ' + name + ' ' + day + '.', held: null};
  return {
    lead: 'No trend cleared our checks in ' + name + ' ' + day + '.',
    held: count === null ? 'How many were held back is unavailable.' : count === 1 ? '1 is held back.' : readerFigure(count) + ' are held back.',
  };
}

/* A server note reads as a sentence: a leading "Data issue: " is kept for
   screen readers only (the red bar already says it), and the first letter
   left after it is set as a capital without changing the words. */
const DATA_ISSUE_PREFIX = /^Data issue: /;
function BannerWords({banner}){
  const text = banner.text;
  const prefix = DATA_ISSUE_PREFIX.exec(text);
  const rest = prefix ? text.slice(prefix[0].length) : text;
  const ends = /[.!?]$/.test(rest) ? '' : '.';
  return (
    <>
      <span className={'t42-banner t42-banner-' + banner.kind}>
        {prefix && <span className="sr-only">{prefix[0]}</span>}
        {prefix && rest ? <><span className="t42-market-status__cap">{rest[0]}</span>{rest.slice(1)}</> : rest}
      </span>
      {ends}
    </>
  );
}

function jumpTo(id){
  return (event) => {
    const target = document.getElementById(id);
    if (!target) return;
    event.preventDefault();
    target.scrollIntoView({block: 'start'});
    target.focus({preventScroll: true});
  };
}

/* Redesign, 4 October 2026: the market's notes (server banners, the source
   line and, with no cards, the empty-market sentence) are one calm status
   block under Trending on Google instead of three separate lines. */
function MarketStatus({market, banners, sourceDetails, empty}){
  const day = useContext(BriefDay);
  const hasSourceProblems = sourceDetails.length > 0;
  if (!empty && banners.length === 0 && !hasSourceProblems) return null;
  const words = empty ? emptyMarketWords(market, day) : null;
  const heldId = 't42-held-' + market.market;
  return (
    <div className="t42-market-status" data-market-status="" data-status-tone={empty ? 'alert' : 'plain'}>
      {empty && <p className="t42-market-status__lead" data-empty-market-reason="">{words.lead}</p>}
      {(empty || banners.length > 0 || hasSourceProblems) && (
        <p className="t42-market-status__detail">
          {empty && words.held && <span data-held-count="">{words.held}</span>}
          {banners.map((banner, index) => (
            <span key={banner.kind + banner.text}>{(empty || index > 0) ? ' ' : ''}<BannerWords banner={banner} /></span>
          ))}
          {hasSourceProblems && <>{(empty || banners.length > 0) ? ' ' : ''}<span data-source-problem="">Some sources were incomplete {day}</span>.</>}
        </p>
      )}
      {(empty || hasSourceProblems) && (
        <div className="t42-market-status__links">
          {empty && words.held && <a className="t42-link" href={'#' + heldId} onClick={jumpTo(heldId)}>Why each was held</a>}
          {hasSourceProblems && (
            <details data-section="source-details">
              <summary>Source details</summary>
              <ul className="t42-rows">
                {sourceDetails.map((line, index) => <li key={index}>{line}</li>)}
              </ul>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

function ClientHeld({count}){
  if (!(count > 0)) return null;
  return (
    <p className="t42-line-text" data-client-held="">
      {count === 1
        ? '1 trend the brief cleared is not shown here, because its example posts did not pass this page’s own check.'
        : count + ' trends the brief cleared are not shown here, because their example posts did not pass this page’s own check.'}
    </p>
  );
}

function MarketBlock({market, headline, rejected = 0, compact, date, skipBanner, onAuth, watch, onFeedback, specificityByItemId, searchingNow, showHistoryLink, onOpenMarket, dataIssue = false}){
  const [all, setAll] = useState(false);
  const top = Array.isArray(market.cards) ? market.cards : [];
  const more = Array.isArray(market.more) ? market.more : [];
  const cards = compact ? top.slice(0, 3) : all ? top.concat(more) : top;
  const banners = (market.banners || []).filter((b) => b.text !== skipBanner);
  const isSourceFailureBanner = (banner) => banner.kind === 'data_issue'
    && typeof banner.text === 'string' && /^\d+ sources? failed today\b/i.test(banner.text);
  const sourceBanners = banners.filter((banner) => banner.kind === 'thin_coverage' || isSourceFailureBanner(banner));
  const coverageIssues = market.coverage && Array.isArray(market.coverage.issues) ? market.coverage.issues : [];
  const sourceDetails = sourceBanners.map((banner) => banner.text).concat(coverageIssues);
  const listId = 't42-cards-' + market.market;
  return (
    <section className="t42-market t42-today-market" data-market={market.market} aria-label={market.label}>
      {/* The tab already names a single market; only All names each one. */}
      {compact && <h2 className="t42-market-name">{market.label}</h2>}
      <MarketStatus market={market} empty={cards.length === 0} sourceDetails={sourceDetails}
        banners={banners.filter((banner) => banner.kind !== 'thin_coverage' && !isSourceFailureBanner(banner))} />
      <ClientHeld count={rejected} />
      {cards.length > 0
        ? <>
          <p className="t42-line-text" data-today-count-window="">Creator and post counts are in the last 3 days.</p>
          <ol className="t42-cards" data-ranked="" id={listId}>
            {cards.map((card, index) => (
              <StoryCard key={card.item_id} index={index} card={watch.mark(card, market.market)} market={market.market} date={card.date || date}
                onAuth={onAuth} onWatch={watch.onWatch} onFeedback={onFeedback}
                className={saysHeadline(headline, card, specificityByItemId.get(card.item_id)) ? 't42-card-headline-said' : undefined}
                todaySpecificity={specificityByItemId.get(card.item_id)} />
            ))}
          </ol>
          {/* The All tab shows three a market; it says so and opens the market for the rest. */}
          {compact && top.length + more.length > cards.length && (
            <p className="t42-line-text" data-all-cap="">
              Showing the top {cards.length} of {top.length + more.length} trends in {market.label}.{' '}
              {onOpenMarket && <button type="button" className="t42-button" onClick={() => onOpenMarket(market.market)}>See them in {market.label}</button>}
            </p>
          )}
          </>
        : <>
            {showHistoryLink && <p className="t42-line-text"><a className="t42-link" href="#/history">Choose a past brief in History</a></p>}
            {!hasNoBrief(market) && <HeldForEvidence held={market.held_back} market={market.market} />}
          </>}
      {!compact && more.length > 0 && (
        <button type="button" className="t42-button" aria-expanded={all ? 'true' : 'false'} aria-controls={listId} onClick={() => setAll((v) => !v)}>
          {all ? 'Show fewer' : 'Show all'}
        </button>
      )}
      {/* W8-DEC-04: Google search interest reads after the checked cards, never ahead of them. */}
      <SearchingNow signals={searchingNow} market={market.market} nameMarket={!compact} />
      {!compact && <BelowCards market={market} showHeldBack={cards.length > 0} openLeftOut={dataIssue} />}
    </section>
  );
}

/* UX pass, 3 October 2026: what 42 left out is for checking, not the
   day's reading, so Dropped and Held back sit behind one line that counts
   them. The trend cards stay the first and only thing a newcomer reads. */
function leftOutWords(market, showHeldBack, day = 'today'){
  const dropped = market.dropped && !market.dropped.first_morning && Array.isArray(market.dropped.items) ? market.dropped.items.length : 0;
  const held = market.held_back;
  const heldItems = held && Array.isArray(held.items) ? held.items.length : 0;
  const heldCount = showHeldBack ? (held && Number.isInteger(held.count) && held.count >= 0 ? held.count : heldItems) : 0;
  const parts = [];
  if (market.dropped && market.dropped.first_morning) parts.push('first morning, nothing to compare yet');
  if (dropped > 0) parts.push(dropped + ' dropped since yesterday');
  if (heldCount > 0) parts.push(heldCount + ' held back by our checks');
  return parts.length > 0 ? 'Left out ' + day + ': ' + parts.join(', ') : null;
}

/* A brief with a data issue says its held topics are below with their
   reasons, so the list starts open there. With nothing dropped or held
   there is nothing to fold, so the line is left out rather than saying
   "nothing" under the held list. */
function LeftOut({market, showHeldBack, open}){
  const words = leftOutWords(market, showHeldBack, useContext(BriefDay));
  if (!words) return null;
  return (
    <details className="t42-left-out" data-section="left-out" open={open || undefined}>
      <summary>{words}</summary>
      <div className="t42-left-out-body">
        <Dropped dropped={market.dropped} />
        {showHeldBack && <HeldBack held={market.held_back} />}
      </div>
    </details>
  );
}

function BelowCards({market, showHeldBack, openLeftOut}){
  const day = useContext(BriefDay);
  return (
    <div className="t42-below">
      <LeftOut market={market} showHeldBack={showHeldBack} open={openLeftOut} />
      <NotAssessed audit={market.not_assessed} />
      <TodayBoards boards={market.boards} day={day} />
      <Moments moments={market.moments} />
    </div>
  );
}

function NotAssessed({audit}){
  const known = audit && Array.isArray(audit.items) && Number.isInteger(audit.count);
  return (
    <details className="t42-left-out" data-not-assessed="">
      <summary>Not assessed{known ? ' (' + audit.count + ')' : ''}</summary>
      {!known
        ? <p className="t42-line-text">Selection audit unavailable for this brief.</p>
        : audit.items.length === 0
          ? <p className="t42-line-text">No topics were left unassessed.</p>
          : <ul className="t42-line-list">
              {audit.items.map((item) => <li key={item.item_id}>
                <span>{item.title}</span>
                <p className="t42-line-text">{item.reason_text}</p>
              </li>)}
            </ul>}
    </details>
  );
}

function Dropped({dropped}){
  const items = dropped && Array.isArray(dropped.items) ? dropped.items : [];
  return (
    <section className="t42-section" data-section="dropped">
      <h3 className="t42-section-title">Dropped since yesterday</h3>
      {dropped && dropped.first_morning
        ? <p className="t42-line-text">{dropped.text}</p>
        : items.length > 0
          ? <ul className="t42-rows">
              {items.map((d) => (
                <li key={d.item_id} data-dropped-row="">
                  <span className="t42-row-name">{d.title}<span className="sr-only">: </span></span>
                  <span className="t42-row-reason">{d.reason_text}</span>
                </li>
              ))}
            </ul>
          : <p className="t42-line-text">Nothing dropped since yesterday.</p>}
    </section>
  );
}

function HeldBack({held}){
  const day = useContext(BriefDay);
  const [open, setOpen] = useState(null);
  const items = held && Array.isArray(held.items) ? held.items : [];
  return (
    <section className="t42-section" data-section="held-back">
      <h3 className="t42-section-title">Held back</h3>
      <p className="t42-line-text">{held && held.text ? held.text : 'Nothing held back ' + day + '.'}</p>
      <HeldReasons items={items} />
      {items.length > 0 && (
        <ul className="t42-rows">
          {items.map((h) => {
            const isOpen = open === h.item_id;
            const id = 't42-held-' + h.item_id;
            return (
              <li key={h.item_id}>
                <button type="button" className="t42-disclose" aria-expanded={isOpen ? 'true' : 'false'} aria-controls={id} onClick={() => setOpen(isOpen ? null : h.item_id)}>{h.title}</button>
                <div id={id} data-held-row="">
                  <HeldRowReason item={h} />
                  {isOpen && (
                    <div className="t42-held-detail">
                      <HeldItemDetail item={h} inRow />
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

// The held reason, and the stored check detail when it says something else, shown before the row is opened.
function HeldRowReason({item}){
  const reason = nonEmptyString(item.reason_text) ? item.reason_text.trim() : 'No specific held reason was provided.';
  const heldDetail = nonEmptyString(item.held_detail) ? item.held_detail.trim() : '';
  return (
    <>
      <p className="t42-line-text" data-held-row-reason="">{reason}</p>
      {heldDetail && heldDetail !== reason && <p className="t42-line-text" data-held-row-detail="">{heldDetail}</p>}
    </>
  );
}

// The brief job's fixed wording for a topic a busy model left unexplained (core/brief/payload.py MODEL_BUSY and
// MODEL_REFUSED). No check ran on it, so it reads as what happened, not as a check detail.
const BUSY_WORDS = {
  'Model busy: not explained before the deadline': 'The model was busy, so this was not explained before the deadline. It is not a failed check.',
  'Model busy: the model kept refusing calls, so this was not explained': 'The model was busy and kept refusing calls, so this was not explained. It is not a failed check.',
};

// inRow: the Held back row already shows the title, reason and held detail, so the opened part leaves them out.
function HeldItemDetail({item, inRow = false}){
  const reason = nonEmptyString(item.reason_text) ? item.reason_text : 'No specific held reason was provided.';
  const failedReason = nonEmptyString(item.failed_reason) ? item.failed_reason.trim() : '';
  const heldDetail = nonEmptyString(item.held_detail) ? item.held_detail.trim() : '';
  const numbers = Array.isArray(item.numbers)
    ? item.numbers.filter((number) => number && typeof number === 'object' && !Array.isArray(number))
    : [];
  const countLine = nonEmptyString(item.count_line) ? countLineWords(item.count_line) : '';
  const evidence = Array.isArray(item.evidence) ? item.evidence : [];
  const expectsCheckReason = item.reason === 'explanation_failed' || item.explanation_status === 'failed_checks';
  const hasDistinctFailedReason = failedReason && failedReason !== reason.trim();
  const hasDistinctHeldDetail = heldDetail && heldDetail !== reason.trim() && heldDetail !== failedReason;
  const hasStoredCheckDetail = Boolean(hasDistinctFailedReason || heldDetail);
  // check_detail "unavailable": the stored check rows could not be read just now, which is not the same as absent.
  const checkDetailUnavailable = item.check_detail === 'unavailable';

  return (
    <>
      {!inRow && <p className="t42-line-text"><strong>{item.title}</strong></p>}
      {!inRow && <p className="t42-line-text" data-held-reason="">{reason}</p>}
      {!inRow && hasDistinctHeldDetail && <p className="t42-line-text" data-held-detail="">{heldDetail}</p>}
      {hasDistinctFailedReason && (BUSY_WORDS[failedReason]
        ? <p className="t42-line-text" data-held-failed-reason="">{BUSY_WORDS[failedReason]}</p>
        : <p className="t42-line-text" data-held-failed-reason="">Check detail: {failedReason}</p>)}
      {expectsCheckReason && !hasStoredCheckDetail && checkDetailUnavailable
        && <p className="t42-line-text" data-held-check-detail-unavailable="">The check detail could not be read just now.</p>}
      {expectsCheckReason && !hasStoredCheckDetail && !checkDetailUnavailable
        && <p className="t42-line-text" data-held-check-reason-missing="">The specific check reason was not stored for this held item.</p>}
      {(countLine || numbers.length > 0) && <p className="t42-line-text" data-held-numbers-label="">Recorded figures</p>}
      {countLine && <p className="t42-count" data-held-count-line="">{countLine}</p>}
      {numbers.length > 0
        ? <ul className="t42-rows t42-held-numbers" data-held-numbers="">
            {numbers.map((number, index) => {
              const queryId = typeof number.query_id === 'string' && number.query_id.trim() ? number.query_id.trim() : '';
              const unit = typeof number.unit === 'string' ? number.unit.trim() : '';
              const recorded = typeof number.value === 'number' && Number.isFinite(number.value);
              return (
                <li key={index} data-held-number="" data-query-id={queryId || undefined}>
                  {recorded
                    ? <FigureLine number={number} />
                    : <p className="ask42-figure" data-held-number-unrecorded="">
                        <span className="ask42-figure-text">{'Not recorded' + (unit ? ' ' + unit : '') + '.'}</span>
                        {queryId && <span className="ask42-muted ask42-figure-query">{'Query ' + queryId}</span>}
                      </p>}
                </li>
              );
            })}
          </ul>
        : null}
      {evidence.length > 0
        ? <><PostsShownNote shown={evidence.length} numbers={numbers} /><EvidenceList items={evidence} /></>
        : <p className="t42-line-text" data-held-evidence-missing="">No source posts were stored for this held item.</p>}
    </>
  );
}

/* When no trend cleared a market, the held items are the page. Ten items
   shown whole, each with its posts and figures, made a wall in which the
   same reason repeated down the page. Items are grouped by their reason,
   the reason is said once above its group with a count, and each item's
   posts and figures wait behind its own disclosure (NN/g, progressive
   disclosure; Gestalt common region). Nothing is dropped: every item, its
   reason and its detail stay on the page. */
// Keyed by the gate rule, not the reason code: core/brief/job.py also gives
// reason data_issue to evidence that could not be read, where no day was invalid.
const HELD_RULE_HELP = {
  G1: 'At least one of the last three days had invalid data on the main platform, so these could not be checked fairly. They are checked again each day and can clear once no invalid day is left in that window.',
};
/* The gate holds evidence it could not read under G1 as well, but no day was invalid there, so the sentence about invalid days would be false for it. */
const UNREADABLE_EVIDENCE = 'Evidence could not be read';
function heldHelp(item){
  if (item.reason_raw === UNREADABLE_EVIDENCE) return '';
  if (nonEmptyString(item.reason_text) && item.reason_text.trim() === UNREADABLE_EVIDENCE) return '';
  return HELD_RULE_HELP[item.rule] || '';
}

/* A hold that names its own counts, "(3 of 40 with a known location)", ends in a bracket that holds a figure. The counts belong to the topic, so a group is made from the words ahead of them. */
const HELD_COUNTS = /\s*\([^()]*\d[^()]*\)\s*$/;
function heldStem(text){
  return text.replace(HELD_COUNTS, '').trim() || text;
}

/* A failed explanation reads the same in reason_text whichever check held it. The check is the name ahead of the colon in failed_reason ("Critic: ...", "Support check: ..."), so it splits the groups. A topic held for a busy model ran no check and keeps its own reason. */
const CHECK_FAMILY = /^([A-Z][A-Za-z ]{1,38}):/;
function checkFamily(item){
  if (item.reason !== 'explanation_failed' || !nonEmptyString(item.failed_reason) || item.failed_reason.startsWith('Model busy:')) return '';
  const match = CHECK_FAMILY.exec(item.failed_reason.trim());
  return match ? match[1] : '';
}

/* A group is a reason code plus the words of the reason with the topic's own counts set aside, so two not-local holds with different counts are one group. A group of one reads its reason whole; a group of several reads the words they share and leaves each topic its own counts. */
const NO_REASON = 'No specific held reason was provided.';
function heldGroups(items){
  const groups = [];
  const byReason = new Map();
  for (const item of items){
    const family = checkFamily(item);
    const said = nonEmptyString(item.reason_text) ? item.reason_text.trim() : NO_REASON;
    const stem = heldStem(said);
    const help = heldHelp(item);
    const code = nonEmptyString(item.reason) ? item.reason : '';
    const key = code + '\n' + (family ? stem + ': ' + family : stem) + '\n' + help;
    let group = byReason.get(key);
    if (!group){
      group = {key, stem, family, help, items: []};
      byReason.set(key, group);
      groups.push(group);
    }
    group.items.push(item);
  }
  for (const group of groups){
    const said = group.items.map((item) => (nonEmptyString(item.reason_text) ? item.reason_text.trim() : NO_REASON));
    const whole = said.every((words) => words === said[0]) ? said[0] : group.stem;
    group.whole = whole;
    group.reason = group.family ? whole + ': ' + group.family : whole;
  }
  return groups.sort((a, b) => b.items.length - a.items.length);
}

/* Charts, 3 October 2026: why topics were held back, as one bar split by
   the check each did not pass, counted from the held items themselves. */
function HeldReasons({items}){
  if (items.length < 2) return null;
  const groups = heldGroups(items);
  return (
    <PartsBar title="Why topics were held back" data="data-held-reasons"
      caption={items.length + ' held topics, by the check each did not pass'}
      parts={groups.map((group) => ({key: group.key, label: group.reason, value: group.items.length}))} />
  );
}

function HeldForEvidence({held, market}){
  const day = useContext(BriefDay);
  const items = held && Array.isArray(held.items) ? held.items : [];
  return (
    <details open data-section="held-for-evidence" id={'t42-held-' + market} tabIndex={-1}>
      <summary>Held back</summary>
      <HeldReasons items={items} />
      {items.length > 0
        ? heldGroups(items).map((group, index) => (
            <section key={group.key} className="t42-held-group" data-held-group="" aria-labelledby={'t42-held-group-' + market + '-' + index}>
              <h3 className="t42-held-group-reason" id={'t42-held-group-' + market + '-' + index} data-held-group-reason="">
                {group.reason}
                <span className="t42-held-group-count">{group.items.length === 1 ? '1 topic' : group.items.length + ' topics'}</span>
              </h3>
              {group.help && <p className="t42-line-text t42-held-group-help">{group.help}</p>}
              <ul className="t42-rows t42-held-list">
                {group.items.map((item) => <HeldGroupItem key={item.item_id} item={item} reason={group.reason} said={group.whole} />)}
              </ul>
            </section>
          ))
        : held && Number.isInteger(held.count) && held.count === 0
          ? <p className="t42-line-text">Nothing held back {day}.</p>
          : <p className="t42-line-text">Held item details are unavailable.</p>}
    </details>
  );
}

function HeldGroupItem({item, reason, said}){
  const heldDetail = nonEmptyString(item.held_detail) ? item.held_detail.trim() : '';
  const own = nonEmptyString(item.reason_text) ? item.reason_text.trim() : '';
  return (
    <li data-held-item-id={item.item_id}>
      <strong className="t42-held-title">{item.title}</strong>
      {own && own !== said && <p className="t42-line-text" data-held-own-reason="">{own}</p>}
      {heldDetail && heldDetail !== reason && <p className="t42-line-text" data-held-detail="">{heldDetail}</p>}
      <details className="t42-held-more">
        <summary>Posts and figures<span className="sr-only"> for {item.title}</span></summary>
        <div className="t42-held-detail">
          <HeldItemDetail item={item} inRow />
        </div>
      </details>
    </li>
  );
}

function Moments({moments}){
  const items = Array.isArray(moments) ? moments : [];
  return (
    <section className="t42-section" data-section="moments">
      {/* Short enough to hold one line at phone width; each moment carries its date. */}
      <h3 className="t42-section-title">Coming up in 14 days</h3>
      {items.length > 0
        ? <ul className="t42-row">
            {items.map((m) => (
              <li key={m.date + m.name} className="t42-chip">
                {/* One inline line, so the row's flex centring never pulls
                    the facts apart into columns or drops their spaces. */}
                <span><Facts parts={[longDate(m.date), m.name, MOMENT_WORDS[m.kind]]} /></span>
              </li>
            ))}
          </ul>
        : <p className="t42-line-text">No moments in the next 14 days.</p>}
    </section>
  );
}
