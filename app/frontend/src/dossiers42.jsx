/* 42 Dossiers (contract.md section 13.2; EXPERIENCE.md, Dossiers). A draft
   assembles itself from a finished answer; a reviewer keeps, orders and
   notes its claims and ticks them, and Single source and Inferred claims
   need a tick before the version can freeze, as must every typed title or
   note be saved or put back. A frozen version exports as
   HTML and PDF and opens read-only from its share link, #/d/<id>/<version>.

   The server owns every claim. What the server sends is only ever shown;
   an edit sends back which claims to keep, their order, the title and the
   notes, and nothing else. */
import {useEffect, useRef, useState} from 'react';
import {MarketScopeNote, inMarket, pickedMarket} from './marketScope.jsx';
import './styles/ask42.css';
import './styles/dossiers42.css';
import {downloadDossierExport, fetchHistoryAsks, freezeDossier, getDossier, getDossierVersion, listDossiers, tickClaim, updateDossier} from './api42.js';
import {EvidenceChip, postDay} from './ui/EvidenceChip.jsx';
import {Facts} from './ui/Facts.jsx';
import {FigureLine} from './ui/FigureLine.jsx';
import {SourcePanel} from './ui/SourcePanel.jsx';
import {plainSearched} from './readerUnits.js';
import {dossierSummaryWords} from './answerMeta.js';

const MARKET_NAME = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya'};

const CONFIDENCE = {
  observed: {word: 'Observed', shape: '●'},
  corroborated: {word: 'Corroborated', shape: '■'},
  single_source: {word: 'Single source', shape: '▲'},
  inferred: {word: 'Inferred', shape: '○'},
};

const NEEDS_TICK = new Set(['single_source', 'inferred']);

const PAGE = 50;

const readable = (view) => Boolean(view && typeof view === 'object' && Array.isArray(view.claims));
const plural = (count, word) => count + ' ' + word + (count === 1 ? '' : 's');
const when = (value) => Date.parse(String(value || '')) || 0;

function useAuthHandover(onAuth){
  const ref = useRef(onAuth);
  ref.current = onAuth;
  return (error) => { if (error && error.auth && ref.current) ref.current(); };
}

function failureWords(error, fallback){
  if (error && error.auth) return 'Enter the passcode to read dossiers.';
  return (error && error.message) || fallback;
}

/* ---------------- the list, #/dossiers ---------------- */

