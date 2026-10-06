/* PULSE · dedicated #/research route: persona, markets, generate, scrollable doc. */
import {useState, useEffect, useRef, useCallback} from 'react';
import {EvidenceRoom as PackageEvidenceRoom} from 'ogilvy-intelligence-design-system';
/* Route-surface rules travel with the chunk; see explore.jsx. */
import './styles/instrument-route-surfaces.css';
import {apiGet} from './api.js';
import {Icon, useIsMobile} from './parts.jsx';
import {go} from './router.js';
import {safePlainData} from './privatePlainData.js';
import {FLAG, BEHAVIOUR_SCAN, topicLabel} from './model.js';
import {BehaviourScan, PlainFailure} from './behaviourScan.jsx';
import {TopicPicker} from './TopicPicker.jsx';
import {BriefViaQuestion} from './briefViaQuestion.jsx';
import {
  parseResearchUrl, setResearchShareUrl, setResearchPersonaUrl,
  generateResearch, generateResearchBatch, generateConsolidatedResearch,
  focusPayloadForBehaviour, behaviourBriefLabel,
  refineResearch, loadArtifact, normalizeDoc, extractPrompts,
  progressLabel, DocBody, ResearchRefineBar,
  ResearchProgressSteps, ResearchBatchProgress, OutcomePreviewCard, ResearchDocToolbar,
  researchShareUrl, marketOutcomeHint, framePresetActive, downloadResearchHtml,
  normalizeResearchProductFrame, WORKBENCH_NAV_EVENT,
} from './researchLib.jsx';
import {
  resolveResearchRouteSelection,
  resolveResearchRouteUpdate,
  resolveResearchPersonaSelection,
} from './workbenchRoute.js';

export {parseResearchUrl, buildResearchDeepLink} from './researchLib.jsx';

export function ResearchEvidenceRoom({open, title, evidenceSummary, ribbonModel, selectedReceiptId=null, onSelectReceipt, onClose}){
  return <PackageEvidenceRoom open={open} signalTitle={title} summary={evidenceSummary}
    ribbonModel={ribbonModel} selectedReceiptId={selectedReceiptId}
    onSelectReceipt={onSelectReceipt} onClose={onClose} />;
}

function citationWindow(source){
  const observedAt = source?.receipt?.published_at || source?.published_at || '';
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(observedAt)) return null;
  const observedTime = Date.parse(observedAt);
  if (!Number.isFinite(observedTime)) return null;
  const day = observedAt.slice(0, 10);
  const checkedAt = new Date(Date.parse(`${day}T00:00:00Z`) + 86_400_000).toISOString();
  return {observedAt, day, checkedAt};
}

