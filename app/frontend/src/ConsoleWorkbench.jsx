/* PULSE · Console Workbench: one desk for Ask, cited briefs, and recent work. */
import {useEffect, useMemo, useRef, useState} from 'react';
import {IntelligenceConsole} from 'ogilvy-intelligence-design-system';
/* Route-surface rules travel with the chunk; see explore.jsx. */
import './styles/instrument-route-surfaces.css';
import {apiGetFresh, apiPost, useApi} from './api.js';
import {COVERAGE_PATH, askCoverage, coverageDate, coverageLine} from './askPresentation.js';
import {ChatPage, loadThreads, threadTitle, timeAgo} from './chat.jsx';
import {ResearchPage} from './ResearchDocPanel.jsx';
import {EvidenceRoom} from './evidenceRoom.jsx';
import {currentReviewSession, endReviewSession, reviewSignInConfigured, subscribeReviewSession, useDossierResource} from './dossierResource.js';
import {ClientReadExport, DossierReview} from './ui/DossierReview.jsx';
import {buildDossierConsoleModel} from './ui/IntelligenceDossier.jsx';
import {InvestigationIndex} from './ui/InvestigationIndex.jsx';
import {InvestigationFraming} from './ui/InvestigationFraming.jsx';
import {buildFramingState, createInvestigationFrom} from './investigationIndex.js';
import {
  WORKBENCH_NAV_EVENT,
  buildWorkbenchHash,
  navigateWorkbench,
  parseWorkbenchRoute,
} from './researchLib.jsx';
import {go} from './router.js';
import {askCountText} from './plainLabels.js';
import {FlowBar, MarketChip, PageHero} from './ui/index.js';
import './styles/console.css';

function prettifyPersona(raw){
  const s = String(raw || '').trim();
  if (!s) return '';
  if (!/[_-]/.test(s)) return s;
  return s
    .split(/[_-]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

function normaliseRecent(row){
  const doc = row && row.doc ? row.doc : {};
  const json = doc.json || {};
  const markets = row.markets || json.markets || [];
  const marketList = Array.isArray(markets) ? markets : [];
  const persona = prettifyPersona(row.persona_label || row.persona_id || json.persona_label || '');
  const created = row.created_at || row.ts || row.updated_at || '';

  return {
    id: row.artifact_id || row.id || '',
    persona,
    markets: marketList,
    quality: row.signal_quality || json.signal_quality || '',
    created,
  };
}

/* Quiet register, 23 Sept 2026: the day reads as a reader says it, "6 Sept",
   with no leading zero and whatever the machine's locale (rule 15). */
function shortDate(raw){
  if (!raw) return '';
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return String(raw).slice(0, 16);
  return d.getDate() + ' ' + d.toLocaleDateString('en-ZA', {month: 'short'});
}

// 24h HH:mm, hand-padded rather than left to toLocaleTimeString so the format
// never drifts to a 12h locale (AM/PM) on a machine with a different locale.
function shortTime(raw){
  if (!raw) return '';
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return '';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return hh + ':' + mm;
}

function marketsFromRegion(region){
  const code = String(region || 'ZA').toUpperCase();
  if (code === 'ALL') return ['za', 'ng', 'ke'];
  return [String(region || 'za').toLowerCase()];
}

function WorkbenchStart({onAsk, onBrief}){
  return (
    <section className="workbench-start" aria-label="What do you need from the signal?">
      <PageHero
        eyebrow="Intelligence console"
        title="What do you need from the signal?"
        sub="Ask a cultural question, or frame an investigation with its evidence visible."
      />
      <div className="workbench-actions">
        <button type="button" className="workbench-action-card" onClick={onAsk}>
          <span className="workbench-action-k">Explore</span>
          <span className="workbench-action-title">Ask the desk</span>
          <span className="workbench-action-copy">Explore a question and inspect the available evidence.</span>
        </button>
        <button type="button" className="workbench-action-card primary" onClick={onBrief}>
          <span className="workbench-action-k">Deliver</span>
          <span className="workbench-action-title">Build cited brief</span>
          <span className="workbench-action-copy">Shape a brief, inspect its sources and prepare it for review.</span>
        </button>
      </div>
    </section>
  );
}

/* Recents identity, round 2: same persona repeats and rows can collide on the
   minute, so identity leads with the time spine, then persona as demoted
   context, then a market + artifact-hash fingerprint as the tiebreaker. */
const QUALITY_LEVEL = {strong: 3, medium: 2, weak: 1};
/* Monochrome by product law: green/red mean direction only, never quality.
   Three text tokens, because the tone also paints the ramp's label. */
const QUALITY_TONE = {strong: 'var(--ink)', medium: 'var(--ink-2)', weak: 'var(--muted)'};

function shortHash(id){
  return String(id || '').replace(/^[a-z]+[_-]+/i, '').slice(0, 4);
}

function QualityRamp({quality}){
  const q = String(quality || '').toLowerCase();
  const lvl = QUALITY_LEVEL[q];
  if (!lvl) return null;
  const tone = QUALITY_TONE[q];
  return (
    <span className="workbench-recent-ramp" title={'Signal quality: ' + q}>
      <span className="workbench-recent-pips">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className={'workbench-recent-pip' + (i < lvl ? ' on' : '')}
            style={i < lvl ? {height: (7 + i * 2) + 'px', background: tone} : {height: (7 + i * 2) + 'px'}}
          />
        ))}
      </span>
      <span className="workbench-recent-ramp-label" style={{color: tone}}>{q.charAt(0).toUpperCase() + q.slice(1)}</span>
    </span>
  );
}