export function DossiersPage({onAuth, region}){
  const picked = pickedMarket(region);
  const [state, setState] = useState({phase: 'loading', dossiers: [], next: null, error: null});
  const handover = useAuthHandover(onAuth);

  async function load(before){
    setState((current) => ({...current, phase: 'loading', error: null}));
    try {
      const page = await listDossiers({limit: PAGE, before});
      const found = Array.isArray(page && page.dossiers) ? page.dossiers : [];
      setState((current) => {
        const seen = new Set(found.map((item) => item.dossier_id));
        const all = [...(before ? current.dossiers.filter((item) => !seen.has(item.dossier_id)) : []), ...found];
        all.sort((a, b) => when(b.created_at) - when(a.created_at));
        return {phase: 'ready', dossiers: all, next: (page && page.next_before) || null, error: null};
      });
    } catch (error){
      setState((current) => ({...current, phase: 'error', error}));
      handover(error);
    }
  }

  useEffect(() => { load(null); }, []);

  const {phase, dossiers: allDossiers, next, error} = state;
  const dossiers = allDossiers.filter((item) => inMarket(item.market, picked));
  return (
    <main className="dossiers42" aria-labelledby="dossiers42-title">
      <h1 className="dossiers42-title" id="dossiers42-title">Dossiers</h1>
      <p className="dossiers42-lede">Which answers have you checked and can share? Start a dossier from any finished answer on Ask, check its claims, freeze it, then export it.</p>
      {phase === 'error' && <p className="dossiers42-error" role="alert">{failureWords(error, 'The dossiers could not be read.')}</p>}
      {phase === 'error' && !(error && error.auth) && <button type="button" className="t42-button" onClick={() => load(null)}>Try again</button>}
      <MarketScopeNote picked={picked} hidden={allDossiers.length - dossiers.length} what={allDossiers.length - dossiers.length === 1 ? 'dossier is' : 'dossiers are'} />
      {phase === 'loading' && allDossiers.length === 0 && <p className="dossiers42-muted">Reading the dossiers</p>}
      {phase === 'ready' && allDossiers.length === 0 && (
        <section className="dossiers42-empty" aria-labelledby="dossiers42-empty-title">
          {/* Layout pass two, 4 October 2026: what is missing and the one
              action share a full-width line, the four steps run across under
              it, and the answers that could start a dossier follow, so the
              empty page shows real work instead of a blank lower half. */}
          <div className="dossiers42-empty-start">
            <div className="dossiers42-empty-copy">
              <h2 className="dossiers42-empty-title" id="dossiers42-empty-title">No dossiers yet</h2>
              <p>Ask a question, then choose Add to dossier on the answer.</p>
            </div>
            <a className="dossiers42-empty-link" href="#/ask">Go to Ask</a>
          </div>
          <ol className="dossiers42-steps" aria-label="From an answer to a dossier">
            <li><span className="dossiers42-step-n" aria-hidden="true">1</span><strong>Start from an answer</strong><span>Any finished answer on Ask or a finished investigation.</span></li>
            <li><span className="dossiers42-step-n" aria-hidden="true">2</span><strong>Check each claim</strong><span>Tick what holds, cut what does not, and read the posts behind it.</span></li>
            <li><span className="dossiers42-step-n" aria-hidden="true">3</span><strong>Freeze a version</strong><span>A frozen version stops changing, so everyone reads the same thing.</span></li>
            <li><span className="dossiers42-step-n" aria-hidden="true">4</span><strong>Export and share</strong><span>Export it as a PDF or send its read-only link to the team.</span></li>
          </ol>
          <ReadyAnswers picked={picked} />
        </section>
      )}
      {dossiers.length > 0 && (
        <ol className="dossiers42-list" aria-label="Dossiers, newest first">
          {dossiers.map((item) => {
            const isFrozen = item.state === 'frozen';
            const meta = [
              MARKET_NAME[item.market],
              postDay(item.created_at),
              typeof item.kept === 'number' ? plural(item.kept, 'claim') + ' kept' : '',
              !isFrozen && item.frozen_version ? 'version ' + item.frozen_version + ' is frozen' : '',
            ].filter(Boolean);
            return (
              <li className="dossiers42-row" key={item.dossier_id} data-dossier={item.dossier_id}>
                <span className="dossiers42-state" data-state={isFrozen ? 'frozen' : 'draft'}>{isFrozen ? 'Frozen' : 'Draft'}</span>
                <a className="dossiers42-row-title" href={'#/dossiers/' + encodeURIComponent(item.dossier_id)}>{item.title || 'Untitled dossier'}</a>
                {meta.length > 0 && <span className="dossiers42-muted"><Facts parts={meta} /></span>}
              </li>
            );
          })}
        </ol>
      )}
      {next && phase !== 'loading' && (
        <button type="button" className="dossiers42-quiet" onClick={() => load(next)}>Show older</button>
      )}
    </main>
  );
}

/* Finished answers from History that could start a dossier, newest first.
   A side read: when it fails or needs the passcode the block stays quiet,
   since the list above already owns the passcode hand-over, and never says
   there are no answers when they could not be read. */
