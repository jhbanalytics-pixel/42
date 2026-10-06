/* Watch on the 42 API (core/api/contract.md section 10.5): the rule chooser
   a card's Watch opens, and the rule words the Alerts screen shows. Every
   rule has data behind it now: rising, growth, reach, a creator breakout
   (breakout_signals, BUILD.md 2.13), a tone flip (v_item_tone_daily, from
   the enrichment tone) and a creator's views surge (detect's watch matches,
   from the breakout step's usual views). The views surge fires only on a
   creator, so it is offered only when the target is one. Demo polish,
   2 October 2026: a rule with no data yet is not offered at all; it would be
   listed in LATER, and SHOW_UNBUILT_RULES (or the chooser's showUnbuilt)
   would bring it back as a disabled choice that says what it needs. */
import {useEffect, useId, useRef, useState} from 'react';
import {sentenceCase} from '../api.js';
import {useFocusTrap} from './useFocusTrap.js';
import '../styles/alerts42.css';

export const MARKET_WORDS = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya', all: 'All markets'};
const BREAKOUT = 'When a creator breaks out';
const TONE_FLIP = 'When the tone flips';
const SURGE = "When the creator's views surge";
/* Rules the API accepts with no data behind them yet: none now. */
const LATER = [];
export const FIRST_CHOICE = {kind: 'rising', ratio: '2', reach: '50'};
export const SHOW_UNBUILT_RULES = false;

const stateWord = (code) => sentenceCase(String(code || '').replace(/_/g, ' '));
const later = (rule) => LATER.find((l) => rule && rule[l.id]);

export function ruleWords(rule){
  const r = rule || {};
  if (Array.isArray(r.state_in)){
    if (r.state_in.length === 1 && r.state_in[0] === 'rising') return 'When it starts rising';
    return 'When it enters ' + r.state_in.map(stateWord).join(' or ');
  }
  if (r.ratio_over !== undefined) return 'When growth passes ' + r.ratio_over + ' times';
  if (r.reach_over !== undefined) return 'When reach passes ' + r.reach_over + ' creators';
  if (r.breakout) return BREAKOUT;
  if (r.tone_flip) return TONE_FLIP;
  if (r.creator_surge) return SURGE;
  const l = later(r);
  return l ? l.label : 'A rule this screen does not know';
}

/* The waiting line for a rule whose data does not exist yet, else null. */
export function waitingWords(rule){
  const l = later(rule);
  return l ? l.waiting : null;
}

/* An alert whose item the gate held back says so, with the gate's reason. */
export function heldWords(held){
  return held.reason_text ? 'Held back: ' + held.reason_text : 'Held back by 42\'s checks';
}

/* The chooser's state as the API rule, or the words saying what is wrong.
   creator says whether the target is a creator, which a views surge needs. */
export function ruleFrom(choice, {creator = false} = {}){
  if (choice.kind === 'ratio'){
    const n = Number(choice.ratio);
    return String(choice.ratio).trim() && Number.isFinite(n) && n > 0 ? {rule: {ratio_over: n}} : {error: 'Growth must be a number above 0.'};
  }
  if (choice.kind === 'reach'){
    const n = Number(choice.reach);
    return String(choice.reach).trim() && Number.isInteger(n) && n > 0 ? {rule: {reach_over: n}} : {error: 'Reach must be a whole number of creators above 0.'};
  }
  if (choice.kind === 'breakout') return {rule: {breakout: true}};
  if (choice.kind === 'tone_flip') return {rule: {tone_flip: true}};
  if (choice.kind === 'creator_surge') return creator ? {rule: {creator_surge: true}} : {error: 'A views surge needs a creator to watch.'};
  return {rule: {state_in: ['rising']}};
}

export function RuleChoice({choice, onChange, creator = false, showUnbuilt = SHOW_UNBUILT_RULES}){
  const set = (patch) => onChange({...choice, ...patch});
  return (
    <fieldset className="w42-rules">
      <legend className="w42-legend">Alert me</legend>
      <label className="w42-rule">
        <input type="radio" name="rule" value="rising" checked={choice.kind === 'rising'} onChange={() => set({kind: 'rising'})} />
        When it starts rising
      </label>
      <span className="w42-rule">
        <label>
          <input type="radio" name="rule" value="ratio" checked={choice.kind === 'ratio'} onChange={() => set({kind: 'ratio'})} />
          When growth passes
        </label>
        <input type="number" name="ratio" className="w42-number" min="0.1" step="0.1" value={choice.ratio} aria-label="Growth, times its usual level"
          onFocus={() => set({kind: 'ratio'})} onChange={(e) => set({kind: 'ratio', ratio: e.target.value})} />
        times
      </span>
      <span className="w42-rule">
        <label>
          <input type="radio" name="rule" value="reach" checked={choice.kind === 'reach'} onChange={() => set({kind: 'reach'})} />
          When reach passes
        </label>
        <input type="number" name="reach" className="w42-number" min="1" step="1" value={choice.reach} aria-label="Reach, creators in 3 days"
          onFocus={() => set({kind: 'reach'})} onChange={(e) => set({kind: 'reach', reach: e.target.value})} />
        creators
      </span>
      <label className="w42-rule">
        <input type="radio" name="rule" value="breakout" checked={choice.kind === 'breakout'} onChange={() => set({kind: 'breakout'})} />
        {BREAKOUT}
      </label>
      <label className="w42-rule">
        <input type="radio" name="rule" value="tone_flip" checked={choice.kind === 'tone_flip'} onChange={() => set({kind: 'tone_flip'})} />
        {TONE_FLIP}
      </label>
      {creator && (
        <label className="w42-rule">
          <input type="radio" name="rule" value="creator_surge" checked={choice.kind === 'creator_surge'} onChange={() => set({kind: 'creator_surge'})} />
          {SURGE}
        </label>
      )}
      {showUnbuilt && LATER.map((l) => (
        <label key={l.id} className="w42-rule w42-rule-later">
          <input type="radio" name="rule" value={l.id} disabled />
          {l.label} <span className="w42-needs">{l.needs}</span>
        </label>
      ))}
    </fieldset>
  );
}

