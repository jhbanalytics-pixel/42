/* Seeds: what 42 will search for next in one market, and what its last
   searches found (core/api/contract.md section 19).

   Page port, 3 October 2026: the old page showed editorial seed behaviours
   from the desk's /api/seeds, which f42-api never served and which had no 42
   data behind them. It now reads 42's own GET /api/seeds, built from the
   seed_queue table the morning detect run and the news harvest write and the
   02:00 collection reads. Every number keeps its query id; a search that
   names a person is only ever a count, and the page refuses to show one even
   if a payload carried it. */
import {useEffect, useState} from 'react';
import {forgetCached, useApi, readerFigure, sentenceCase} from './api.js';
import {REGION_NAME} from './model.js';
import {NextActions, PageHero, EmptyState} from './ui/index.js';
import {longDate} from './ui/TrendCard.jsx';
import {LoopRail} from './loopRail.jsx';
import './styles/seeds42.css';

const MARKETS = ['ZA', 'NG', 'KE'];
const STATUSES = ['ok', 'empty', 'not_ready'];
const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

/* The next places a reader goes from Seeds. Rendered under the page as a
   next action row, the same row the workspaces carry. */
const SEEDS_NEXT = [
  {label: 'Open Discover', route: '/explore'},
  {label: 'Open Seed path', route: '/seedpath'},
];

/* How 42 picked a search, in the reader's words. The API sends the same
   words; the page keeps its own copy so an unknown lane is never named. */
const LANE_WORDS = {
  expansion: 'Following what is rising',
  exploration: 'Trying something new',
  anchor: 'Re-checking what worked',
};
export function laneWords(lane){
  return Object.hasOwn(LANE_WORDS, lane || '') ? LANE_WORDS[lane] : 'Other searches';
}

/* Quiet register, 23 Sept 2026: a label on Seeds is the sans at 14px in
   muted, sentence case written at the source, with no tracking. */
const LABEL = {
  fontFamily: 'var(--sans)', fontSize: 'var(--type-3)', color: 'var(--muted)', fontWeight: 400,
};

const record = (v) => !!v && typeof v === 'object' && !Array.isArray(v);
const text = (v) => typeof v === 'string';
const isFigure = (v) => record(v) && typeof v.value === 'number' && text(v.query_id);
const figureOrNull = (v) => v === null || v === undefined || isFigure(v);

function validSeed(s, numbers){
  return record(s) && (s.label === null || text(s.label)) && (s.href === null || s.href === undefined || text(s.href))
    && Array.isArray(s.platforms) && s.platforms.every(text) && numbers.every((key) => figureOrNull(s[key]));
}

/* A response is shown only when it answers this market and every number it
   carries is a Figure. Anything else is refused as incomplete, never patched. */
export function validSeedsPayload(value, market = 'ZA'){
  if (!record(value) || String(value.market || '') !== String(market || '').toUpperCase()) return false;
  if (!STATUSES.includes(value.status) || !('queue' in value) || !('results' in value)) return false;
  if (value.status !== 'ok' && !(text(value.message) && value.message.trim())) return false;
  const q = value.queue;
  const r = value.results;
  if (q !== null && !(record(q) && Array.isArray(q.seeds) && q.seeds.every((s) => validSeed(s, ['credits']))
      && Array.isArray(q.lanes) && isFigure(q.seeds_total) && figureOrNull(q.credits_total))) return false;
  if (r !== null && !(record(r) && Array.isArray(r.seeds) && r.seeds.every((s) => validSeed(s, ['posts', 'new_creators']))
      && Array.isArray(r.lanes) && record(r.totals) && isFigure(r.totals.seeds))) return false;
  return true;
}

/* A seed that names a person never reaches the screen (contract section
   12.1). The API already counts these as creator accounts; this holds the
   rule on the page too. */
function namesAPerson(s){
  return s.kind === 'creator' || /(^|[^\w.])@[\w.]/.test(s.label || '');
}

function dayWords(value){
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''));
  if (!match) return null;
  const day = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))).getUTCDay();
  return WEEKDAYS[day] + ' ' + longDate(value);
}

