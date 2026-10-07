/* Client skins on the 42 API (core/api/contract.md section 15.1): a saved
   lens over the same engine, with a name, markets, terms and hashtags,
   approved organisation accounts, linked watches and a weekly report.
   #/skins lists them and makes a new one; #/skins/<id> shows Today narrowed
   to the skin, its watches and alerts, Build weekly report as the one red
   action, and the weekly reports that became dossiers. Accounts come only
   from the approved list, so nothing here lets a handle be typed. Masked
   handles ("@***") arrive masked and are shown as they are. */
import {useEffect, useId, useRef, useState} from 'react';
import {archiveSkin, createSkin, getSkin, getSkinToday, listDossiers, listInvestigations, listSkins, listWatches, startSkinReport, updateSkin} from './api42.js';
import {rememberBudget} from './investigations42.jsx';
import {go} from './router.js';
import {EvidenceList, TrendCard, longDate, platformWord, topicHref} from './ui/TrendCard.jsx';
import {MARKET_WORDS, heldWords, ruleWords} from './ui/WatchDialog.jsx';
import './styles/today42.css';
import './styles/alerts42.css';
import './styles/skins42.css';

const MARKETS = ['ZA', 'NG', 'KE'];
const TEMPLATES = [{id: 'weekly_report', label: 'Weekly report'}];
const ROLE_WORDS = {client: 'client', competitor: 'competitor', partner: 'partner'};
const KIND_WORDS = {item: 'Trend', hashtag: 'Hashtag', sound: 'Sound', creator: 'Creator', brand: 'Brand', query: 'Query'};
const NO_ANSWER = 'The 42 service did not answer. Try again.';
const NO_ACCOUNTS = 'No approved accounts yet. Accounts come from the approved list only.';
const DOSSIER_READ = 200;

const list = (value) => (Array.isArray(value) ? value : []);
const marketWord = (market) => MARKET_WORDS[market] || market;
const count = (n, word) => n + ' ' + word + (n === 1 ? '' : 's');
const lines = (value) => String(value || '').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
const accountKey = (a) => a.platform + ':' + a.handle;
const words = (names) => (names.length > 1 ? names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1] : names.join(''));
const serverWords = (error) => (error && error.status ? error.message : NO_ANSWER);
/* The SAST day, which is the day Today is published for. */
const sastToday = () => new Date(Date.now() + 2 * 3600000).toISOString().slice(0, 10);

export function marketsWords(markets){
  return words(list(markets).map(marketWord));
}

/* Demo polish, 2 October 2026: a count is written only when the skin holds
   one, because "0 accounts" read as something broken. A skin that holds
   none of the three says so once. */
function sizeWords(skin){
  const held = [[list(skin.terms).length, 'term'], [list(skin.hashtags).length, 'hashtag'], [list(skin.accounts).length, 'account']]
    .filter(([n]) => n > 0)
    .map(([n, word]) => count(n, word));
  return held.length ? held.join(' · ') : 'Nothing added yet';
}

function accountWords(a){
  const role = ROLE_WORDS[a.role];
  return a.handle + ' on ' + platformWord(a.platform) + (a.org ? ', ' + a.org : '') + (role ? ' (' + role + ')' : '');
}

function useAuthOnce(onAuth){
  const ref = useRef(onAuth);
  ref.current = onAuth;
  return () => { if (ref.current) ref.current(); };
}

/* ---------------- the list, #/skins ---------------- */

