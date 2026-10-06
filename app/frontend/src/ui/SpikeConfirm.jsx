/* The confirm before a question that spends credits (contract sections 6
   and 14.1). It names the ceiling, Cancel takes first focus so Enter never
   spends by accident, and closing hands focus back to what opened it.
   Credit pass, 3 October 2026 (Albert: "the user needs context"): given
   `does` and `get`, it reads as three plain answers before anyone spends:
   what it does, what it costs and what you get, with one line on what a
   credit is. */
export const CREDIT_NOTE = 'Credits pay for the live posts 42 reads from social platforms. The cost shown is the most this can use.';

import {useEffect, useId, useRef, useState} from 'react';
import {askSpike} from '../api42.js';
import {go} from '../router.js';
import {longDate} from './TrendCard.jsx';
import {useFocusTrap} from './useFocusTrap.js';
import '../styles/today42.css';
import '../styles/alerts42.css';

export function CostConfirm({title, where, line, does, get, confirmWord, busy = false, error = '', onConfirm, onClose}){
  const titleId = useId();
  const box = useRef(null);
  useEffect(() => {
    const from = typeof document !== 'undefined' ? document.activeElement : null;
    const first = box.current && box.current.querySelector('.t42-button');
    if (first) first.focus();
    return () => {
      if (from && from.isConnected && typeof from.focus === 'function') from.focus();
    };
  }, []);
  useFocusTrap(box);

  // While the ask is on its way the dialog stays open, so its answer is followed, not lost.
  const close = () => { if (!busy) onClose(); };

  return (
    <div className="w42-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div ref={box} className="w42-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}
        onKeyDown={(e) => { if (e.key === 'Escape'){ e.stopPropagation(); close(); } }}>
        <h2 id={titleId} className="w42-title">{title}</h2>
        {where && <p className="w42-where">{where}</p>}
        {does || get
          ? <>
              <dl className="w42-cost" data-cost-confirm="">
                {does && <div><dt>What it does</dt><dd>{does}</dd></div>}
                <div><dt>What it costs</dt><dd className="w42-line">{line}</dd></div>
                {get && <div><dt>What you get</dt><dd>{get}</dd></div>}
              </dl>
              <p className="w42-cost-note">{CREDIT_NOTE}</p>
            </>
          : <p className="w42-line">{line}</p>}
        {error && <p className="w42-error" role="alert">{error}</p>}
        <div className="w42-actions">
          <button type="button" className="w42-primary" onClick={onConfirm} disabled={busy} aria-busy={busy ? 'true' : 'false'}>{confirmWord}</button>
          <button type="button" className="t42-button" onClick={close} disabled={busy}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

/* The spike confirm, shared by the topic page and Compare: a quick scan
   spends up to 60 credits. On the ask's 202 the Ask page follows it; a
   refusal (no measurement that day, too many questions, no agent) shows the
   server's own words. `day` is {date, words, series}; series is optional. */
export function SpikeConfirm({day, itemId, market, onAuth, onClose}){
  const [state, setState] = useState({status: 'idle'});
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => { live.current = false; };
  }, []);

  const ask = () => {
    setState({status: 'asking'});
    askSpike({item_id: itemId, market, date: day.date, series: day.series})
      .then((started) => {
        if (!live.current) return;
        go('/ask?follow=' + encodeURIComponent(started.ask_id));
      })
      .catch((failure) => {
        if (!live.current) return;
        if (failure && failure.auth){ onClose(); if (onAuth) onAuth(); return; }
        setState({status: 'error', message: failure && failure.status ? failure.message : 'The 42 service did not answer. Try again.'});
      });
  };

  return (
    <CostConfirm
      title="Ask why this day jumped?"
      where={day.words + ', ' + longDate(day.date)}
      line="A quick scan, up to 60 credits."
      does="42 reads the posts from that day again and looks for what set the jump off."
      get="An answer on the Ask page, with the posts behind each point."
      confirmWord="Ask"
      busy={state.status === 'asking'}
      error={state.status === 'error' ? state.message : ''}
      onConfirm={ask}
      onClose={onClose}
    />
  );
}
