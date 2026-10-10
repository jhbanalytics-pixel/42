/* PULSE · Console Research shared: URL helpers, API, doc rendering. */
import {useState, useRef, useEffect} from 'react';
import {apiGet, apiGetFresh, apiPost, credential} from './api.js';
import {Icon, useDialog} from './parts.jsx';
import {ProgressRail} from './ui/index.js';
import {countOf, human, readerWord, topicLabel} from './model.js';
import {
  buildWorkbenchHash,
  buildWorkbenchPath,
  parseWorkbenchRoute,
} from './workbenchRoute.js';
import {
  focusNoteForBehaviour,
  focusPayloadForBehaviour,
  behaviourBriefLabel,
  runGenerateBatch,
} from './researchBatchCore.js';

export {focusNoteForBehaviour, focusPayloadForBehaviour, behaviourBriefLabel} from './researchBatchCore.js';

export {buildWorkbenchHash, buildWorkbenchPath, parseWorkbenchRoute} from './workbenchRoute.js';

const POLL_INTERVAL_MS = 1500;
const POLL_DEADLINE_MS = 95000;
const BATCH_POLL_DEADLINE_MS = 600000;
export const MONO_CAP = {
  fontFamily: 'var(--mono)', fontSize: '10px', letterSpacing: '0.16em',
  textTransform: 'uppercase', color: 'var(--faint)', fontWeight: 700,
};

const sleep = (ms, signal) => new Promise((resolve) => {
  if (signal && signal.aborted){ resolve(); return; }
  const id = setTimeout(resolve, ms);
  if (signal) signal.addEventListener('abort', () => { clearTimeout(id); resolve(); }, {once: true});
});

const abortError = () => { const e = new Error('aborted'); e.aborted = true; return e; };
export const WORKBENCH_NAV_EVENT = 'pulse-workbench-nav';

export function navigateWorkbench(state, opts){
  const next = buildWorkbenchHash(state);
  const replace = opts && opts.replace;
  if (window.location.hash === next) {
    window.dispatchEvent(new CustomEvent(WORKBENCH_NAV_EVENT, {detail: parseWorkbenchRoute(next)}));
    return;
  }
  if (replace) {
    window.history.replaceState(null, '', next);
    window.dispatchEvent(new CustomEvent(WORKBENCH_NAV_EVENT, {detail: parseWorkbenchRoute(next)}));
  } else {
    window.location.hash = next;
  }
}

export function parseResearchUrl(){
  const out = parseWorkbenchRoute(window.location.hash || '#/research');
  return {artifactId: out.artifactId, personaId: out.personaId, markets: out.markets};
}

export function setResearchShareUrl(artifactId){
  if (!artifactId) return;
  navigateWorkbench({work: 'brief', artifactId}, {replace: true});
}

export function setResearchPersonaUrl(personaId, markets){
  navigateWorkbench({work: 'brief', personaId, markets}, {replace: true});
}

export function clearResearchQuery(){
  navigateWorkbench({work: 'brief'}, {replace: true});
}

export function buildResearchDeepLink(personaId, markets){
  return buildWorkbenchPath({
    work: 'brief',
    personaId,
    markets: markets || [],
  });
}

export function researchShareUrl(artifactId){
  return window.location.origin + window.location.pathname
    + buildWorkbenchHash({work: 'brief', artifactId});
}

export function evidencePackUrl(artifactId){
  if (!artifactId) return '';
  return window.location.origin + '/api/research/' + encodeURIComponent(artifactId) + '/evidence-pack.html';
}

async function pollResearchJob(jobId, signal, onProgress, deadlineMs){
  const base = '/api/research/status?job_id=' + encodeURIComponent(jobId);
  const deadline = Date.now() + (deadlineMs || POLL_DEADLINE_MS);
  while (Date.now() < deadline){
    if (signal && signal.aborted) throw abortError();
    await sleep(POLL_INTERVAL_MS, signal);
    if (signal && signal.aborted) throw abortError();
    let j = null;
    try { j = await apiGetFresh(base + '&_=' + Date.now(), signal); }
    catch (e){ if (e && e.auth) throw e; if (signal && signal.aborted) throw abortError(); continue; }
    if (j && j.pending) {
      if (onProgress) onProgress(j);
      continue;
    }
    if (j && onProgress) onProgress(j);
    if (j && j.status === 'failed') throw new Error(j.message || j.error || 'Research generation failed.');
    if (j && ['completed', 'completed_degraded'].includes(j.status)) return j;
    if (j && j.error) throw new Error(j.error);
  }
  throw new Error('Research is taking too long. Try again in a moment.');
}

export async function generateResearch(personaId, markets, productFrame, signal, onProgress, focus, parentArtifactId){
  const body = {
    persona_id: personaId,
    product_frame: normalizeResearchProductFrame(productFrame),
  };
  if (markets && markets.length) body.markets = markets;
  if (focus && focus.queryGroups && focus.queryGroups.length) body.focus_query_groups = focus.queryGroups;
  if (focus && focus.note) body.focus_note = focus.note;
  if (focus && focus.examples && focus.examples.length) body.focus_examples = focus.examples;
  if (parentArtifactId) body.parent_artifact_id = parentArtifactId;
  const start = await apiPost('/api/research/generate', body, signal);
  if (!start || !start.job_id) throw new Error('The engine did not start research.');
  return pollResearchJob(start.job_id, signal, (j) => {
    if (onProgress && j && j.status) onProgress(j.status);
  });
}

/** Server-side batch: one rate-limit slot, N briefs queued on the backend. */
export async function generateResearchBatch(personaId, productFrame, behaviours, signal, onBatchProgress){
  const body = {
    persona_id: personaId,
    product_frame: normalizeResearchProductFrame(productFrame),
    behaviours: (behaviours || []).map((b) => ({
      market: b.market,
      query_group: b.query_group,
      behaviour: b.behaviour,
      signal_topic: b.signal_topic,
      note: b.note || '',
      examples: b.examples || [],
    })),
  };
  const start = await apiPost('/api/research/generate-batch', body, signal);
  if (!start || !start.job_id) throw new Error('The engine did not start the batch.');
  const final = await pollResearchJob(start.job_id, signal, (j) => {
    if (!onBatchProgress || !j) return;
    const idx = typeof j.batch_index === 'number' ? j.batch_index : 0;
    const total = j.batch_total || (behaviours && behaviours.length) || 1;
    const row = behaviours && behaviours[idx];
    onBatchProgress({
      index: idx,
      total,
      behaviour: row,
      status: j.status || '',
      phase: j.pending ? 'running' : 'done',
      batch_label: j.batch_label,
    });
  }, BATCH_POLL_DEADLINE_MS);
  const rows = final.batch_results || [];
  const results = rows.map((r) => ({
    behaviour: r.behaviour,
    result: {
      status: r.status,
      artifact_id: r.artifact_id,
      doc: r.doc,
      signal_quality: r.signal_quality,
      evidence_graph: r.evidence_graph,
      degraded_reason: r.degraded_reason,
      message: r.message,
    },
  }));
  return {results, parentArtifactId: final.parent_artifact_id || final.artifact_id || null};
}

