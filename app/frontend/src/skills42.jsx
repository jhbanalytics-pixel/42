/* The brand lens, creator brand-fit and context pack under Ask (core/api/
   contract.md section 15.2). Each form fills an ask with its skill and
   tier; a confirm tap names the credit ceiling, with Cancel taking first
   focus; the ask is then followed on #/ask?follow=<id>, where Ask's own
   page, checks, export and dossier path show the answer. Brands are
   organisations; a person is never the subject. Creator fit is offered
   only for a creator whose page 42 is allowed to show. */
import {useEffect, useId, useRef, useState} from 'react';
import {askQuestion, fetchCreator} from './api42.js';
import {go} from './router.js';
import {platformWord} from './ui/TrendCard.jsx';
import {MARKET_WORDS} from './ui/WatchDialog.jsx';
import {useFocusTrap} from './ui/useFocusTrap.js';
import {CREDIT_NOTE} from './ui/SpikeConfirm.jsx';
import './styles/today42.css';
import './styles/alerts42.css';

/* Ask takes T2 only for the brand lens and context pack, and only while
   f42-agent's F42_T2_READY is on (contract sections 6 and 15.2); /api/health
   says so as t2_ready. Anything but true keeps those two forms hidden. */
export function t2ReadyFrom(health){
  return Boolean(health && typeof health === 'object' && health.t2_ready === true);
}

const MARKETS = ['ZA', 'NG', 'KE'];
const COMPETITORS = 4;
const MAX_BRIEF = 2000;
const NO_ANSWER = 'The 42 service did not answer. Try again.';
const CEILING = {
  T1: 'A quick scan, up to 60 credits.',
  T2: 'A deep read, up to 300 credits.',
};
const marketWord = (market) => MARKET_WORDS[market] || market;
const pickMarket = (market) => (MARKETS.includes(market) ? market : 'ZA');
const joinWords = (names) => (names.length > 1 ? names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1] : names.join(''));

export function SkillForms({market, fit, t2Ready = false, onAuth}){
  const [pending, setPending] = useState(null);
  const start = pickMarket(market);
  /* A skill Ask cannot run yet is not shown (QA, 2 Oct 2026), so with T2
     refused and no creator to fit there is nothing to offer here. */
  if (!t2Ready && !fit) return null;
  return (
    <section className="t42-section a42-section sk42-skills" aria-labelledby="sk42-skills-title">
      <h2 className="a42-title" id="sk42-skills-title">Ask with a skill</h2>
      <BrandLens market={start} ready={t2Ready} onConfirm={setPending} />
      <ContextPack market={start} ready={t2Ready} onConfirm={setPending} />
      {fit
        ? <CreatorFit creatorId={fit} market={start} onConfirm={setPending} onAuth={onAuth} />
        : t2Ready && <p className="t42-line-text">Creator fit opens from a creator page.</p>}
      {pending && <SkillConfirm ask={pending} onAuth={onAuth} onClose={() => setPending(null)} />}
    </section>
  );
}

function SkillBox({name, title, lede, ready, error, onSubmit, children}){
  if (!ready) return null;

  return (
    <div className="t42-section" data-skill={name}>
      <h3 className="a42-subtitle">{title}</h3>
      <p className="t42-line-text">{lede}</p>
      <form className="a42-form" onSubmit={(event) => { event.preventDefault(); if (ready) onSubmit(); }} noValidate>
        <fieldset className="w42-rules" disabled={!ready}>
          {children}
        </fieldset>
        {error && <p className="w42-error" role="alert">{error}</p>}
      </form>
    </div>
  );
}

function MarketField({id, value, onChange}){
  return (
    <span className="a42-field">
      <label className="a42-label" htmlFor={id}>Market</label>
      <select id={id} name="market" className="a42-control" value={value} onChange={(e) => onChange(e.target.value)}>
        {MARKETS.map((m) => <option key={m} value={m}>{marketWord(m)}</option>)}
      </select>
    </span>
  );
}