export function queueDateLine(date){
  const words = dayWords(date);
  return words ? 'Queued for ' + words + '.' : 'The date of this queue was not recorded.';
}

function plural(n, one, many){
  return readerFigure(n) + ' ' + (Number(n) === 1 ? one : many);
}

/* Credit estimates are planning figures, so they read to one decimal; the
   exact value stays in the title (live audit F24: "about 27.313364055299537"). */
export function creditWords(n){
  const value = Number(n);
  if (!Number.isFinite(value)) return readerFigure(n);
  return readerFigure(Math.round(value * 10) / 10);
}

/* A number with its query: the id rides on the element and in its title. */
function Fig({figure, children, credit = false}){
  if (!isFigure(figure)) return <span className="sd42-none">Not recorded</span>;
  const title = 'Query ' + figure.query_id + (credit ? ' · exact ' + figure.value : '');
  return <span className="sd42-fig" data-query-id={figure.query_id} title={title}>{children ?? (credit ? creditWords(figure.value) : readerFigure(figure.value))}</span>;
}

function askHref(label, market){
  const where = REGION_NAME[market] || market;
  const q = 'What is happening with "' + label + '" in ' + where + ' right now?';
  return '#/ask?q=' + encodeURIComponent(q) + '&market=' + market + '&draft=1';
}

function SeedName({s}){
  const label = s.label || 'Not named';
  return s.href ? <a className="sd42-topic" href={s.href}>{label}</a> : <span className="sd42-topic">{label}</span>;
}