function RecentBriefRow({item, active, onOpen}){
  const hash = shortHash(item.id);
  return (
    <button
      type="button"
      className={'workbench-recent-row' + (active ? ' active' : '')}
      onClick={() => onOpen(item.id)}
    >
      <span className="workbench-recent-spine">
        <span className="workbench-recent-when">
          <span className="workbench-recent-time">{shortTime(item.created)}</span>
          <span className="workbench-recent-day">{shortDate(item.created)}</span>
        </span>
        <QualityRamp quality={item.quality} />
      </span>
      {item.persona && <span className="workbench-recent-persona">{item.persona}</span>}
      <span className="workbench-recent-fingerprint">
        <span className="workbench-recent-markets">
          {(item.markets || []).map((m) => <MarketChip key={m} market={m} />)}
        </span>
        {hash && <span className="workbench-recent-hash">#{hash}</span>}
      </span>
    </button>
  );
}

/* What the recents rail may say about itself.

   It began as an empty array that fetched on mount, so the first paint stated
   there were no saved briefs in this session. That is a claim about the
   session made before looking at it, and it is indistinguishable on screen
   from a session that genuinely has none. Empty is only sayable once the read
   has come back. */
export function recentsState({loaded, error, items}){
  if (error) return 'error';
  if (!loaded) return 'loading';
  return (items || []).length ? 'ready' : 'empty';
}

function RecentBriefs({items, activeId, onOpen, loaded = true, error = ''}){
  const state = recentsState({loaded, error, items});
  if (state === 'loading') {
    return <div className="workbench-empty" aria-busy="true">Reading recent briefs.</div>;
  }
  if (state === 'error') {
    return <div className="workbench-empty" role="alert">Recent briefs are unavailable. Nothing is known about this session either way.</div>;
  }
  if (state === 'empty') {
    return <div className="workbench-empty">None yet.</div>;
  }
  return (
    <div className="workbench-recent-list">
      {items.slice(0, 6).map((item) => (
        <RecentBriefRow key={item.id} item={item} active={!!activeId && item.id === activeId} onOpen={onOpen} />
      ))}
    </div>
  );
}

/* Quiet register, 23 Sept 2026: the recent questions include the saved
   answer the page is showing. A saved answer opened from Fieldwork or a
   shared link lives on the server, not in this browser's conversations, so
   the rail said "None yet." beside it. It is listed first as the current
   item unless one of this browser's conversations already holds that
   answer, in which case that conversation is the current item. */
function threadHolds(thread, requestId){
  return (thread.messages || []).some((m) => m && m.intelligence && m.intelligence.request_id === requestId);
}

function RecentAsks({threads, onOpen, stored = null, currentRequestId = null}){
  const list = (threads || []).slice(0, 5);
  const holder = stored ? list.find((t) => threadHolds(t, stored.requestId)) : null;
  const storedRow = stored && !holder;
  const isCurrent = (requestId) => Boolean(requestId) && requestId === currentRequestId;
  if (!list.length && !storedRow) return <div className="workbench-empty">None yet.</div>;
  return (
    <div className="workbench-recent-list">
      {storedRow && (
        <a
          className={'workbench-recent-row' + (isCurrent(stored.requestId) ? ' active' : '')}
          href={buildWorkbenchHash({work: 'ask', requestId: stored.requestId})}
          aria-current={isCurrent(stored.requestId) ? 'page' : undefined}
        >
          <span className="workbench-recent-title">{threadTitle({messages: [{role: 'user', content: stored.question}]})}</span>
          <span className="workbench-recent-meta">{'Saved answer' + (stored.asOf ? ' · ' + coverageDate(stored.asOf) : '')}</span>
        </a>
      )}
      {list.map((t) => {
        const current = holder && t.id === holder.id && isCurrent(stored.requestId);
        return (
          <button key={t.id} type="button" className={'workbench-recent-row' + (current ? ' active' : '')} aria-current={current ? 'page' : undefined} onClick={() => onOpen(t)}>
            <span className="workbench-recent-title">{threadTitle(t)}</span>
            <span className="workbench-recent-meta">{askCountText((t.messages || []).filter((m) => m.role === 'user').length)} · {timeAgo(t.ts)}</span>
          </button>
        );
      })}
    </div>
  );
}

const FLOWBAR_SCOPE_ITEMS = [
  ['ZA', 'South Africa'],
  ['NG', 'Nigeria'],
  ['KE', 'Kenya'],
  ['ALL', 'All markets'],
];

export function ConsoleWorkbench({region, setRegion, session, onAuth, initialQuery}){
  const [routeState, setRouteState] = useState(() => {
    const st = parseWorkbenchRoute(window.location.hash || '#/console');
    return initialQuery && st.work === 'landing' ? {...st, work: 'ask'} : st;
  });
  const [threads, setThreads] = useState(loadThreads);
  /* The saved answer the Ask stage last opened, so the rail can list it. */
  const [storedAsk, setStoredAsk] = useState(null);
  const [recent, setRecent] = useState([]);
  const [recentErr, setRecentErr] = useState('');
  const [recentLoaded, setRecentLoaded] = useState(false);
  /* The entrance reads. Listing what exists and asking which scopes are
     available are reads: they create nothing, generate nothing and spend
     nothing. Each keeps its own loaded flag so an unfinished read is never
     shown as an empty estate. */
  const [investigations, setInvestigations] = useState(null);
  const [investigationsLoaded, setInvestigationsLoaded] = useState(false);
  const [investigationsRevision, setInvestigationsRevision] = useState(0);
  const [scopes, setScopes] = useState(null);
  const [scopesLoaded, setScopesLoaded] = useState(false);
  const [askBusy, setAskBusy] = useState(false);
  const [briefBusy, setBriefBusy] = useState(false);
  const [askBridge, setAskBridge] = useState(null);
  const [briefStep, setBriefStep] = useState('scan');
  const [approveCount, setApproveCount] = useState(0);
  const [askAnswered, setAskAnswered] = useState(false);
  /* The one read of the linked artifact version, made by the Evidence Room
     and shown by the Client Read panel. */
  const [artifactRead, setArtifactRead] = useState(null);
  // Bumped on every explicit "New brief" so the ResearchPage key changes and
  // React remounts it with clean state. Without this, starting a new brief
  // while a doc was open only nulled the route artifactId; the panel's
  // internal doc/scan state survived and the old brief kept rendering
  // (Jo's report: "new brief just opens the existing one").
  const [briefNonce, setBriefNonce] = useState(0);
  // Same remount pattern for the Ask side. askSel drives the ChatPage key and
  // its initial thread: threadId null = fresh conversation, a string opens
  // that saved thread, undefined (initial mount) keeps ChatPage's own
  // resume-latest default. Nonce bumps force the remount even when the
  // threadId value repeats (New ask twice in a row).
  const [askSel, setAskSel] = useState({nonce: 0, threadId: undefined});
  /* The dates Ask covers, stated once in the page head under its title. */
  const [coverageRes] = useApi(COVERAGE_PATH, session, onAuth);
  const askWindow = coverageRes.state === 'ready' ? askCoverage(coverageRes.data) : null;
  const stageRef = useRef(null);

  const work = routeState.work === 'ask' || routeState.work === 'brief' ? routeState.work : (initialQuery ? 'ask' : 'landing');
  const isBrief = work === 'brief';
  const isAsk = work === 'ask';
  /* The open investigation's working view, read by the identities on the URL
     and nothing else. Back, forward, reload and a copied link all name the
     same investigation and exact version, so they reopen the same content. */
  const [reviewSession, setReviewSession] = useState(currentReviewSession);
  useEffect(() => subscribeReviewSession(setReviewSession), []);
  /* The working view is internal and read only under a review session. */
  const dossierResource = useDossierResource({
    investigationId: isBrief ? routeState.investigationId : null,
    dossierVersion: routeState.dossierVersion,
    csrfToken: reviewSession ? reviewSession.csrfToken : null,
    onSessionLost: endReviewSession,
    onAuth,
  });

  const refreshRecent = () => {
    apiGetFresh('/api/research/recent?_=' + Date.now())
      .then((d) => {
        const rows = d && (d.artifacts || d.items || d.recent || d.rows || d);
        setRecent(Array.isArray(rows) ? rows.map(normaliseRecent).filter((r) => r.id) : []);
        setRecentErr('');
        setRecentLoaded(true);
      })
      .catch((e) => {
        if (e && e.auth) onAuth && onAuth();
        else setRecentErr(e && e.message ? e.message : 'Recent briefs unavailable.');
        setRecentLoaded(true);
      });
  };

  useEffect(() => {
    const sync = () => setRouteState(parseWorkbenchRoute(window.location.hash || '#/console'));
    const syncCustom = (e) => setRouteState(e && e.detail ? e.detail : parseWorkbenchRoute(window.location.hash || '#/console'));
    window.addEventListener('hashchange', sync);
    window.addEventListener(WORKBENCH_NAV_EVENT, syncCustom);
    return () => {
      window.removeEventListener('hashchange', sync);
      window.removeEventListener(WORKBENCH_NAV_EVENT, syncCustom);
    };
  }, []);

  useEffect(() => { refreshRecent(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let live = true;
    setInvestigations(null);
    setInvestigationsLoaded(false);
    apiPost('/api/v2/investigations/list?contract_version=intelligence_dossier_v1')
      .then((data) => { if (live){ setInvestigations(data); setInvestigationsLoaded(true); } })
      .catch(() => { if (live) setInvestigationsLoaded(true); });
    return () => { live = false; };
  }, [investigationsRevision]);

  useEffect(() => {
    let live = true;
    apiPost('/api/v2/investigations/scopes?contract_version=intelligence_dossier_v1')
      .then((data) => { if (live){ setScopes(data); setScopesLoaded(true); } })
      .catch(() => { if (live) setScopesLoaded(true); });
    return () => { live = false; };
  }, []);

  /* The stage heading takes focus when the reader moves inside the workbench,
     never on mount. A fresh load that pulled focus into the stage put the
     shell chrome after the workbench in the Tab order and sent the walk off
     the end of the document onto body before the skip link; the route move
     into the console is the shell's, which lands on the workspace heading
     itself. */
  const stageMounted = useRef(false);
  const pageRef = useRef(null);
  useEffect(() => {
    if (!stageMounted.current){ stageMounted.current = true; return undefined; }
    const id = requestAnimationFrame(() => {
      /* An empty Ask stage has no heading of its own: the question field
         leads it. The page title then names what changed. */
      const node = (stageRef.current && stageRef.current.querySelector('h1, h2, [data-stage-heading]'))
        || (pageRef.current && pageRef.current.querySelector('.workbench-title'));
      if (!node) return;
      if (!node.hasAttribute('tabindex')) node.setAttribute('tabindex', '-1');
      try { node.focus({preventScroll: true}); } catch (e){ node.focus(); }
    });
    return () => cancelAnimationFrame(id);
  /* Focus follows the investigation too. Opening a different one changes
     everything on the stage, and a reader who does not move with it is
     left reading the previous investigation's heading. */
  }, [work, routeState.artifactId, routeState.investigationId]);

  const goState = (state, opts) => {
    const next = {...routeState, requestId:null, error:null, ...state};
    /* A version belongs to its own investigation or artifact. Moving to
       another one drops the version, so it can never be read under the
       wrong identity. */
    if (next.investigationId !== routeState.investigationId && !(state && 'dossierVersion' in state)) next.dossierVersion = null;
    if (next.artifactId !== routeState.artifactId && !(state && 'artifactVersion' in state)) next.artifactVersion = null;
    if (state && state.artifactId) setAskBridge(null);
    navigateWorkbench(next, opts);
    setRouteState(parseWorkbenchRoute(buildWorkbenchHash(next)));
  };

  /* An investigation opened without a version reads the current one, and the
     version it turned out to be is written onto the URL in place, so a reload
     or a copied link reopens that exact version rather than whatever is
     current later. */
  const pinnedVersion = dossierResource.status === 'ready' && dossierResource.dossier ? dossierResource.dossier.dossier_version : null;
  useEffect(() => {
    if (!isBrief || !routeState.investigationId || routeState.dossierVersion || !pinnedVersion) return;
    goState({dossierVersion: pinnedVersion}, {replace: true});
  }, [pinnedVersion, isBrief, routeState.investigationId, routeState.dossierVersion]); // eslint-disable-line react-hooks/exhaustive-deps

  /* A conflict means the version on screen is no longer the one to act on.
     Dropping the pinned version reads the current one, which then pins
     itself, and the earlier version stays readable at its own link. */
  const reloadLatestDossier = () => {
    goState({dossierVersion: null, artifactId: null}, {replace: true});
    dossierResource.reload();
  };

  const frameInvestigation = async (input) => {
    try {
      const record = await createInvestigationFrom(buildFramingState({payload: scopes}), input, apiPost);
      setInvestigationsRevision((revision) => revision + 1);
      goState({work: 'brief', investigationId: record.investigation_id, artifactId: null}, {replace: false});
    } catch (error){
      if (error?.auth && onAuth) onAuth();
      throw error;
    }
  };

  const startFreshBrief = () => {
    setAskBridge(null);
    setBriefNonce((n) => n + 1);
    goState({work: 'brief', investigationId: null, artifactId: null, personaId: null, markets: null, topics: null}, {replace: false});
  };

  const startFreshAsk = () => {
    setAskSel((s) => ({nonce: s.nonce + 1, threadId: null}));
    goState({work: 'ask', investigationId: null, artifactId: null}, {replace: false});
  };

  const openAskThread = (t) => {
    if (!t || !t.id) return;
    setAskSel((s) => ({nonce: s.nonce + 1, threadId: t.id}));
    goState({work: 'ask', artifactId: null}, {replace: false});
  };

  const handleBuildBriefFromAsk = (ctx) => {
    if (!ctx) {
      setAskBridge(null);
      goState({work: 'brief', artifactId: null}, {replace: false});
      return;
    }
    const markets = ctx.markets && ctx.markets.length
      ? ctx.markets
      : marketsFromRegion(ctx.market || region);
    setAskBridge({...ctx, markets});
    goState({work: 'brief', artifactId: null, markets}, {replace: false});
  };

  const flowFeed = useMemo(() => {
    if (work === 'landing') {
      return {
        steps: [{key: 'choose', label: 'Choose a path'}],
        status: 'idle',
        scope: FLOWBAR_SCOPE_ITEMS.map(([code]) => ({
          code,
          on: String(region || 'ZA').toUpperCase() === code,
          onToggle: () => setRegion && setRegion(code),
        })),
        cta: null,
        step: 'choose',
      };
    }
    /* Ask redesign, 23 Sept 2026: Ask has no flow strip. Its two step
       squares were decoration, and the cited brief is offered at the end of
       each answer instead. A polite status line below says what the strip's
       live region used to. */
    if (work === 'ask') {
      return {steps: [], step: null, status: askBusy ? 'busy' : 'active', scope: null, cta: null};
    }
    /* Quiet register, 23 Sept 2026: an open investigation is not the scan,
       shape and doc flow, so the strip and its Continue, which scrolls to a
       control that page does not have, stay off it. */
    if (routeState.investigationId) {
      return {steps: [], step: null, status: 'active', scope: null, cta: null};
    }
    // brief
    return {
      steps: [
        {key: 'scan', label: 'Scan'},
        {key: 'shape', label: 'Shape'},
        {key: 'doc', label: 'Doc'},
      ],
      step: briefStep,
      status: briefBusy ? 'busy' : 'active',
      scope: null,
      cta: {
        label: approveCount > 0 ? approveCount + ' approved · Continue' : 'Continue',
        disabled: briefBusy || approveCount === 0,
        title: approveCount === 0 ? 'Approve at least one behaviour' : undefined,
        onClick: () => scrollToRealSubmit(),
      },
    };
  }, [work, region, setRegion, askBusy, askAnswered, briefStep, briefBusy, approveCount, routeState.investigationId]); // eslint-disable-line react-hooks/exhaustive-deps

  // The FlowBar CTA does not own the submit: the real generate action lives on
  // the BehaviourScan footer (Continue) or the ResearchPage generate button,
  // both of which branch on consolidated-vs-per-behaviour state this bar does
  // not track. Scrolling to the real control keeps a single source of truth
  // for what "continue" does, rather than duplicating that branch here.
  const scrollToRealSubmit = () => {
    const node = stageRef.current && stageRef.current.querySelector(
      '.bscan-footer .research-generate-btn, .research-generate-btn',
    );
    if (node && node.scrollIntoView) node.scrollIntoView({behavior: 'smooth', block: 'center'});
  };

  /* The current mode's recents lead the rail. */
  const recentPanel = useMemo(() => {
    const briefs = (
      <div className="workbench-rail-section" key="briefs">
        <h2 className="workbench-rail-k">Recent briefs</h2>
        <RecentBriefs items={recent} loaded={recentLoaded} error={recentErr} activeId={routeState.artifactId} onOpen={(id) => { setAskBridge(null); goState({work: 'brief', artifactId: id}, {replace: false}); }} />
        {recentErr && <div className="workbench-empty">{recentErr}</div>}
      </div>
    );
    const questions = (
      <div className="workbench-rail-section" key="questions">
        <h2 className="workbench-rail-k">Recent questions</h2>
        <RecentAsks threads={threads} onOpen={openAskThread} stored={storedAsk} currentRequestId={isAsk ? routeState.requestId : null} />
      </div>
    );
    return isBrief ? [briefs, questions] : [questions, briefs];
  }, [recent, recentErr, recentLoaded, threads, storedAsk, routeState.artifactId, routeState.requestId, isAsk, isBrief]); // eslint-disable-line react-hooks/exhaustive-deps

  if (routeState.error) return <div className="page workbench" role="alert">This question link is invalid. <a href="#/fieldwork">Back to Fieldwork</a></div>;

  return (
    <div className="page workbench" data-screen-label="Intelligence Console" ref={pageRef}>
      {/* Ask redesign, 23 Sept 2026: the page head says each thing once. A
          quiet link back to the Briefing, named as the nav names it, then the
          page title on the left, and on Ask one muted line with the market and
          dates a question can be answered for. The rail no longer repeats it.
          Quiet register, 23 Sept 2026: Build brief takes the same serif title
          as Ask, so both tabs share one page head at every width, and a saved
          answer, which Fieldwork lists and opens, names the way back there
          once, here, in place of a second back link inside the answer. */}
      <div className="workbench-topbar">
        {isAsk && routeState.requestId ? (
          <a className="workbench-back" href="#/fieldwork">Back to Fieldwork</a>
        ) : (
          <button type="button" className="workbench-back" onClick={() => go('/pulse')}>
            Back to Briefing
          </button>
        )}
        {isAsk && (
          <>
            <h1 className="workbench-title">Ask</h1>
            {/* Round three, 24 Sept 2026: the line holds its place from the
                first paint while the coverage read is out, and the dates fill
                it in, so their late arrival moves nothing under the page
                head. A read that ends with no dates to offer drops it. */}
            {askWindow ? (
              <p className="workbench-coverage" data-ask-window="" data-ask-coverage={askWindow.start + '/' + askWindow.end}>
                {coverageLine(askWindow, region)}
              </p>
            ) : coverageRes.state === 'loading' && <p className="workbench-coverage" data-ask-window="" />}
          </>
        )}
        {isBrief && <h1 className="workbench-title">Build brief</h1>}
        {!isAsk && !isBrief && (
          <div className="workbench-topbar-title">
            <span>Build</span>
            <em>Workbench</em>
          </div>
        )}
      </div>
      <div className="workbench-shell">
        <nav className="workbench-rail" aria-label="Workbench">
          <div className="workbench-mode-tabs" role="group" aria-label="Workbench modes">
            <button type="button" aria-current={isAsk ? 'page' : undefined} onClick={() => goState({work: 'ask', artifactId: null}, {replace: false})}>
              Ask
            </button>
            <button type="button" aria-current={isBrief ? 'page' : undefined} onClick={() => goState({work: 'brief', artifactId: null}, {replace: false})}>
              Build brief
            </button>
          </div>
          {/* One fresh start per mode: a new question on Ask, a new brief on
              Build brief. */}
          {(isAsk || isBrief) && (
            <div className="workbench-new-brief-wrap">
              <button type="button" className="workbench-new-brief" onClick={isAsk ? startFreshAsk : startFreshBrief}>
                <span aria-hidden="true">＋</span>
                {isAsk ? 'New question' : 'New brief'}
              </button>
            </div>
          )}
          {recentPanel}
        </nav>

        <div className="workbench-mobile-switch" role="group" aria-label="Workbench modes">
          <button type="button" aria-pressed={isAsk} onClick={() => goState({work: 'ask', artifactId: null}, {replace: false})}>Ask</button>
          <button type="button" aria-pressed={isBrief} onClick={() => goState({work: 'brief', artifactId: null}, {replace: false})}>Build brief</button>
          <button
            type="button"
            className="workbench-new-brief"
            onClick={isAsk ? startFreshAsk : startFreshBrief}
            title={isAsk ? 'New question' : 'New brief'}
          >
            <span aria-hidden="true">＋</span> New
          </button>
        </div>

        {/* FlowBar and stage share the second grid column inside one
           wrapper: as loose grid siblings the bar auto-placed into the column
           and the stage dropped under the rail, breaking the console layout. */}
        <div className="workbench-main">
        <FlowBar
          step={flowFeed.step}
          steps={flowFeed.steps}
          scope={flowFeed.scope}
          status={flowFeed.status}
          cta={flowFeed.cta}
        />
        {isAsk && (
          <p className="sr-only" role="status" aria-live="polite">
            {askBusy ? 'Answering your question.' : askAnswered ? 'Answer ready.' : ''}
          </p>
        )}

        <section className="workbench-stage" ref={stageRef}>
          {work === 'landing' && (
            <WorkbenchStart
              onAsk={() => goState({work: 'ask'}, {replace: false})}
              onBrief={startFreshBrief}
            />
          )}
          {work === 'landing' && (
            /* The dossier landing the contract describes. It lists what exists
               and opens it, which is the door the rest of the layer was
               missing. */
            <InvestigationIndex payload={investigations} loading={!investigationsLoaded} />
          )}
          {isAsk && (
            <ChatPage
              /* Same remount pattern as the brief panel: New ask or opening a
                 recent ask bumps the nonce, so the chat remounts on the right
                 thread (or a clean one) instead of resuming the last thread. */
              key={'ask:' + (routeState.requestId || askSel.nonce)}
              requestId={routeState.requestId}
              /* The follow-up is asked under the lens of the answer it
                 continues, so the live console opens on that lens. */
              onContinueQuestion={(threadId,market,question,clientLensId)=>{setRegion(market);setAskSel(s=>({nonce:s.nonce+1,threadId,question}));goState({work:'ask',clientLensId:clientLensId||null},{replace:false});}}
              /* A stored refusal's rephrase opens a fresh question, filled in
                 and not sent. */
              onNewQuestion={(market,draft)=>{setRegion(market);setAskSel(s=>({nonce:s.nonce+1,threadId:null,draft}));goState({work:'ask'},{replace:false});}}
              region={region}
              session={session}
              onAuth={onAuth}
              initialQuery={initialQuery}
              initialThreadId={askSel.threadId}
              initialSend={askSel.question}
              initialDraft={askSel.draft}
              /* The rephrase fills the field once; dropping it keeps a later return to Ask empty. */
              onInitialDraftUsed={() => setAskSel(s => ({...s, draft: undefined}))}
              /* The follow-up is asked once. Clearing it here keeps the key
                 and the mounted chat as they are, and a later remount on the
                 same thread only opens it. */
              onInitialSent={() => setAskSel(s => ({...s, question: undefined}))}
              embedded
              onThreadsChange={setThreads}
              onStoredQuestion={setStoredAsk}
              onBusyChange={setAskBusy}
              onActiveAnswerChange={setAskAnswered}
              onBuildBrief={handleBuildBriefFromAsk}
            />
          )}
          {isBrief && routeState.investigationId && <EvidenceRoom routeState={routeState} onAuth={onAuth} onArtifactRead={setArtifactRead} />}
          {isBrief && routeState.investigationId && routeState.artifactId && (
            /* The Client Read preview, at the one hash the contract gives
               it: the artifact inside its investigation. It renders what
               is already loaded and starts nothing, and it needs both
               identities because an artifact without its investigation is
               workspace_request_invalid. */
            <IntelligenceConsole {...buildDossierConsoleModel(dossierResource.dossier ? dossierResource.dossier.projection || null : null)} />
          )}
          {isBrief && routeState.investigationId && routeState.artifactId && routeState.artifactVersion && (
            /* The exact approved export, read at the artifact version the
               link names and at no other. */
            <ClientReadExport
              key={routeState.investigationId + ':' + routeState.artifactId + ':' + routeState.artifactVersion}
              investigationId={routeState.investigationId}
              artifactId={routeState.artifactId}
              artifactVersion={routeState.artifactVersion}
              onAuth={onAuth}
              read={artifactRead && artifactRead.key === routeState.investigationId + ':' + routeState.artifactId + ':' + routeState.artifactVersion ? artifactRead : null}
            />
          )}
          {isBrief && routeState.investigationId && (
            /* The working sequence. Keyed by investigation so nothing a
               reviewer typed for one investigation survives into another. */
            <DossierReview
              key={'review:' + routeState.investigationId}
              investigationId={routeState.investigationId}
              resource={dossierResource}
              reviewSession={reviewSession}
              signInConfigured={reviewSignInConfigured()}
              onAuth={onAuth}
              onVersionChange={(version) => goState({dossierVersion: version, artifactId: null}, {replace: false})}
              onReloadLatest={reloadLatestDossier}
              onOpenClientRead={({artifactId, artifactVersion}) => goState({artifactId, artifactVersion}, {replace: false})}
            />
          )}
          {isBrief && !routeState.investigationId && (
            /* Framing sits above the existing brief flow rather than replacing
               it. The legacy research path is in use, and removing a working
               route to install a new one is a separate decision from making
               the new one reachable. */
            <InvestigationFraming payload={scopes} loading={!scopesLoaded} onFrame={frameInvestigation} />
          )}
          {isBrief && !routeState.investigationId && (
            <ResearchPage
              /* The nonce keys the panel's identity. "New brief" bumps it, so
                 React remounts the panel with clean state instead of keeping
                 the previous brief's doc/scan state alive (the bug where a new
                 brief just showed the existing one). The key deliberately does
                 NOT include the artifact id: the panel already self-loads when
                 the route's artifactId changes (open a recent brief), and the
                 post-generate route update would otherwise remount mid-flow
                 and drop the multi-brief batch tabs. */
              key={'new:' + briefNonce}
              onAuth={onAuth}
              embedded
              routeState={routeState}
              onRouteStateChange={goState}
              onRunningChange={setBriefBusy}
              onRecentChange={refreshRecent}
              onBuildAsk={() => goState({work: 'ask'}, {replace: false})}
              askBridge={askBridge}
              onClearAskBridge={() => setAskBridge(null)}
              onFlowChange={setBriefStep}
              onApproveCount={setApproveCount}
            />
          )}
        </section>
        </div>
      </div>
    </div>
  );
}

export default ConsoleWorkbench;