function BrandLens({market, ready, onConfirm}){
  const [brand, setBrand] = useState('');
  const [rivals, setRivals] = useState(() => Array(COMPETITORS).fill(''));
  const [where, setWhere] = useState(market);
  const [error, setError] = useState('');
  const id = useId();

  const submit = () => {
    const name = brand.trim();
    if (!name){ setError('Type the brand.'); return; }
    setError('');
    const others = rivals.map((r) => r.trim()).filter(Boolean);
    const question = 'Brand lens on ' + name + ' in ' + marketWord(where)
      + (others.length ? ', against ' + joinWords(others) : '')
      + ': share of voice, tone, competitor content and what people say the brand is.';
    onConfirm({title: 'Ask for a brand lens?', where: name + ' in ' + marketWord(where), tier: 'T2', skill: 'brand-implication', question, market: where});
  };

  return (
    <SkillBox name="brand-lens" title="Brand lens" ready={ready} error={error} onSubmit={submit}
      lede="Share of voice, tone, competitor content and what people say a brand is, for one brand and up to four competitors.">
      <span className="a42-field">
        <label className="a42-label" htmlFor={id + '-brand'}>Brand</label>
        <input id={id + '-brand'} name="brand" type="text" className="a42-control" maxLength={80} value={brand} onChange={(e) => setBrand(e.target.value)} />
      </span>
      {rivals.map((value, index) => (
        <span className="a42-field" key={index}>
          <label className="a42-label" htmlFor={id + '-rival-' + index}>{'Competitor ' + (index + 1) + ' (optional)'}</label>
          <input id={id + '-rival-' + index} name="competitor" type="text" className="a42-control" maxLength={80} value={value}
            onChange={(e) => { const next = e.target.value; setRivals((current) => current.map((r, i) => (i === index ? next : r))); }} />
        </span>
      ))}
      <MarketField id={id + '-market'} value={where} onChange={setWhere} />
      <button type="submit" className="t42-button">Read the brand</button>
    </SkillBox>
  );
}

function ContextPack({market, ready, onConfirm}){
  const [brief, setBrief] = useState('');
  const [where, setWhere] = useState(market);
  const [error, setError] = useState('');
  const id = useId();

  const submit = () => {
    const text = brief.trim();
    if (text.length < 3 || text.length > MAX_BRIEF){ setError('Type a brief of 3 to 2,000 characters.'); return; }
    setError('');
    onConfirm({title: 'Ask for a context pack?', where: 'A brief for ' + marketWord(where), tier: 'T2', skill: 'context-pack', question: text, market: where});
  };

  return (
    <SkillBox name="context-pack" title="Context pack" ready={ready} error={error} onSubmit={submit}
      lede="Tensions, formats, sounds, creators, and language to use and avoid for a brief, each with its proof.">
      <span className="a42-field">
        <label className="a42-label" htmlFor={id + '-brief'}>Brief (up to 2,000 characters)</label>
        <textarea id={id + '-brief'} name="brief" className="a42-control" rows={4} maxLength={MAX_BRIEF} style={{padding: 'var(--s-2) var(--s-3)', resize: 'vertical'}}
          value={brief} onChange={(e) => setBrief(e.target.value)} />
      </span>
      <MarketField id={id + '-market'} value={where} onChange={setWhere} />
      <button type="submit" className="t42-button">Build the pack</button>
    </SkillBox>
  );
}

/* The creator page is read first: the API answers only for a creator 42
   may name (section 12.1), so a hash typed by hand for anyone else gets no
   form. */