export function QueueTable({seeds, market}){
  const mk = String(market || 'ZA').toUpperCase();
  const shown = (seeds || []).filter((s) => !namesAPerson(s));
  return (
    <table className="sd42-table">
      <thead>
        <tr>
          <th scope="col">Search</th>
          <th scope="col">How 42 picked it</th>
          <th scope="col">Where 42 will look</th>
          <th scope="col" className="sd42-num">Credits</th>
          <th scope="col"><span className="sd42-hide">Ask</span></th>
        </tr>
      </thead>
      <tbody>
        {shown.map((s, i) => (
          <tr key={(s.item_id || s.label || '') + i}>
            <th scope="row"><SeedName s={s} />{s.kind ? <span className="sd42-kind">{sentenceCase(s.kind)}</span> : null}</th>
            <td data-label="How 42 picked it">{laneWords(s.lane)}</td>
            <td data-label="Where 42 will look">{s.platforms.join(', ')}</td>
            <td data-label="Credits" className="sd42-num"><Fig figure={s.credits} credit /></td>
            <td className="sd42-ask">{s.label ? <a className="sd42-link" href={askHref(s.label, mk)}>Ask about this</a> : null}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ResultsTable({seeds}){
  const shown = (seeds || []).filter((s) => !namesAPerson(s));
  return (
    <table className="sd42-table">
      <thead>
        <tr>
          <th scope="col">Search</th>
          <th scope="col">How 42 picked it</th>
          <th scope="col" className="sd42-num">New posts</th>
          <th scope="col" className="sd42-num">New creators</th>
        </tr>
      </thead>
      <tbody>
        {shown.map((s, i) => (
          <tr key={(s.item_id || s.label || '') + i}>
            <th scope="row"><SeedName s={s} />{s.kind ? <span className="sd42-kind">{sentenceCase(s.kind)}</span> : null}</th>
            <td data-label="How 42 picked it">{laneWords(s.lane)}</td>
            <td data-label="New posts" className="sd42-num"><Fig figure={s.posts} /></td>
            <td data-label="New creators" className="sd42-num"><Fig figure={s.new_creators} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Queue({q, where, market}){
  const day = dayWords(q.seed_date) || 'a day not recorded';
  const credits = isFigure(q.credits_total) ? q.credits_total.value : null;
  const people = q.creator_accounts && isFigure(q.creator_accounts.seeds) ? q.creator_accounts.seeds.value : 0;
  const searches = <Fig figure={q.seeds_total}>{plural(q.seeds_total.value, 'search', 'searches')}</Fig>;
  return (
    <section className="sd42-section" aria-labelledby="sd42-next">
      <h2 id="sd42-next" className="sd42-title">Next searches</h2>
      {/* Review, 3 October 2026: the latest queued day can already have run
          or be an old day, so only a day still to come says will run. */}
      <p className="sd42-lead">
        {q.when === 'today'
          ? <>42 queued {searches} in {where} for today, {day}</>
          : q.when === 'past'
            ? <>The last list 42 queued in {where} was for {day}: {searches}</>
            : <>42 will run {searches} in {where} on {day}</>}
        {credits !== null ? <>, about <Fig figure={q.credits_total} credit>{creditWords(credits) + ' ' + (Number(credits) === 1 ? 'credit' : 'credits')}</Fig></> : null}.
        {q.when === 'past' ? ' Nothing newer is queued yet.' : null}
      </p>
      {q.lanes.length > 0 && (
        <>
        <p style={LABEL}>By how 42 picked them</p>
        <ul className="sd42-lanes">
          {q.lanes.map((l) => (
            <li key={String(l.lane)}><span className="sd42-lane">{laneWords(l.lane)}</span> <Fig figure={l.seeds}>{plural(l.seeds.value, 'search', 'searches')}</Fig></li>
          ))}
        </ul>
        </>
      )}
      <QueueTable seeds={q.seeds} market={market} />
      {people > 0 && (
        <p className="sd42-quiet"><Fig figure={q.creator_accounts.seeds}>{people}</Fig> of these {people === 1 ? 'searches is of a creator account' : 'searches are of creator accounts'}. 42 counts these but does not name the people behind them here.</p>
      )}
      {q.truncated && <p className="sd42-quiet">This list is cut; only the first searches of the day were read.</p>}
    </section>
  );
}

function Results({r}){
  const day = dayWords(r.seed_date) || 'a day not recorded';
  const t = r.totals;
  const people = r.creator_accounts && isFigure(r.creator_accounts.seeds) ? r.creator_accounts.seeds.value : 0;
  return (
    <section className="sd42-section" aria-labelledby="sd42-found">
      <h2 id="sd42-found" className="sd42-title">What the last searches found</h2>
      <p className="sd42-lead">
        The <Fig figure={t.seeds}>{plural(t.seeds.value, 'search', 'searches')}</Fig> of {day} found{' '}
        <Fig figure={t.posts}>{isFigure(t.posts) ? plural(t.posts.value, 'new post', 'new posts') : null}</Fig> and{' '}
        <Fig figure={t.new_creators}>{isFigure(t.new_creators) ? plural(t.new_creators.value, 'new creator', 'new creators') : null}</Fig>.
      </p>
      {r.lanes.length > 0 && (
        <>
        <p style={LABEL}>Found, by how 42 picked the searches</p>
        <ul className="sd42-lanes">
          {r.lanes.map((l) => (
            <li key={String(l.lane)}>
              <span className="sd42-lane">{laneWords(l.lane)}</span>{' '}
              <Fig figure={l.posts}>{isFigure(l.posts) ? plural(l.posts.value, 'new post', 'new posts') : null}</Fig> from <Fig figure={l.seeds}>{plural(l.seeds.value, 'search', 'searches')}</Fig>
            </li>
          ))}
        </ul>
        </>
      )}
      <ResultsTable seeds={r.seeds} />
      {people > 0 && (
        <p className="sd42-quiet">Searches of <Fig figure={r.creator_accounts.seeds}>{plural(people, 'creator account', 'creator accounts')}</Fig> found <Fig figure={r.creator_accounts.posts}>{isFigure(r.creator_accounts.posts) ? plural(r.creator_accounts.posts.value, 'new post', 'new posts') : null}</Fig>; 42 does not name those people here.</p>
      )}
      {r.truncated && <p className="sd42-quiet">This list is cut; only the first searches of the day were read.</p>}
    </section>
  );
}

function HowPicked({about}){
  const rows = (about || []).filter((a) => record(a) && text(a.about) && Object.hasOwn(LANE_WORDS, a.lane || ''));
  if (!rows.length) return null;
  return (
    <details className="sd42-how">
      <summary>How 42 picks its searches</summary>
      <dl>
        {rows.map((a) => (
          <div key={a.lane}><dt>{laneWords(a.lane)}</dt><dd>{a.about}</dd></div>
        ))}
      </dl>
      <p>The searches run in the morning collection. A search's credits are an estimate made when it was queued.</p>
    </details>
  );
}

export function SeedsPage({session, onAuth, region, setRegion}){
  const start = MARKETS.includes(String(region || '').toUpperCase()) ? String(region).toUpperCase() : 'ZA';
  const [market, setMarket] = useState(start);
  useEffect(() => { setMarket(start); }, [start]);
  /* The queue, its waiting state and its "today" all change with the
     morning run, so each visit drops the session's earlier Seeds reads and
     reads again, before the first read below. */
  useState(() => forgetCached('/api/seeds?'));
  const [data, retry] = useApi('/api/seeds?market=' + market, session, onAuth);
  const where = REGION_NAME[market] || market;
  /* Page data audit, 3 October 2026: a service without the route answers
     its "No such API route." 404. Try again could never help there, so the
     page says Seeds is not in this version and offers Today. */
  const unavailable = data.state === 'error' && data.code === 'http_404' && data.message === 'No such API route.';
  const waiting409 = data.state === 'error' && ['not_ready', 'http_409'].includes(data.code);
  const valid = data.state === 'ready' && validSeedsPayload(data.data, market);
  const d = valid ? data.data : null;
  const shows = !!d && d.status === 'ok' && (d.queue || d.results);

  const choose = (mk) => {
    setMarket(mk);
    setRegion?.(mk);
  };

  return (
    <div className="page seeds">
      <div className="page-shell">
        <div className="reveal">
          <PageHero
            title="Seeds"
            sub="What will 42 search for next, and what did its last searches find? One market at a time."
          >
            {/* Design audit, 2 October 2026: a key for lists that are not on
                screen is noise, so it shows only once a list renders. */}
            {shows && (
              <p className="seed-key">
                <span style={LABEL}>How 42 picks a search:</span>
                {Object.values(LANE_WORDS).map((w) => <span key={w} className="sd42-key-item">{w}</span>)}
              </p>
            )}
          </PageHero>
        </div>

        <LoopRail active="seeds" />

        <div className="sd42-markets loop-tabs" role="group" aria-labelledby="sd42-market-label">
          <span id="sd42-market-label" className="sd42-hide">Market</span>
          {MARKETS.map((mk) => (
            <button key={mk} type="button" className="legacy-chip loop-tab" aria-pressed={market === mk} onClick={() => choose(mk)}>{REGION_NAME[mk]}</button>
          ))}
        </div>

        {data.state === 'loading' && <p className="sd42-quiet">Loading available seeds…</p>}
        {unavailable && <EmptyState title="Seeds is not available yet" body="Seeds are not served in this version, so no seed data was read." actions={[{label: 'Open Today', href: '#/pulse', primary: true}]} />}
        {(waiting409 || (d && d.status === 'not_ready')) && (
          <EmptyState title="Seeds is waiting for its first list" body={d ? d.message : 'Seeds fills once 42 has written its first list of searches.'} />
        )}
        {!unavailable && !waiting409 && (data.state === 'error' || (data.state === 'ready' && !valid)) && <EmptyState title="Seeds could not load" body="Try again in a moment." cta={{label: 'Try again', onClick: retry}} />}
        {d && d.status === 'empty' && <EmptyState title={'Nothing queued for ' + where + ' yet'} body={d.message} />}

        {shows && (
          <>
            {d.queue
              ? <Queue q={d.queue} where={where} market={market} />
              : <p className="sd42-lead">Nothing is queued for {where} yet. 42 writes the next list each morning.</p>}
            {d.results && <Results r={d.results} />}
            {d.notes && d.notes.length > 0 && (
              <ul className="sd42-notes" aria-label="Notes">
                {d.notes.map((n) => <li key={n}>{n}</li>)}
              </ul>
            )}
            <HowPicked about={d.lanes_about} />
          </>
        )}

        {['ready', 'error'].includes(data.state) && <NextActions actions={SEEDS_NEXT} />}
      </div>
    </div>
  );
}

export default SeedsPage;