export function SkinsPage42({onAuth}){
  const [skins, setSkins] = useState({state: 'loading'});
  const [tick, setTick] = useState(0);
  const [making, setMaking] = useState(false);
  const handover = useAuthOnce(onAuth);

  useEffect(() => {
    const ctrl = new AbortController();
    setSkins({state: 'loading'});
    listSkins({signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setSkins({state: 'ready', items: list(data && data.skins)}); })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){ setSkins({state: 'auth'}); handover(); return; }
        setSkins({state: 'error'});
      });
    return () => ctrl.abort();
  }, [tick]);

  return (
    <section className="page t42 a42 sk42">
      {/* The intro says what a skin is in plain words. New skin sits beside
          it as the screen's one red action; once the form opens it steps
          aside and the save is the red action instead. */}
      <header className="t42-head sk42-head">
        <div className="sk42-head-text">
          <h1 className="t42-heading">Skins</h1>
          <p className="t42-status">A skin is a client's own view of 42: their markets, terms, hashtags, approved accounts and watches, with a weekly report.</p>
        </div>
        {!making && <button type="button" className="w42-primary a42-head-action" onClick={() => setMaking(true)}>New skin</button>}
      </header>
      <div className="sk42-grid">
        <div className="sk42-main">
          <section className="t42-section a42-section" data-section="skins" aria-labelledby="sk42-list-title">
            <h2 className="a42-title" id="sk42-list-title">Saved skins</h2>
            <SkinList skins={skins} making={making} onRetry={() => setTick((t) => t + 1)} />
          </section>
          {making && (
            <SkinForm
              onSaved={(made) => go('/skins/' + encodeURIComponent(made.skin_id))}
              onCancel={() => setMaking(false)}
              onAuth={handover}
            />
          )}
        </div>
        <SkinAnatomy />
      </div>
    </section>
  );
}

/* What a skin holds, beside the list, so a new user sees what the page
   will contain before the first skin exists. */
const ANATOMY = [
  ['Markets', 'South Africa, Nigeria or Kenya, one or more. Today and the report read only these.'],
  ['Terms and hashtags', 'Up to 30 of each, one per line. Today on the skin page is narrowed to them.'],
  ['Organisation accounts', 'Chosen from the approved list, never typed, so a handle is always one we may read.'],
  ['Linked watches', 'Watches from Alerts. Their alerts show on the skin page.'],
  ['Weekly report', 'Build weekly report starts the research. The finished report is kept as a dossier.'],
];

function SkinAnatomy(){
  return (
    <aside className="sk42-aside" aria-labelledby="sk42-anatomy-title">
      <h2 className="sk42-aside-title" id="sk42-anatomy-title">What a skin holds</h2>
      <dl className="sk42-anatomy">
        {ANATOMY.map(([term, line]) => (
          <div key={term} className="sk42-anatomy-row">
            <dt>{term}</dt>
            <dd>{line}</dd>
          </div>
        ))}
      </dl>
    </aside>
  );
}

function SkinList({skins, making, onRetry}){
  if (skins.state === 'loading') return <p className="t42-status" role="status">Loading skins</p>;
  if (skins.state === 'auth') return <p className="t42-status">Enter the passcode to read the skins.</p>;
  if (skins.state === 'error'){
    return (
      <>
        <p className="t42-status" role="alert">The skins could not load.</p>
        <button type="button" className="t42-button" onClick={onRetry}>Try again</button>
      </>
    );
  }
  if (skins.items.length === 0) return <p className="t42-line-text a42-empty"><strong className="a42-empty-lead">No skins yet.</strong>{making ? '' : " Choose New skin to set up a client's markets, terms and accounts."}</p>;
  return (
    <ul className="a42-list">
      {skins.items.map((s) => (
        <li key={s.skin_id} className="a42-watch" data-skin={s.skin_id}>
          <a className="t42-link a42-name" href={'#/skins/' + encodeURIComponent(s.skin_id)}>{s.name}</a>
          <span className="a42-meta">{marketsWords(s.markets) + ' · ' + sizeWords(s)}</span>
          <span className="a42-state">{s.status === 'archived' ? 'Archived' : 'Active'}</span>
        </li>
      ))}
    </ul>
  );
}

/* ---------------- the form, new or edit ---------------- */

/* The accounts a skin already holds are the only ones this screen knows
   are on the approved list: the list itself is read by the server alone.
   So a new skin offers none, and an edit offers the ones it holds. */