/** Consolidated multi-behaviour doc from approved scan rows. */
export async function generateConsolidatedResearch(personaId, productFrame, behaviours, markets, signal, onProgress, seoNotes){
  const body = {
    persona_id: personaId,
    product_frame: normalizeResearchProductFrame(productFrame),
    behaviours: (behaviours || []).map((b) => ({
      id: b.id,
      market: b.market,
      query_group: b.query_group,
      behaviour: b.behaviour,
      signal_topic: b.signal_topic,
      note: b.note || '',
      examples: b.examples || [],
      metric: b.metric || {},
    })),
  };
  if (markets && markets.length) body.markets = markets;
  if (seoNotes) body.seo_notes = seoNotes;
  const start = await apiPost('/api/research/generate-consolidated', body, signal);
  if (!start || !start.job_id) throw new Error('The engine did not start consolidated research.');
  return pollResearchJob(start.job_id, signal, (j) => {
    if (onProgress && j && j.status) onProgress(j.status);
  }, BATCH_POLL_DEADLINE_MS);
}

export async function refineResearch(artifactId, instruction, signal, onProgress){
  const j = await apiPost('/api/research/refine', {artifact_id: artifactId, instruction}, signal);
  if (j && j.job_id) return pollResearchJob(j.job_id, signal, onProgress);
  if (j && j.doc) return j;
  throw new Error('Refine did not return a doc.');
}

export async function loadArtifact(artifactId, signal){
  const row = await apiGetFresh('/api/research/' + encodeURIComponent(artifactId) + '?_=' + Date.now(), signal);
  if (!row || !row.artifact_id) throw new Error('Research doc not found.');
  return {
    status: 'completed',
    doc: row.doc,
    artifact_id: row.artifact_id,
    signal_quality: row.signal_quality,
    evidence_graph: row.evidence_graph,
    degraded_reason: row.degraded_reason || '',
    persona_id: row.persona_id,
    persona_label: row.persona_label,
    markets: row.markets,
  };
}

export function normalizeDoc(payload){
  const doc = payload && payload.doc ? payload.doc : payload;
  const rawJson = doc && doc.json ? doc.json : (doc || {});
  const json = migrateResearchDoc(rawJson);
  const markdown = doc && doc.markdown ? doc.markdown : '';
  let sources = (doc && doc.sources) || json.sources || json._refs || payload.sources || [];
  if (!sources.length) {
    const eg = payload.evidence_graph || {};
    sources = eg.refs || eg.evidence || payload.evidence || [];
  }
  return {json, markdown, sources: Array.isArray(sources) ? sources : []};
}

/** Map legacy artifact JSON and legacy synth fields into the standard section ladder. */
export function migrateResearchDoc(json){
  if (!json || typeof json !== 'object') return json || {};
  const out = {...json};
  if (!out.grounded || typeof out.grounded !== 'object') out.grounded = {};
  const g = {...out.grounded};
  if (Object.prototype.hasOwnProperty.call(g, 'voice_of_genz')){
    const legacyVoice = g.voice_of_genz;
    delete g.voice_of_genz;
    if (!g.voice_read || !g.voice_read.length) g.voice_read = legacyVoice;
  }
  if ((!g.user_insights || !g.user_insights.length) && g.behavioural_synthesis && g.behavioural_synthesis.length){
    g.user_insights = g.behavioural_synthesis;
  }
  if ((!g.user_insights || !g.user_insights.length) && out.behavioural_synthesis && out.behavioural_synthesis.length){
    g.user_insights = out.behavioural_synthesis;
  }
  out.grounded = g;
  if (!out.inference || typeof out.inference !== 'object') out.inference = out.inference || {};
  const inf = {...out.inference};
  if (!inf.goal && (inf.positioning_frame || out.positioning_frame)){
    inf.goal = inf.positioning_frame || out.positioning_frame;
  }
  out.inference = inf;
  return out;
}

function dedupeRefIndices(nums){
  const seen = new Set();
  return nums.filter((n) => {
    if (!Number.isFinite(n) || n < 1 || seen.has(n)) return false;
    seen.add(n);
    return true;
  });
}

function sourceAt(sources, idx){
  const n = Number(idx);
  if (!Number.isFinite(n) || n < 1) return null;
  return sources[n - 1] || sources.find((s) => s.index === n || s.ref_index === n) || null;
}

function sourceSnippet(source){
  if (!source) return 'No snippet recorded.';
  if (Array.isArray(source.detail_lines) && source.detail_lines.length){
    const lines = source.detail_lines.map((line) => String(line || '').trim()).filter(Boolean);
    if (lines.length) return lines.join(' · ');
  }
  return source.snippet || source.excerpt || source.text || source.title || source.name
    || source.trend_synthesis || source.cultural_context || source.headline || source.behaviour
    || 'No snippet recorded.';
}

function citationMetricLines(source){
  if (!source || !source.metrics || typeof source.metrics !== 'object') return [];
  if (classifyEvidenceTier(source) === 'voice'){
    const line = postMetricsLine(source);
    return line ? [line] : [];
  }
  return Object.entries(source.metrics)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => {
      const label = String(k).replace(/_/g, ' ');
      return label + ': ' + (typeof v === 'number' && String(k).includes('pct') ? v + '%' : String(v));
    });
}

function citationBodySnippet(source){
  if (!source) return 'No snippet recorded.';
  const tier = classifyEvidenceTier(source);
  if (tier === 'engine') return engineEvidenceSnippet(source);
  if (tier === 'voice') return String(source.text || sourceSnippet(source));
  if (Array.isArray(source.detail_lines) && source.detail_lines.length){
    return source.detail_lines.map((line) => String(line || '').trim()).filter(Boolean).join('\n');
  }
  return sourceSnippet(source);
}

const ENGINE_REF_TYPES = new Set(['brief', 'digest', 'seed']);
const VOICE_REF_TYPES = new Set(['post', 'comment']);
const SEARCH_REF_TYPES = new Set(['google_trends_rising', 'lexicon']);

function evidenceRefIndex(source, fallback){
  if (source && source.ref_id){
    const n = parseInt(String(source.ref_id).replace(/^ref_/, ''), 10);
    if (Number.isFinite(n) && n > 0) return n;
  }
  return fallback;
}

export function classifyEvidenceTier(source){
  const t = String((source && (source.ref_type || source.type)) || '').toLowerCase();
  if (ENGINE_REF_TYPES.has(t)) return 'engine';
  if (VOICE_REF_TYPES.has(t)) return 'voice';
  if (String((source && source.voice_kind) || '').toLowerCase() === 'comment') return 'voice';
  if (SEARCH_REF_TYPES.has(t) || t.includes('search') || t === 'lexicon') return 'search';
  return 'engine';
}

function engineEvidenceSnippet(source){
  if (!source) return 'No engine signal recorded.';
  const parts = [];
  if (source.behaviour) parts.push(String(source.behaviour));
  if (source.headline) parts.push(String(source.headline));
  const syn = source.trend_synthesis || source.cultural_context || source.the_shift || '';
  if (syn) parts.push(String(syn).slice(0, 180));
  if (!parts.length && source.query_group) parts.push(topicLabel(source.query_group) + ' desk signal');
  if (typeof source.trend_score === 'number') parts.push('score ' + source.trend_score.toFixed(2));
  if (typeof source.item_count === 'number') parts.push(source.item_count + ' tracked items');
  if (!parts.length) return sourceSnippet(source);
  return parts.join(' · ');
}

