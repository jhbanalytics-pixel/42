/* Creator pages and communities on the 42 API (contract.md sections 12.1 to
   12.3; EXPERIENCE.md, topic and creator pages tell the story of one thing).

   Anything that names a person shows only what the API sent: the API keeps
   creators below macro out of pages and member lists, keeps sensitive,
   held-back, coordinated and paid-led items away from names, and drops
   Evidence flags. So this page never builds a handle, a follower count or
   a profile link of its own, and never reads coord_score. A part the API
   has not filled yet says so in words instead of hiding.

   Staff hide a person from the app (section 16) from a creator page or
   beside a named member of a community. The app only adds to L1's list and
   shows it at #/people/hidden; nothing here lifts a hide. */
import React, {useEffect, useId, useRef, useState} from 'react';
import {fetchCommunities, fetchCommunity, fetchCreator, hidePerson, listHidden} from './api42.js';
import {readerFigure, sentenceCase} from './api.js';
import {go} from './router.js';
import {safeUrl} from './safeUrl.js';
import {TrendCard, longDate, platformWord, topicHref} from './ui/TrendCard.jsx';
import {useFocusTrap} from './ui/useFocusTrap.js';
import './styles/people42.css';
import './styles/alerts42.css';
import './styles/hidden42.css';
import './styles/communities42.css';

const MARKET_WORDS = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya'};
const TIER_WORDS = {nano: 'Nano', micro: 'Micro', mid: 'Mid', macro: 'Macro', mega: 'Mega'};
const LANGUAGE_WORDS = {
  en: 'English', pcm: 'Nigerian Pidgin', yo: 'Yoruba', ha: 'Hausa', ig: 'Igbo', sw: 'Swahili', sheng: 'Sheng',
  zu: 'isiZulu', xh: 'isiXhosa', af: 'Afrikaans', st: 'Sesotho', tn: 'Setswana', nso: 'Sepedi', ts: 'Xitsonga',
  ve: 'Tshivenda', ss: 'siSwati', nr: 'isiNdebele', fr: 'French', ar: 'Arabic', und: 'Language not identified',
  /* Visual QA, 5 October 2026: every code enrichment may record has a name
     (core/understand/enrich.py LANGS), so Kenya no longer shows "so". */
  pt: 'Portuguese', so: 'Somali', am: 'Amharic', luo: 'Dholuo', ki: 'Gikuyu', kam: 'Kikamba', ff: 'Fulfulde',
  efi: 'Efik', tiv: 'Tiv',
};
const NO_SENSITIVE_SET = 'Topics per creator appear once 42 can keep sensitive topics off named pages';
/* Demo polish, 2 October 2026: the method line says plainly what the
   grouping is and what it does not count yet. Only the service's exact old
   line is reworded; any other line it sends is shown as sent. */
const OLD_INTERACTION = 'Grouped by shared topics; interaction not yet measured';
const INTERACTION = 'Grouped by the topics they share. Replies and mentions between them are not counted yet.';
const methodLine = (text) => (!text || text === OLD_INTERACTION ? INTERACTION : text);
const MATCHED_NONE = 'No one matches this now; they will be hidden if they appear.';
const NO_ANSWER = 'The 42 service did not answer. Try again.';

/* The words a hide that matched no one leaves for the list it leads to,
   read once when that list mounts. */
let hiddenNote = null;

const isFigure = (value) => Boolean(value && typeof value === 'object' && value.value !== undefined && value.value !== null);
const marketWord = (market) => MARKET_WORDS[market] || market;
const tierWord = (tier) => TIER_WORDS[tier] || sentenceCase(tier);
const languageWord = (lang) => LANGUAGE_WORDS[lang] || (lang ? 'Other language (' + lang + ')' : 'Language not identified');
const creatorHref = (creatorId, market) => '#/creators/' + encodeURIComponent(creatorId) + (market ? '?market=' + encodeURIComponent(market) : '');
const communityHref = (communityId, market) => '#/communities/' + encodeURIComponent(communityId) + (market ? '?market=' + encodeURIComponent(market) : '');
/* Rule 3 of section 12.1: no coordination or payment next to a name. The
   API already leaves those items off named pages; a card that still arrives
   with one of them, by code or by word, is left out here too. Other flags
   the API passes on purpose (not assessed, data issue) stay. */
const RULE_3_FLAGS = new Set(['likely_coordinated', 'check_pattern', 'paid_led']);
const RULE_3_WORDS = new Set(['likely coordinated', 'check pattern', 'check the pattern', 'paid led']);
const flagWords = (value) => String(value || '').toLowerCase().replace(/[-_\s]+/g, ' ').trim();
const unflagged = (card) => Boolean(card)
  && !RULE_3_FLAGS.has(card.flag)
  && !RULE_3_WORDS.has(flagWords(card.flag_word))
  && !(Array.isArray(card.flags) && card.flags.some((f) => RULE_3_FLAGS.has(f)));