function CreatorFit({creatorId, market, onConfirm, onAuth}){
  const [load, setLoad] = useState({state: 'loading'});
  const [brand, setBrand] = useState('');
  const [error, setError] = useState('');
  const authRef = useRef(onAuth);
  authRef.current = onAuth;
  const id = useId();

  useEffect(() => {
    const ctrl = new AbortController();
    setLoad({state: 'loading'});
    fetchCreator(creatorId, market, {signal: ctrl.signal})
      .then((data) => {
        if (ctrl.signal.aborted) return;
        const creator = data && data.creator;
        setLoad(creator && creator.handle ? {state: 'ready', creator, market: data.market || market} : {state: 'missing'});
      })
      .catch((failure) => {
        if (ctrl.signal.aborted) return;
        if (failure && failure.auth){ setLoad({state: 'auth'}); if (authRef.current) authRef.current(); return; }
        setLoad(failure && failure.status === 404 ? {state: 'missing'} : {state: 'error'});
      });
    return () => ctrl.abort();
  }, [creatorId, market]);

  if (load.state === 'loading') return <p className="t42-line-text">Reading the creator page</p>;
  if (load.state === 'auth') return <p className="t42-line-text">Enter the passcode to read the creator page.</p>;
  if (load.state === 'missing') return <p className="t42-line-text">Creator fit is offered only from a creator page 42 can show.</p>;
  if (load.state === 'error') return <p className="t42-line-text">The creator page could not be read, so creator fit is not offered.</p>;

  const creator = load.creator;
  const named = creator.handle + ' on ' + platformWord(creator.platform);
  const submit = () => {
    const name = brand.trim();
    if (!name){ setError('Type the brand.'); return; }
    setError('');
    const question = 'How well do the public posts of ' + named + ' fit ' + name + '? The topics, formats, tone and language of the posts against the brand.';
    onConfirm({title: 'Ask how this creator fits the brand?', where: named + ' and ' + name, tier: 'T1', skill: 'creator-read', question, market: load.market});
  };

  return (
    <SkillBox name="creator-fit" title="Creator fit" ready error={error} onSubmit={submit}
      lede="The topics, formats, tone and language of one creator's public posts against a brand. Fit only.">
      <p className="t42-line-text">{'Creator: ' + named + ' · ' + marketWord(load.market)}</p>
      <span className="a42-field">
        <label className="a42-label" htmlFor={id + '-brand'}>Brand</label>
        <input id={id + '-brand'} name="brand" type="text" className="a42-control" maxLength={80} value={brand} onChange={(e) => setBrand(e.target.value)} />
      </span>
      <button type="submit" className="t42-button">Read the fit</button>
    </SkillBox>
  );
}

/* The one tap that spends. On the ask's 202 the Ask page follows it; a
   refusal shows the server's own words. */
function SkillConfirm({ask, onAuth, onClose}){
  const [state, setState] = useState({status: 'idle'});
  const titleId = useId();
  const box = useRef(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    const from = typeof document !== 'undefined' ? document.activeElement : null;
    // Cancel takes first focus, so Enter never spends by accident.
    const first = box.current && box.current.querySelector('.t42-button');
    if (first) first.focus();
    return () => {
      live.current = false;
      if (from && from.isConnected && typeof from.focus === 'function') from.focus();
    };
  }, []);
  useFocusTrap(box);

  const send = () => {
    setState({status: 'asking'});
    askQuestion({question: ask.question, market: ask.market, tier: ask.tier, skill: ask.skill})
      .then((started) => {
        if (!live.current) return;
        onClose();
        go('/ask?follow=' + encodeURIComponent(started.ask_id));
      })
      .catch((failure) => {
        if (!live.current) return;
        if (failure && failure.auth){ onClose(); if (onAuth) onAuth(); return; }
        setState({status: 'error', message: failure && failure.status ? failure.message : NO_ANSWER});
      });
  };

  const close = () => { if (state.status !== 'asking') onClose(); };

  return (
    <div className="w42-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div ref={box} className="w42-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}
        onKeyDown={(e) => { if (e.key === 'Escape'){ e.stopPropagation(); close(); } }}>
        <h2 id={titleId} className="w42-title">{ask.title}</h2>
        <p className="w42-where">{ask.where}</p>
        <p className="w42-line">{CEILING[ask.tier]}</p>
        <p className="w42-cost-note">{CREDIT_NOTE}</p>
        {state.status === 'error' && <p className="w42-error" role="alert">{state.message}</p>}
        <div className="w42-actions">
          <button type="button" className="t42-button" onClick={close} disabled={state.status === 'asking'}>Cancel</button>
          <button type="button" className="w42-primary" onClick={send} disabled={state.status === 'asking'} aria-busy={state.status === 'asking' ? 'true' : 'false'}>Ask</button>
        </div>
      </div>
    </div>
  );
}