function voiceEvidenceSnippet(source){
  if (!source) return 'No post text recorded.';
  return String(source.text || sourceSnippet(source));
}

/* Per-post metrics line: "12.3k likes · 840 shares · 2.1M views · 96 comments".
   Reads the ref's metrics dict (granular counters from enriched_content);
   falls back to engagement_total when no counter survived. */
export function postMetricsLine(source){
  if (!source || typeof source !== 'object') return '';
  const metrics = (source.metrics && typeof source.metrics === 'object') ? source.metrics : {};
  const bits = [];
  [['likes', 'likes'], ['shares', 'shares'], ['views', 'views'], ['comments', 'comments']].forEach(([key, label]) => {
    const v = Number(metrics[key]);
    if (Number.isFinite(v) && v > 0) bits.push(human(v) + ' ' + label);
  });
  if (bits.length) return bits.join(' · ');
  const eng = Number(metrics.engagement_total ?? source.engagement_total ?? source.engagement);
  if (Number.isFinite(eng) && eng > 0) return human(eng) + ' engagement';
  return '';
}

function topicTag(source){
  return source && source.query_group ? topicLabel(source.query_group) : '';
}

const HOLLOW_SNIPPETS = new Set([
  'No snippet recorded.',
  'No engine signal recorded.',
  'No post text recorded.',
]);

function hasUsableSnippet(text){
  const s = String(text || '').trim();
  return !!s && !HOLLOW_SNIPPETS.has(s);
}

export function buildEvidenceSections(sources){
  const engine = [];
  const voice = [];
  const search = [];
  (sources || []).forEach((s, i) => {
    if (!s || typeof s !== 'object') return;
    const index = evidenceRefIndex(s, i + 1);
    const tier = classifyEvidenceTier(s);
    const base = {
      index,
      source: s,
      market: s.market || '',
      refType: s.ref_type || s.type || '',
      topic: topicTag(s),
    };
    if (tier === 'engine'){
      const snippet = engineEvidenceSnippet(s);
      if (!hasUsableSnippet(snippet)) return;
      engine.push({...base, snippet, title: s.headline || s.behaviour || base.topic || 'Engine signal'});
    } else if (tier === 'voice'){
      const rt = String(s.ref_type || s.type || '').toLowerCase();
      const isComment = rt === 'comment' || String(s.voice_kind || '').toLowerCase() === 'comment';
      const threadComments = !isComment && Array.isArray(s.thread_comments) ? s.thread_comments : [];
      const snippet = voiceEvidenceSnippet(s);
      if (!hasUsableSnippet(snippet)) return;
      voice.push({
        ...base,
        snippet,
        platform: s.platform || '',
        engagement: s.engagement_total,
        handle: s.author_handle || s.handle || '',
        isComment,
        threadSize: threadComments.length,
        threadComments,
      });
    } else {
      const snippet = sourceSnippet(s);
      if (!hasUsableSnippet(snippet)) return;
      search.push({...base, snippet, title: s.headline || s.name || s.title || base.topic || 'Search signal'});
    }
  });
  return {
    engine,
    voice,
    search,
    counts: {engine: engine.length, voice: voice.length, search: search.length},
  };
}

function formatReceipt(receipt){
  if (!receipt) return '';
  if (typeof receipt === 'string') return receipt;
  if (typeof receipt === 'object') {
    return Object.entries(receipt)
      .filter(([, v]) => v !== null && v !== undefined && v !== '')
      .map(([k, v]) => k + ': ' + (typeof v === 'object' ? JSON.stringify(v) : String(v)))
      .join('\n');
  }
  return String(receipt);
}

function evidenceLabel(source){
  if (!source) return 'Evidence';
  if (source.label) return String(source.label);
  if (source.topic) return String(source.topic);
  if (source.query_group) return topicLabel(source.query_group);
  return source.headline || source.title || source.name || source.ref_type || 'Evidence';
}

function citationKind(source){
  const tier = classifyEvidenceTier(source);
  if (tier === 'engine') return 'Engine signal';
  if (tier === 'voice') return 'Voice proof';
  if (tier === 'search') return 'Search & reach';
  const raw = String((source && (source.type || source.ref_type)) || '').toLowerCase();
  if (raw === 'brief') return 'Evidence brief';
  if (raw === 'post') return 'Creator post';
  if (raw.includes('search')) return 'Search signal';
  return raw ? raw.charAt(0).toUpperCase() + raw.slice(1) : 'Evidence';
}

function citationTitle(source){
  const label = evidenceLabel(source);
  if (!label || /^brief$/i.test(label) || /^evidence$/i.test(label)) {
    return 'Signal used in this brief';
  }
  return label;
}

function citationSupportLine(source){
  if (!source) return 'This source supports a cited claim in the research doc.';
  const qg = source.query_group ? topicLabel(source.query_group) : '';
  const market = source.market ? String(source.market).toUpperCase() : '';
  const type = citationKind(source).toLowerCase();
  const parts = [];
  if (market) parts.push(market);
  if (qg) parts.push(qg);
  if (type) parts.push(type);
  if (!parts.length) return 'This source supports a cited claim in the research doc.';
  return 'Used as supporting evidence for ' + parts.join(' · ') + '.';
}

function matchLabel(score){
  if (typeof score !== 'number') return '';
  if (score >= 1) return 'Strong source match';
  if (score >= 0.7) return 'Good source match';
  return 'Supporting source';
}

export function extractPrompts(json){
  const out = [];
  const act = json.activation || {};
  (act.google_prompts || []).forEach((p) => {
    const mk = String(p.market || '').toUpperCase();
    const prompt = p.prompt || p.google_prompt || '';
    if (prompt) out.push((mk ? mk + ': ' : '') + prompt);
  });
  const markets = act.markets || json.per_market_actions || json.market_actions || [];
  (Array.isArray(markets) ? markets : []).forEach((m) => {
    const mk = (m.market || m.region || '').toUpperCase();
    const prompt = m.google_prompt || m.prompt || m.google_ai_mode_prompt || '';
    const angle = m.angle || m.label || '';
    if (prompt) out.push((mk ? mk + ': ' : '') + (angle ? angle + '\n' : '') + prompt);
  });
  return out.join('\n\n');
}

function addEvidenceUse(map, sources, idx, section, behaviour){
  const n = Number(idx);
  if (!Number.isFinite(n) || n < 1) return;
  const source = sourceAt(sources, n);
  if (!source) return;
  const key = String(n);
  const existing = map.get(key) || {
    index: n,
    source,
    usedIn: new Set(),
    behaviours: new Set(),
  };
  existing.usedIn.add(section);
  if (behaviour) existing.behaviours.add(behaviour);
  map.set(key, existing);
}

