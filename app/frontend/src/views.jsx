/* PULSE · detail panel (Prompt Pulse), browse, method */
import {useState, useEffect, useRef} from 'react';
import {apiGetFresh, failureCode} from './api.js';
import {Icon, MOM_META, LIFECYCLE_META, CONTINUITY_META, MomentumChart, Sentiment, Gloss, SignalLoader, useDialog} from './parts.jsx';
import {seriesChange} from './model.js';
import {refineBrief} from './ask.jsx';
import {PageHero} from './ui/index.js';
import {human, readerWord, webHref} from './model.js';
import {runSuggestions} from './suggestions.js';
import {briefText, hasBriefContent} from './briefText.js';
import './styles/boardviews.css';

export {briefText};

/* Forecast outlook chip: the engine's 7-day BQML read, glyph + colour echoing
   the momentum scale. Only these three values render; anything else no-ops. */
const OUTLOOK_META = {
  heating: {label: 'Heating', icon: Icon.arrUp,   color: 'var(--rising)'},
  steady:  {label: 'Steady',  icon: Icon.arrFlat,  color: 'var(--steady)'},
  cooling: {label: 'Cooling', icon: Icon.arrDown,  color: 'var(--cooling)'},
};

/* per-topic team state lives client-side: status, notes, briefed toggles */
const STATUS_KEY = 'pulse-status';
const NOTES_KEY = 'pulse-notes';
const BRIEFED_KEY = 'pulse-briefed';
function readStore(k){ try { const v = JSON.parse(localStorage.getItem(k) || '{}'); return v && typeof v === 'object' ? v : {}; } catch (e){ return {}; } }
function writeStore(k, v){ try { localStorage.setItem(k, JSON.stringify(v)); } catch (e){} }

export function CopyBtn({text}){
  const [done, setDone] = useState(false);
  const copy = (e) => {
    e.stopPropagation();
    navigator.clipboard && navigator.clipboard.writeText(text).catch(() => {});
    setDone(true); setTimeout(() => setDone(false), 1600);
  };
  return <button className={'copy-btn' + (done ? ' done' : '')} onClick={copy}>{done ? 'Copied' : 'Copy'}</button>;
}

export function CopyBriefBtn({t}){
  const [done, setDone] = useState(false);
  if (!hasBriefContent(t?.brief)) return null;
  const copy = () => {
    navigator.clipboard && navigator.clipboard.writeText(briefText(t)).catch(() => {});
    setDone(true); setTimeout(() => setDone(false), 1800);
  };
  return <button className={'copy-brief' + (done ? ' done' : '')} onClick={copy}>{done ? 'Copied · paste anywhere' : 'Copy brief'}</button>;
}

const PP_STEPS = [
  ['Trend', 'trend', 'What signal is picking up speed right now.'],
  ['Relevance', 'relevance', 'What the data says about scale: fad or moment.'],
  ['Opportunity', 'opportunity', 'The hidden angle everyone else is missing.'],
  ['Idea', 'idea', 'How Nano or Lyria fills the gap with one piece.'],
  ['Prompt', 'prompt', 'The exact instructions to get the best result.'],
];

export function RefineBar({t, onUpdate}){
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);
  const [q, setQ] = useState('');
  const go = async (instruction) => {
    if (busy || !instruction.trim()) return;
    setBusy(true); setErr(false);
    try {
      const next = await refineBrief(t, instruction.trim());
      onUpdate(next);
      setQ('');
    } catch (e){ setErr(true); }
    setBusy(false);
  };
  return (
    <div className="refine">
      <div className="dh">Refine this brief</div>
      <div className="refine-chips">
        {['Sharper PR angle', 'Alternative mechanic', 'Stronger evidence challenge'].map((c) => (
          <button key={c} className="legacy-chip" disabled={busy} onClick={() => go(c)}>{c}</button>
        ))}
      </div>
      <div className="refine-row">
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') go(q); }}
          placeholder="or tell the engine how to adjust it…" disabled={busy} />
        <button className="legacy-action" disabled={busy} onClick={() => go(q)}>{busy ? 'Refining…' : 'Refine'}</button>
      </div>
      {err && <div className="refine-err">Couldn’t refine right now. Try again.</div>}
    </div>
  );
}