function citationAuthority(source){
  if (/^https?:\/\//.test(source?.url || '')) return {url: source.url};
  const trace = source?.bq_trace;
  if (!trace || typeof trace.table !== 'string' || !trace.table) return null;
  const key = source.id || source.content_id || trace.row_key || trace.trend_date;
  return key ? {recordAuthority: `warehouse:${trace.table}:${key}`} : null;
}

function uncheckedCitationModel(title, reason){
  return {
    title,
    evidenceSummary: {
      contractVersion: '1.0.0', state: 'unchecked', receipts: [],
      independence: {status: 'unvalidated', familyCount: 0, groupingAuthority: null},
      direction: {status: 'unknown', supportingReceiptIds: [], opposingReceiptIds: []},
      window: {start: '', end: '', method: '', closed: false}, checkedAt: '', limitations: [reason],
    },
    ribbonModel: null,
    selectedReceiptId: null,
  };
}

export function adaptResearchCitation(input, refIndex){
  const boundary = safePlainData(input);
  if (!boundary.ok) return uncheckedCitationModel(`Citation ${refIndex}`, 'Citation authority is unavailable.');
  const source = boundary.value;
  const title = source?.title || source?.headline || source?.name || `Citation ${refIndex}`;
  const authority = citationAuthority(source);
  const window = citationWindow(source);
  const excerpt = source?.snippet || source?.excerpt || source?.text || source?.trend_synthesis || '';
  const id = source?.id || source?.content_id || (authority?.url ? authority.url : null);
  const sourceName = source?.platform || source?.ref_type || source?.type || '';
  const familyId = source?.source_family || source?.query_group || source?.ref_type || '';
  if (!authority || !window || !id || !sourceName || !familyId || !excerpt){
    return uncheckedCitationModel(title, 'Citation authority is unavailable.');
  }
  const receipt = {
    id: String(id), sourceName: String(sourceName), familyId: String(familyId), ...authority,
    observedAt: window.observedAt, direction: 'unknown', excerpt: String(excerpt),
    quality: 'thin', qualifying: false,
  };
  const evidenceSummary = {
    contractVersion: '1.0.0', state: 'thin', receipts: [receipt],
    independence: {status: 'unvalidated', familyCount: 0, groupingAuthority: null},
    direction: {status: 'unknown', supportingReceiptIds: [], opposingReceiptIds: []},
    window: {start: window.day, end: window.day, method: 'host citation publication date', closed: true},
    checkedAt: window.checkedAt, limitations: ['One citation does not establish independent agreement.'],
  };
  return {
    title,
    evidenceSummary,
    ribbonModel: {
      state: 'thin',
      axis: {start: window.day, end: window.day, interval: null, normalization: null, valueDomain: null},
      strands: [], primaryPath: {visible: false, receiptIds: [], interpretationId: null, points: []},
      historicalAnchor: {visible: false, fit: null, receiptId: null, historicalRun: null},
      limitations: ['No authorized time series is attached to this citation.'],
    },
    selectedReceiptId: String(id),
  };
}

/* Quiet register, 23 Sept 2026: plain words for a failed persona read, with
   the server's reason behind Details. */
const PERSONAS_FAILED = 'The persona lenses could not be loaded, so a brief cannot be shaped yet. Try again in a moment.';
const PERSONAS_UNAVAILABLE = 'Persona lenses are not available in this workspace, so a brief cannot be shaped here.';

function sourceAt(sources, idx){
  const n = Number(idx);
  if (!Number.isFinite(n) || n < 1) return null;
  return sources[n - 1] || sources.find((s) => s.index === n || s.ref_index === n) || null;
}

function AskBridgeBanner({bridge, onClear}){
  if (!bridge || !bridge.question) return null;
  return (
    <section className="research-ask-bridge" aria-labelledby="research-ask-bridge-title">
      <div className="research-ask-bridge-head">
        <div>
          <div className="research-field-label" id="research-ask-bridge-title">Starting from your ask</div>
          <p className="research-ask-bridge-note">Choose persona and markets below. This stays on screen until you generate or remove it. It is not sent to synthesis until you hit generate.</p>
        </div>
        <button type="button" className="research-ask-bridge-clear" onClick={onClear}>Remove</button>
      </div>
      <blockquote className="research-ask-bridge-q">{bridge.question}</blockquote>
      {bridge.answerSummary && (
        <p className="research-ask-bridge-a">{bridge.answerSummary}</p>
      )}
    </section>
  );
}

/* Shown in place of the brief flow where the server cannot write briefs, so a
   reader is not walked through the scan and the lens to a refusal at the end. */
export function BriefWritingUnavailable({message, onBuildAsk}){
  return (
    <section className="bscan" aria-labelledby="brief-unavailable-title">
      <header className="bscan-head">
        <div className="research-field-label">Build brief</div>
        <h2 id="brief-unavailable-title" data-stage-heading tabIndex={-1}>Cited briefs are not available here</h2>
        <p className="bscan-sub">{message || 'This server cannot write cited briefs. Ask gives a cited answer from the same evidence.'}</p>
        {onBuildAsk && <button type="button" className="research-generate-btn" onClick={onBuildAsk}>Go to Ask</button>}
      </header>
    </section>
  );
}

export function ResearchPage({onAuth, embedded=false, routeState=null, onRouteStateChange, onRunningChange, onRecentChange, onBuildAsk, askBridge=null, onClearAskBridge, onFlowChange, onApproveCount}){
  const mobile = useIsMobile();
  const ctrl = useRef(null);
  const generationActive = useRef(false);
  const [params, setParams] = useState(() => routeState || parseResearchUrl());

  const [personas, setPersonas] = useState([]);
  const [personaId, setPersonaId] = useState('');
  const [markets, setMarkets] = useState([]);
  const [productFrame, setProductFrame] = useState('');
  const [phase, setPhase] = useState('idle');
  const [progressStatus, setProgressStatus] = useState('');
  const [err, setErr] = useState('');
  const [capacityRetryContext, setCapacityRetryContext] = useState('');
  const [artifactId, setArtifactId] = useState(null);
  const [doc, setDoc] = useState(null);
  const [evidenceGraph, setEvidenceGraph] = useState(null);
  const [signalQuality, setSignalQuality] = useState('');
  const [degradedReason, setDegradedReason] = useState('');
  const [activeRef, setActiveRef] = useState(null);
  const [refining, setRefining] = useState(false);
  const [metaErr, setMetaErr] = useState('');
  const [copyState, setCopyState] = useState('');
  const runToken = useRef(0);
  const landedTimers = useRef([]);
  const landedFinal = useRef('ready');
  const consolidatedPending = useRef(false);
  const bridgeApplied = useRef(null);
  const [scanDone, setScanDone] = useState(false);
  const [consolidatedMode, setConsolidatedMode] = useState(false);
  const [approvedBehaviours, setApprovedBehaviours] = useState([]);
  const [briefResults, setBriefResults] = useState([]);
  const [activeBriefIndex, setActiveBriefIndex] = useState(0);
  const [batchJobs, setBatchJobs] = useState([]);
  const [manualTopics, setManualTopics] = useState(false);
  const [availability, setAvailability] = useState(null);
  const generationContext = JSON.stringify({
    routeState,
    params,
    personaId,
    markets,
    productFrame,
    approvedBehaviours,
  });
  const capacityRetryReady = Boolean(capacityRetryContext)
    && capacityRetryContext === generationContext;

  useEffect(() => { setCapacityRetryContext(''); }, [generationContext]);

  const applyTopicSelection = useCallback((approved) => {
    setConsolidatedMode(false);
    setApprovedBehaviours(approved || []);
    const mks = [...new Set((approved || []).map((b) => b.market).filter(Boolean))];
    if (mks.length) setMarkets(mks);
    setManualTopics(false);
    setScanDone(true);
  }, []);

  useEffect(() => {
    if (!params.topics || !params.topics.length || scanDone) return;
    applyTopicSelection(params.topics.map((t) => ({
      id: t.market + ':' + t.query_group,
      market: t.market,
      signal_topic: t.query_group,
      behaviour: t.query_group,
    })));
  }, [params.topics, scanDone, applyTopicSelection]);

  const abort = useCallback(() => {
    if (ctrl.current){ ctrl.current.abort(); ctrl.current = null; }
    generationActive.current = false;
  }, []);

  const clearLandedTimers = useCallback(() => {
    landedTimers.current.forEach((t) => clearTimeout(t));
    landedTimers.current = [];
  }, []);

  /* Cancel stops the client from polling; the job keeps running server-side
     (no kill endpoint exists), so the artifact still lands and Recent picks
     it up later. This only walks the UI back to idle. */
  const cancelGenerate = useCallback(() => {
    abort();
    clearLandedTimers();
    setPhase('idle');
    setProgressStatus('');
  }, [abort, clearLandedTimers]);

  useEffect(() => {
    if (routeState) {
      setParams(routeState);
      return undefined;
    }
    const sync = () => setParams(parseResearchUrl());
    const syncWorkbench = (e) => setParams(e && e.detail ? e.detail : parseResearchUrl());
    window.addEventListener('hashchange', sync);
    window.addEventListener(WORKBENCH_NAV_EVENT, syncWorkbench);
    return () => {
      window.removeEventListener('hashchange', sync);
      window.removeEventListener(WORKBENCH_NAV_EVENT, syncWorkbench);
    };
  }, [routeState]);

  useEffect(() => { onRunningChange && onRunningChange(phase === 'running' || refining); }, [onRunningChange, phase, refining]);

  useEffect(() => {
    if (!onFlowChange) return;
    if (!scanDone) { onFlowChange('scan'); return; }
    if (phase === 'running' || phase === 'landed' || phase === 'ready' || phase === 'degraded') { onFlowChange('doc'); return; }
    onFlowChange('shape');
  }, [onFlowChange, scanDone, phase]);

  useEffect(() => {
    if (!onApproveCount) return;
    onApproveCount(approvedBehaviours.length);
  }, [onApproveCount, approvedBehaviours.length]);

  const loadMeta = useCallback(async () => {
    try {
      setMetaErr('');
      const p = await apiGet('/api/research/personas');
      const list = (p && p.personas) || [];
      setPersonas(list);
    } catch (e){
      if (e && e.auth) onAuth && onAuth();
      else setMetaErr(e && e.message ? e : {message: 'Could not load research personas.'});
    }
  }, [onAuth]);

  useEffect(() => { loadMeta(); }, [loadMeta]);

  /* What this deployment can do, read once. An unreadable record leaves the
     flow as it was: the server still refuses a brief it cannot write. */
  useEffect(() => {
    let live = true;
    apiGet('/api/research/availability')
      .then((value) => { if (live) setAvailability(value || null); })
      .catch((e) => { if (live && e && e.auth) onAuth && onAuth(); });
    return () => { live = false; };
  }, [onAuth]);

  useEffect(() => {
    if (!askBridge || !askBridge.question) return;
    const key = String(askBridge.threadId || '') + '|' + askBridge.question;
    if (bridgeApplied.current === key) return;
    bridgeApplied.current = key;
    if (askBridge.markets && askBridge.markets.length) {
      setMarkets(askBridge.markets.slice());
      if (onRouteStateChange) {
        onRouteStateChange({work: 'brief', personaId: params.personaId || personaId || null, markets: askBridge.markets.slice()}, {replace: true});
      }
    }
  }, [askBridge, onRouteStateChange, params.personaId, personaId]);

  useEffect(() => {
    if (!askBridge) bridgeApplied.current = null;
  }, [askBridge]);

  useEffect(() => {
    const selection = resolveResearchRouteUpdate(
      params,
      personas,
      personaId,
      productFrame,
    );
    setPersonaId(selection.personaId);
    setProductFrame(selection.productFrame);
    setMarkets(selection.markets);
  }, [params.personaId, params.markets, personas, personaId, productFrame]);

  const applyBriefResult = useCallback((res, index) => {
    const eg = res.evidence_graph || evidenceGraph;
    const norm = normalizeDoc({...res, evidence_graph: eg});
    setBriefResults((prev) => {
      const next = prev.slice();
      next[index] = {...(next[index] || {}), result: res, doc: norm};
      return next;
    });
    if (index === activeBriefIndex || !doc) {
      setDoc(norm);
      if (res.evidence_graph) setEvidenceGraph(res.evidence_graph);
      setSignalQuality(res.signal_quality || norm.json.signal_quality || '');
      setDegradedReason(res.degraded_reason || '');
      if (res.artifact_id) {
        setArtifactId(res.artifact_id);
        onRouteStateChange
          ? onRouteStateChange({work: 'brief', artifactId: res.artifact_id}, {replace: true})
          : setResearchShareUrl(res.artifact_id);
      }
      setPhase(res.status === 'completed_degraded' ? 'degraded' : 'ready');
      setProgressStatus(res.status || 'completed');
    }
  }, [activeBriefIndex, doc, evidenceGraph, onRouteStateChange]);

  /* theatre: only live single-rail generation gets the landed beat. The rail
     holds at 100% for a beat, resolves into the landed reveal, then the doc
     develops in, auto after 2.4s or on the Open button. Artifact loads,
     refines and batch results cut straight to the doc as before. */
  const applyResult = useCallback((res, theatre) => {
    const eg = res.evidence_graph || evidenceGraph;
    const norm = normalizeDoc({...res, evidence_graph: eg});
    setDoc(norm);
    if (res.evidence_graph) setEvidenceGraph(res.evidence_graph);
    setSignalQuality(res.signal_quality || norm.json.signal_quality || '');
    setDegradedReason(res.degraded_reason || '');
    if (res.artifact_id){
      setArtifactId(res.artifact_id);
      onRouteStateChange
        ? onRouteStateChange({work: 'brief', artifactId: res.artifact_id}, {replace: true})
        : setResearchShareUrl(res.artifact_id);
      onRecentChange && onRecentChange();
    }
    const finalPhase = res.status === 'completed_degraded' ? 'degraded' : 'ready';
    setProgressStatus(res.status || 'completed');
    if (theatre) {
      landedFinal.current = finalPhase;
      clearLandedTimers();
      landedTimers.current.push(setTimeout(() => {
        setPhase('landed');
        landedTimers.current.push(setTimeout(() => setPhase(finalPhase), 2400));
      }, 800));
    } else {
      setPhase(finalPhase);
    }
  }, [clearLandedTimers, evidenceGraph, onRecentChange, onRouteStateChange]);

  const openBriefNow = useCallback(() => {
    clearLandedTimers();
    setPhase(landedFinal.current || 'ready');
  }, [clearLandedTimers]);

  const selectBrief = useCallback((index) => {
    const row = briefResults[index];
    if (!row || !row.result) return;
    setActiveBriefIndex(index);
    applyResult(row.result);
  }, [applyResult, briefResults]);

  const runGenerate = useCallback(async () => {
    if (!personaId || phase === 'running' || generationActive.current) return;
    abort();
    generationActive.current = true;
    clearLandedTimers();
    const c = new AbortController();
    ctrl.current = c;
    const token = ++runToken.current;
    setPhase('running');
    setProgressStatus('starting');
    setErr('');
    setCapacityRetryContext('');
    setDoc(null);
    setArtifactId(null);
    setActiveRef(null);
    setEvidenceGraph(null);
    setSignalQuality('');
    setDegradedReason('');
    setBriefResults([]);
    setBatchJobs([]);
    setActiveBriefIndex(0);
    let standardRequest = false;
    try {
      const persona = personas.find((p) => p.id === personaId);
      const frame = normalizeResearchProductFrame(productFrame);
      const mks = markets.length ? markets : (persona && persona.default_markets);
      if (consolidatedMode && approvedBehaviours.length > 0) {
        const res = await generateConsolidatedResearch(
          personaId,
          frame,
          approvedBehaviours,
          mks,
          c.signal,
          setProgressStatus,
        );
        if (c.signal.aborted || token !== runToken.current) return;
        generationActive.current = false;
        ctrl.current = null;
        applyResult(res, true);
        onRecentChange && onRecentChange();
        loadMeta();
        return;
      }
      const fanOut = BEHAVIOUR_SCAN && approvedBehaviours.length > 0 && !consolidatedMode;
      if (fanOut && approvedBehaviours.length > 1) {
        const initialJobs = approvedBehaviours.map((b) => ({behaviour: b, phase: 'queued', status: ''}));
        setBatchJobs(initialJobs);
        const batch = await generateResearchBatch(
          personaId,
          frame,
          approvedBehaviours,
          c.signal,
          (evt) => {
            if (c.signal.aborted || token !== runToken.current) return;
            setBatchJobs((prev) => {
              const next = prev.slice();
              while (next.length <= evt.index) next.push({phase: 'queued', status: ''});
              next[evt.index] = {...next[evt.index], ...evt, behaviour: evt.behaviour || next[evt.index].behaviour};
              return next;
            });
            if (evt.status) setProgressStatus(evt.status);
          },
        );
        if (c.signal.aborted || token !== runToken.current) return;
        generationActive.current = false;
        ctrl.current = null;
        const packed = batch.results.map(({behaviour, result}) => ({behaviour, result}));
        setBriefResults(packed);
        if (packed.length && packed[0].result) {
          applyBriefResult(packed[0].result, 0);
          onRecentChange && onRecentChange();
        }
        loadMeta();
        return;
      }
      const focus = fanOut
        ? focusPayloadForBehaviour(approvedBehaviours[0])
        : null;
      standardRequest = true;
      const res = await generateResearch(
        personaId,
        fanOut && focus && focus.markets.length ? focus.markets : mks,
        frame,
        c.signal,
        setProgressStatus,
        focus,
      );
      if (c.signal.aborted || token !== runToken.current) return;
      generationActive.current = false;
      ctrl.current = null;
      if (fanOut) {
        setBriefResults([{behaviour: approvedBehaviours[0], result: res}]);
      }
      applyResult(res, true);
      loadMeta();
    } catch (e){
      if (token === runToken.current) generationActive.current = false;
      if (e && e.aborted) return;
      ctrl.current = null;
      if (e && e.auth){ onAuth && onAuth(); setPhase('idle'); return; }
      setErr(e && e.message ? e.message : 'Research failed.');
      setCapacityRetryContext(
        standardRequest && e && e.code === 'capacity_busy' ? generationContext : '',
      );
      setPhase('error');
    }
  }, [abort, applyBriefResult, applyResult, approvedBehaviours, clearLandedTimers, consolidatedMode, generationContext, loadMeta, markets, onAuth, onRecentChange, personaId, personas, phase, productFrame]);

  const loadArtifactById = useCallback(async (id) => {
    if (!id) return;
    abort();
    clearLandedTimers();
    const c = new AbortController();
    ctrl.current = c;
    const token = ++runToken.current;
    setPhase('running');
    setProgressStatus('gathering');
    setErr('');
    setDoc(null);
    setActiveRef(null);
    setEvidenceGraph(null);
    setSignalQuality('');
    setDegradedReason('');
    try {
      const res = await loadArtifact(id, c.signal);
      if (c.signal.aborted || token !== runToken.current) return;
      ctrl.current = null;
      applyResult(res);
      if (res.persona_id) setPersonaId(res.persona_id);
      if (res.markets && res.markets.length) setMarkets(res.markets.slice());
    } catch (e){
      if (e && e.aborted) return;
      ctrl.current = null;
      if (e && e.auth){ onAuth && onAuth(); setPhase('idle'); return; }
      setErr(e && e.message ? e.message : 'Could not load research doc.');
      setPhase('error');
    }
  }, [abort, applyResult, clearLandedTimers, onAuth]);

  useEffect(() => {
    if (!params.artifactId) return;
    if (artifactId === params.artifactId && doc) return;
    loadArtifactById(params.artifactId);
  }, [params.artifactId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => { abort(); clearLandedTimers(); }, [abort, clearLandedTimers]);

  useEffect(() => {
    if (!consolidatedPending.current || !consolidatedMode || !scanDone || !personaId) return;
    if (params.artifactId || phase === 'running' || doc) return;
    consolidatedPending.current = false;
    runGenerate();
  }, [consolidatedMode, scanDone, personaId, params.artifactId, phase, doc, runGenerate]);

  const onRefine = async (instruction) => {
    if (!artifactId || refining) return;
    abort();
    const c = new AbortController();
    ctrl.current = c;
    const token = ++runToken.current;
    setRefining(true);
    setErr('');
    try {
      const res = await refineResearch(artifactId, instruction, c.signal, setProgressStatus);
      if (c.signal.aborted || token !== runToken.current) return;
      if (res.artifact_id) {
        onRecentChange && onRecentChange();
        const loaded = await loadArtifact(res.artifact_id, c.signal);
        if (c.signal.aborted || token !== runToken.current) return;
        applyResult(loaded);
        if (loaded.persona_id) setPersonaId(loaded.persona_id);
        if (loaded.markets && loaded.markets.length) setMarkets(loaded.markets.slice());
      }
      else applyResult({...res, evidence_graph: evidenceGraph, artifact_id: artifactId});
      loadMeta();
    } catch (e){
      if (e && e.aborted) return;
      if (e && e.auth) onAuth && onAuth();
      else setErr(e && e.message ? e.message : 'Refine failed.');
    } finally {
      if (token === runToken.current) {
        setRefining(false);
        ctrl.current = null;
      }
    }
  };

  const batchProgressLabel = () => {
    if (phase !== 'running' || batchJobs.length <= 1) return progressLabel(progressStatus);
    const runningIdx = batchJobs.findIndex((j) => j.phase === 'running');
    const doneCount = batchJobs.filter((j) => j.phase === 'done' || j.phase === 'error').length;
    const activeIdx = runningIdx >= 0 ? runningIdx : Math.min(doneCount, batchJobs.length - 1);
    const b = batchJobs[activeIdx] && batchJobs[activeIdx].behaviour;
    const label = behaviourBriefLabel(b || {}, activeIdx);
    return batchJobs.length === 1
      ? progressLabel(progressStatus)
      : 'Brief ' + (activeIdx + 1) + ' of ' + batchJobs.length + ': ' + label;
  };
  const selectedPersona = personas.find((p) => p.id === personaId);
  const singlePersonaMode = personas.length === 1;
  const frameOptions = (selectedPersona && selectedPersona.product_frame_options) || [];
  const signalTopics = (selectedPersona && selectedPersona.signal_topics) || [];
  const activeFramePreset = frameOptions.find((o) => framePresetActive(productFrame, o.value));
  const marketsHint = marketOutcomeHint(markets);
  const {json, markdown, sources} = doc || {json: {}, markdown: '', sources: []};
  const title = json.title || (selectedPersona ? selectedPersona.label + ' research' : 'Research');
  const promptsText = extractPrompts(json);
  const citeSource = activeRef ? sourceAt(sources, activeRef) : null;
  const citationModel = adaptResearchCitation(citeSource, activeRef);
  const docReady = phase === 'ready' || phase === 'degraded';
  /* Landed reveal copy: doc title, falling back to the doc H1; meta drops any
     segment whose real value is missing. */
  const landedTitle = json.title
    || (String(markdown || '').match(/^#\s+(.+)$/m) || [])[1]
    || 'Research brief';
  const landedMeta = [
    markets.length ? markets.map((m) => m.toUpperCase()).join(' · ') : '',
    '30d',
    approvedBehaviours.length ? approvedBehaviours.length + ' behaviours' : '',
    sources.length ? sources.length + ' citations' : '',
  ].filter(Boolean).join(' · ');
  const jobDone = ['completed', 'completed_degraded'].includes(progressStatus);

  const pickPersona = (id) => {
    const selection = resolveResearchPersonaSelection(id, personas, markets);
    if (!selection.personaId) return;
    setPersonaId(selection.personaId);
    setProductFrame(selection.productFrame);
    setMarkets(selection.markets);
    onRouteStateChange
      ? onRouteStateChange({work: 'brief', personaId: selection.personaId, markets: selection.markets}, {replace: true})
      : setResearchPersonaUrl(selection.personaId, selection.markets);
  };

  const toggleMarket = (mk) => {
    const m = String(mk).toLowerCase();
    setMarkets((prev) => {
      const next = prev.includes(m) ? prev.filter((x) => x !== m) : [...prev, m];
      if (personaId) {
        onRouteStateChange
          ? onRouteStateChange({work: 'brief', personaId, markets: next}, {replace: true})
          : setResearchPersonaUrl(personaId, next);
      }
      return next;
    });
  };

  const copyShare = () => {
    if (!artifactId) return;
    if (!navigator.clipboard) {
      setCopyState('Copy failed');
      setTimeout(() => setCopyState(''), 1800);
      return;
    }
    navigator.clipboard.writeText(researchShareUrl(artifactId))
      .then(() => {
        setCopyState('Copied');
        setTimeout(() => setCopyState(''), 1600);
      })
      .catch(() => {
        setCopyState('Copy failed');
        setTimeout(() => setCopyState(''), 1800);
      });
  };

  const writing = availability && availability.brief_writing;
  if (writing && writing.available === false && !params.artifactId && !doc) {
    return (
      <div className={'page research' + (embedded ? ' research-embedded' : '')} data-screen-label="Research">
        <div className="research-shell">
          {availability.brief_via_question === true
            ? <BriefViaQuestion
                onAuth={onAuth}
                initialTopic={(askBridge && askBridge.question) || (params.topics || []).map((t) => topicLabel(t.query_group)).join(', ')}
                initialMarket={markets.length === 1 ? markets[0] : markets.length > 1 ? 'all' : 'za'}
              />
            : <BriefWritingUnavailable message={writing.message} onBuildAsk={onBuildAsk} />}
        </div>
      </div>
    );
  }

  const showScan = BEHAVIOUR_SCAN && embedded && !scanDone && !params.artifactId && !doc && !manualTopics;
  if (manualTopics && BEHAVIOUR_SCAN && embedded && !scanDone) {
    return (
      <div className={'page research' + (embedded ? ' research-embedded' : '')} data-screen-label="Research">
        <div className="research-shell">
          <TopicPicker
            markets={markets.length ? markets : ['za', 'ng', 'ke']}
            onAuth={onAuth}
            onContinue={applyTopicSelection}
          />
          <button type="button" className="research-inline-action" onClick={() => setManualTopics(false)}>Back to behaviour scan</button>
        </div>
      </div>
    );
  }
  if (showScan) {
    return (
      <div className={'page research' + (embedded ? ' research-embedded' : '')} data-screen-label="Research">
        <div className="research-shell">
          {metaErr && <PlainFailure summary={PERSONAS_FAILED} unavailable={PERSONAS_UNAVAILABLE} error={metaErr} onRetry={loadMeta} />}
          <BehaviourScan
            markets={markets.length ? markets : ['za', 'ng', 'ke']}
            onAuth={onAuth}
            onPickTopics={() => setManualTopics(true)}
            onContinue={(approved) => {
              setConsolidatedMode(false);
              setApprovedBehaviours(approved || []);
              const mks = [...new Set((approved || []).map((b) => b.market).filter(Boolean))];
              if (mks.length) setMarkets(mks);
              setScanDone(true);
            }}
            onBuildConsolidated={(approved) => {
              setConsolidatedMode(true);
              consolidatedPending.current = true;
              setApprovedBehaviours(approved || []);
              const mks = [...new Set((approved || []).map((b) => b.market).filter(Boolean))];
              if (mks.length) setMarkets(mks);
              setScanDone(true);
            }}
          />
        </div>
      </div>
    );
  }

  return (
    <div className={'page research' + (embedded ? ' research-embedded' : '')} data-screen-label="Research">
      <div className="research-shell">
        <header className="research-head">
          <div className="research-head-top">
            {!embedded && <button type="button" className="research-back" onClick={() => go('/console')}>
              <Icon.arrRight style={{width: 14, height: 14, transform: 'rotate(180deg)'}} />
              Console
            </button>}
            {embedded && onBuildAsk && <button type="button" className="research-back" onClick={onBuildAsk}>Ask the desk</button>}
            <div className="research-kicker">Market research</div>
          </div>
          <h1 className={'research-title' + (mobile ? ' research-title-mobile' : '')}>
            {docReady && doc ? title : 'Research doc'}
          </h1>
          <p className="research-sub">
            {selectedPersona ? selectedPersona.label : (personas.length ? 'Pick a persona' : 'Loading…')}
            {markets.length ? ' · ' + markets.map((m) => m.toUpperCase()).join(' · ') : ''}
          </p>
        </header>

        {!embedded && <p className="research-explainer">
          Generates a cited market research document from connected evidence sources and search behaviour for the selected role and markets.
        </p>}
        {!embedded && <p className="research-persona-note">Personas are configured by the insights team.</p>}

        {mobile && <OutcomePreviewCard productFrame={productFrame} markets={markets} />}

        <div className="research-controls">
          {embedded && askBridge && askBridge.question && (
            <AskBridgeBanner bridge={askBridge} onClear={onClearAskBridge} />
          )}
          {BEHAVIOUR_SCAN && approvedBehaviours.length > 0 && (
            <section className="research-ask-bridge" aria-labelledby="research-scan-title">
              <div className="research-ask-bridge-head">
                <div>
                  <div className="research-field-label" id="research-scan-title">Approved behaviours</div>
                  <p className="research-ask-bridge-note">
                    {consolidatedMode
                      ? 'One consolidated doc across the approved behaviours. Choose the lens below, then generate.'
                      : 'Choose the lens below. Each approved behaviour becomes its own brief.'}
                  </p>
                </div>
                <button type="button" className="research-ask-bridge-clear" onClick={() => { setScanDone(false); setConsolidatedMode(false); setApprovedBehaviours([]); }}>Back to scan</button>
              </div>
              <ul className="research-scan-approved">
                {approvedBehaviours.map((b) => (
                  <li key={b.id}>
                    <span className="research-scan-approved-topic">{(FLAG[String(b.market).toUpperCase()] || '') + ' ' + b.signal_topic}</span>
                    <span className="research-scan-approved-line">{b.behaviour}</span>
                    {b.note && <span className="research-scan-approved-note">Note: {b.note}</span>}
                  </li>
                ))}
              </ul>
            </section>
          )}
          <p className="research-query-note">
            Choose the strategic lens, then build a cited brief from connected evidence sources and search behaviour.
          </p>
          {metaErr && <PlainFailure summary={PERSONAS_FAILED} unavailable={PERSONAS_UNAVAILABLE} error={metaErr} onRetry={loadMeta} />}

          <div className="research-field">
            <div className="research-field-label">Persona lens</div>
            <div className="research-persona-pick" role="group" aria-label="Persona lens">
              {personas.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  className={'research-persona-card' + (personaId === p.id ? ' active' : '')}
                  onClick={() => pickPersona(p.id)}
                  aria-pressed={personaId === p.id}
                >
                  <span className="research-persona-card-name">{p.label || p.id}</span>
                  {p.description && <span className="research-persona-card-desc">{p.description}</span>}
                </button>
              ))}
              {!personas.length && <span className="research-loading-hint">Loading personas…</span>}
            </div>
          </div>

          {selectedPersona && (
            <div className="research-persona-detail">
              {selectedPersona.default_markets && selectedPersona.default_markets.length > 0 && (
                <p className="research-default-hint">
                  Default markets: {selectedPersona.default_markets.map((m) => FLAG[m.toUpperCase()] || m.toUpperCase()).join(' · ')}
                </p>
              )}
              {signalTopics.length > 0 && (
                <div className="research-query-groups">
                  <div className="research-field-label">Signal topics this doc will pull</div>
                  <div className="research-topic-list">
                    {signalTopics.map((t) => (
                      <span key={t.id} className="research-topic-chip" title={t.hint}>
                        <span className="research-topic-label">{t.label}</span>
                        <span className="research-topic-hint">{t.hint}</span>
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {!signalTopics.length && selectedPersona.query_groups && selectedPersona.query_groups.length > 0 && (
                <div className="research-query-groups">
                  <div className="research-field-label">Signal topics this doc will pull</div>
                  <div className="research-query-chips">
                    {selectedPersona.query_groups.map((g) => (
                      <span key={g} className="research-chip">{topicLabel(g)}</span>
                    ))}
                  </div>
                </div>
              )}
              <div className="research-product-frame">
                <label htmlFor="research-product-frame" className="research-field-label">
                  Product frame
                </label>
                {frameOptions.length > 0 && (
                  <div className="research-frame-presets">
                    {frameOptions.map((opt) => {
                      const active = framePresetActive(productFrame, opt.value);
                      return (
                        <button
                          key={opt.value}
                          type="button"
                          className={'research-frame-chip' + (active ? ' active' : '')}
                          onClick={() => setProductFrame(opt.value)}
                          title={opt.hint}
                          aria-pressed={active}
                        >
                          {opt.value}
                        </button>
                      );
                    })}
                  </div>
                )}
                {activeFramePreset && activeFramePreset.hint && (
                  <p className="research-frame-chip-hint">{activeFramePreset.hint}</p>
                )}
                <input
                  id="research-product-frame"
                  type="text"
                  className="research-product-frame-input"
                  value={productFrame}
                  onChange={(e) => setProductFrame(e.target.value)}
                  placeholder="Product, service or brand role to test"
                />
                <p className="research-frame-note">Add the role you want the evidence to test. Leave it blank to keep the research open.</p>
              </div>
            </div>
          )}

          <div className="research-field">
            <div className="research-field-label">Markets</div>
            <div className="research-market-pick">
              {['za', 'ng', 'ke'].map((m) => (
                <button
                  key={m}
                  type="button"
                  className={'research-market-btn' + (markets.includes(m) ? ' active' : '')}
                  onClick={() => toggleMarket(m)}
                  aria-pressed={markets.includes(m)}
                >
                  <span className="research-market-code">{m.toUpperCase()}</span>
                </button>
              ))}
            </div>
            <p className="research-markets-hint">{marketsHint}</p>
          </div>

          {!mobile && <OutcomePreviewCard productFrame={productFrame} markets={markets} />}

          <button
            type="button"
            className="research-generate-btn"
            onClick={runGenerate}
            disabled={!personaId || phase === 'running' || !markets.length}
          >
            {phase === 'running'
              ? batchProgressLabel()
              : (consolidatedMode && approvedBehaviours.length > 0
                ? 'Generate consolidated doc (' + approvedBehaviours.length + ')'
                : (BEHAVIOUR_SCAN && approvedBehaviours.length > 1
                  ? 'Generate ' + approvedBehaviours.length + ' briefs'
                  : (singlePersonaMode ? 'Generate doc for this persona' : 'Generate research doc')))}
          </button>
        </div>

        <div className="research-doc">
          {phase === 'running' && batchJobs.length > 1 && (
            <ResearchBatchProgress jobs={batchJobs} />
          )}
          {phase === 'running' && batchJobs.length <= 1 && (
            <>
              {/* Once the job reports completed the room brightens: focus dim
                 off, and Cancel goes away (there is nothing left to cancel). */}
              <ResearchProgressSteps status={progressStatus} focus={embedded && !jobDone} />
              {embedded && !jobDone && (
                <div className="research-cancel-row">
                  <p className="research-cancel-note">Cancel stops the client. The job finishes on the server and lands in Recent.</p>
                  <button type="button" className="research-cancel-btn" onClick={cancelGenerate}>
                    Cancel
                  </button>
                </div>
              )}
            </>
          )}

          {phase === 'landed' && doc && (
            <div className="research-landed" role="status">
              <div className="research-landed-card">
                <div className="research-landed-badge" aria-hidden="true">
                  <span className="research-landed-ring" />
                  <span className="research-landed-fill" />
                  <svg className="research-landed-check" viewBox="0 0 24 24" width="56" height="56" fill="none" stroke="var(--accent)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M7 12.5l3.2 3.2L17 8.5" strokeDasharray="26" />
                  </svg>
                </div>
                <div className="research-landed-k">Cited brief ready</div>
                <h2 className="research-landed-title">{landedTitle}</h2>
                {landedMeta && <div className="research-landed-meta">{landedMeta}</div>}
                <button type="button" className="research-landed-open" onClick={openBriefNow}>Open the brief →</button>
                <p className="research-landed-note">Also saved to Recent · opening automatically…</p>
              </div>
            </div>
          )}

          {briefResults.length > 1 && docReady && (
            <div className="research-brief-tabs" role="tablist" aria-label="Generated briefs">
              {briefResults.map((row, i) => {
                const b = row.behaviour || {};
                const label = behaviourBriefLabel(b, i);
                return (
                  <button
                    key={b.id || i}
                    type="button"
                    role="tab"
                    className={'research-brief-tab' + (activeBriefIndex === i ? ' active' : '')}
                    aria-selected={activeBriefIndex === i}
                    onClick={() => selectBrief(i)}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          )}

          {(phase === 'degraded' || progressStatus === 'completed_degraded') && (
            <div className="research-degraded-banner" role="status">
              {degradedReason || 'A monitored source was unavailable; the document uses the evidence that completed.'}
            </div>
          )}

          {phase === 'error' && err && (
            <div className="research-error" role="alert"><p>{err}</p>{capacityRetryReady && <button type="button" className="research-inline-action" onClick={runGenerate} disabled={generationActive.current || phase === 'running'}>Retry</button>}</div>
          )}

          {docReady && doc && (
            <div className="research-doc-enter v3develop">
              <ResearchDocToolbar
                signalQuality={signalQuality}
                personaLabel={selectedPersona && selectedPersona.label}
                markets={markets}
                markdown={markdown}
                promptsText={promptsText}
                artifactId={artifactId}
                onShare={copyShare}
                onCopyEvidencePack={!!artifactId}
                onDownloadHtml={() => {
                  downloadResearchHtml(artifactId, {
                    docJson: json,
                    sources,
                    personaLabel: selectedPersona && selectedPersona.label,
                    markets,
                  }).catch((e) => {
                    if (e && e.auth) onAuth && onAuth();
                    else setErr(e && e.message ? e.message : 'Download failed.');
                  });
                }}
              />
              {copyState && <div className="research-copy-state" role="status">{copyState}</div>}
              <DocBody json={json} markdown={markdown} sources={sources} signalQuality={signalQuality} onRef={setActiveRef} />
              {artifactId && <ResearchRefineBar artifactId={artifactId} busy={refining} onRefine={onRefine} />}
            </div>
          )}
        </div>
      </div>

      <ResearchEvidenceRoom
        open={Boolean(activeRef)}
        title={citationModel.title}
        evidenceSummary={citationModel.evidenceSummary}
        ribbonModel={citationModel.ribbonModel}
        selectedReceiptId={citationModel.selectedReceiptId}
        onClose={() => setActiveRef(null)}
      />
    </div>
  );
}