function SkinForm({skin, onSaved, onCancel, onAuth}){
  const editing = Boolean(skin);
  const held = list(skin && skin.accounts);
  const [key, setKey] = useState(skin ? skin.skin_key : '');
  const [name, setName] = useState(skin ? skin.name : '');
  const [markets, setMarkets] = useState(skin ? list(skin.markets) : ['ZA']);
  const [terms, setTerms] = useState(skin ? list(skin.terms).join('\n') : '');
  const [hashtags, setHashtags] = useState(skin ? list(skin.hashtags).join('\n') : '');
  const [accounts, setAccounts] = useState(held.map(accountKey));
  const [watchIds, setWatchIds] = useState(skin ? list(skin.watch_ids) : []);
  const [template, setTemplate] = useState(skin && skin.template ? skin.template : 'weekly_report');
  const [watches, setWatches] = useState({state: 'loading'});
  const [note, setNote] = useState({status: 'idle'});
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const id = useId();

  useEffect(() => {
    const ctrl = new AbortController();
    listWatches({signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setWatches({state: 'ready', items: list(data && data.watches)}); })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){ setWatches({state: 'auth'}); onAuth(); return; }
        setWatches({state: 'error'});
      });
    return () => ctrl.abort();
  }, []);

  const flip = (setter, order) => (value) => setter((current) => (current.includes(value)
    ? current.filter((x) => x !== value)
    : order.filter((x) => x === value || current.includes(x))));

  const submit = (event) => {
    event.preventDefault();
    const body = {
      skin_key: key.trim(), name: name.trim(), markets, terms: lines(terms), hashtags: lines(hashtags),
      accounts: held.filter((a) => accounts.includes(accountKey(a))).map((a) => ({platform: a.platform, handle: a.handle})),
      watch_ids: watchIds, template,
    };
    setNote({status: 'saving'});
    (editing ? updateSkin(skin.skin_id, body) : createSkin(body))
      .then((saved) => { if (live.current) onSaved(saved); })
      .catch((failure) => {
        if (!live.current) return;
        if (failure && failure.auth){ setNote({status: 'idle'}); onAuth(); return; }
        setNote({status: 'error', message: serverWords(failure)});
      });
  };

  const watchIdsKnown = watches.state === 'ready' ? watches.items.map((w) => w.watch_id) : watchIds;
  return (
    <section className="t42-section a42-section" data-section="skin-form" aria-labelledby={id + '-title'}>
      <h2 className="a42-title" id={id + '-title'}>{editing ? 'Edit this skin' : 'New skin'}</h2>
      <form className="a42-form" onSubmit={submit} noValidate>
        <div className="a42-fields">
          <span className="a42-field">
            <label className="a42-label" htmlFor={id + '-key'}>Short key (lower-case letters, digits or underscores)</label>
            <input id={id + '-key'} name="skin_key" type="text" className="a42-control" maxLength={40} readOnly={editing}
              value={key} onChange={(e) => setKey(e.target.value)} />
          </span>
          <span className="a42-field">
            <label className="a42-label" htmlFor={id + '-name'}>Name</label>
            <input id={id + '-name'} name="name" type="text" className="a42-control" maxLength={80}
              value={name} onChange={(e) => setName(e.target.value)} />
          </span>
        </div>
        <fieldset className="w42-rules">
          <legend className="w42-legend">Markets</legend>
          {MARKETS.map((m) => (
            <label key={m} className="w42-rule">
              <input type="checkbox" name="markets" value={m} checked={markets.includes(m)} onChange={() => flip(setMarkets, MARKETS)(m)} />
              {marketWord(m)}
            </label>
          ))}
        </fieldset>
        <span className="a42-field">
          <label className="a42-label" htmlFor={id + '-terms'}>Terms, one per line (up to 30)</label>
          <textarea id={id + '-terms'} name="terms" className="a42-control" rows={4} style={{padding: 'var(--s-2) var(--s-3)', resize: 'vertical'}}
            value={terms} onChange={(e) => setTerms(e.target.value)} />
        </span>
        <span className="a42-field">
          <label className="a42-label" htmlFor={id + '-hashtags'}>Hashtags, one per line (up to 30)</label>
          <textarea id={id + '-hashtags'} name="hashtags" className="a42-control" rows={3} style={{padding: 'var(--s-2) var(--s-3)', resize: 'vertical'}}
            value={hashtags} onChange={(e) => setHashtags(e.target.value)} />
        </span>
        <fieldset className="w42-rules" data-part="accounts">
          <legend className="w42-legend">Organisation accounts</legend>
          {held.length === 0
            ? <p className="t42-line-text">{NO_ACCOUNTS}</p>
            : <>
                <p className="t42-line-text">Accounts come from the approved list only.</p>
                {held.map((a) => (
                  <label key={accountKey(a)} className="w42-rule">
                    <input type="checkbox" name="account" value={accountKey(a)} checked={accounts.includes(accountKey(a))} onChange={() => flip(setAccounts, held.map(accountKey))(accountKey(a))} />
                    {accountWords(a)}
                  </label>
                ))}
              </>}
        </fieldset>
        <fieldset className="w42-rules" data-part="watches">
          <legend className="w42-legend">Linked watches</legend>
          {watches.state === 'loading' && <p className="t42-line-text">Loading watches</p>}
          {watches.state === 'auth' && <p className="t42-line-text">Enter the passcode to read the watches.</p>}
          {watches.state === 'error' && <p className="t42-line-text">The watches could not load.</p>}
          {watches.state === 'ready' && watches.items.length === 0 && <p className="t42-line-text">No watches yet. <a className="t42-link" href="#/alerts">Add one on Alerts</a>.</p>}
          {watches.state === 'ready' && watches.items.map((w) => (
            <label key={w.watch_id} className="w42-rule">
              <input type="checkbox" name="watch_ids" value={w.watch_id} checked={watchIds.includes(w.watch_id)} onChange={() => flip(setWatchIds, watchIdsKnown)(w.watch_id)} />
              {w.label} <span className="a42-meta">{marketWord(w.market)}</span>
            </label>
          ))}
        </fieldset>
        <span className="a42-field">
          <label className="a42-label" htmlFor={id + '-template'}>Report</label>
          <select id={id + '-template'} name="template" className="a42-control" value={template} onChange={(e) => setTemplate(e.target.value)}>
            {TEMPLATES.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
          </select>
        </span>
        {note.status === 'error' && <p className="w42-error" role="alert">{note.message}</p>}
        <div className="w42-actions">
          <button type="submit" className="w42-primary" disabled={note.status === 'saving'} aria-busy={note.status === 'saving' ? 'true' : 'false'}>Save skin</button>
          <button type="button" className="t42-button" onClick={onCancel} disabled={note.status === 'saving'}>Cancel</button>
        </div>
      </form>
    </section>
  );
}