const READY_LIMIT = 6;
function ReadyAnswers({picked = ''}){
  const [asks, setAsks] = useState(null);
  useEffect(() => {
    const ctrl = new AbortController();
    setAsks(null);
    fetchHistoryAsks({limit: 20, market: picked || undefined}, {signal: ctrl.signal})
      .then((page) => {
        if (ctrl.signal.aborted) return;
        const rows = (Array.isArray(page && page.asks) ? page.asks : [])
          .filter((ask) => ask.ask_id && (ask.status === 'complete' || ask.status === 'ok')
            && (ask.answer_status === 'complete' || ask.answer_status === 'partial'));
        setAsks(rows.slice(0, READY_LIMIT));
      })
      .catch(() => { if (!ctrl.signal.aborted) setAsks(null); });
    return () => ctrl.abort();
  }, [picked]);
  if (asks === null) return null;
  return (
    <section className="dossiers42-ready" aria-labelledby="dossiers42-ready-title">
      <div className="dossiers42-ready-head">
        <h3 className="dossiers42-ready-title" id="dossiers42-ready-title">Answers ready for a dossier</h3>
        <a className="dossiers42-ready-all" href="#/history">All past asks</a>
      </div>
      {asks.length === 0
        ? <p className="dossiers42-muted">No finished answers yet. Each answer you get on Ask lands here, ready to start a dossier.</p>
        : <ol className="dossiers42-ready-list">
            {asks.map((ask) => (
              <li key={ask.ask_id} className="dossiers42-ready-row" data-ready-ask={ask.ask_id}>
                <a className="dossiers42-ready-q" href={'#/ask?follow=' + encodeURIComponent(ask.ask_id)}>{ask.question || 'A question with no text'}</a>
                <span className="dossiers42-muted"><Facts parts={[MARKET_NAME[ask.market], ask.at ? postDay(ask.at) : '', ask.answer_status === 'partial' ? 'Partial answer' : 'Full answer']} /></span>
              </li>
            ))}
          </ol>}
    </section>
  );
}

/* ---------------- one claim ---------------- */

function ClaimBody({claim, records, pinnedId, onPin}){
  const confidence = CONFIDENCE[claim.label];
  const cited = (claim.evidence_ids || []).map((id) => records.get(id)).filter(Boolean);
  const quotes = Array.isArray(claim.quotes) ? claim.quotes : [];
  const numbers = Array.isArray(claim.numbers) ? claim.numbers : [];
  return (
    <>
      <span className="dossiers42-claim-head">
        {confidence && (
          <span className="ask42-confidence" data-label={claim.label}>
            <span className="ask42-confidence-shape" aria-hidden="true">{confidence.shape}</span>
            {confidence.word}
          </span>
        )}
        <span className="dossiers42-claim-text">{claim.text}</span>
      </span>
      {cited.length > 0 && (
        <span className="ask42-chips">
          {cited.map((record) => (
            <EvidenceChip
              key={record.id}
              evidence={record}
              quotes={quotes.filter((quote) => quote.evidence_id === record.id).map((quote) => quote.text)}
              pinned={pinnedId === record.id}
              onPin={onPin}
            />
          ))}
        </span>
      )}
      {quotes.map((quote, index) => {
        const source = records.get(quote.evidence_id);
        return (
          <blockquote className="dossiers42-quote" key={index}>
            {'“' + quote.text + '”'}
            {source && source.handle && <span className="dossiers42-muted">{' · ' + source.handle}</span>}
          </blockquote>
        );
      })}
      {numbers.map((number, index) => <FigureLine key={index} number={number} />)}
    </>
  );
}

/* ---------------- the body both views share ---------------- */

function metaLine(view){
  const parts = [];
  if (MARKET_NAME[view.market]) parts.push(MARKET_NAME[view.market]);
  parts.push(view.state === 'frozen' ? 'Frozen version ' + view.version : 'Draft version ' + view.version);
  const day = postDay(view.created_at);
  if (day) parts.push(day);
  return parts.join(' · ');
}

function Gaps({gaps}){
  if (!Array.isArray(gaps) || gaps.length === 0) return null;
  return (
    <section className="dossiers42-section" aria-labelledby="dossiers42-gaps-title">
      <h2 className="dossiers42-section-title" id="dossiers42-gaps-title">What we do not know</h2>
      <ul className="dossiers42-plain-list">
        {gaps.map((gap, index) => (
          <li key={index}>{gap.what}{gap.searched && <span className="dossiers42-muted">{' · Searched ' + plainSearched(gap.searched)}</span>}</li>
        ))}
      </ul>
    </section>
  );
}

function LeftOut({items}){
  if (!Array.isArray(items) || items.length === 0) return null;
  return (
    <p className="dossiers42-muted">
      {plural(items.length, 'claim') + ' from the answer did not pass its checks and cannot be added: '
        + [...new Set(items.map((item) => item.reason))].join('; ') + '.'}
    </p>
  );
}