/* A creator as the API names them: the handle it sent, else the platform. */
const creatorName = (c) => (c && typeof c.handle === 'string' && c.handle.trim()) ? c.handle : platformWord(c && c.platform) + ' creator';

/* A Figure's value with the query that produced it on the element. */
function Fig({figure, children}){
  return (
    <span className="pp42-fig" data-query-id={figure.query_id} title={'Query ' + figure.query_id + ', run ' + figure.run_id}>
      {children ?? readerFigure(figure.value)}
    </span>
  );
}

function FigureWords({figure, missing}){
  if (!isFigure(figure)) return <span className="pp42-muted">{missing}</span>;
  return <><Fig figure={figure} />{figure.unit ? ' ' + String(figure.unit).trim() : ''}</>;
}

/* One read, its loading, passcode, not-found and failure states. */
function useRead(read, deps, onAuth, timeoutMs = 0){
  const [load, setLoad] = useState({state: 'loading'});
  const [tick, setTick] = useState(0);
  const authRef = useRef(onAuth);
  authRef.current = onAuth;
  useEffect(() => {
    const ctrl = new AbortController();
    let timer = null;
    const clearTimer = () => {
      if (timer !== null){
        clearTimeout(timer);
        timer = null;
      }
    };
    setLoad({state: 'loading'});
    if (timeoutMs > 0){
      timer = setTimeout(() => {
        if (!ctrl.signal.aborted){
          setLoad({state: 'error', message: 'The 42 service did not answer.'});
          ctrl.abort();
        }
      }, timeoutMs);
    }
    read({signal: ctrl.signal})
      .then((data) => {
        clearTimer();
        if (!ctrl.signal.aborted) setLoad({state: 'ready', data: data || {}});
      })
      .catch((error) => {
        clearTimer();
        if (ctrl.signal.aborted) return;
        if (error && error.auth){
          setLoad({state: 'auth'});
          if (authRef.current) authRef.current();
        } else if (error && error.status === 404){
          setLoad({state: 'missing', message: error.message});
        } else {
          setLoad({state: 'error', message: error && error.status ? error.message : 'The 42 service did not answer.'});
        }
      });
    return () => {
      clearTimer();
      ctrl.abort();
    };
  }, [...deps, tick, timeoutMs]);
  return [load, () => setTick((t) => t + 1)];
}

function Frame({load, retry, what, missingTitle, children}){
  if (load.state === 'loading'){
    return <section className="page pp42" aria-busy="true"><p className="pp42-status" role="status">Loading the {what}</p></section>;
  }
  if (load.state === 'auth'){
    return <section className="page pp42"><p className="pp42-status" role="status">Enter the passcode to read this {what}.</p></section>;
  }
  if (load.state === 'missing'){
    return (
      <section className="page pp42">
        <h1 className="pp42-heading">{missingTitle}</h1>
        <p className="pp42-status">{load.message}</p>
      </section>
    );
  }
  if (load.state === 'error'){
    return (
      <section className="page pp42">
        <h1 className="pp42-heading">The {what} could not load</h1>
        <p className="pp42-status" role="alert">{load.message}</p>
        <button type="button" className="pp42-button" onClick={retry}>Try again</button>
      </section>
    );
  }
  return children(load.data);
}

function Part({name, title, children}){
  return (
    <section className="pp42-part" data-section={name}>
      <h2 className="pp42-part-title">{title}</h2>
      {children}
    </section>
  );
}

function windowLine(data){
  const w = data.window;
  if (!w || !w.from || !w.to) return null;
  return 'Posts first seen in ' + marketWord(data.market) + ', ' + longDate(w.from) + ' to ' + longDate(w.to);
}

function ProfileLink({url}){
  const href = safeUrl(url);
  return href ? <a className="pp42-link" href={href} target="_blank" rel="noopener noreferrer">Open the profile</a> : null;
}