/* ---------------- one skin, #/skins/<id> ---------------- */

export function SkinPage42({skinId, onAuth}){
  const [load, setLoad] = useState({state: 'loading'});
  const [tick, setTick] = useState(0);
  const [today, setToday] = useState({state: 'loading'});
  const [todayTick, setTodayTick] = useState(0);
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [action, setAction] = useState({status: 'idle'});
  const asked = useRef(false);
  const handover = useAuthOnce(onAuth);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);

  /* Every read on this page can meet the same 401; the passcode is asked for once. */
  const needPass = () => { if (!asked.current){ asked.current = true; handover(); } };

  useEffect(() => {
    const ctrl = new AbortController();
    setLoad({state: 'loading'});
    getSkin(skinId, {signal: ctrl.signal})
      .then((skin) => { if (!ctrl.signal.aborted) setLoad({state: 'ready', skin}); })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){ setLoad({state: 'auth'}); needPass(); return; }
        if (error && error.status === 404){ setLoad({state: 'missing', message: error.message}); return; }
        setLoad({state: 'error', message: serverWords(error)});
      });
    return () => ctrl.abort();
  }, [skinId, tick]);

  useEffect(() => {
    const ctrl = new AbortController();
    setToday({state: 'loading'});
    getSkinToday(skinId, undefined, {signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setToday({state: 'ready', data: data || {}}); })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){ setToday({state: 'auth'}); needPass(); return; }
        setToday({state: 'error', message: serverWords(error), retry: !(error && error.status === 409)});
      });
    return () => ctrl.abort();
  }, [skinId, todayTick]);

  if (load.state === 'loading'){
    return <section className="page t42" aria-busy="true"><p className="t42-status" role="status">Loading the skin</p></section>;
  }
  if (load.state === 'auth'){
    return <section className="page t42"><p className="t42-status" role="status">Enter the passcode to read this skin.</p></section>;
  }
  if (load.state === 'missing' || load.state === 'error'){
    return (
      <section className="page t42">
        <p className="t42-line-text"><a className="t42-link" href="#/skins">All skins</a></p>
        <h1 className="t42-heading">{load.state === 'missing' ? 'Skin not found' : 'The skin could not load'}</h1>
        <p className="t42-status" role="alert">{load.message}</p>
        {load.state === 'error' && <button type="button" className="t42-button" onClick={() => setTick((t) => t + 1)}>Try again</button>}
      </section>
    );
  }

  const skin = load.skin;
  const archived = skin.status === 'archived';
  const leftOut = MARKETS.filter((m) => !list(skin.markets).includes(m));
  const outText = today.state === 'ready' && today.data.skin && today.data.skin.text
    ? today.data.skin.text
    : leftOut.length ? 'Not in this skin: ' + marketsWords(leftOut) : 'Every market is in this skin';

  const buildReport = () => {
    setAction({status: 'report'});
    startSkinReport(skin.skin_id)
      .then((made) => {
        if (!live.current) return;
        rememberBudget(made.investigation_id, made.budget_left);
        go('/investigations/' + encodeURIComponent(made.investigation_id));
      })
      .catch((error) => {
        if (!live.current) return;
        if (error && error.auth){ setAction({status: 'idle'}); handover(); return; }
        setAction({status: 'error', message: serverWords(error)});
      });
  };

  const archive = () => {
    setAction({status: 'archiving'});
    archiveSkin(skin.skin_id)
      .then((next) => {
        if (!live.current) return;
        setLoad({state: 'ready', skin: next && next.skin_id ? next : {...skin, status: 'archived'}});
        setConfirming(false);
        setAction({status: 'idle'});
      })
      .catch((error) => {
        if (!live.current) return;
        if (error && error.auth){ setAction({status: 'idle'}); handover(); return; }
        setAction({status: 'archive-error', message: serverWords(error)});
      });
  };

  const saved = (next) => {
    setLoad({state: 'ready', skin: next});
    setEditing(false);
    setTodayTick((t) => t + 1);
  };

  const busy = action.status === 'report' || action.status === 'archiving';
  return (
    <section className="page t42 a42">
      <p className="t42-line-text"><a className="t42-link" href="#/skins">All skins</a></p>
      <header className="t42-head">
        <h1 className="t42-heading">{skin.name}</h1>
        <p className="t42-status">{[marketsWords(skin.markets), sizeWords(skin), archived ? 'Archived' : 'Active'].join(' · ')}</p>
        <p className="t42-status">{outText}</p>
        {archived && <p className="t42-status">This skin is archived. It stays listed, and nothing in it is deleted.</p>}
      </header>

      {!archived && !editing && (
        <div className="w42-actions">
          <button type="button" className="w42-primary" onClick={buildReport} disabled={busy} aria-busy={action.status === 'report' ? 'true' : 'false'}>Build weekly report</button>
          <button type="button" className="t42-button" onClick={() => setEditing(true)} disabled={busy}>Edit</button>
          <button type="button" className="t42-button" onClick={() => setConfirming(true)} disabled={busy || confirming}>Archive</button>
        </div>
      )}
      {action.status === 'report' && <p className="t42-status" role="status">Drafting the report plan</p>}
      {action.status === 'error' && <p className="w42-error" role="alert" data-part="report-error">{action.message}</p>}
      {confirming && !archived && (
        <ArchiveConfirm busy={action.status === 'archiving'} error={action.status === 'archive-error' ? action.message : ''}
          onConfirm={archive} onCancel={() => { setConfirming(false); setAction({status: 'idle'}); }} />
      )}
      {editing && <SkinForm skin={skin} onSaved={saved} onCancel={() => setEditing(false)} onAuth={handover} />}

      <SkinToday today={today} onRetry={() => setTodayTick((t) => t + 1)} />
      <SkinWatches skin={skin} today={today} onAuth={needPass} />
      <SkinReports skinId={skin.skin_id} onAuth={needPass} />
    </section>
  );
}