/* The chooser for one card. A card already watched says where to manage
   its watch instead of making a second one. */
export function WatchDialog({card, market, create, onDone, onClose, onAuth}){
  const [choice, setChoice] = useState(FIRST_CHOICE);
  const [state, setState] = useState({status: 'idle'});
  const titleId = useId();
  const box = useRef(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    const first = box.current && box.current.querySelector('input:not([disabled]), button, a');
    if (first) first.focus();
    return () => { live.current = false; };
  }, []);
  useFocusTrap(box);
  const watching = Boolean(card.watch_id);

  const submit = (event) => {
    event.preventDefault();
    const {rule, error} = ruleFrom(choice, {creator: card.kind === 'creator'});
    if (error){ setState({status: 'error', message: error}); return; }
    setState({status: 'saving'});
    Promise.resolve()
      .then(() => create({target: {kind: 'item', item_id: card.item_id}, market, rule, label: String(card.title || '').slice(0, 120)}))
      .then((watch) => { if (live.current) onDone(watch || {}); })
      .catch((failure) => {
        if (!live.current) return;
        if (failure && failure.auth){ onClose(); if (onAuth) onAuth(); return; }
        setState({status: 'error', message: failure && failure.status ? failure.message : 'The 42 service did not answer. Try again.'});
      });
  };

  return (
    <div className="w42-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={box} className="w42-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}
        onKeyDown={(e) => { if (e.key === 'Escape'){ e.stopPropagation(); onClose(); } }}>
        <h2 id={titleId} className="w42-title">{(watching ? 'Watching ' : 'Watch ') + card.title}</h2>
        <p className="w42-where">{MARKET_WORDS[market] || market}</p>
        {watching
          ? <>
              <p className="w42-line">You are watching this. Pause or resume it on <a className="t42-link" href="#/alerts">Alerts</a>.</p>
              <div className="w42-actions"><button type="button" className="t42-button" onClick={onClose}>Close</button></div>
            </>
          : <form onSubmit={submit} noValidate>
              <RuleChoice choice={choice} onChange={setChoice} creator={card.kind === 'creator'} />
              {state.status === 'error' && <p className="w42-error" role="alert">{state.message}</p>}
              <div className="w42-actions">
                <button type="submit" className="w42-primary" disabled={state.status === 'saving'} aria-busy={state.status === 'saving' ? 'true' : 'false'}>Watch</button>
                <button type="button" className="t42-button" onClick={onClose}>Cancel</button>
              </div>
            </form>}
      </div>
    </div>
  );
}

/* One chooser per page. Without create (no watch route to call) there is no
   onWatch, so the cards show no Watch button. A watch made here marks its
   card Watching until the page reads the card again. */
export function useCardWatch(create, onAuth){
  const [open, setOpen] = useState(null);
  const [made, setMade] = useState({});
  const opener = useRef(null);
  const restore = useRef(false);
  useEffect(() => {
    if (open || !restore.current) return;
    restore.current = false;
    const el = opener.current;
    opener.current = null;
    if (el && el.isConnected && typeof el.focus === 'function') el.focus();
  }, [open]);
  if (!create) return {onWatch: undefined, mark: (card) => card, dialog: null};

  const key = (card, market) => (card.market || market) + ':' + card.item_id;
  const mark = (card, market) => {
    const id = made[key(card, market)];
    return id && !card.watch_id ? {...card, watch_id: id} : card;
  };
  const onWatch = (card, market) => {
    opener.current = typeof document !== 'undefined' ? document.activeElement : null;
    setOpen({card, market: card.market || market});
  };
  const close = () => { restore.current = true; setOpen(null); };
  const dialog = open
    ? <WatchDialog
        card={mark(open.card, open.market)}
        market={open.market}
        create={create}
        onAuth={onAuth}
        onClose={close}
        onDone={(watch) => {
          setMade((m) => ({...m, [key(open.card, open.market)]: watch.watch_id || 'watching'}));
          close();
        }}
      />
    : null;
  return {onWatch, mark, dialog};
}
