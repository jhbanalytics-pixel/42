/* PULSE · Behaviour scan: market-wide signal read before a brief is written.
   Shows 10 behaviours per market with proof, approve/reject/note per row, then
   hands the approved set to the brief step. No synthesis happens here. */
import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {apiGetFresh} from './api.js';
import {REGION_NAME, human, readerWord, webHref} from './model.js';
import {Skeleton} from './ui/index.js';

const SCAN_MARKETS = ['za', 'ng', 'ke'];
const SCAN_OFF_CODE = 'behaviour_scan_off';
const SCAN_CACHE_TTL_MS = 5 * 60 * 1000;
let scanCache = null;
let scanCacheAt = 0;
let scanInflight = null;

function fetchScanData(signal, force){
  const now = Date.now();
  if (!force && scanCache && now - scanCacheAt < SCAN_CACHE_TTL_MS) {
    return Promise.resolve(scanCache);
  }
  if (!force && scanInflight) return scanInflight;
  scanInflight = apiGetFresh(
    '/api/research/behaviours?markets=' + encodeURIComponent(SCAN_MARKETS.join(',')),
    signal,
  ).then((res) => {
    scanCache = res;
    scanCacheAt = Date.now();
    scanInflight = null;
    return res;
  }).catch((e) => {
    scanInflight = null;
    throw e;
  });
  return scanInflight;
}

// One instrument grammar: every proof cell is a mono numeral over a mono
// uppercase unit, tabular, so posts / comments / engagement / platforms line
// up as a strip rather than a mixed-format meta run. Only real fields render;
// a metric the API did not send has no cell (DATA_SPEC: real data or hide).
function metricCells(metric, showEngine){
  if (!metric) return [];
  const cells = [];
  if (typeof metric.post_count === 'number') cells.push({v: String(metric.post_count), u: 'posts'});
  if (typeof metric.comment_count === 'number') cells.push({v: String(metric.comment_count), u: 'comments'});
  if (metric.engagement_total) cells.push({v: human(metric.engagement_total), u: 'engagement'});
  if (metric.platform_count) cells.push({v: String(metric.platform_count), u: metric.platform_count === 1 ? 'platform' : 'platforms'});
  if (metric.mention_count) cells.push({v: String(metric.mention_count), u: 'mentions'});
  if (showEngine && typeof metric.trend_score === 'number') cells.push({v: metric.trend_score.toFixed(2), u: 'score'});
  return cells;
}

const SCAN_VISIBLE_ROWS = 5;

/* Quiet register, 23 Sept 2026: a failed read on the brief flow says what
   happened and what to do in plain words, and keeps the server's own words
   and code one click down behind Details (rule 20). A raw server string such
   as "Missing fixture" or "No configured research persona is served." was
   printed in red on the surface; the error now reads in ink, and red stays
   with the current step and the one primary action (rule 9). The code shows
   only when the read carried one or a status, never a guessed one. A 501
   means the server does not offer the feature at all, so when the caller
   names that case it replaces the summary and no Retry is offered. */
export function PlainFailure({summary, unavailable = null, error = null, onRetry = null}){
  const notOffered = Boolean(unavailable) && Boolean(error) && error.status === 501;
  const detail = error && typeof error.message === 'string' ? error.message.trim() : '';
  const code = error && typeof error.code === 'string' && error.code ? error.code
    : error && Number.isInteger(error.status) ? 'http_' + error.status : '';
  return (
    <div className="research-error" role="alert">
      <p className="research-error-summary">{notOffered ? unavailable : summary}</p>
      {(detail || code) && (
        <details className="research-error-details">
          <summary>Details</summary>
          {detail && <p>Reason given: {detail}</p>}
          {code && <p>Code: <code>{code}</code></p>}
        </details>
      )}
      {onRetry && !notOffered && <button type="button" className="research-inline-action" onClick={onRetry}>Retry</button>}
    </div>
  );
}

/* An example's url is collected data; only a web address stays a link. */
export function behaviourExamples(row){
  return (row.examples || []).map((ex) => ({...ex, url: webHref(ex.url)}));
}