function ArchiveConfirm({busy, error, onConfirm, onCancel}){
  const cancel = useRef(null);
  // Cancel takes first focus, so Enter never archives by accident.
  useEffect(() => { if (cancel.current) cancel.current.focus(); }, []);
  return (
    <div className="t42-section" data-part="archive-confirm" role="group" aria-label="Archive this skin">
      <p className="t42-line-text">Archive this skin? It stays listed as archived. Nothing is deleted.</p>
      {error && <p className="w42-error" role="alert">{error}</p>}
      <div className="w42-actions">
        <button type="button" className="t42-button" ref={cancel} onClick={onCancel} disabled={busy}>Cancel</button>
        <button type="button" className="t42-button" onClick={onConfirm} disabled={busy} aria-busy={busy ? 'true' : 'false'}>Archive this skin</button>
      </div>
    </div>
  );
}

function SkinToday({today, onRetry}){
  let body;
  let heading = 'Today in this skin';
  if (today.state === 'loading') body = <p className="t42-status" role="status">Loading Today for this skin</p>;
  else if (today.state === 'auth') body = <p className="t42-status">Enter the passcode to read Today.</p>;
  else if (today.state === 'error'){
    body = (
      <>
        <p className="t42-status" role="alert">{today.message}</p>
        {today.retry && <button type="button" className="t42-button" onClick={onRetry}>Try again</button>}
      </>
    );
  } else {
    const data = today.data;
    const markets = list(data.markets);
    /* The skin reads the latest brief, which can be from an earlier day; it
       says so as the Today page does, so it is never read as today's. */
    const briefDate = typeof data.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(data.date) ? data.date : null;
    const earlier = briefDate && briefDate < sastToday();
    if (earlier) heading = 'Brief for ' + longDate(briefDate) + ' in this skin';
    body = (
      <>
        {earlier && <p className="t42-notice" role="status" data-skin-today-earlier="">Earlier brief: <span className="t42-nowrap">{longDate(briefDate)}</span>. This is not today’s brief.</p>}
        {data.headline && data.headline.text && <p className="t42-headline">{data.headline.text}</p>}
        {markets.map((m) => <SkinMarket key={m.market} market={m} date={data.date} earlier={earlier} />)}
        {markets.length === 0 && <p className="t42-status">None of this skin's markets is in {earlier ? 'the brief for ' + longDate(briefDate) : "today's brief"}.</p>}
      </>
    );
  }
  return (
    <section className="t42-section" data-section="skin-today" aria-labelledby="sk42-today-title">
      <h2 className="a42-title" id="sk42-today-title">{heading}</h2>
      {body}
    </section>
  );
}

function SkinMarket({market, date, earlier}){
  const cards = list(market.cards).concat(list(market.more));
  const held = market.held_back || {};
  const heldItems = list(held.items);
  const dropped = list(market.dropped && market.dropped.items);
  return (
    <section className="t42-market" data-market={market.market} aria-label={market.label}>
      <h3 className="t42-market-name">{market.label}</h3>
      {market.skin_note && market.skin_note.text && <p className="t42-line-text">{market.skin_note.text}</p>}
      {list(market.banners).map((b) => <p key={b.kind + b.text} className={'t42-banner t42-banner-' + b.kind}>{b.text}</p>)}
      {cards.length > 0
        ? <ol className="t42-cards">
            {cards.map((card) => <TrendCard key={card.item_id} card={card} market={market.market} date={card.date || date} />)}
          </ol>
        : <p className="t42-status">No trends in this skin passed the checks for {market.label} {earlier ? 'on ' + longDate(date) : 'today'}.</p>}
      <div className="t42-below">
        <section className="t42-section" data-part="held-back">
          <h4 className="t42-section-title">Held back</h4>
          <p className="t42-line-text">{held.text || 'Nothing held back'}</p>
          {heldItems.length > 0 && (
            <ul className="t42-rows">
              {heldItems.map((h) => (
                <li key={h.item_id}>
                  <p className="t42-line-text">{h.title}: {h.reason_text}</p>
                  {list(h.evidence).length > 0 && <EvidenceList items={h.evidence} />}
                </li>
              ))}
            </ul>
          )}
        </section>
        {dropped.length > 0 && (
          <section className="t42-section" data-part="dropped">
            <h4 className="t42-section-title">Dropped since yesterday</h4>
            <ul className="t42-rows">{dropped.map((d) => <li key={d.item_id}>{d.title}: {d.reason_text}</li>)}</ul>
          </section>
        )}
      </div>
    </section>
  );
}

function SkinWatches({skin, today, onAuth}){
  const [watches, setWatches] = useState({state: 'loading'});
  const ids = list(skin.watch_ids);
  const idsKey = ids.join(',');

  useEffect(() => {
    if (ids.length === 0){ setWatches({state: 'ready', items: []}); return undefined; }
    const ctrl = new AbortController();
    listWatches({signal: ctrl.signal})
      .then((data) => { if (!ctrl.signal.aborted) setWatches({state: 'ready', items: list(data && data.watches).filter((w) => ids.includes(w.watch_id))}); })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){ setWatches({state: 'auth'}); onAuth(); return; }
        setWatches({state: 'error'});
      });
    return () => ctrl.abort();
  }, [idsKey]);

  const alerts = today.state === 'ready' && today.data.alerts ? today.data.alerts : null;
  const fired = alerts ? list(alerts.alerts).filter((a) => !a.waiting) : [];
  const waiting = alerts ? list(alerts.alerts).filter((a) => a.waiting).concat(list(alerts.waiting)) : [];
  const labelOf = (entry) => {
    if (entry.label) return entry.label;
    const watch = watches.state === 'ready' ? watches.items.find((w) => w.watch_id === entry.watch_id) : null;
    return watch && watch.label ? watch.label : 'A watch';
  };

  let list42;
  if (ids.length === 0) list42 = <p className="t42-line-text">No watches are linked to this skin.</p>;
  else if (watches.state === 'loading') list42 = <p className="t42-line-text">Loading watches</p>;
  else if (watches.state === 'auth') list42 = <p className="t42-line-text">Enter the passcode to read the watches.</p>;
  else if (watches.state === 'error') list42 = <p className="t42-line-text">The watches could not load.</p>;
  else {
    list42 = (
      <ul className="a42-list">
        {watches.items.map((w) => {
          const target = w.target || {};
          return (
            <li key={w.watch_id} className="a42-watch" data-watch={w.watch_id}>
              <span className="a42-name">{w.label}</span>
              <span className="a42-meta">{(KIND_WORDS[target.kind] || 'Watch') + ' · ' + marketWord(w.market) + ' · ' + ruleWords(w.rule)}</span>
              <span className="a42-state">{w.status === 'paused' ? 'Paused' : 'Active'}</span>
            </li>
          );
        })}
      </ul>
    );
  }

  let alertsBody = null;
  if (today.state === 'ready' && alerts){
    alertsBody = (
      <>
        <p className="t42-line-text">{fired.length === 0 ? 'No alerts today.' : fired.length === 1 ? '1 alert today' : fired.length + ' alerts today'}</p>
        {alerts.note && <p className="a42-meta">{alerts.note}</p>}
        {fired.length > 0 && (
          <ul className="a42-list">
            {fired.map((a) => {
              const held = a.card && a.card.held_back;
              return (
                <li key={a.watch_id + ':' + a.market + ':' + a.item_id} className="a42-alert" data-held={held ? '' : undefined}>
                  <a className="t42-link a42-name" href={topicHref(a.item_id, a.market)}>{a.label}</a>
                  {held && <span className="a42-held">{heldWords(held)}</span>}
                  <span className="a42-meta">{a.fired_because} · {marketWord(a.market)}{a.since ? ' · since ' + longDate(a.since) : ''}</span>
                </li>
              );
            })}
          </ul>
        )}
        {waiting.length > 0 && (
          <>
            <h3 className="a42-subtitle">Not checked yet</h3>
            <ul className="a42-waiting">{waiting.map((w) => <li key={w.watch_id}>{labelOf(w)}: {w.waiting}</li>)}</ul>
          </>
        )}
      </>
    );
  } else if (today.state === 'ready' || today.state === 'error'){
    alertsBody = <p className="t42-line-text">The alerts come with Today and are not available right now.</p>;
  }

  return (
    <section className="t42-section a42-section" data-section="skin-watches" aria-labelledby="sk42-watches-title">
      <h2 className="a42-title" id="sk42-watches-title">Watches and alerts</h2>
      {list42}
      {alertsBody}
      <a className="t42-link" href="#/alerts">All alerts and watches</a>
    </section>
  );
}

