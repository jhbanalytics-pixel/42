/* PULSE · live "Ask": generate a Prompt Pulse brief through the engine API */
import {useRef} from 'react';
import {apiGetFresh, apiPost} from './api.js';
import {decorate, REGION_NAME} from './model.js';
import {Icon, SignalLoader, useDialog} from './parts.jsx';

/* Generation is asynchronous server-side: a cold ask answers 202 {pending}
   immediately and the brief lands in the cache, so every individual request
   stays well under a corporate proxy's timeout. The client polls the status
   endpoint until the brief is ready. */
const POLL_INTERVAL_MS = 2500;
const POLL_DEADLINE_MS = 90000;

/* abort-aware sleep: an aborted signal settles the wait immediately and the
   loop sees it on the next check, so a closed panel stops polling at once. */
const sleep = (ms, signal) => new Promise((resolve) => {
  if (signal && signal.aborted){ resolve(); return; }
  const id = setTimeout(resolve, ms);
  if (signal) signal.addEventListener('abort', () => { clearTimeout(id); resolve(); }, {once: true});
});

const abortError = () => { const e = new Error('aborted'); e.aborted = true; return e; };

async function pollBriefStatus(query, region, signal){
  const base = '/api/ask/brief/status?query=' + encodeURIComponent(query) + '&region=' + encodeURIComponent(region);
  const deadline = Date.now() + POLL_DEADLINE_MS;
  while (Date.now() < deadline){
    if (signal && signal.aborted) throw abortError();
    await sleep(POLL_INTERVAL_MS, signal);
    if (signal && signal.aborted) throw abortError();
    /* Corporate proxies can swallow rapid identical GETs as bot traffic, so
       every poll gets a unique cache-buster, and a single failed poll is a
       skipped beat, never a fatal error; only the deadline or a recorded
       generation failure ends the wait. */
    let j = null;
    try { j = await apiGetFresh(base + '&_=' + Date.now(), signal); }
    catch (e){ if (e && e.auth) throw e; if (signal && signal.aborted) throw abortError(); continue; }
    if (j && j.error){ throw new Error('The engine could not generate this brief. Ask again.'); }
    if (j && !j.pending) return j;
  }
  throw new Error('The brief is taking too long. Ask again in a moment.');
}

/* POST /api/ask/brief {query, region} -> a full topic object (generated: true)
   on a cache hit, or {pending: true} on a cold ask, resolved by polling.
   Region forcing happens server-side; the client just sends the active desk.
   A thin result ({thin: true}) means the engine has too little live signal to
   brief honestly; it surfaces as the error panel, not a fabricated brief. */
export async function generateBrief(query, region, signal){
  const reg = String(region).toLowerCase();
  let j = await apiPost('/api/ask/brief', {query: query, region: reg}, signal);
  if (j && j.pending){ j = await pollBriefStatus(query, reg, signal); }
  if (j && j.thin){ const e = new Error('thin'); e.thin = true; throw e; }
  const topic = decorate(j);
  if (!topic || !topic.topic){ throw new Error('The engine returned an unreadable brief.'); }
  topic.generated = true;
  return topic;
}

/* POST /api/ask/refine {topic, instruction} -> the same topic with its prose
   reworked. The server preserves every number, the series, the receipts and
   the real quotes; only prose moves. Never cached. */
export async function refineBrief(topic, instruction){
  const j = await apiPost('/api/ask/refine', {topic: topic, instruction: instruction});
  const next = decorate(j);
  if (!next || !next.topic){ throw new Error('The engine returned an unreadable brief.'); }
  next.generated = true;
  return next;
}

/* loading / error panel shown while the brief generates */
export function AskLoadingPanel({query, error, region, onClose, onRetry}){
  const panelRef = useRef(null);
  const scanLabel = ['ZA', 'NG', 'KE'].includes(region) ? REGION_NAME[region] + ' · ' + region + ' desk' : 'ZA · NG · KE';
  const open = !!(query || error);
  const thin = error === 'thin';
  const capacity = error && error.code === 'capacity_busy';
  useDialog(panelRef, open);
  return (
    <>
      <div className={'scrim' + (open ? ' show settled' : '')} onClick={onClose} />
      <aside ref={panelRef} className={'panel' + (open ? ' show settled' : '')} role="dialog" aria-modal="true" aria-labelledby="ask-panel-title" aria-hidden={open ? undefined : 'true'}>
        {open && (
          <>
            <div className="panel-head">
              <div className="top">
                <div className="panel-region">{thin ? 'Not enough live signal' : capacity ? 'Desk at capacity' : error ? 'Could not reach the engine' : 'Generating brief'}</div>
                <button className="panel-close" aria-label="Close" onClick={onClose}><Icon.close /></button>
              </div>
              <h2 className="panel-title" id="ask-panel-title" style={{wordBreak: 'break-word'}}>{query || 'Try again'}</h2>
              <div className="panel-label">{thin ? 'The engine will not invent a brief it cannot back' : error ? 'The live engine is unavailable right now' : 'Reading the live signal · ' + scanLabel}</div>
            </div>
            <div className="panel-body" style={{display: 'flex', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: '26px'}}>
              {error ? (
                <div style={{textAlign: 'center', maxWidth: '320px'}}>
                  <p style={{fontFamily: 'var(--serif)', fontSize: '19px', color: 'var(--muted)', lineHeight: 1.5}}>
                    {thin
                      ? 'Too little observed signal on that topic to brief honestly. Try a broader phrasing, or browse the tracked topics.'
                      : capacity ? 'The desk is at capacity. Try again shortly.' : 'Couldn’t generate a live brief. Check the connection and ask again, or browse the tracked topics.'}
                  </p>
                  {capacity
                    ? <button className="legacy-action legacy-action--primary" style={{marginTop: '20px', display: 'inline-flex'}} onClick={onRetry}>Retry</button>
                    : <button className="legacy-action legacy-action--primary" style={{marginTop: '20px', display: 'inline-flex'}} onClick={onClose}>Back to Today</button>}
                </div>
              ) : (
                <>
                  <SignalLoader />
                  <div className="eyebrow" style={{animation: 'pulseFade 1.6s ease-in-out infinite'}}>Scoring momentum · classifying · briefing</div>
                </>
              )}
            </div>
          </>
        )}
      </aside>
    </>
  );
}