function BehaviourRow({row, state, onApprove, onReject, onNote, showEngine}){
  const status = state ? state.status : '';
  const [open, setOpen] = useState(false);
  const examples = behaviourExamples(row);
  const linkCount = examples.filter((e) => e.url).length;
  const cells = metricCells(row.metric, showEngine);
  return (
    <li className={'bscan-row' + (status ? ' bscan-row-' + status : '')}>
      <div className="bscan-row-main">
        {/* scan order 1: topic eyebrow */}
        <div className="bscan-row-head">
          <span className="bscan-row-topic">{row.signal_topic}</span>
        </div>
        {/* scan order 2: the behaviour name, serif, this is what you read */}
        <p className="bscan-row-behaviour">{row.behaviour}</p>
        {/* scan order 3: proof, one instrument grammar, mono numeral over mono unit */}
        {cells.length > 0 && (
          <div className="bscan-row-metrics">
            {cells.map((c, i) => (
              <div key={i} className="bscan-row-metric-cell">
                <span className="bscan-row-metric-v">{c.v}</span>
                <span className="bscan-row-metric-u">{c.u}</span>
              </div>
            ))}
          </div>
        )}
        {examples.length > 0 && (
          <>
            <button
              type="button"
              className="bscan-dig"
              aria-expanded={open}
              onClick={() => setOpen((v) => !v)}
            >
              {open ? 'Hide' : 'Show'} {examples.length} example{examples.length === 1 ? '' : 's'}
              {linkCount ? ' · ' + linkCount + ' with links' : ''}
            </button>
            {open && (
              <ul className="bscan-examples">
                {examples.map((ex, i) => (
                  <li key={i} className="bscan-example">
                    <span className="bscan-example-meta">
                      {[ex.voice_kind === 'comment' ? 'Comment' : '', readerWord(ex.platform) || 'Post', ex.handle].filter(Boolean).join(' · ')}
                    </span>
                    <span className="bscan-example-text">{ex.text}</span>
                    {ex.url && (
                      <a className="bscan-example-link" href={ex.url} target="_blank" rel="noopener noreferrer">
                        View original
                      </a>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
      <div className="bscan-row-actions">
        <div className="bscan-decide" role="group" aria-label={'Decision for ' + row.signal_topic}>
          <button
            type="button"
            className={'bscan-btn bscan-approve' + (status === 'approved' ? ' active' : '')}
            aria-pressed={status === 'approved'}
            onClick={() => onApprove(row)}
          >
            Approve
          </button>
          <button
            type="button"
            className={'bscan-btn bscan-reject' + (status === 'rejected' ? ' active' : '')}
            aria-pressed={status === 'rejected'}
            onClick={() => onReject(row)}
          >
            Reject
          </button>
        </div>
        <input
          type="text"
          className="bscan-note"
          placeholder="Add a note (optional)"
          value={state ? state.note : ''}
          onChange={(e) => onNote(row, e.target.value)}
          aria-label={'Note for ' + row.signal_topic}
        />
        {linkCount > 0 && <span className="bscan-row-proof-chip">{linkCount} link{linkCount === 1 ? '' : 's'}</span>}
      </div>
    </li>
  );
}

/* Shape-matched loader for the scan: the filter row (label + market chips)
   over two market blocks, each a head line plus a few row placeholders, so the
   loading state reads as the tabs-plus-row-list layout that lands once the
   /behaviours fetch resolves rather than a single line of text. */
function BehaviourScanSkeleton(){
  return (
    <div className="bscan-loading-sk" role="status" aria-label="Loading the ZA, NG and KE behaviour scan">
      <div className="bscan-filter" aria-hidden="true">
        <Skeleton width="52px" height="16px" />
        {SCAN_MARKETS.map((mk) => (
          <Skeleton key={mk} variant="row" width="58px" height="34px" />
        ))}
        <Skeleton variant="row" width="104px" height="34px" />
      </div>
      {[0, 1].map((block) => (
        <div key={block} className="bscan-market" aria-hidden="true">
          <div className="bscan-market-head">
            <Skeleton width="150px" height="17px" />
            <Skeleton width="86px" height="13px" />
          </div>
          <div className="bscan-list">
            <Skeleton variant="row" count={3} height="96px" />
          </div>
        </div>
      ))}
    </div>
  );
}

function MarketBehaviourList({mk, rows, decisions, setDecision, setNote, showEngine}){
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? rows : rows.slice(0, SCAN_VISIBLE_ROWS);
  const hiddenCount = rows.length - visible.length;
  return (
    <>
      <ul className="bscan-list">
        {visible.map((row) => (
          <BehaviourRow
            key={row.id}
            row={row}
            state={decisions[row.id]}
            onApprove={(r) => setDecision(r, 'approved')}
            onReject={(r) => setDecision(r, 'rejected')}
            onNote={setNote}
            showEngine={showEngine}
          />
        ))}
      </ul>
      {hiddenCount > 0 && (
        <button
          type="button"
          className="bscan-more"
          onClick={() => setExpanded(true)}
        >
          + {hiddenCount} more behaviour{hiddenCount === 1 ? '' : 's'} in {mk.toUpperCase()}
        </button>
      )}
    </>
  );
}

export function BehaviourScan({onContinue, onBuildConsolidated, onAuth, onPickTopics}){
  const [phase, setPhase] = useState('idle');
  const [err, setErr] = useState('');
  const [failure, setFailure] = useState(null);
  const [data, setData] = useState(null);
  const [decisions, setDecisions] = useState({});
  const [hidden, setHidden] = useState({});
  const [showEngine, setShowEngine] = useState(false);
  const ctrl = useRef(null);
  const onAuthRef = useRef(onAuth);
  onAuthRef.current = onAuth;

  const runScan = useCallback(async (force) => {
    if (ctrl.current) ctrl.current.abort();
    const c = new AbortController();
    ctrl.current = c;
    setPhase('running');
    setErr('');
    try {
      const res = await fetchScanData(c.signal, !!force);
      if (c.signal.aborted) return;
      ctrl.current = null;
      setData(res);
      setShowEngine(!(res && res.client_metrics_default));
      setPhase('ready');
    } catch (e){
      if (e && (e.name === 'AbortError' || e.aborted)) return;
      ctrl.current = null;
      if (e && e.auth){ onAuthRef.current && onAuthRef.current(); setPhase('idle'); return; }
      setErr(e && e.message ? e.message : 'Behaviour scan failed.');
      setFailure(e || null);
      /* A scan switched off on this server is a setting, not a fault: Retry
         cannot change it, so the reader is pointed at the manual route. */
      setPhase(e && e.code === SCAN_OFF_CODE ? 'off' : 'error');
    }
  }, []);

  useEffect(() => {
    runScan(false);
    return () => { if (ctrl.current) ctrl.current.abort(); };
  }, [runScan]);

  const setDecision = (row, status) => {
    setDecisions((prev) => {
      const cur = prev[row.id] || {note: ''};
      const nextStatus = cur.status === status ? '' : status;
      return {...prev, [row.id]: {...cur, status: nextStatus, row}};
    });
  };
  const setNote = (row, note) => {
    setDecisions((prev) => ({...prev, [row.id]: {...(prev[row.id] || {row}), note, row}}));
  };

  const approved = useMemo(
    () => Object.values(decisions).filter((d) => d.status === 'approved'),
    [decisions],
  );

  const behaviours = data && data.behaviours ? data.behaviours : {};

  return (
    <section className="bscan" aria-labelledby="bscan-title">
      <header className="bscan-head">
        <div className="research-field-label">Step 1</div>
        <h2 id="bscan-title" data-stage-heading tabIndex={-1}>See the behaviours first</h2>
        <p className="bscan-sub">
          Market-wide signal, broken into behaviours with proof. Approve the ones worth a brief, then choose the lens. Nothing is written until you continue.
        </p>
        {onPickTopics && (
          <button type="button" className="research-inline-action" onClick={onPickTopics}>Pick topics manually</button>
        )}
      </header>

      {phase === 'running' && <BehaviourScanSkeleton />}
      {phase === 'off' && <p className="bscan-sub" role="status">{err}</p>}
      {phase === 'error' && (
        <PlainFailure
          summary="The behaviours could not be loaded. Try again in a moment."
          error={failure && failure.message ? failure : {message: err}}
          onRetry={() => runScan(true)}
        />
      )}

      {phase === 'ready' && (
        <div className="bscan-filter" role="group" aria-label="Scan options">
          <span className="bscan-filter-label">Markets</span>
          {SCAN_MARKETS.map((mk) => {
            const on = !hidden[mk];
            return (
              <button
                key={mk}
                type="button"
                className={'bscan-filter-chip' + (on ? ' active' : '')}
                aria-pressed={on}
                onClick={() => setHidden((prev) => ({...prev, [mk]: !prev[mk]}))}
              >
                {mk.toUpperCase()}
              </button>
            );
          })}
          <button
            type="button"
            className={'bscan-filter-chip' + (showEngine ? ' active' : '')}
            aria-pressed={showEngine}
            onClick={() => setShowEngine((v) => !v)}
          >
            Engine detail
          </button>
        </div>
      )}

      {phase === 'ready' && SCAN_MARKETS.filter((mk) => !hidden[mk]).map((mk) => {
        const rows = behaviours[mk] || [];
        return (
          <div key={mk} className="bscan-market">
            <div className="bscan-market-head">
              {REGION_NAME[mk.toUpperCase()] || mk.toUpperCase()}
              <span className="bscan-market-count">{rows.length} behaviours</span>
            </div>
            {rows.length === 0 && <div className="bscan-empty">No behaviours for this market today.</div>}
            {rows.length > 0 && (
              <MarketBehaviourList
                mk={mk}
                rows={rows}
                decisions={decisions}
                setDecision={setDecision}
                setNote={setNote}
                showEngine={showEngine}
              />
            )}
          </div>
        );
      })}

      {phase === 'ready' && (
        <div className="bscan-footer">
          <span className="bscan-selected">{approved.length} approved</span>
          <div className="bscan-footer-actions">
            {onBuildConsolidated && (
              <button
                type="button"
                className="research-copy-btn"
                disabled={approved.length < 1}
                onClick={() => onBuildConsolidated && onBuildConsolidated(approved.map((d) => ({...d.row, note: d.note || ''})))}
              >
                {approved.length < 1 ? 'Approve behaviours first' : 'Build consolidated doc (' + approved.length + ')'}
              </button>
            )}
            <button
              type="button"
              className="research-generate-btn"
              disabled={approved.length < 1}
              onClick={() => onContinue && onContinue(approved.map((d) => ({...d.row, note: d.note || ''})))}
            >
              {approved.length < 1 ? 'Approve at least one behaviour' : 'Continue to brief (' + approved.length + ')'}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

export default BehaviourScan;