/* A weekly report is an investigation whose plan carries this skin; once
   finished it becomes a dossier made from that investigation's answer. The
   dossier list names its source answer, so the two reads are matched on the
   answer's ask id. The newest DOSSIER_READ dossiers are read. */
function SkinReports({skinId, onAuth}){
  const [reports, setReports] = useState({state: 'loading'});

  useEffect(() => {
    const ctrl = new AbortController();
    Promise.all([listInvestigations({}, {signal: ctrl.signal}), listDossiers({limit: DOSSIER_READ}, {signal: ctrl.signal})])
      .then(([investigations, dossiers]) => {
        if (ctrl.signal.aborted) return;
        const asks = new Set(list(investigations && investigations.investigations)
          .filter((inv) => inv.plan && inv.plan.skin_id === skinId && inv.ask_id)
          .map((inv) => inv.ask_id));
        setReports({state: 'ready', items: list(dossiers && dossiers.dossiers).filter((d) => asks.has(d.source_ask_id))});
      })
      .catch((error) => {
        if (ctrl.signal.aborted) return;
        if (error && error.auth){ setReports({state: 'auth'}); onAuth(); return; }
        setReports({state: 'error'});
      });
    return () => ctrl.abort();
  }, [skinId]);

  let body;
  if (reports.state === 'loading') body = <p className="t42-line-text">Loading the weekly reports</p>;
  else if (reports.state === 'auth') body = <p className="t42-line-text">Enter the passcode to read the weekly reports.</p>;
  else if (reports.state === 'error') body = <p className="t42-line-text">The weekly reports could not load.</p>;
  else if (reports.items.length === 0) body = <p className="t42-line-text">No weekly report has become a dossier yet.</p>;
  else {
    body = (
      <ul className="a42-list">
        {reports.items.map((d) => (
          <li key={d.dossier_id} className="a42-watch" data-dossier={d.dossier_id}>
            <a className="t42-link a42-name" href={'#/dossiers/' + encodeURIComponent(d.dossier_id)}>{d.title}</a>
            <span className="a42-meta">{[d.state === 'frozen' ? 'Frozen' : 'Draft', longDate(d.created_at)].filter(Boolean).join(' · ')}</span>
          </li>
        ))}
      </ul>
    );
  }
  return (
    <section className="t42-section a42-section" data-section="skin-reports" aria-labelledby="sk42-reports-title">
      <h2 className="a42-title" id="sk42-reports-title">Weekly reports</h2>
      {body}
    </section>
  );
}