export function TopicDetail({t, onClose, onUpdate}){
  const panelRef = useRef(null);
  useDialog(panelRef, !!t);
  const [settled, setSettled] = useState(false);
  const [status, setStatusRaw] = useState('new');
  const [note, setNote] = useState('');
  const [briefed, setBriefed] = useState({});
  /* lazy opportunity lens for engine-tracked briefs: fetched on panel open
     when the brief ships without one. Quiet shimmer while it loads; the step
     hides entirely on an empty answer or a failure. Generated briefs carry
     their own opportunity and never fetch. */
  const [opp, setOpp] = useState('');
  const [oppLoading, setOppLoading] = useState(false);
  useEffect(() => {
    setOpp(''); setOppLoading(false);
    if (!t || t.generated || !hasBriefContent(t.brief) || t.brief.opportunity) return;
    let live = true;
    setOppLoading(true);
    const region = String(t.region || 'all').toLowerCase();
    apiGetFresh('/api/desk/opportunity/' + encodeURIComponent(t.id) + '?region=' + encodeURIComponent(region))
      .then((d) => {
        if (!live) return;
        setOppLoading(false);
        if (d && typeof d.opportunity === 'string' && d.opportunity) setOpp(d.opportunity);
      })
      .catch(() => { if (live) setOppLoading(false); });
    return () => { live = false; };
  }, [t && t.id]);
  useEffect(() => {
    if (!t) return;
    setStatusRaw((readStore(STATUS_KEY)[t.id] || {}).s || 'new');
    setNote(readStore(NOTES_KEY)[t.id] || '');
    setBriefed(readStore(BRIEFED_KEY));
  }, [t && t.id]);
  useEffect(() => {
    if (!t){ setSettled(false); return; }
    const id = setTimeout(() => setSettled(true), 550);
    return () => clearTimeout(id);
  }, [t]);
  const setStatus = (s) => {
    setStatusRaw(s);
    const m = readStore(STATUS_KEY); m[t.id] = {s: s, at: Date.now()}; writeStore(STATUS_KEY, m);
  };
  const saveNote = (v) => {
    setNote(v);
    const m = readStore(NOTES_KEY); m[t.id] = v; writeStore(NOTES_KEY, m);
  };
  const toggleBriefed = (h) => {
    const m = readStore(BRIEFED_KEY); m[h] = !m[h]; writeStore(BRIEFED_KEY, m); setBriefed({...m});
  };
  const color = t ? MOM_META[t.momentum].color : 'var(--accent)';
  const outlook = t && t.outlook ? OUTLOOK_META[t.outlook] : null;
  const OutlookArr = outlook ? outlook.icon : null;
  /* Wave 1 badges: lifecycle phase and continuity state. Both stay null until
     the engine writes the column, so the badge renders nothing today. */
  const lifecycle = t && t.lifecycle ? LIFECYCLE_META[t.lifecycle] : null;
  const continuity = t && t.continuity ? CONTINUITY_META[t.continuity] : null;
  const platforms = t && Array.isArray(t.platforms) ? t.platforms : [];
  const peakSrc = platforms.length ? Math.max(...platforms.map((p) => p[1])) : 1;
  const brief = hasBriefContent(t?.brief) ? t.brief : null;
  /* no hollow cards: an idea without text or a prompt without a nano/lyria
     body drops its numbered slot entirely, same rule as citations */
  const idea = brief && brief.idea && brief.idea.text ? brief.idea : null;
  const prompt = brief && brief.prompt && (brief.prompt.nano || brief.prompt.lyria) ? brief.prompt : null;
  const oppText = brief ? (brief.opportunity || opp) : '';
  /* The url column (index 5) of a receipt tuple is collected data; only a web address stays a link. */
  const receipts = (t && Array.isArray(t.receipts) ? t.receipts : []).map((r) => (Array.isArray(r) ? r.map((cell, index) => (index === 5 ? webHref(cell) : cell)) : r));
  const dData = t && t.series ? t.series.slice(-14) : null;
  const dChg = dData ? seriesChange(dData) : null;
  /* the only chart annotation is the client-side lifecycle marker */
  const dEvents = dData && status === 'activated' ? [{i: dData.length - 1, label: 'Activated'}] : null;
  return (
    <>
      <div className={'scrim' + (t ? ' show' : '') + (settled ? ' settled' : '')} onClick={onClose} />
      <aside ref={panelRef} className={'panel' + (t ? ' show' : '') + (settled ? ' settled' : '')} style={{'--mom-color': color}} data-screen-label="Topic brief panel" role="dialog" aria-modal="true" aria-labelledby="topic-panel-title" aria-hidden={t ? undefined : 'true'}>
        {t && <>
          <div className="panel-head">
            <div className="top">
              <div className="panel-region">{t.regionName} · {t.region}{t.generated ? ' · Live brief' : ''}</div>
              <div style={{display: 'flex', alignItems: 'center', gap: '8px'}}>
                <CopyBriefBtn t={t} />
                <button className="copy-brief" onClick={() => window.print()}>PDF</button>
                <button className="panel-close" aria-label="Close brief" onClick={onClose}><Icon.close /></button>
              </div>
            </div>
            <h2 className="panel-title" id="topic-panel-title">{t.topic}</h2>
            <div className="panel-label">{t.label}</div>
            <div className="panel-meta">
              <div className="m"><span className="v mom-v">{MOM_META[t.momentum].label}</span><div className="k">Momentum</div></div>
              {typeof t.score === 'number' && <div className="m"><span className="v tnum">{t.score.toFixed(3)}</span><div className="k">Score</div></div>}
              {typeof t.mentions === 'number' && <div className="m"><span className="v tnum">{t.mentions}</span><div className="k">Mentions</div></div>}
              {typeof t.creators === 'number' && <div className="m"><span className="v tnum">{t.creators}</span><div className="k">Creators</div></div>}
              {typeof t.sources === 'number' && <div className="m"><span className="v tnum">{t.sources}</span><div className="k">Sources</div></div>}
              {outlook && <div className="m"><span className="v outlook-v" style={{color: outlook.color}}><OutlookArr className="outlook-arr" /> {outlook.label}</span><div className="k">7-day outlook</div></div>}
              {lifecycle && <div className="m"><span className="v badge-v" style={{color: lifecycle.color}}>{lifecycle.label}</span><div className="k">Lifecycle</div></div>}
              {continuity && <div className="m"><span className="v badge-v" style={{color: continuity.color}}>{continuity.label}</span><div className="k">Continuity</div></div>}
            </div>
            <div className="status-row">
              <span className="st-lab">Status</span>
              <div className="status-seg">
                {[['new', 'New'], ['reviewing', 'Reviewing'], ['activated', 'Activated']].map(([k, l]) => (
                  <button key={k} className={status === k ? 'on' : ''} onClick={() => setStatus(k)}>{l}</button>
                ))}
              </div>
              {t.generated && <span className="est-note">Live retrieval figures · not engine-tracked</span>}
            </div>
          </div>

          <div className="panel-body">
            {dData && (
              <div className="detail-chart">
                <div className="ch-head">
                  <span className="t">14-day signal trend</span>
                  {dChg !== null && <span className={'change ' + (dChg >= 0 ? 'up' : 'down')}>{dChg >= 0 ? '▲' : '▼'} {Math.abs(dChg)}%</span>}
                </div>
                <MomentumChart data={dData} color={color} height={150} events={dEvents} />
              </div>
            )}

            {t.voices && t.voices.length > 0 && (
              <div className="cited-voices">
                <div className="dh" style={{display: 'flex', alignItems: 'center', gap: '8px'}}><span className="cited-voices-dot" />Cited voices from the feed</div>
                <div className="cited-voices-rows">
                  {t.voices.map((g, i) => (
                    <div className="cited-voices-row" key={i}>
                      <span className="cited-voices-src">{readerWord(g[0])}</span>
                      <span className="cited-voices-q">“<Gloss text={g[1]} />”</span>
                      {g[2] ? <span className="cited-voices-age tnum">{g[2]}</span> : null}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {brief && <>
              <div className="pp-intro"><span>Signal brief</span><span className="ln" /></div>
              <div className="pp-goal">North star · turn this signal into participation with <b>Nano Banana</b> &amp; <b>Lyria</b></div>
              <div className="pp">
                {PP_STEPS.map(([label, key, hint], idx) => {
                  if (key === 'idea' && !idea) return null;
                  if (key === 'prompt' && !prompt) return null;
                  if (key === 'opportunity'){
                    if (!oppText && !oppLoading) return null;
                  } else if (key !== 'idea' && key !== 'prompt' && !brief[key]) return null;
                  return (
                    <div className="pp-step" key={key}>
                      <div className="pp-num"><span className="n">{idx + 1}</span></div>
                      <div>
                        <div className="pp-k">The {label}</div>
                        {key === 'idea' ? (
                          <>
                            <span className="pp-tool">{idea.tool === 'Lyria' ? <Icon.music className="ic" /> : <Icon.image className="ic" />} {idea.tool === 'Nano' ? 'Nano Banana' : idea.tool}</span>
                            <div className="pp-v">{idea.text}</div>
                          </>
                        ) : key === 'prompt' ? (
                          <>
                            <div className="pp-v" style={{color: 'var(--muted)', fontSize: '13px', marginBottom: '4px'}}>{hint}</div>
                            {prompt.nano && (
                              <div className="prompt-box">
                                <div className="pb-k"><span>Nano Banana · image</span><CopyBtn text={prompt.nano} /></div>
                                <div className="pb-v">{prompt.nano}</div>
                              </div>
                            )}
                            {prompt.lyria && (
                              <div className="prompt-box">
                                <div className="pb-k"><span>Lyria · music</span><CopyBtn text={prompt.lyria} /></div>
                                <div className="pb-v">{prompt.lyria}</div>
                              </div>
                            )}
                          </>
                        ) : key === 'opportunity' ? (
                          oppText
                            ? <div className="pp-v">{oppText}</div>
                            : <div className="pp-v opp-shimmer" aria-hidden="true"><span /><span /></div>
                        ) : (
                          <div className="pp-v">{brief[key]}</div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </>}

            {t.generated && onUpdate && <RefineBar t={t} onUpdate={onUpdate} />}

            {receipts.length > 0 ? (
              <div className="detail-sec">
                <div className="dh">The receipts · top signal sources</div>
                <div className="receipts">
                  {receipts.map((r, i) => (
                    <div className="receipt" key={i}>
                      <span className="rc-src">{readerWord(r[0])}</span>
                      <span className="rc-body">{r[5] ? <a href={r[5]} target="_blank" rel="noopener noreferrer" style={{color: 'var(--accent-text)', fontWeight: 600}}>{r[1]}</a> : <b>{r[1]}</b>} · {r[2]}</span>
                      <span className="rc-metric tnum">{r[3]}{r[4] ? <span className="rc-age">{(r[3] ? ' · ' : '') + r[4]}</span> : null}</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : t.generated ? (
              <div className="detail-sec">
                <div className="dh">The receipts · top signal sources</div>
                <div className="rc-empty">Live brief. Engine-tracked sources attach once this topic enters the tracked set.</div>
              </div>
            ) : null}

            {platforms.length > 0 && (
              <div className="detail-sec">
                <div className="dh">Where it lives</div>
                <div className="plat-bars">
                  {platforms.map(([name, val]) => (
                    <div className="plat-row" key={name}>
                      <span className="pn">{name}</span>
                      <span className="track"><span className="fill" style={{width: (val / peakSrc * 100) + '%'}} /></span>
                      <span className="pv tnum">{val}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {t.creators_list && t.creators_list.length > 0 && (
              <div className="detail-sec">
                <div className="dh">Top creators to brief</div>
                <div className="creator-cards">
                  {t.creators_list.map((c) => (
                    <div className="creator-card" key={c}>
                      <span className="cc-h">{c}</span>
                      <button className={'cc-brief' + (briefed[c] ? ' on' : '')} onClick={() => toggleBriefed(c)}>{briefed[c] ? 'Briefed ✓' : 'Brief'}</button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {(typeof t.sentiment === 'number' || t.social_mood) && (
              <div className="detail-sec">
                <div className="dh">How it reads</div>
                <div style={{display: 'flex', alignItems: 'center', gap: '14px', flexWrap: 'wrap'}}>
                  {typeof t.sentiment === 'number' && <Sentiment s={t.sentiment} />}
                  {t.social_mood && <span style={{fontFamily: 'var(--sans)', fontSize: '13px', fontWeight: 600, color: 'var(--ink)'}}>{t.social_mood}</span>}
                  <span style={{fontFamily: 'var(--mono)', fontSize: '10px', color: 'var(--muted)'}}>
                    {typeof t.sentiment === 'number' ? 'media tone ' + (0.5 + t.sentiment / 2).toFixed(2) + ' (news)' : ''}{t.social_mood ? ' · social mood · monitored source' : ''}
                  </span>
                </div>
              </div>
            )}

            <div className="detail-sec">
              <div className="dh">Team notes</div>
              <textarea className="notes" value={note} onChange={(e) => saveNote(e.target.value)}
                placeholder="Drop context for the team: angle taken, client reaction, next step…" rows={3}></textarea>
            </div>
          </div>
        </>}
      </aside>
    </>
  );
}

/* PULSE · Browse, the post archive search. Real /api/ask retrieval: volume
   over time, platform split, the trends it surfaces (statement + how to use
   it), real quote cards, and one overall read. No model brief is generated
   here (that is Ask/Console); this is ranked retrieval and aggregation only.
   Every zone maps to a real field and hides when the field is absent, per
   the DATA_SPEC in the V3 handoff: thin/broad flags, unit·window·market on
   every figure, no fabricated placeholders. */
function browseMomColor(m){
  return (MOM_META[m] || MOM_META.steady).color;
}

function BrowseVolumeChart({series, chart}){
  const values = (Array.isArray(series) ? series : []).map((p) => Number(p && p.n) || 0);
  if (values.length < 2) return null;
  const w = 400, h = 130, pad = 12;
  const min = Math.min(...values), max = Math.max(...values), rng = (max - min) || 1;
  const x = (i) => (i / (values.length - 1)) * w;
  const y = (v) => h - pad - ((v - min) / rng) * (h - pad * 2);
  const line = values.map((v, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1)).join(' ');
  const area = line + ' L' + w + ' ' + h + ' L0 ' + h + ' Z';
  const isBars = chart === 'Bars';
  const isArea = chart !== 'Bars' && chart !== 'Line';
  const bw = w / values.length;
  return (
    <svg viewBox={'0 0 ' + w + ' ' + h} preserveAspectRatio="none" style={{width: '100%', height: '150px', display: 'block'}}>
      <defs>
        <linearGradient id="brw-vol-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--accent)" stopOpacity="0.26" />
          <stop offset="1" stopColor="var(--accent)" stopOpacity="0" />
        </linearGradient>
      </defs>
      {isBars ? (
        values.map((v, i) => {
          const bh = 6 + ((v - min) / rng) * (h - pad * 2 - 6);
          return <rect key={i} x={(i * bw + bw * 0.18).toFixed(1)} y={(h - bh).toFixed(1)} width={(bw * 0.64).toFixed(1)} height={bh.toFixed(1)} rx="1.5" fill="var(--accent)" />;
        })
      ) : (
        <>
          {isArea && <path d={area} fill="url(#brw-vol-grad)" />}
          <path d={line} fill="none" stroke="var(--accent)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
        </>
      )}
    </svg>
  );
}

export function BrowsePage({region, desk, onAuth}){
  const reg = String(region || 'all').toLowerCase();
  /* Search prompts are the completed run's admitted signals for this market;
     a run that admitted nothing prompts nothing. */
  const suggestions = runSuggestions(desk, reg);
  const [term, setTerm] = useState('');
  const [q, setQ] = useState('');
  const [res, setRes] = useState({state: 'idle'});
  const [retry, setRetry] = useState(0);
  /* design-variant host props: default state is the shipped design, per the
     handoff's "in-design tweak controls" note. Reading mode changes measure
     width only; chart shape changes only how the same real volume series
     renders (area default, line or bars as alternates). */
  const [reading] = useState('Analyst');
  const [chart] = useState('Area');

  useEffect(() => {
    if (!q){ setRes({state: 'idle'}); return; }
    let live = true;
    let timedOut = false;
    const controller = new AbortController();
    const timeout = setTimeout(() => {
      timedOut = true;
      controller.abort();
      if (live) setRes({state: 'timeout'});
    }, 30_000);
    setRes({state: 'loading'});
    const path = '/api/ask?q=' + encodeURIComponent(q) + '&market=' + encodeURIComponent(reg);
    apiGetFresh(path, controller.signal).then((d) => { if (live && !timedOut) setRes({state: 'ready', data: d}); })
      .catch((e) => {
        if (!live || timedOut) return;
        if (e && e.auth){ onAuth && onAuth(); return; }
        if (failureCode(e) === 'http_404' && e.message === 'No such API route.'){
          setRes({state: 'unavailable'});
          return;
        }
        setRes({state: 'error', message: e && e.message ? e.message : 'The completed run could not be read just now.'});
      })
      .finally(() => clearTimeout(timeout));
    return () => {
      live = false;
      clearTimeout(timeout);
      controller.abort();
    };
  }, [q, reg, retry, onAuth]);

  const submit = (e) => { e.preventDefault(); const t = term.trim(); if (t) { if (t === q) setRetry(value => value + 1); else setQ(t); } };
  const d = res.state === 'ready' ? res.data : null;

  const maxW = reading === 'Brief' ? '840px' : reading === 'Immersive' ? '1280px' : 'var(--maxw)';
  const quoteCols = reading === 'Brief' ? 1 : 3;

  const trends = d && Array.isArray(d.trends) ? d.trends : [];
  const quotes = d && Array.isArray(d.quotes) ? d.quotes : [];
  const platformSplit = d && Array.isArray(d.platform_split) ? d.platform_split : [];
  const peakPlatform = platformSplit.length ? Math.max(...platformSplit.map((p) => Number(p.n) || 0)) : 1;

  return (
    <div className="page" data-screen-label="Browse · the post archive">
      {/* Shell consistency, 2 October 2026: the shared shell gutter, the
          menu's noun as the title and the old headline as the sentence under
          it. A reading width narrows the column from the left edge, so the
          title never moves off the line the other pages start on. */}
      <div className="page-shell" style={{maxWidth: maxW}}>
        <div className="reveal">
          <h1 className="page-title">Browse</h1>
          <p className="page-sub">Search what people actually said in the post archive.</p>

          <form onSubmit={submit} className="brw-search-form" style={{display: 'flex', alignItems: 'center', gap: '12px', padding: '0 0 0 20px', maxWidth: '640px'}}>
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="var(--accent)" strokeWidth="2"><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></svg>
            {/* A placeholder is not a name. The field went six cells with no
                accessible name at all, so the label states it and stays out of
                the layout. */}
            <label className="brw-search-label sr-only" htmlFor="brw-search-input">Search the post archive</label>
            <input className="brw-search-input" id="brw-search-input" value={term} onChange={(e) => setTerm(e.target.value)} placeholder="amapiano, a hashtag, a brand"
              style={{flex: 1, fontFamily: 'var(--sans)', fontSize: 'var(--type-4)'}} />
            <button type="submit" className="brw-search-btn">Search</button>
          </form>

          {res.state === 'loading' && (
            <div style={{display: 'flex', alignItems: 'center', gap: '14px', marginTop: '26px'}}>
              <SignalLoader />
              <span style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-2)', color: 'var(--muted)'}}>Searching the archive</span>
            </div>
          )}
          {res.state === 'unavailable' && <p role="alert" style={{marginTop: '26px', color: 'var(--muted)'}}>Browse is not available yet. This app cannot search the post archive.</p>}
          {(res.state === 'error' || res.state === 'timeout') && (
            <div role="alert" style={{marginTop: '26px', color: 'var(--muted)'}}>
              <p>{res.state === 'timeout' ? 'The archive search is taking too long.' : 'The archive could not be read just now. ' + (res.message || '')}</p>
              <button type="button" className="brw-search-btn" onClick={() => setRetry(value => value + 1)}>Retry search</button>
            </div>
          )}

          {d && (
            <div style={{display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap', margin: '18px 0 26px'}}>
              <span style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', color: 'var(--muted)'}}>{human(d.total_matches)} matches · 30d · {reg.toUpperCase()}</span>
              <span className="instrument-provenance" style={{color: 'var(--muted)', border: '1px solid var(--line)', padding: '3px 9px'}}>{d.thin ? 'Limited retrieved evidence' : 'Retrieved evidence'}</span>
            </div>
          )}
        </div>

        {d && (
          <>
            <div className="brw-read-limits" style={{margin: '0 0 24px'}}>
              <p style={{fontSize: 'var(--type-2)', lineHeight: 1.5, color: 'var(--muted)'}}>{quotes.length ? 'These excerpts do not include links to the original posts. Review the sources before using this evidence.' : 'No source excerpts were returned for this search.'}</p>
              {(trends.length > 0 || (d.overall_read && !d.thin)) && <p style={{fontSize: 'var(--type-2)', lineHeight: 1.5, color: 'var(--muted)'}}>Any saved reading below needs source review.</p>}
              <a className="legacy-action" href={'#/ask?q=' + encodeURIComponent(q) + (['za', 'ng', 'ke'].includes(reg) ? '&market=' + reg.toUpperCase() : '') + '&draft=1'}>Ask about this search</a>
            </div>
            <div className="brw-vol-row stagger" style={{display: 'grid', gridTemplateColumns: '1.6fr 1fr', gap: '14px', marginBottom: '16px'}}>
              <div style={{border: '1px solid var(--line)', background: 'var(--surface)', padding: '18px 20px 12px'}}>
                <div style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', letterSpacing: '0.14em', textTransform: 'uppercase', color: 'var(--muted)', marginBottom: '8px'}}>Volume · matches per day · 30d</div>
                <BrowseVolumeChart series={d.volume_series} chart={chart} />
              </div>
              {platformSplit.length > 0 && (
                <div style={{border: '1px solid var(--line)', background: 'var(--surface)', padding: '18px 20px'}}>
                  <div style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', letterSpacing: '0.14em', textTransform: 'uppercase', color: 'var(--muted)', marginBottom: '12px'}}>Where it lives</div>
                  {platformSplit.map((p) => (
                    <div key={p.platform} style={{display: 'grid', gridTemplateColumns: '74px 1fr auto', gap: '12px', alignItems: 'center', marginBottom: '10px'}}>
                      <span style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', color: 'var(--ink-2)'}}>{p.platform}</span>
                      <span style={{height: '6px', background: 'var(--surface-inset)', overflow: 'hidden', display: 'block'}}>
                        <span style={{display: 'block', height: '100%', width: (Number(p.n) / peakPlatform * 100) + '%', background: 'var(--accent)'}} />
                      </span>
                      <span style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', color: 'var(--faint)'}}>{human(p.n)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {trends.length > 0 && (
              <>
                <div style={{display: 'flex', alignItems: 'center', gap: '14px', margin: '30px 0 16px'}}>
                  <span style={{fontFamily: 'var(--sans)', fontSize: 'var(--type-2)', letterSpacing: '0.14em', textTransform: 'uppercase', fontWeight: 600, color: 'var(--ink)'}}><span style={{display: 'inline-block', width: '16px', height: '2px', background: 'var(--accent)', marginRight: '12px', verticalAlign: 'middle'}} />What the search is telling you</span>
                  <span style={{flex: 1, height: '1px', background: 'var(--line)'}} />
                </div>
                <div className="stagger" style={{display: 'flex', flexDirection: 'column', gap: '12px', marginBottom: '30px'}}>
                  {trends.map((t, i) => {
                    const color = browseMomColor(t.momentum);
                    return (
                      <div key={i} className="brw-trend-card" style={{border: '1px solid var(--line)', borderLeft: '2px solid ' + color, background: 'var(--surface)', padding: '18px 20px'}}>
                        <div style={{display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '8px'}}>
                          <span style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', letterSpacing: '0.1em', textTransform: 'uppercase', fontWeight: 600, padding: '3px 7px', color, border: '1px solid ' + color}}>{(MOM_META[t.momentum] || MOM_META.steady).label}</span>
                          <span style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', letterSpacing: '0.06em', textTransform: 'uppercase', color: 'var(--faint)'}}>{t.evidence_count} posts{Array.isArray(t.platforms) && t.platforms.length ? ' · ' + t.platforms.join(', ') : ''}</span>
                        </div>
                        <p style={{fontFamily: 'var(--serif)', fontSize: 'var(--type-5)', lineHeight: 1.35, color: 'var(--ink)', margin: '0 0 8px'}}>{t.statement}</p>
                        <p style={{fontSize: 'var(--type-3)', lineHeight: 1.5, color: 'var(--muted)', margin: 0}}><b style={{color: 'var(--ink-2)'}}>How to use it:</b> {t.how_to_use}</p>
                      </div>
                    );
                  })}
                </div>
              </>
            )}

            {quotes.length > 0 && (
              <>
                <div style={{display: 'flex', alignItems: 'center', gap: '14px', margin: '30px 0 16px'}}>
                  <span style={{fontFamily: 'var(--sans)', fontSize: 'var(--type-2)', letterSpacing: '0.14em', textTransform: 'uppercase', fontWeight: 600, color: 'var(--ink)'}}><span style={{display: 'inline-block', width: '16px', height: '2px', background: 'var(--accent)', marginRight: '12px', verticalAlign: 'middle'}} />Real quotes</span>
                  <span style={{flex: 1, height: '1px', background: 'var(--line)'}} />
                  <span style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', letterSpacing: '0.1em', color: 'var(--faint)', textTransform: 'uppercase'}}>selected excerpts</span>
                </div>
                <div className="brw-quote-grid" style={{display: 'grid', gridTemplateColumns: 'repeat(' + quoteCols + ',1fr)', gap: '13px', marginBottom: '28px'}}>
                  {quotes.map((qt, i) => (
                    <div key={i} style={{border: '1px solid var(--line)', background: 'var(--surface)', padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: '10px'}}>
                      <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between'}}>
                        <span style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--accent-text)', fontWeight: 600}}>{qt.platform}{qt.market ? ' · ' + qt.market : ''}</span>
                        {qt.age && <span style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', color: 'var(--faint)'}}>{qt.age}</span>}
                      </div>
                      <p style={{fontFamily: 'var(--serif)', fontSize: 'var(--type-4)', lineHeight: 1.5, color: 'var(--ink)', margin: 0}}>&ldquo;{qt.text}&rdquo;</p>
                      <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px', marginTop: 'auto', paddingTop: '10px', borderTop: '1px solid var(--hairline)'}}>
                        {qt.handle && <span style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', color: 'var(--accent-text)', fontWeight: 600}}>{qt.handle}</span>}
                        {typeof qt.engagement === 'number' && <span style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', color: 'var(--faint)'}}>{human(qt.engagement)} eng</span>}
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}

            {d.overall_read && (
              <div style={{border: '1px solid var(--line)', borderLeft: '2px solid var(--accent)', background: 'var(--surface-2)', padding: '20px 24px'}}>
                <div style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-1)', letterSpacing: '0.16em', textTransform: 'uppercase', color: 'var(--accent-text)', fontWeight: 600, marginBottom: '10px'}}>Overall read</div>
                <p style={{fontSize: 'var(--type-4)', lineHeight: 1.6, color: 'var(--ink)', margin: 0, maxWidth: 'min(72ch,100%)'}}>{d.overall_read}</p>
              </div>
            )}
          </>
        )}

        {!d && res.state === 'idle' && suggestions.length > 0 && (
          <div style={{marginTop: '30px', display: 'flex', gap: '8px', flexWrap: 'wrap'}}>
            {suggestions.map((s) => (
              <button key={s} type="button" className="legacy-chip" onClick={() => { setTerm(s); setQ(s); }}>{s}</button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* Section headings on Method are plain headings in sentence case, set in
   the section face every other page uses (nightdesk.css), so Method no
   longer titles its sections smaller than its siblings. */
const methodHeading = {margin: 'var(--s-6) 0 var(--s-4)'};
/* Steps and rules share one list style: short paragraphs at a reading measure. */
const methodList = {margin: 0, paddingInlineStart: '1.4em', display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: '14px'};
const methodItem = {fontSize: 'var(--type-body)', lineHeight: 1.55, color: 'var(--ink-2)', paddingInlineStart: '4px'};

/* Demo polish, 2 October 2026: the steps and rules are written for a
   marketing reader, with no pipeline, staging or model names. */
const PIPELINE_STEPS = [
  {no: '01', title: 'Collect', desc: 'Each day 42 gathers public posts, searches, news and music from social platforms and the open web in South Africa, Nigeria and Kenya.'},
  {no: '02', title: 'Organise', desc: 'Posts are grouped into topics. A topic reaches Today only once it passes 42\'s evidence checks; the rest stay in Discover, marked as not yet checked or held back with the reason.'},
  {no: '03', title: 'Read', desc: 'Today puts the topics that are moving most at the top, one market at a time, with the posts that show it.'},
  {no: '04', title: 'Brief', desc: 'Topics on Today come with a short brief: what changed and why it matters, with the posts behind it. Topics still being checked say No explanation yet.'},
];

const DATA_RULES = [
  'Creators are ranked by the attention their posts earn, not by how often they post. Radar ranks topics by how many different creators post about them.',
  'Share of voice is a percentage within one market, adding up to 100 across that market.',
  'Figures show their unit, time window and market. A figure 42 could not measure says so instead of showing 0.',
  'South Africa, Nigeria and Kenya are never added together into one figure, unless the view says it covers all markets.',
  'Momentum is movement we have seen. Anything predicted or inferred is labelled as such.',
];

export function MethodPage(){
  return (
    <div className="page" data-screen-label="Method · how 42 reads">
      <div className="page-shell">
        <div className="method-hero reveal">
          {/* Shell consistency, 2 October 2026: the menu's noun is the title
              and the old headline leads the sentence under it. */}
          <PageHero
            title="Method"
            sub="How 42 reads the signal: the latest posts in South Africa, Nigeria and Kenya, with a figure shown only when its source and unit are known."
          />
        </div>

        <div className="method-grid">
        <section>
        <h2 style={methodHeading}>How it works</h2>
        {/* An ordered process reads as an ordered list: short paragraphs with
            run-in heads at a reading measure, not a row of feature cards. */}
        <ol className="method-steps stagger" style={methodList}>
          {PIPELINE_STEPS.map((s) => (
            <li key={s.no} style={methodItem}>
              <strong style={{fontWeight: 600, color: 'var(--ink)'}}>{s.title + '.'}</strong>{' ' + s.desc}
            </li>
          ))}
        </ol>

        </section>
        <section>
        {/* The rules were a boxed table with a wide number column, so on a
            phone each rule ran three words to a line. They are a list too. */}
        <h2 style={methodHeading}>Rules every number follows</h2>
        <ol className="method-rules" style={methodList}>
          {DATA_RULES.map((text) => <li key={text} style={methodItem}>{text}</li>)}
        </ol>
        </section>
        </div>

        <nav className="legacy-next" aria-label="Next">
          <span className="legacy-next-label">Next</span>
          <a className="legacy-action" href="#/pulse">Open Today</a>
          <a className="legacy-action" href="#/explore">Open Discover</a>
          <a className="legacy-action" href="#/source-lab">Inspect the sources</a>
        </nav>
      </div>
    </div>
  );
}