export function buildEvidenceBank(json, sources){
  const doc = migrateResearchDoc(json);
  const map = new Map();
  const grounded = doc.grounded || {};
  const objective = grounded.objective || {};
  (objective.evidence_indices || []).forEach((idx) => addEvidenceUse(map, sources, idx, 'Objective', 'Market opportunity'));
  (grounded.user_insights || grounded.behavioural_synthesis || []).forEach((item) => {
    const text = typeof item === 'string' ? item : (item.text || item.body || '');
    const behaviour = text.split(/[.;]/)[0].slice(0, 90) || 'User behaviour';
    ((item && item.evidence_indices) || []).forEach((idx) => addEvidenceUse(map, sources, idx, 'Know the user', behaviour));
  });
  const act = doc.activation || {};
  (act.google_prompts || []).forEach((item) => {
    const behaviour = item.behaviour_trigger || item.angle || item.market || 'Activation signal';
    (item.evidence_indices || []).forEach((idx) => addEvidenceUse(map, sources, idx, 'Activation', behaviour));
  });
  (sources || []).forEach((source, i) => {
    const index = i + 1;
    const key = String(index);
    if (!map.has(key)) {
      map.set(key, {
        index,
        source,
        usedIn: new Set(),
        behaviours: new Set(),
      });
    }
  });
  return Array.from(map.values())
    .sort((a, b) => a.index - b.index)
    .map((item) => {
      const s = item.source || {};
      return {
        index: item.index,
        market: s.market || '',
        type: s.ref_type || s.type || 'evidence',
        label: evidenceLabel(s),
        snippet: sourceSnippet(s),
        usedIn: Array.from(item.usedIn),
        behaviours: Array.from(item.behaviours),
        confidence: s.relevance_score,
        source: s,
      };
    });
}

export function progressLabel(status){
  if (status === 'starting') return 'Starting research';
  if (status === 'gathering_seeds') return 'Loading initial observations';
  if (status === 'gathering_bq') return 'Loading engine evidence';
  if (status === 'gathering') return 'Gathering signal across markets';
  if (status === 'synthesizing') return 'Synthesising evidence into doc';
  if (status === 'completed_degraded') return 'Doc ready (degraded)';
  if (status === 'completed') return 'Doc ready';
  if (status === 'failed') return 'Generation failed';
  if (String(status).startsWith('gathering_behaviour_')) return 'Reading market behaviours';
  return 'Starting research';
}

export const PROGRESS_STEPS = [
  {key: 'gathering_seeds', label: 'Initial observations'},
  {key: 'gathering_bq', label: 'Engine evidence'},
  {key: 'synthesizing', label: 'Editorial synthesis'},
];

export function progressStepIndex(status){
  const order = PROGRESS_STEPS.map((s) => s.key);
  const idx = order.indexOf(status);
  if (idx >= 0) return idx;
  if (status === 'gathering') return 1;
  if (['completed', 'completed_degraded'].includes(status)) return order.length;
  if (status === 'starting') return -1;
  if (String(status).startsWith('gathering_behaviour_')) return 1;
  return -1;
}

/* Honest status-to-percent milestones, campaign mapping (v3-design-system.md
   section 4). The bar only ever moves on a real status change from
   pollResearchJob, never on a timer. 'starting' is the client-side gap
   between the 202 accept and the first real poll stage. 'gathering' (legacy
   alias and refine) sits at the same milestone as gathering_bq. Any
   gathering_behaviour_* status (per-behaviour scan sub-states) is checked via
   prefix AFTER the exact-match cases and BEFORE the default. 'failed' and any
   unrecognised status floor to 0 so a failed or unknown job never reads as
   progress. */
export function progressPercent(status){
  switch (status){
    case 'starting': return 5;
    case 'gathering_seeds': return 25;
    case 'gathering_bq': return 50;
    case 'gathering': return 50;
    case 'synthesizing': return 90;
    case 'completed': return 100;
    case 'completed_degraded': return 100;
    case 'failed': return 0;
    default: break;
  }
  if (String(status).startsWith('gathering_behaviour_')) return 50;
  return 0;
}

export function marketOutcomeHint(markets){
  const mk = (markets || []).map((m) => String(m).toLowerCase()).filter(Boolean);
  if (!mk.length) return 'Pick at least one market to scope the brief.';
  if (mk.length === 1) {
    const code = mk[0].toUpperCase();
    return `Doc focuses on ${code} signal in a single-market brief.`;
  }
  const labels = mk.map((m) => m.toUpperCase()).join(', ');
  return `Doc will compare and contrast ${labels} in one brief.`;
}

export function normalizeResearchProductFrame(value){
  return typeof value === 'string' ? value.trim() : '';
}

export function outcomePreview(productFrame, markets){
  const {headline, lanes} = outcomePreviewSections(productFrame, markets);
  const laneText = lanes.map((l) => l.items.join(', ')).join(' · ');
  return headline + ': ' + laneText;
}

export function outcomePreviewSections(productFrame, markets){
  const frame = normalizeResearchProductFrame(productFrame);
  const mk = (markets || []).map((m) => String(m).toUpperCase()).filter(Boolean);
  const marketText = mk.length ? mk.join(' · ') : 'Selected markets';
  return {
    headline: 'Research plan for ' + marketText,
    lanes: [
      {
        id: 'signal',
        label: 'Signal',
        title: 'Establish what is observed',
        summary: 'Will test the objective, observed behaviour, market cues and available proof.',
        items: ['Objective', 'Know the user', 'Evidence bank'],
      },
      {
        id: 'inference',
        label: 'Strategic read',
        title: 'Test the strategic read',
        summary: 'Will examine the tension, goal, product role and desired shift.',
        items: ['Tension frame', 'Goal', 'Product magic', 'Get to by'],
      },
      {
        id: 'activation',
        label: 'Possible response',
        title: frame ? 'Test the role of ' + frame : 'Test for a credible brand role',
        summary: 'Will test user friction, brand permission, execution and available proof.',
        items: ['User friction', 'Brand role', 'Creative execution'],
      },
    ],
  };
}

export function framePresetActive(productFrame, presetValue){
  const a = (productFrame || '').trim().toLowerCase();
  const b = (presetValue || '').trim().toLowerCase();
  if (!a || !b) return false;
  return a === b;
}

function InferenceBadge({label}){
  return (
    <span className="research-inference-badge">{label || 'Strategic read · inference'}</span>
  );
}

function RefButton({n, onClick}){
  return (
    <button type="button" className="research-ref-btn" onClick={() => onClick(n)} aria-label={'Citation ' + n}>{n}</button>
  );
}

function renderWithRefs(text, sources, onRef){
  const s = String(text || '');
  if (!s) return null;
  const parts = [];
  const re = /\[(\d+(?:,\s*\d+)*)\]/g;
  let last = 0;
  let m;
  let k = 0;
  while ((m = re.exec(s))){
    if (m.index > last) parts.push(s.slice(last, m.index));
    const nums = dedupeRefIndices(m[1].split(',').map((x) => parseInt(x.trim(), 10)).filter((n) => n > 0));
    nums.forEach((n, i) => {
      parts.push(<RefButton key={'r' + k++} n={n} onClick={onRef} />);
      if (i < nums.length - 1) parts.push(', ');
    });
    last = m.index + m[0].length;
  }
  if (last < s.length) parts.push(s.slice(last));
  if (!parts.length) return s;
  return parts;
}

function SynthesisBlock({items, sources, onRef}){
  const list = Array.isArray(items) ? items : [];
  if (!list.length) return null;
  return (
    <div className="research-synthesis">
      {list.map((item, i) => {
        const text = typeof item === 'string' ? item : (item.text || item.body || '');
        const refs = dedupeRefIndices((item && item.evidence_indices) || []);
        return (
          <p key={i} className="research-synthesis-p">
            {renderWithRefs(text, sources, onRef)}
            {refs.map((n) => <RefButton key={'e' + n} n={n} onClick={onRef} />)}
          </p>
        );
      })}
    </div>
  );
}