function Exports({view, onFailure}){
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  async function download(format){
    setBusy(format);
    setError('');
    try { await downloadDossierExport(view.dossier_id, view.version, format); }
    catch (failure){
      setError(failureWords(failure, 'The export failed.'));
      onFailure(failure);
    }
    finally { setBusy(''); }
  }
  const share = '#/d/' + encodeURIComponent(view.dossier_id) + '/' + view.version;
  return (
    <div className="dossiers42-actions">
      <button type="button" className="dossiers42-primary" onClick={() => download('pdf')} disabled={Boolean(busy)} aria-busy={busy === 'pdf' ? 'true' : 'false'}>Export PDF</button>
      <button type="button" className="dossiers42-quiet" onClick={() => download('html')} disabled={Boolean(busy)} aria-busy={busy === 'html' ? 'true' : 'false'}>Export HTML</button>
      <a className="dossiers42-share" href={share}>Share link to this version</a>
      {error && <p className="dossiers42-error" role="alert">{error}</p>}
    </div>
  );
}

/* A frozen version, read-only: its kept claims with the reviews that stood
   when it froze. Edit again is offered only on the dossier's own page, where
   the frozen version is the latest; it asks the server for a new draft
   version and never changes this one. */
function FrozenBody({view, onFailure, editAgain}){
  const [pinnedId, setPinnedId] = useState(null);
  const records = new Map((view.evidence || []).map((record) => [record.id, record]));
  const reviews = view.reviews || {};
  const kept = view.claims.filter((claim) => claim.kept);
  const pinned = pinnedId ? records.get(pinnedId) : null;
  return (
    <div className="ask42-answer-layout">
      <article className="dossiers42-body">
        <p className="dossiers42-meta">{metaLine(view)}</p>
        <p className="dossiers42-summary">{String(view.summary || '').trim() ? view.summary : dossierSummaryWords(view)}</p>
        <ol className="dossiers42-claims" aria-label="Claims">
          {kept.map((claim) => {
            const review = reviews[claim.claim_id];
            return (
              <li className="dossiers42-claim" key={claim.claim_id} data-claim={claim.claim_id}>
                <ClaimBody claim={claim} records={records} pinnedId={pinnedId} onPin={setPinnedId} />
                {review && review.ticked && <span className="dossiers42-muted">{'Reviewed' + (review.note ? ': ' + review.note : '')}</span>}
                {claim.note && <span className="dossiers42-note">{"Reviewer's note: " + claim.note}</span>}
              </li>
            );
          })}
        </ol>
        <Gaps gaps={view.gaps} />
        <Exports view={view} onFailure={onFailure} />
        {editAgain && (
          <div className="dossiers42-actions">
            <button type="button" className="dossiers42-quiet" disabled={editAgain.busy} aria-busy={editAgain.busy ? 'true' : 'false'} onClick={editAgain.start}>Edit again</button>
            <span className="dossiers42-muted">Starts a new draft version from this one. This version stays as it is.</span>
          </div>
        )}
        {editAgain && editAgain.problem && (
          <div className="dossiers42-error" role="alert">
            <p>{editAgain.problem.message}</p>
            {editAgain.problem.stale && <button type="button" className="dossiers42-quiet" disabled={editAgain.busy} onClick={editAgain.reload}>Reload</button>}
          </div>
        )}
      </article>
      <SourcePanel evidence={pinned} quotes={pinned ? (kept.flatMap((claim) => claim.quotes || []).filter((quote) => quote.evidence_id === pinned.id).map((quote) => quote.text)) : []} onClose={() => setPinnedId(null)} />
    </div>
  );
}

/* ---------------- the review, #/dossiers/<id> ---------------- */