/* Posts as the API sent them: flags are already dropped on named pages. */
function Posts({items}){
  return (
    <ul className="pp42-posts">
      {items.map((e) => {
        const views = e.engagement && typeof e.engagement.views === 'number' ? e.engagement.views : null;
        const meta = [platformWord(e.platform), e.handle, e.posted_at ? longDate(e.posted_at) : null, views !== null ? readerFigure(views) + ' views' : null]
          .filter(Boolean).join(' · ');
        const name = e.handle ? 'Post by ' + e.handle + ' on ' + platformWord(e.platform) : 'Post on ' + platformWord(e.platform);
        const url = safeUrl(e.url);
        const src = safeUrl(e.thumbnail_url);
        return (
          <li key={e.id} className="pp42-post">
            {src
              ? <img className="pp42-thumb" src={src} alt={name} width="72" height="96" loading="lazy" />
              : <span className="pp42-thumb pp42-thumb-empty" role="img" aria-label={name + ', no image'} />}
            <div className="pp42-post-body">
              <p className="pp42-post-meta">{meta}</p>
              {e.text && <p className="pp42-post-text">{e.text}</p>}
              {url && <a className="pp42-link" href={url} target="_blank" rel="noopener noreferrer">Open the post</a>}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/* ---------------- hiding a person (section 16) ---------------- */

/* A quiet button, never the page's red, and the dialog it opens. Closing
   the dialog gives focus back to the button. */
function HideAction({creator, onAuth}){
  const [open, setOpen] = useState(false);
  const opener = useRef(null);
  const name = creatorName(creator);
  const close = () => {
    setOpen(false);
    if (opener.current) opener.current.focus();
  };
  return (
    <>
      <button ref={opener} type="button" className="pp42-button" aria-label={'Hide this person, ' + name} onClick={() => setOpen(true)}>Hide this person</button>
      {open && <HideDialog creator={creator} onAuth={onAuth} onClose={close} />}
    </>
  );
}

/* Cancel takes first focus, so Enter on opening hides no one, and Hide is
   the one red action. On the 201 the page is left for the list, since the
   person is gone from the next read; a refusal shows the server's words. */
function HideDialog({creator, onAuth, onClose}){
  const [reason, setReason] = useState('');
  const [who, setWho] = useState('');
  const [state, setState] = useState({status: 'idle'});
  const id = useId();
  const cancel = useRef(null);
  const box = useRef(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    if (cancel.current) cancel.current.focus();
    return () => { live.current = false; };
  }, []);
  useFocusTrap(box);

  const busy = state.status === 'hiding';
  const send = () => {
    const why = reason.trim();
    const name = who.trim();
    if (why.length < 3 || why.length > 300){ setState({status: 'error', message: 'Say why in 3 to 300 characters.'}); return; }
    if (name.length < 2 || name.length > 60){ setState({status: 'error', message: 'Give your name in 2 to 60 characters.'}); return; }
    setState({status: 'hiding'});
    hidePerson({creator_id: creator.creator_id, reason: why, who: name})
      .then((row) => {
        if (!live.current) return;
        hiddenNote = row && row.matched === 0 ? MATCHED_NONE : null;
        onClose();
        go('/people/hidden');
      })
      .catch((failure) => {
        if (!live.current) return;
        if (failure && failure.auth){ onClose(); if (onAuth) onAuth(); return; }
        setState({status: 'error', message: failure && failure.status ? failure.message : NO_ANSWER});
      });
  };
  const close = () => { if (!busy) onClose(); };
  const where = creatorName(creator) + (creator.handle && creator.platform ? ' on ' + platformWord(creator.platform) : '');

  return (
    <div className="w42-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div ref={box} className="w42-dialog" role="dialog" aria-modal="true" aria-labelledby={id + '-title'}
        onKeyDown={(e) => { if (e.key === 'Escape'){ e.stopPropagation(); close(); } }}>
        <h2 id={id + '-title'} className="w42-title">Hide this person?</h2>
        <p className="w42-where">{where}</p>
        <p className="w42-line">They leave creator pages, communities and reports from the next load. Only the 42 team can bring them back.</p>
        <form className="a42-form" onSubmit={(e) => { e.preventDefault(); if (!busy) send(); }} noValidate>
          <span className="a42-field">
            <label className="a42-label" htmlFor={id + '-reason'}>Why</label>
            <textarea id={id + '-reason'} name="reason" className="a42-control" rows={3} maxLength={300} aria-describedby={id + '-reason-note'}
              style={{padding: 'var(--s-2) var(--s-3)', resize: 'vertical'}} value={reason} onChange={(e) => setReason(e.target.value)} />
            <span id={id + '-reason-note'} className="a42-label">3 to 300 characters. No contact details.</span>
          </span>
          <span className="a42-field">
            <label className="a42-label" htmlFor={id + '-who'}>Your name</label>
            <input id={id + '-who'} name="who" type="text" className="a42-control" maxLength={60} aria-describedby={id + '-who-note'}
              value={who} onChange={(e) => setWho(e.target.value)} />
            <span id={id + '-who-note'} className="a42-label">2 to 60 characters, shown on the list of hidden people.</span>
          </span>
          {state.status === 'error' && <p className="w42-error" role="alert">{state.message}</p>}
          <div className="w42-actions">
            <button ref={cancel} type="button" className="pp42-button" onClick={close} disabled={busy}>Cancel</button>
            <button type="submit" className="w42-primary" disabled={busy} aria-busy={busy ? 'true' : 'false'}>Hide</button>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ---------------- a creator, #/creators/<id>?market= ---------------- */

export function CreatorPage42({creatorId, market, onAuth}){
  const [load, retry] = useRead((options) => fetchCreator(creatorId, market, options), [creatorId, market], onAuth);
  return (
    <Frame load={load} retry={retry} what="creator" missingTitle="Creator not shown">
      {(data) => <Creator data={data} market={market} onAuth={onAuth} />}
    </Frame>
  );
}

function Creator({data, market, onAuth}){
  const c = data.creator || {};
  const where = data.market || market;
  const items = Array.isArray(data.items) ? data.items.filter((row) => row && unflagged(row.card)) : data.items;
  const recent = data.recent_posts;
  const reach = data.reach || {};
  const line = windowLine(data);
  const facts = [
    c.platform ? platformWord(c.platform) : null,
    c.tier ? tierWord(c.tier) : null,
  ].filter(Boolean);
  return (
    <section className="page pp42">
      <header className="pp42-head" data-section="creator">
        <h1 className="pp42-heading">{creatorName(c)}</h1>
        <p className="pp42-sub">
          {facts.join(' · ')}
          {isFigure(c.followers) && <>{facts.length ? ' · ' : ''}<Fig figure={c.followers} /> followers</>}
          {c.home_market && <>{' · '}Home market {marketWord(c.home_market)}</>}
        </p>
        <ProfileLink url={c.profile_url} />
        {c.creator_id && <p className="pp42-line"><a className="pp42-link" href={'#/ask?fit=' + encodeURIComponent(c.creator_id) + '&market=' + encodeURIComponent(where)}>Creator fit</a></p>}
        {c.creator_id && <HideAction creator={c} onAuth={onAuth} />}
      </header>
      {line && <p className="pp42-line">{line}</p>}

      <Part name="recent" title="Recent posts">
        {recent === null || recent === undefined
          ? data.recent_posts_note && <p className="pp42-line">{data.recent_posts_note}</p>
          : recent.length > 0 ? <Posts items={recent} /> : <p className="pp42-line">No posts first seen in the last 28 days.</p>}
      </Part>

      <Part name="items" title="Topics">
        {items === null || items === undefined
          ? <p className="pp42-line">{data.note || NO_SENSITIVE_SET}</p>
          : items.length > 0
            ? <ul className="pp42-cards">
                {items.map((row) => (
                  <li key={row.card.item_id} className="pp42-card" data-item={row.card.item_id}>
                    <ul className="pp42-card-list">
                      <TrendCard card={row.card} market={row.card.market || where} date={row.card.date} linkTopic posts={false} />
                    </ul>
                    {isFigure(row.posts) && <p className="pp42-line"><FigureWords figure={row.posts} /></p>}
                  </li>
                ))}
              </ul>
            : <p className="pp42-line">No topics 42 can show beside this creator.</p>}
      </Part>

      <div className="pp42-grid">
        <Part name="formats" title="Formats">
          {Array.isArray(data.formats) && data.formats.length > 0
            ? <ul className="pp42-rows">
                {data.formats.map((f) => <li key={f.format}>{sentenceCase(String(f.format).replace(/_/g, ' '))}: <FigureWords figure={f.posts} missing="not measured yet" /></li>)}
              </ul>
            : <p className="pp42-line">Formats are not measured yet.</p>}
        </Part>
        <Part name="reach" title="Reach">
          <ul className="pp42-rows">
            <li>Views on the middle post: {isFigure(reach.median_views) ? <Fig figure={reach.median_views} /> : <span className="pp42-muted">not measured yet</span>}</li>
            <li><FigureWords figure={reach.posts_28d} missing="Posts not measured yet" /></li>
          </ul>
        </Part>
        {data.community && data.community.community_id && (
          <Part name="community" title="Community">
            <a className="pp42-link" href={communityHref(data.community.community_id, where)}>{data.community.label}</a>
          </Part>
        )}
      </div>
    </section>
  );
}

/* ---------------- communities, #/communities?market= ---------------- */

/* What fills a community, said once where a list is empty. */
const COMMUNITY_RULE = 'A community forms when five or more creators in one market post about the same topics within 28 days. 42 links creators by the topics they share, leaving out sensitive, held-back and flagged topics.';
const NAMED_ON_LIST = 4;
const communitiesHref = (market) => '#/communities?market=' + encodeURIComponent(market);
/* The cards the API sent for a community, minus any that still carry a rule 3 flag. */
const sharedTopics = (c) => (Array.isArray(c && c.top_items) ? c.top_items.filter(unflagged) : []);
const plural = (n, one, many) => (n === 1 ? one : many);

export function CommunitiesPage42({market, onAuth}){
  const [load, retry] = useRead((options) => fetchCommunities(market, options), [market], onAuth, 30000);
  return (
    <Frame load={load} retry={retry} what="communities" missingTitle="Communities not found">
      {(data) => <Communities data={data} market={market} />}
    </Frame>
  );
}

function MethodWords({data}){
  return (
    <>
      <p className="pp42-line">{methodLine(data.interaction)}</p>
      {data.note && <p className="pp42-line">{data.note}</p>}
    </>
  );
}

/* The window line with each date kept whole, so a day never parts from its month. */
function WindowWords({data}){
  const w = data.window;
  if (!w || !w.from || !w.to) return null;
  return (
    <p className="pp42-sub">
      Posts first seen in {marketWord(data.market)}, <span className="cm42-nowrap">{longDate(w.from)}</span> to <span className="cm42-nowrap">{longDate(w.to)}</span>
    </p>
  );
}

/* South Africa, Nigeria, Kenya as links, the one on screen marked. */
function MarketLinks({where}){
  return (
    <nav className="cm42-markets" aria-label="Market">
      {Object.keys(MARKET_WORDS).map((code) => (
        <a key={code} className="cm42-market" href={communitiesHref(code)} aria-current={code === where ? 'page' : undefined}>{MARKET_WORDS[code]}</a>
      ))}
    </nav>
  );
}

function TopicLinks({cards, market}){
  if (cards.length === 0) return null;
  return (
    <ul className="cm42-topics" aria-label="Top shared topics">
      {cards.map((card) => (
        <li key={card.item_id}><a className="cm42-topic" href={topicHref(card.item_id, card.market || market)}>{card.title}</a></li>
      ))}
    </ul>
  );
}

function Communities({data, market}){
  const where = data.market || market;
  const list = Array.isArray(data.communities) ? data.communities : [];
  return (
    <section className="page pp42 cm42">
      <header className="pp42-head cm42-head">
        <h1 className="pp42-heading">Communities in {marketWord(where)}</h1>
        <WindowWords data={data} />
        <MarketLinks where={where} />
      </header>
      <div className={'cm42-grid' + (list.length > 0 ? '' : ' cm42-grid-empty')}>
        <div className="cm42-main">
          <MethodWords data={data} />
          {list.length > 0
            ? <ul className="cm42-list">
                {list.map((c) => <CommunityRow key={c.community_id} c={c} where={where} />)}
              </ul>
            : <EmptyCommunities where={where} />}
        </div>
        {list.length === 0 && <CommunityAnatomy />}
      </div>
    </section>
  );
}

/* What each community on the list shows, beside an empty market, so the
   page says what it will contain once a community forms. */
const COMMUNITY_PARTS = [
  ['Size', 'How many creators are in it.'],
  ['Shared topics', 'The topics its members post about, each a link to the topic.'],
  ['Named creators', 'The first few members, each a link to their creator page.'],
  ['Platforms and languages', 'Where its members post, and in which languages.'],
];

function CommunityAnatomy(){
  return (
    <aside className="cm42-aside" aria-labelledby="cm42-parts-title">
      <h2 className="cm42-aside-title" id="cm42-parts-title">What each community shows</h2>
      <dl className="cm42-parts">
        {COMMUNITY_PARTS.map(([term, line]) => (
          <div key={term} className="cm42-part"><dt>{term}</dt><dd>{line}</dd></div>
        ))}
      </dl>
    </aside>
  );
}

/* One community on the list: its size leads, then the topics its members
   share and the creators the API names (page tier only, rule 1). The list
   adds no name, handle or count of its own. */
function CommunityRow({c, where}){
  const topics = sharedTopics(c);
  const members = Array.isArray(c.members) ? c.members : null;
  const shown = members ? members.slice(0, NAMED_ON_LIST) : [];
  const platforms = Array.isArray(c.platforms) ? c.platforms : [];
  const languages = Array.isArray(c.languages) ? c.languages : [];
  return (
    <li className="cm42-row" data-community={c.community_id}>
      <div className="cm42-size">
        {isFigure(c.creators)
          ? <><span className="cm42-size-value"><Fig figure={c.creators} /></span> <span className="cm42-size-words">{c.creators.unit || 'creators'}</span></>
          : <span className="cm42-size-words">Creators not counted yet</span>}
      </div>
      <div className="cm42-body">
        <h2 className="cm42-title"><a className="cm42-title-link" href={communityHref(c.community_id, where)}><TopicLabel label={c.label} /></a></h2>
        <dl className="cm42-facts">
          {topics.length > 0 && <div className="cm42-fact"><dt>Shared topics</dt><dd><TopicLinks cards={topics} market={where} /></dd></div>}
          {shown.length > 0 && (
            <div className="cm42-fact">
              <dt>Named creators</dt>
              <dd>
                <ul className="cm42-names">
                  {shown.map((m) => <li key={m.creator_id}><a className="pp42-link" href={creatorHref(m.creator_id, where)}>{creatorName(m)}</a></li>)}
                </ul>
                {members.length > shown.length && <a className="cm42-more" href={communityHref(c.community_id, where)}>See every named creator</a>}
              </dd>
            </div>
          )}
          {platforms.length > 0 && <div className="cm42-fact"><dt>Platforms</dt><dd>{platforms.map(platformWord).join(', ')}</dd></div>}
          {languages.length > 0 && <div className="cm42-fact"><dt>Languages</dt><dd>{languages.map((l) => languageWord(l.lang)).join(', ')}</dd></div>}
        </dl>
      </div>
    </li>
  );
}

/* The other markets read for real, so the way out of an empty market says
   how many communities each has. A market that does not answer is still a
   link, with no number. */
function EmptyCommunities({where}){
  const others = Object.keys(MARKET_WORDS).filter((code) => code !== where);
  const [counts, setCounts] = useState({});
  useEffect(() => {
    const ctrl = new AbortController();
    setCounts({});
    others.forEach((code) => {
      fetchCommunities(code, {signal: ctrl.signal})
        .then((body) => {
          if (ctrl.signal.aborted) return;
          const n = body && Array.isArray(body.communities) ? body.communities.length : null;
          setCounts((prev) => ({...prev, [code]: n === null ? {state: 'unknown'} : {state: 'ready', n}}));
        })
        .catch(() => { if (!ctrl.signal.aborted) setCounts((prev) => ({...prev, [code]: {state: 'unknown'}})); });
    });
    return () => ctrl.abort();
  }, [where]);
  return (
    <div className="pp42-empty cm42-empty" data-section="empty">
      <p className="pp42-empty-title">No community of five or more creators in {marketWord(where)} in the last 28 days.</p>
      <ul className="cm42-elsewhere" aria-label="Other markets">
        {others.map((code) => {
          const read = counts[code];
          let words = 'Checking';
          if (read && read.state === 'ready') words = read.n === 0 ? 'None in 28 days' : read.n + ' ' + plural(read.n, 'community', 'communities');
          else if (read) words = 'Could not be checked';
          return (
            <li key={code} className="cm42-elsewhere-row" data-market={code}>
              <a className="pp42-link" href={communitiesHref(code)}>{MARKET_WORDS[code]}</a>
              <span className="pp42-muted" aria-live="polite">{words}</span>
            </li>
          );
        })}
      </ul>
      {/* Design audit, 2 October 2026: inverted pyramid (NN/g, empty states
          are the state and a way on, two lines at most). The state sentence
          leads, the other markets are the way on, and the definition waits
          in a closed disclosure with every word kept. */}
      {/* Layout pass two, 4 October 2026: an empty market opens the
          definition, since it is the one thing left to read. */}
      <details className="pp42-about" open>
        <summary>How communities form</summary>
        <p className="pp42-about-body">{COMMUNITY_RULE}</p>
      </details>
    </div>
  );
}

/* ---------------- one community, #/communities/<id> ---------------- */

export function CommunityPage42({communityId, market, onAuth}){
  const [load, retry] = useRead((options) => fetchCommunity(communityId, market, options), [communityId, market], onAuth);
  return (
    <Frame load={load} retry={retry} what="community" missingTitle="Community not found">
      {(data) => <Community data={data} market={market} onAuth={onAuth} />}
    </Frame>
  );
}

/* Posts per language as bars on one axis from 0 to the largest. */
function LanguageBars({languages}){
  const rows = languages.filter((l) => isFigure(l.posts) && typeof l.posts.value === 'number');
  const top = Math.max(0, ...rows.map((l) => l.posts.value));
  return (
    <ul className="cm42-bars">
      {languages.map((l) => {
        const value = isFigure(l.posts) && typeof l.posts.value === 'number' ? l.posts.value : null;
        return (
          <li key={l.lang} className="cm42-bar-row">
            <span className="cm42-bar-name">{languageWord(l.lang)}</span>
            <span className="cm42-bar-track" aria-hidden="true">
              <span className="cm42-bar" style={{'--cm42-w': value !== null && top > 0 ? value / top : 0}} />
            </span>
            <span className="cm42-bar-value">
              {value !== null
                ? <><Fig figure={l.posts} /> {value === 1 ? String(l.posts.unit || '').replace(/^posts\b/, 'post') : l.posts.unit}</>
                : <span className="pp42-muted">not measured yet</span>}
            </span>
          </li>
        );
      })}
      {top > 0 && (
        <li className="cm42-bar-row cm42-axis" aria-hidden="true">
          <span />
          <span className="cm42-axis-ticks"><span>0</span><span>{readerFigure(top)}</span></span>
          <span />
        </li>
      )}
    </ul>
  );
}

/* A thumbnail that fails to load leaves a plain tile: decorative (the post
   link carries the words), no broken-image glyph. */
function Thumb({src}){
  const [failed, setFailed] = useState(false);
  if (!src || failed) return <span className="cm42-thumb cm42-thumb-none" aria-hidden="true" />;
  return <img className="cm42-thumb" src={src} alt="" width="72" height="96" loading="lazy" onError={() => setFailed(true)} />;
}

/* Example posts with the meta line in whole pieces, each separator kept
   with the piece after it, so no line ends on a dot. */
function CommunityPosts({items}){
  return (
    <ul className="pp42-posts">
      {items.map((e) => {
        const views = e.engagement && typeof e.engagement.views === 'number' ? e.engagement.views : null;
        const meta = [platformWord(e.platform), e.handle, e.posted_at ? longDate(e.posted_at) : null, views !== null ? readerFigure(views) + ' views' : null].filter(Boolean);
        const url = safeUrl(e.url);
        return (
          <li key={e.id} className="pp42-post">
            <Thumb src={safeUrl(e.thumbnail_url)} />
            <div className="pp42-post-body">
              <p className="pp42-post-meta cm42-post-meta">{meta.map((part, i) => <React.Fragment key={i}>{i > 0 ? ' ' : ''}<span>{i > 0 ? '· ' : ''}{part}</span></React.Fragment>)}</p>
              {e.text && <p className="pp42-post-text">{e.text}</p>}
              {url && <a className="pp42-link" href={url} target="_blank" rel="noopener noreferrer">Open the post</a>}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/* A label is its top topics joined by commas; each topic is kept whole so
   a heading breaks only between topics. */
function TopicLabel({label}){
  const parts = String(label || '').split(', ');
  return parts.map((part, i) => <React.Fragment key={i}>{i > 0 ? ' ' : ''}<span className="cm42-label-part">{part}{i < parts.length - 1 ? ',' : ''}</span></React.Fragment>);
}

function Community({data, market, onAuth}){
  const c = data.community || {};
  const where = data.market || market;
  const top = sharedTopics(c);
  const platforms = Array.isArray(c.platforms) ? c.platforms : [];
  return (
    <section className="page pp42 cm42">
      <header className="pp42-head cm42-head">
        {where && <a className="cm42-back" href={communitiesHref(where)}>All communities in {marketWord(where)}</a>}
        <h1 className="pp42-heading cm42-heading"><TopicLabel label={c.label} /></h1>
        <p className="cm42-stats">
          {isFigure(c.creators) && <><span className="cm42-size-value"><Fig figure={c.creators} /></span> <span className="cm42-size-words">{c.creators.unit || 'creators'}</span></>}
          {where && <>{isFigure(c.creators) ? ' ' : ''}<span className="cm42-size-words">in</span> {marketWord(where)}</>}
        </p>
      </header>
      <MethodWords data={data} />

      <Part name="top-items" title="What they share">
        {top.length > 0
          ? <ul className="pp42-cards">
              {top.map((card) => (
                <li key={card.item_id} className="pp42-card">
                  <ul className="pp42-card-list">
                    <TrendCard card={card} market={card.market || where} date={card.date} linkTopic posts={false} />
                  </ul>
                </li>
              ))}
            </ul>
          : <p className="pp42-line">No shared topic 42 can show here.</p>}
      </Part>

      {Array.isArray(c.members) && (
        <Part name="members" title="Named creators">
          {c.members.length > 0
            ? <ul className="cm42-members">
                {c.members.map((m) => (
                  <li key={m.creator_id} className="cm42-member">
                    <span className="cm42-member-who">
                      <a className="pp42-row-title" href={creatorHref(m.creator_id, where)}>{creatorName(m)}</a>
                      <span className="pp42-muted">{[m.platform ? platformWord(m.platform) : null, m.tier ? tierWord(m.tier) : null].filter(Boolean).join(' · ')}</span>
                    </span>
                    <span className="cm42-member-actions">
                      <ProfileLink url={m.profile_url} />
                      {m.creator_id && <HideAction creator={m} onAuth={onAuth} />}
                    </span>
                  </li>
                ))}
              </ul>
            : <p className="pp42-line">No creator in this community is large enough to name.</p>}
          {c.members.length > 0 && <p className="pp42-line">Only creators large enough for their own page in 42 are named. Everyone else is counted in the total above.</p>}
        </Part>
      )}

      <div className="pp42-grid">
        <Part name="platforms" title="Platforms">
          <p className="pp42-line">{platforms.length > 0 ? platforms.map(platformWord).join(', ') : 'No platform recorded.'}</p>
        </Part>
        <Part name="languages" title="Languages">
          {Array.isArray(c.languages) && c.languages.length > 0
            ? <LanguageBars languages={c.languages} />
            : <p className="pp42-line">Languages are not measured yet.</p>}
        </Part>
      </div>

      {Array.isArray(c.example_posts) && (
        <Part name="examples" title="Example posts">
          {c.example_posts.length > 0 ? <CommunityPosts items={c.example_posts} /> : <p className="pp42-line">No example posts from named creators yet.</p>}
        </Part>
      )}
    </section>
  );
}

/* ---------------- hidden people, #/people/hidden ---------------- */

/* The list as the API sent it, newest first: who, why, the name the hider
   gave and when. There is no Lift here; only the 42 team brings someone
   back, because the list also carries removal requests that must stay in
   force. The heading and what hiding does stay on screen through loading
   and failure, so the page never opens on a bare status line; what hiding
   does follows the state, closed, as the design audit of 2 October 2026
   set it. */
export function HiddenPeoplePage42({onAuth}){
  const [note] = useState(() => hiddenNote);
  useEffect(() => { hiddenNote = null; }, []);
  const [load, retry] = useRead((options) => listHidden(options), [], onAuth);
  return (
    <section className="page pp42 hp42" aria-busy={load.state === 'loading' ? 'true' : undefined}>
      <header className="pp42-head hp42-head">
        <h1 className="pp42-heading">Hidden people</h1>
      </header>
      <p className="pp42-sub hp42-intro">No creator page, community list or client report in 42 names the people listed here.</p>
      {/* Layout pass two, 4 October 2026: the state, then one open block
          that says what hiding is and, beside it, where a hide applies, so
          the page has one real piece of content rather than a closed
          disclosure next to a list that repeated it. */}
      <div className="hp42-main">
        {note && <p className="pp42-status hp42-status" role="status">{note}</p>}
        <HiddenBody load={load} retry={retry} />
        <details className="pp42-about hp42-about" data-part="about" open>
          <summary>About hiding people</summary>
          <div className="pp42-about-body hp42-about-body">
            <div className="hp42-about-text">
              <p>Staff hide someone who asked to be left out of 42, for example under POPIA or another privacy law, or who should not be named in our work.</p>
              <p>From the next load they are gone from creator pages, community member lists and client reports, and anyone who later appears under the same handle is hidden too. Hiding only adds to this list. Ask the 42 team to bring someone back.</p>
            </div>
            <div className="hp42-aside">
              <h2 className="hp42-aside-title" id="hp42-reach-title">Where a hide applies</h2>
              <ul className="hp42-reach" aria-labelledby="hp42-reach-title">
                <li>Creator pages</li>
                <li>Community member lists</li>
                <li>Client reports</li>
                <li>Anyone who later posts under the same handle</li>
              </ul>
            </div>
          </div>
        </details>
      </div>
    </section>
  );
}

function HiddenBody({load, retry}){
  if (load.state === 'loading') return <p className="pp42-status hp42-status" role="status">Loading the list of hidden people</p>;
  if (load.state === 'auth') return <p className="pp42-status hp42-status" role="status">Enter the passcode to read the list of hidden people.</p>;
  if (load.state === 'error' || load.state === 'missing'){
    return (
      <div className="hp42-failed">
        <p className="pp42-empty-title">The list could not load</p>
        <p className="pp42-status hp42-status" role="alert">{load.message}</p>
        <button type="button" className="pp42-button" onClick={retry}>Try again</button>
      </div>
    );
  }
  return <Hidden data={load.data} />;
}

/* Demo polish, 2 October 2026: a row with no handle reads "A creator"; the
   internal creator id is never shown as a name. */
const hiddenName = (row) => (row.handle ? (row.platform ? platformWord(row.platform) + ' ' : '') + row.handle : 'A creator');
const byNewest = (rows) => rows
  .map((row, index) => [row, index])
  .sort(([a, i], [b, j]) => (Date.parse(b.status_at) || 0) - (Date.parse(a.status_at) || 0) || i - j)
  .map(([row]) => row);

function Hidden({data}){
  const rows = byNewest(Array.isArray(data.suppressions) ? data.suppressions : []);
  if (rows.length === 0){
    return (
      <div className="pp42-empty hp42-empty">
        <p className="pp42-empty-title">No one is hidden.</p>
        <p className="pp42-empty-next">To hide someone, open their creator page and choose Hide this person. Only larger creators are named in 42, so only they have a creator page; to leave anyone else out, ask the 42 team.</p>
        <p className="hp42-next"><a className="pp42-link" href="#/communities">Open Communities</a></p>
      </div>
    );
  }
  return (
    <>
      <p className="hp42-count" data-part="count">{rows.length} on the list, newest first.</p>
      <ul className="pp42-list hp42-list">
        {rows.map((row) => (
          <li key={row.suppression_id} className="hp42-row" data-suppression={row.suppression_id}>
            <div className="hp42-who">
              <span className="pp42-row-title">{hiddenName(row)}</span>
              {row.status === 'lifted' && <span className="hp42-tag">Brought back</span>}
            </div>
            <p className="hp42-reason">{row.reason}</p>
            <p className="hp42-meta">
              {/* One run with no breaking space except inside the name, so
                  the line can wrap inside a long name but never at the dot
                  or inside the date. On a phone the date takes its own line
                  and the dot is not drawn. */}
              {(row.status === 'lifted' ? 'by\u00a0' : 'hidden\u00a0by\u00a0') + row.who}
              {row.status_at ? <><span className="hp42-dot">{'\u00a0\u00b7\u00a0'}</span><time dateTime={row.status_at}>{longDate(row.status_at).replace(/ /g, '\u00a0')}</time></> : null}
            </p>
          </li>
        ))}
      </ul>
    </>
  );
}