export function CitationDrawer({refIndex, source, onClose}){
  const drawerRef = useRef(null);
  useDialog(drawerRef, !!source);
  useEffect(() => {
    if (!source) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') onClose && onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [source, onClose]);
  if (!source) return null;
  const score = source.relevance_score;
  const receipt = formatReceipt(source.receipt);
  const detailLines = Array.isArray(source.detail_lines)
    ? source.detail_lines.map((line) => String(line || '').trim()).filter(Boolean)
    : [];
  const metricLines = citationMetricLines(source);
  const bodySnippet = citationBodySnippet(source);
  const tier = classifyEvidenceTier(source);
  const rt = String((source.ref_type || source.type) || '').toLowerCase();
  const isStandaloneComment = rt === 'comment' || String(source.voice_kind || '').toLowerCase() === 'comment';
  const threadComments = !isStandaloneComment && Array.isArray(source.thread_comments) ? source.thread_comments : [];
  return (
    <>
      <div className="scrim show settled" onClick={onClose} style={{zIndex: 195}} />
      <aside ref={drawerRef} className="panel research-citation-panel show settled" role="dialog" aria-modal="true" aria-labelledby="cite-title"
        style={{zIndex: 200, width: 'min(420px, 100vw)'}}>
        <div className="panel-head">
          <div className="top">
            <div className="panel-region">Citation {refIndex} · {citationKind(source)}</div>
            <button className="panel-close" aria-label="Close citation" onClick={onClose}><Icon.close /></button>
          </div>
          <h2 className="panel-title" id="cite-title">{citationTitle(source)}</h2>
          <div className="panel-label">
            {source.market ? String(source.market).toUpperCase() + ' desk' : 'Source evidence'}
          </div>
        </div>
        <div className="panel-body">
          <div className="research-citation-support">
            <span>What this supports</span>
            <p>{citationSupportLine(source)}</p>
          </div>
          {(source.label || source.topic || source.query_group) && (
            <div className="research-citation-topic">
              {source.label || source.topic || topicLabel(source.query_group)}
            </div>
          )}
          <div className="research-citation-k">
            {tier === 'engine' ? 'Engine intelligence' : isStandaloneComment ? 'Standalone comment' : 'Post excerpt'}
          </div>
          <p className="research-citation-snippet" style={detailLines.length > 1 ? {whiteSpace: 'pre-line'} : undefined}>
            {bodySnippet}
          </p>
          {detailLines.length > 0 && tier !== 'search' && (
            <div className="research-citation-detail">
              {detailLines.map((line, i) => (
                <p key={i} className="research-citation-detail-line">{line}</p>
              ))}
            </div>
          )}
          {metricLines.length > 0 && (
            <div className="research-citation-metrics">
              <span>Metrics</span>
              {metricLines.map((line, i) => (
                <p key={i}>{line}</p>
              ))}
            </div>
          )}
          {tier === 'voice' && !isStandaloneComment && (source.platform || source.engagement_total || source.author_handle) && (
            <div className="research-citation-voice-meta">
              {[source.platform, source.author_handle ? '@' + String(source.author_handle).replace(/^@/, '') : '', postMetricsLine(source)].filter(Boolean).join(' · ')}
            </div>
          )}
          {threadComments.length > 0 && (
            <section className="research-citation-thread" aria-labelledby="cite-thread-title">
              <h3 className="research-citation-thread-title" id="cite-thread-title">
                Comments on this post ({threadComments.length})
              </h3>
              <ol className="research-citation-thread-list">
                {threadComments.map((c, i) => (
                  <li key={i} className="research-citation-thread-item">
                    <div className="research-citation-thread-meta">
                      {[c.platform || source.platform, c.handle ? '@' + String(c.handle).replace(/^@/, '') : '', c.engagement != null ? c.engagement + ' engagement' : ''].filter(Boolean).join(' · ')}
                    </div>
                    <p className="research-citation-thread-text">{c.text || ''}</p>
                  </li>
                ))}
              </ol>
            </section>
          )}
          {isStandaloneComment && (
            <div className="research-citation-standalone-k">Comment without a parent post in this evidence pull.</div>
          )}
          {receipt && (
            <div className="research-receipt">
              <div style={MONO_CAP}>Receipt</div>
              <div className="research-receipt-body">{receipt}</div>
            </div>
          )}
          {typeof score === 'number' && (
            <div className="research-citation-score">
              {matchLabel(score)}
            </div>
          )}
        </div>
      </aside>
    </>
  );
}

export function LabeledCopyBtn({text, label, className}){
  const [done, setDone] = useState(false);
  const [failed, setFailed] = useState(false);
  const copy = () => {
    if (!text) return;
    if (!navigator.clipboard) {
      setFailed(true);
      setTimeout(() => setFailed(false), 2000);
      return;
    }
    navigator.clipboard.writeText(text)
      .then(() => {
        setFailed(false);
        setDone(true);
        setTimeout(() => setDone(false), 1600);
      })
      .catch(() => {
        setDone(false);
        setFailed(true);
        setTimeout(() => setFailed(false), 2200);
      });
  };
  return (
    <button type="button" className={'research-copy-btn' + (done ? ' done' : '') + (failed ? ' failed' : '') + (className ? ' ' + className : '')} onClick={copy} disabled={!text} aria-live="polite">
      {failed ? 'Copy failed' : done ? 'Copied' : label}
    </button>
  );
}

export function EvidenceBank({sources, onRef}){
  const sections = buildEvidenceSections(sources);
  const {engine, voice, search, counts} = sections;
  const total = counts.engine + counts.voice + counts.search;
  if (!total) return null;

  const badge = countOf(total, 'source') + ' · ' + counts.engine + ' engine · ' + counts.voice + ' voice · ' + counts.search + ' search';

  const renderEngineCard = (item) => (
    <button key={'e' + item.index} type="button" className="research-evidence-card research-evidence-card-engine" onClick={() => onRef(item.index)}>
      <span className="research-evidence-k">Engine signal</span>
      <span className="research-evidence-title">{item.title}</span>
      <span className="research-evidence-snippet">{item.snippet}</span>
      <span className="research-evidence-meta">
        {item.market ? String(item.market).toUpperCase() + ' · ' : ''}{readerWord(item.refType)}
        {item.topic ? ' · ' + item.topic : ''}
      </span>
    </button>
  );

  const renderVoiceCard = (item) => {
    const isComment = !!item.isComment;
    const kindLabel = isComment ? 'Standalone comment' : (item.threadSize > 0 ? 'Post · ' + item.threadSize + ' comments' : 'Post');
    const metaBits = [readerWord(item.platform), item.handle ? '@' + item.handle : '', postMetricsLine(item.source) || (item.engagement != null ? human(item.engagement) + ' engagement' : ''), item.topic].filter(Boolean);
    return (
    <button key={'v' + item.index} type="button" className={'research-evidence-card research-evidence-card-voice' + (isComment ? ' research-evidence-card-orphan' : ' research-evidence-card-thread')} onClick={() => onRef(item.index)}>
      <span className="research-evidence-k">Voice proof · {kindLabel}</span>
      <span className="research-evidence-snippet">{item.snippet}</span>
      {!isComment && item.threadSize > 0 && item.threadComments && item.threadComments.length > 0 && (
        <span className="research-evidence-thread-preview">
          {item.threadComments.slice(0, 2).map((c, i) => (
            <span key={i} className="research-evidence-thread-line">{c.text || ''}</span>
          ))}
        </span>
      )}
      <span className="research-evidence-meta">
        {metaBits.join(' · ')}
        {isComment && !(item.source && item.source.url) ? ' · no public link' : ''}
      </span>
    </button>
    );
  };

  const voicePosts = voice.filter((item) => !item.isComment);
  const voiceComments = voice.filter((item) => item.isComment);

  const renderSearchCard = (item) => (
    <button key={'s' + item.index} type="button" className="research-evidence-card research-evidence-card-search" onClick={() => onRef(item.index)}>
      <span className="research-evidence-k">Search &amp; reach</span>
      <span className="research-evidence-title">{item.title}</span>
      <span className="research-evidence-snippet">{item.snippet}</span>
      <span className="research-evidence-meta">
        {item.market ? String(item.market).toUpperCase() + ' · ' : ''}{readerWord(item.refType)}
      </span>
    </button>
  );

  return (
    <section className="research-evidence-bank" aria-labelledby="evidence-bank-title">
      <div className="research-section-head">
        <span className="research-lane-tag">Sources</span>
        <h3 className="research-section-title" id="evidence-bank-title">Evidence sources</h3>
        <span className="research-evidence-mix-badge">{badge}</span>
      </div>
      <p className="research-evidence-bank-intro">
        Engine signal drives the brief. Voice posts open with the conversation underneath when BQ has comment rows for that receipt.
      </p>
      {engine.length > 0 && (
        <div className="research-evidence-group">
          <h4 className="research-evidence-group-title">Engine signal</h4>
          <div className="research-evidence-grid">{engine.map(renderEngineCard)}</div>
        </div>
      )}
      {voice.length > 0 && (
        <div className="research-evidence-group">
          <h4 className="research-evidence-group-title">Voice proof</h4>
          {voicePosts.length > 0 && (
            <>
              <p className="research-evidence-subhead">Posts</p>
              <div className="research-evidence-grid">{voicePosts.map(renderVoiceCard)}</div>
            </>
          )}
          {voiceComments.length > 0 && (
            <>
              <p className="research-evidence-subhead">Comments</p>
              <div className="research-evidence-grid">{voiceComments.map(renderVoiceCard)}</div>
            </>
          )}
        </div>
      )}
      {search.length > 0 && (
        <div className="research-evidence-group">
          <h4 className="research-evidence-group-title">Search &amp; reach</h4>
          <div className="research-evidence-grid">{search.map(renderSearchCard)}</div>
        </div>
      )}
    </section>
  );
}

export function ResearchProgressSteps({status, focus = false}){
  return (
    <div className="research-progress">
      <ProgressRail
        status={status}
        steps={PROGRESS_STEPS}
        percent={progressPercent(status)}
        label={progressLabel(status)}
        focus={focus}
      />
    </div>
  );
}

/* Per-item rail state for the batch list. The batch API (generateResearchBatch)
   only ever exposes a fine status (gathering_bq etc.) on the ONE running item,
   via onBatchProgress; queued items carry no status and finished items are
   reported by phase alone. So derive the rail from phase first, then refine the
   running item with its own status when the backend gave one. An errored item
   reuses the completed_degraded treatment (100% + degraded badge) so it reads as
   distinct-but-finished, and the row keeps its explicit "Failed" label. */
function batchItemRail(job){
  const phase = job && job.phase;
  if (phase === 'done') return {status: 'completed', percent: 100};
  if (phase === 'error') return {status: 'completed_degraded', percent: 100};
  if (phase === 'running'){
    const s = job && job.status;
    if (s && progressPercent(s) > 0) return {status: s, percent: progressPercent(s)};
    return {status: 'starting', percent: progressPercent('starting')};
  }
  return {status: '', percent: 0}; // queued
}

/* Batch theatre: the single-doc path gets ProgressRail's focus mode (a raised
   surface plus a fixed dim layer behind it, see ui/ProgressRail.jsx and
   ui.css's .ui-progress-rail--focus / .ui-progress-rail-dim). Batch generation
   is a list, not one rail, so the equivalent lift happens at the container
   level: research-batch--live carries the same raised-surface treatment
   (surface bg, shadow-lg, padding) while any item is running, and the caller
   (ResearchDocPanel) wraps it with the same dim-sibling layer used for the
   single-doc focus view. Per-item rails stay unfocused (no focus prop) so
   only the list as a whole gets the theatre framing, not every row. */
export function ResearchBatchProgress({jobs}){
  const list = Array.isArray(jobs) ? jobs : [];
  if (!list.length) return null;
  const runningIdx = list.findIndex((j) => j.phase === 'running');
  const doneCount = list.filter((j) => j.phase === 'done' || j.phase === 'error').length;
  const activeIdx = runningIdx >= 0 ? runningIdx : Math.min(doneCount, list.length - 1);
  const activeJob = list[activeIdx] || {};
  const activeLabel = behaviourBriefLabel(activeJob.behaviour || {}, activeIdx);
  const headline = list.length === 1
    ? 'Generating brief: ' + activeLabel
    : 'Brief ' + (activeIdx + 1) + ' of ' + list.length + ': ' + activeLabel;
  const live = runningIdx >= 0;
  return (
    <div className={'research-batch-progress' + (live ? ' research-batch--live' : '')} role="status" aria-live="polite">
      {live && <div className="ui-progress-rail-dim" aria-hidden="true" />}
      <p className="research-progress-label">{headline}</p>
      <p className="research-batch-sub">{progressLabel(activeJob.status || 'gathering_seeds')}</p>
      <ol className="research-batch-list">
        {list.map((job, i) => {
          const label = behaviourBriefLabel(job.behaviour || {}, i);
          const done = job.phase === 'done' || job.phase === 'error';
          const failed = job.phase === 'error';
          const current = i === activeIdx && !done;
          const running = job.phase === 'running' || current;
          const rail = batchItemRail(running ? {...job, phase: 'running'} : job);
          const statusText = failed ? 'Failed' : done ? 'Ready' : running ? progressLabel(job.status || 'gathering') : 'Queued';
          return (
            <li key={(job.behaviour && job.behaviour.id) || i} className={'research-batch-item' + (done ? ' done' : '') + (current ? ' current' : '') + (failed ? ' error' : '')}>
              <div className="research-batch-item-head">
                <span className="research-batch-label">{label}</span>
                <span className="research-batch-status">{statusText}</span>
              </div>
              {(running || done) && (
                <ProgressRail
                  status={rail.status}
                  steps={PROGRESS_STEPS}
                  percent={rail.percent}
                  label={statusText}
                />
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function saveHtmlDownload(blob, artifactId){
  const slug = (artifactId || 'brief').slice(0, 12);
  const objectUrl = URL.createObjectURL(blob);
  try {
    const a = document.createElement('a');
    a.href = objectUrl;
    a.download = '42-brief-' + slug + '.html';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

async function fetchResearchHtmlResponse(url, init){
  const res = await credential.fetch(url, init);
  if (res.status === 401 && credential.refused(res)) {
    const e = new Error('Passcode required');
    e.auth = true;
    throw e;
  }
  if (!res.ok) {
    throw new Error('Could not download this brief (' + res.status + ').');
  }
  return res;
}

/** Prefer in-memory doc (POST) so export works when the artifact cache has expired. */
export async function downloadResearchHtml(artifactId, opts){
  if (!artifactId && !(opts && opts.docJson)) return;
  const slug = (artifactId || 'brief').slice(0, 12);
  const sources = (opts && opts.sources) || [];
  const docPayload = {...((opts && opts.docJson) || {})};
  if (sources.length && !docPayload._refs) {
    docPayload._refs = sources;
  }
  let res;
  if (opts && opts.docJson) {
    res = await fetchResearchHtmlResponse('/api/research/export-html', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        doc_json: docPayload,
        persona_label: opts.personaLabel || '',
        markets: opts.markets || [],
        artifact_id: artifactId || '',
        sources,
      }),
    });
  } else {
    const url = '/api/research/' + encodeURIComponent(artifactId) + '/export.html';
    res = await fetchResearchHtmlResponse(url, {});
  }
  const blob = await res.blob();
  saveHtmlDownload(blob, slug);
}

export function OutcomePreviewCard({productFrame, markets}){
  const {headline, lanes} = outcomePreviewSections(productFrame, markets);
  return (
    <div className="research-outcome-card" role="region" aria-label="Research plan preview">
      <div className="research-outcome-top">
        <div>
          <div className="research-field-label">Brief blueprint</div>
          <p className="research-outcome-headline">{headline}</p>
        </div>
        <span className="research-outcome-badge">Planned structure · evidence required</span>
      </div>
      <div className="research-outcome-flow" aria-hidden="true">
        {lanes.map((lane, i) => (
          <span key={lane.id} className={'research-outcome-flow-dot research-outcome-flow-' + lane.id}>
            {String(i + 1).padStart(2, '0')}
          </span>
        ))}
      </div>
      <div className="research-outcome-lanes">
        {lanes.map((lane, i) => (
          <div key={lane.id} className={'research-outcome-lane research-outcome-lane-' + lane.id}>
            <span className="research-outcome-lane-k">{String(i + 1).padStart(2, '0')} · {lane.label}</span>
            <span className="research-outcome-lane-title">{lane.title}</span>
            <span className="research-outcome-lane-summary">{lane.summary}</span>
            <ul className="research-outcome-lane-items">
              {lane.items.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}

function formatQualityLabel(raw){
  const s = String(raw || '').trim();
  if (!s) return '';
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

export function ResearchDocToolbar({signalQuality, personaLabel, markets, markdown, promptsText, artifactId, onShare, onDownloadHtml, onCopyEvidencePack}){
  const mk = (markets || []).map((m) => String(m).toUpperCase()).filter(Boolean);
  return (
    <div className="research-doc-toolbar">
      <div className="research-doc-toolbar-meta">
        {signalQuality && (
          <span className={'research-quality-pill research-quality-' + String(signalQuality).toLowerCase()}>
            Signal quality · {formatQualityLabel(signalQuality)}
          </span>
        )}
        {personaLabel && <span className="research-doc-meta-item">{personaLabel}</span>}
        {mk.length > 0 && <span className="research-doc-meta-item">{mk.join(' · ')}</span>}
      </div>
      <div className="research-copy-toolbar">
        {markdown && <LabeledCopyBtn text={markdown} label="Markdown" />}
        {promptsText && <LabeledCopyBtn text={promptsText} label="Prompts" />}
        {artifactId && onCopyEvidencePack && (
          <LabeledCopyBtn text={evidencePackUrl(artifactId)} label="Evidence pack link" />
        )}
        {artifactId && onDownloadHtml && (
          <button type="button" className="research-copy-btn" onClick={onDownloadHtml}>Download HTML</button>
        )}
        {artifactId && (
          <button type="button" className="research-copy-btn" onClick={onShare}>Share link</button>
        )}
      </div>
    </div>
  );
}

export function ResearchRefineBar({artifactId, busy, onRefine}){
  const [q, setQ] = useState('');
  const chips = ['Sharper positioning', 'More KE signal', 'Tighter evidence path'];
  const go = (instruction) => {
    if (busy || !instruction.trim() || !artifactId) return;
    onRefine(instruction.trim());
    setQ('');
  };
  return (
    <div className="refine research-refine">
      <div className="dh">Refine this doc</div>
      <div className="refine-chips">
        {chips.map((c) => (
          <button key={c} className="legacy-chip" disabled={busy} onClick={() => go(c)}>{c}</button>
        ))}
      </div>
      <div className="refine-row">
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') go(q); }}
          placeholder="Tell the engine how to adjust this doc…" disabled={busy} />
        <button className="legacy-action" disabled={busy || !artifactId} onClick={() => go(q)}>{busy ? 'Refining…' : 'Refine'}</button>
      </div>
    </div>
  );
}

export function DocBody({json, markdown, sources, signalQuality, onRef}){
  const doc = migrateResearchDoc(json);
  const grounded = doc.grounded || {};
  const inference = doc.inference || {};
  const activation = doc.activation || {};
  const objective = grounded.objective;
  const userInsights = grounded.user_insights || grounded.behavioural_synthesis || json.behavioural_synthesis || json.synthesis || [];
  const positioning = inference.positioning_frame || json.positioning_frame;
  const posText = typeof positioning === 'string' ? positioning : (positioning && (positioning.text || positioning.frame));
  const getToBy = inference.get_to_by || {};

  const activationRows = [];
  const seen = new Set();
  const addAct = (entry) => {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return;
    const mk = String(entry.market || entry.region || '').toLowerCase();
    const prompt = entry.prompt || entry.google_prompt || entry.google_ai_mode_prompt || '';
    const key = mk + '|' + prompt;
    if (!prompt || seen.has(key)) return;
    seen.add(key);
    activationRows.push({
      market: entry.market || entry.region || mk,
      prompt,
      angle: entry.angle || entry.label || '',
      user_friction: entry.user_friction || '',
      ai_mode_role: entry.ai_mode_role || entry.product_role || '',
      product_role: entry.product_role || '',
      behaviour_trigger: entry.behaviour_trigger || '',
      creative_execution: entry.creative_execution || '',
      evidence_indices: dedupeRefIndices(entry.evidence_indices || []),
    });
  };
  const legacyPrompts = Array.isArray(activation.google_prompts) ? activation.google_prompts : [];
  const marketActions = [activation.markets, json.per_market_actions, json.market_actions]
    .find((items) => Array.isArray(items)) || [];
  legacyPrompts.forEach((p) => addAct(p));
  marketActions.forEach((m) => addAct(m));

  const hasObjective = objective && objective.text;
  const hasInference = inference.tension || inference.goal || posText || inference.product_magic
    || getToBy.get || getToBy.to || getToBy.by
    || inference.brand_safe_notes || inference.mandatories || inference.creative_judge;

  if (markdown && !hasObjective && !userInsights.length && !hasInference && !activationRows.length){
    const migrated = migrateResearchDoc(json);
    const mg = migrated.grounded || {};
    const mInf = migrated.inference || {};
    const mUser = mg.user_insights || mg.behavioural_synthesis || [];
    const mPos = mInf.goal || mInf.positioning_frame;
    const mAct = (migrated.activation && migrated.activation.google_prompts) || [];
    if (mUser.length || (mg.objective && mg.objective.text) || mInf.tension || mPos || mAct.length){
      return <DocBody json={migrated} markdown={markdown} sources={sources} signalQuality={signalQuality} onRef={onRef} />;
    }
    return <div className="research-markdown-fallback">{markdown}</div>;
  }

  const InferenceSection = ({kicker, badge, children}) => (
    <section className="research-inference-block research-lane-inference">
      <div className="research-inference-head">
        <h3 className="research-section-title research-section-title-inference">{kicker}</h3>
        {badge && <InferenceBadge label={badge} />}
      </div>
      <div className="research-prose">{children}</div>
    </section>
  );

  return (
    <div className="research-doc-body">
      {hasObjective && (
        <section className="research-section research-lane-signal">
          <div className="research-section-head">
            <span className="research-lane-tag">Signal</span>
            <h3 className="research-section-title">Objective</h3>
          </div>
          <p className="research-synthesis-p">
            {renderWithRefs(objective.text, sources, onRef)}
            {(dedupeRefIndices(objective.evidence_indices || [])).map((n) => <RefButton key={'o' + n} n={n} onClick={onRef} />)}
          </p>
        </section>
      )}
      {userInsights.length > 0 && (
        <section className="research-section research-lane-signal">
          <div className="research-section-head">
            <span className="research-lane-tag">Signal</span>
            <h3 className="research-section-title">Know the user</h3>
          </div>
          <div className="research-section-sub">Background</div>
          <SynthesisBlock items={userInsights} sources={sources} onRef={onRef} />
        </section>
      )}
      {!userInsights.length && grounded.note && (
        <section className="research-section research-lane-signal">
          <div className="research-section-head">
            <span className="research-lane-tag">Signal</span>
            <h3 className="research-section-title">Know the user</h3>
          </div>
          <p className="research-synthesis-p">{grounded.note}</p>
        </section>
      )}
      {inference.tension && (
        <InferenceSection kicker="The tension" badge="Strategic read · inference">
          <p className="research-positioning">{inference.tension}</p>
        </InferenceSection>
      )}
      {(inference.goal || posText) && (
        <InferenceSection kicker="The goal" badge="Strategic read · inference">
          <p className="research-positioning">{inference.goal || posText}</p>
        </InferenceSection>
      )}
      {inference.product_magic && (
        <InferenceSection kicker="Know the magic" badge="Product frame · inference">
          <p className="research-positioning">{inference.product_magic}</p>
        </InferenceSection>
      )}
      {(getToBy.get || getToBy.to || getToBy.by) && (
        <InferenceSection kicker="Get to by" badge="Strategic read · inference">
          <div className="research-get-to-by">
            {getToBy.get && <p className="research-positioning"><span className="research-gtb-label">Get</span> {getToBy.get}</p>}
            {getToBy.to && <p className="research-positioning"><span className="research-gtb-label">To</span> {getToBy.to}</p>}
            {getToBy.by && <p className="research-positioning"><span className="research-gtb-label">By</span> {getToBy.by}</p>}
          </div>
        </InferenceSection>
      )}
      {inference.brand_safe_notes && (
        <InferenceSection kicker="Brand safety" badge="Guardrails · inference">
          <p className="research-positioning">{inference.brand_safe_notes}</p>
        </InferenceSection>
      )}
      {inference.mandatories && (
        <InferenceSection kicker="Mandatories" badge="Guardrails · inference">
          <p className="research-positioning">{inference.mandatories}</p>
        </InferenceSection>
      )}
      {inference.creative_judge && (
        <InferenceSection kicker="How we judge the work" badge="Creative criteria · inference">
          <p className="research-positioning">{inference.creative_judge}</p>
        </InferenceSection>
      )}
      {activationRows.length > 0 && (
        <section className="research-section research-lane-activation">
          <div className="research-section-head">
            <span className="research-lane-tag research-lane-tag-activation">Possible response</span>
            <h3 className="research-section-title">Possible responses</h3>
            <InferenceBadge label="Working response · inference" />
          </div>
          <div className="research-activation-list">
            {activationRows.map((m, i) => {
              const mk = String(m.market || '').toLowerCase();
              const prompt = m.prompt || '';
              const productRole = m.product_role || m.ai_mode_role;
              const roleLabel = 'Brand role';
              const structured = m.user_friction || productRole || m.behaviour_trigger || m.creative_execution;
              return (
                <div key={i} className="research-activation-card">
                  <div className="research-activation-head">
                    <span className="research-activation-market">{mk.toUpperCase()}</span>
                    <span className="research-activation-angle">{m.angle || mk.toUpperCase() + ' activation'}</span>
                  </div>
                  {structured && (
                    <div className="research-activation-structure">
                      {m.user_friction && (
                        <div><span>User friction</span><p>{m.user_friction}</p></div>
                      )}
                      {productRole && (
                        <div className="research-ai-mode-role"><span>{roleLabel}</span><p>{productRole}</p></div>
                      )}
                      {m.behaviour_trigger && (
                        <div><span>Behaviour trigger</span><p>{m.behaviour_trigger}</p></div>
                      )}
                      {m.creative_execution && (
                        <div><span>Creative execution</span><p>{m.creative_execution}</p></div>
                      )}
                    </div>
                  )}
                  {prompt && (
                    <div className="research-activation-prompt">
                      <div className="research-activation-prompt-k">Paste-ready prompt</div>
                      {prompt}
                    </div>
                  )}
                  {m.evidence_indices && m.evidence_indices.length > 0 && (
                    <div className="research-activation-proof">
                      <span>Proof</span>
                      {m.evidence_indices.map((n) => <RefButton key={'a' + i + '-' + n} n={n} onClick={onRef} />)}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}
      {activation.storyboard && activation.storyboard.friction && (
        <section className="research-section research-lane-activation">
          <div className="research-section-head">
            <span className="research-lane-tag research-lane-tag-activation">Storyboard</span>
            <h3 className="research-section-title">Friction · Interaction · Resolution</h3>
            <InferenceBadge label="Storyboard · inference" />
          </div>
          <table className="research-storyboard-table">
            <thead>
              <tr>
                <th scope="col">Friction</th>
                <th scope="col">Interaction</th>
                <th scope="col">Resolution</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>{activation.storyboard.friction}</td>
                <td>{activation.storyboard.interaction}</td>
                <td>{activation.storyboard.resolution}</td>
              </tr>
            </tbody>
          </table>
          {activation.storyboard.why_it_works && (
            <p className="research-storyboard-why">
              <span className="research-storyboard-k">Why it works</span>
              {activation.storyboard.why_it_works}
              {(activation.storyboard.evidence_indices || []).map((n) => (
                <RefButton key={'sb' + n} n={n} onClick={onRef} />
              ))}
            </p>
          )}
        </section>
      )}
      <EvidenceBank sources={sources} onRef={onRef} />
    </div>
  );
}

export function openCitation(sources, refIndex, setActiveRef){
  const n = Number(refIndex);
  if (!Number.isFinite(n) || n < 1) return;
  const src = sources[n - 1] || sources.find((s) => s.index === n || s.ref_index === n);
  if (src) setActiveRef(n);
}