export function DossierPage({dossierId, onAuth}){
  const [phase, setPhase] = useState('loading');
  const [view, setView] = useState(null);
  const [ticks, setTicks] = useState({});
  const [loadError, setLoadError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState(null);
  const [title, setTitle] = useState('');
  const [noteDrafts, setNoteDrafts] = useState({});
  const [pinnedId, setPinnedId] = useState(null);
  const handover = useAuthHandover(onAuth);

  /* A read replaces every draft. An edit's answer (keep: true) replaces
     only drafts the new version now holds, so a note or title not yet
     saved, or typed while the save was on its way, stays as typed. */
  function show(next, keep = false){
    if (!readable(next)) throw new Error('This dossier could not be read.');
    const before = view ? view.title || '' : '';
    setView(next);
    setTicks(next.ticks && typeof next.ticks === 'object' ? next.ticks : {});
    if (!keep){
      setTitle(next.title || '');
      setNoteDrafts({});
      return;
    }
    setTitle((current) => {
      const typed = current.trim();
      return typed && typed !== before && typed !== (next.title || '') ? current : next.title || '';
    });
    const notes = Object.fromEntries(next.claims.map((claim) => [claim.claim_id, claim.note || null]));
    setNoteDrafts((current) => Object.fromEntries(Object.entries(current)
      .filter(([cid, value]) => (value.trim() || null) !== (notes[cid] || null))));
  }

  useEffect(() => {
    let live = true;
    getDossier(dossierId)
      .then((next) => { if (live){ show(next); setPhase('ready'); } })
      .catch((error) => {
        if (!live) return;
        setLoadError(error);
        setPhase('error');
        handover(error);
      });
    return () => { live = false; };
  }, [dossierId]);

  /* A write made from a version another tab has since moved past is refused
     as stale_version; the server's words show with Reload, which opens the
     latest version. */
  async function write(action, refusal){
    setBusy(true);
    setProblem(null);
    try { await action(); }
    catch (error){
      handover(error);
      if (error && error.code === 'stale_version') setProblem({message: error.message, stale: true});
      else setProblem(refusal ? refusal(error) : {message: failureWords(error, 'The change was not saved.')});
    }
    finally { setBusy(false); }
  }

  function reload(){
    return write(async () => show(await getDossier(dossierId)), (error) => ({message: failureWords(error, 'This dossier could not be read.'), stale: true}));
  }

  const claims = readable(view) ? view.claims : [];
  const kept = claims.filter((claim) => claim.kept).map((claim) => claim.claim_id);
  const textOf = (claimId) => {
    const claim = claims.find((item) => item.claim_id === claimId);
    return claim && claim.text ? claim.text : 'A claim this dossier no longer holds';
  };

  /* The only body an edit ever sends. */
  function edit({keep = kept, title: nextTitle = view.title, notes = {}}){
    const current = Object.fromEntries(claims.map((claim) => [claim.claim_id, claim.note || null]));
    return write(async () => show(await updateDossier(dossierId, {keep, order: keep, title: nextTitle, notes: {...current, ...notes}, fromVersion: view.version}), true));
  }

  function move(claimId, step){
    const at = kept.indexOf(claimId);
    const to = at + step;
    if (at === -1 || to < 0 || to >= kept.length) return;
    const keep = [...kept];
    [keep[at], keep[to]] = [keep[to], keep[at]];
    edit({keep});
  }

  function tick(claimId, ticked){
    return write(async () => {
      const row = await tickClaim(dossierId, claimId, ticked);
      setTicks((current) => ({...current, [claimId]: {ticked: typeof row.ticked === 'boolean' ? row.ticked : ticked, note: row.note || null, at: row.at || null}}));
    });
  }

  function freeze(){
    return write(async () => show(await freezeDossier(dossierId, view.version)), (error) => (
      Array.isArray(error.claims) && error.claims.length
        ? {message: 'This version cannot freeze yet. These claims need attention first:', claims: error.claims.map((item) => ({words: textOf(item.claim_id), reason: item.reason}))}
        : {message: failureWords(error, 'The version did not freeze.')}
    ));
  }

  const heading = readable(view) ? view.title || 'Untitled dossier' : 'Dossier';
  if (phase !== 'ready' || !readable(view)){
    return (
      <main className="dossiers42" aria-labelledby="dossiers42-title">
        <h1 className="dossiers42-title" id="dossiers42-title">{heading}</h1>
        {phase === 'loading'
          ? <p className="dossiers42-muted">Reading the dossier</p>
          : <p className="dossiers42-error" role="alert">{failureWords(loadError, 'This dossier could not be read.')}</p>}
        <a className="dossiers42-share" href="#/dossiers">All dossiers</a>
      </main>
    );
  }

  if (view.state === 'frozen'){
    return (
      <main className="dossiers42" aria-labelledby="dossiers42-title">
        <h1 className="dossiers42-title" id="dossiers42-title">{heading}</h1>
        <FrozenBody view={view} onFailure={handover} editAgain={{busy, problem, start: () => edit({}), reload}} />
      </main>
    );
  }

  const records = new Map((view.evidence || []).map((record) => [record.id, record]));
  const removed = claims.filter((claim) => !claim.kept);
  const needs = (claim) => claim.kept && NEEDS_TICK.has(claim.label) && !(ticks[claim.claim_id] && ticks[claim.claim_id].ticked);
  const waiting = claims.filter(needs).length;
  const pinned = pinnedId ? records.get(pinnedId) : null;
  const titleChanged = title.trim() && title.trim() !== view.title;
  /* Freeze keeps the version as the server holds it, so it waits until
     every title or note typed here is saved or put back. */
  const unsaved = title.trim() !== (view.title || '')
    || claims.some((claim) => claim.kept && Object.hasOwn(noteDrafts, claim.claim_id) && noteDrafts[claim.claim_id].trim() !== (claim.note || ''));

  return (
    <main className="dossiers42" aria-labelledby="dossiers42-title">
      <h1 className="dossiers42-title" id="dossiers42-title">{heading}</h1>
      <div className="ask42-answer-layout">
        <article className="dossiers42-body">
          <p className="dossiers42-meta">{metaLine(view)}</p>
          <form className="dossiers42-title-form" onSubmit={(event) => { event.preventDefault(); if (titleChanged) edit({title: title.trim()}); }}>
            <label className="dossiers42-label" htmlFor="dossiers42-title-input">Title</label>
            <input id="dossiers42-title-input" name="title" className="dossiers42-input" maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} />
            <button type="submit" className="dossiers42-quiet" disabled={busy || !titleChanged}>Save title</button>
          </form>
          <p className="dossiers42-summary">{String(view.summary || '').trim() ? view.summary : dossierSummaryWords(view)}</p>

          <ol className="dossiers42-claims" aria-label="Claims">
            {claims.filter((claim) => claim.kept).map((claim, index, list) => {
              const cid = claim.claim_id;
              const ticked = Boolean(ticks[cid] && ticks[cid].ticked);
              const noteValue = Object.hasOwn(noteDrafts, cid) ? noteDrafts[cid] : (claim.note || '');
              const noteId = 'dossiers42-note-' + cid;
              return (
                <li className="dossiers42-claim" key={cid} data-claim={cid}>
                  <ClaimBody claim={claim} records={records} pinnedId={pinnedId} onPin={setPinnedId} />
                  <div className="dossiers42-review">
                    <label className="dossiers42-tick">
                      <input type="checkbox" checked={ticked} disabled={busy} onChange={(event) => tick(cid, event.target.checked)} />
                      Reviewed
                    </label>
                    {needs(claim) && <span className="dossiers42-needs">needs a tick</span>}
                    <button type="button" className="dossiers42-quiet" disabled={busy || index === 0} onClick={() => move(cid, -1)}>Move up</button>
                    <button type="button" className="dossiers42-quiet" disabled={busy || index === list.length - 1} onClick={() => move(cid, 1)}>Move down</button>
                    <button type="button" className="dossiers42-quiet" disabled={busy} onClick={() => edit({keep: kept.filter((id) => id !== cid)})}>Remove</button>
                  </div>
                  <label className="dossiers42-label" htmlFor={noteId}>Reviewer's note</label>
                  <textarea id={noteId} className="dossiers42-input" rows={2} maxLength={1000} value={noteValue} onChange={(event) => setNoteDrafts((current) => ({...current, [cid]: event.target.value}))} />
                  <button
                    type="button"
                    className="dossiers42-quiet dossiers42-save-note"
                    disabled={busy || noteValue.trim() === (claim.note || '')}
                    onClick={() => edit({notes: {[cid]: noteValue.trim() || null}})}
                  >Save note</button>
                </li>
              );
            })}
          </ol>

          {removed.length > 0 && (
            <section className="dossiers42-section" aria-labelledby="dossiers42-removed-title">
              <h2 className="dossiers42-section-title" id="dossiers42-removed-title">Removed from this dossier</h2>
              <ul className="dossiers42-claims dossiers42-removed">
                {removed.map((claim) => (
                  <li className="dossiers42-claim" key={claim.claim_id} data-claim={claim.claim_id}>
                    <ClaimBody claim={claim} records={records} pinnedId={pinnedId} onPin={setPinnedId} />
                    <div className="dossiers42-review">
                      <button type="button" className="dossiers42-quiet" disabled={busy} onClick={() => edit({keep: [...kept, claim.claim_id]})}>Keep</button>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <LeftOut items={view.left_out} />
          <Gaps gaps={view.gaps} />

          <div className="dossiers42-actions">
            <button type="button" className="dossiers42-primary" disabled={busy || unsaved} aria-busy={busy ? 'true' : 'false'} onClick={freeze}>Freeze</button>
            <span className="dossiers42-muted">
              {unsaved ? 'Save your edits first.' : waiting > 0
                ? plural(waiting, 'claim') + ' still ' + (waiting === 1 ? 'needs' : 'need') + ' a tick before this version can freeze.'
                : 'Freezing keeps this version as it is and enables HTML and PDF export.'}
            </span>
          </div>
          {problem && (
            <div className="dossiers42-error" role="alert">
              <p>{problem.message}</p>
              {problem.claims && (
                <ul className="dossiers42-plain-list">
                  {problem.claims.map((item, index) => (
                    <li key={index}><span className="dossiers42-claim-text">{item.words}</span>{' · ' + item.reason}</li>
                  ))}
                </ul>
              )}
              {problem.stale && <button type="button" className="dossiers42-quiet" disabled={busy} onClick={reload}>Reload</button>}
            </div>
          )}
        </article>
        <SourcePanel
          evidence={pinned}
          quotes={pinned ? claims.flatMap((claim) => claim.quotes || []).filter((quote) => quote.evidence_id === pinned.id).map((quote) => quote.text) : []}
          onClose={() => setPinnedId(null)}
        />
      </div>
    </main>
  );
}

/* ---------------- the share link, #/d/<id>/<version> ---------------- */

export function SharedDossier({dossierId, version, onAuth}){
  const [state, setState] = useState({phase: 'loading', view: null, error: null});
  const handover = useAuthHandover(onAuth);
  const valid = /^[1-9]\d*$/.test(String(version || ''));

  useEffect(() => {
    if (!valid) return undefined;
    let live = true;
    getDossierVersion(dossierId, Number(version))
      .then((view) => { if (live) setState({phase: 'ready', view, error: null}); })
      .catch((error) => {
        if (!live) return;
        setState({phase: 'error', view: null, error});
        handover(error);
      });
    return () => { live = false; };
  }, [dossierId, version]);

  const {phase, view, error} = state;
  const frozen = phase === 'ready' && readable(view) && view.state === 'frozen';
  return (
    <main className="dossiers42" aria-labelledby="dossiers42-title">
      <h1 className="dossiers42-title" id="dossiers42-title">{frozen ? view.title || 'Untitled dossier' : 'Dossier'}</h1>
      {!valid && <p className="dossiers42-error" role="alert">This link does not name a dossier version.</p>}
      {valid && phase === 'loading' && <p className="dossiers42-muted">Reading the dossier</p>}
      {valid && phase === 'error' && <p className="dossiers42-error" role="alert">{failureWords(error, 'This dossier could not be read.')}</p>}
      {valid && phase === 'ready' && !frozen && (
        <p className="dossiers42-muted">This version is not frozen yet. A share link opens frozen versions only; drafts are reached from the dossier list.</p>
      )}
      {frozen && <FrozenBody view={view} onFailure={handover} />}
      {!frozen && <a className="dossiers42-share" href="#/dossiers">All dossiers</a>}
    </main>
  );
}
