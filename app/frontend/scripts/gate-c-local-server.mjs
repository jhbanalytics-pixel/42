import {createServer} from 'node:http';
import {readFileSync, statSync} from 'node:fs';
import {extname, join, normalize, resolve} from 'node:path';

import {ROOT_SIGNAL} from '../src/ui/__tests__/fixtures/instrument-payloads.js';

const root = resolve(process.argv[2]);
const port = Number(process.argv[3]);
const passcode = 'gate-c-local-only';
const fixture = (name) => JSON.parse(readFileSync(new URL(`../src/ui/__tests__/fixtures/${name}`, import.meta.url), 'utf8'));
const today42 = fixture('today42.json');
const compare42 = fixture('compare42_items.json');
const fixtureThumbnailPrefix = 'https://example.invalid/thumb/';
const fixtureThumbnailNames = new Set();
const todayWithLocalThumbnails = JSON.parse(JSON.stringify(today42, (key, value) => {
  if (key !== 'thumbnail_url' || typeof value !== 'string' || !value.startsWith(fixtureThumbnailPrefix) || !value.endsWith('.jpg')) return value;
  const filename = value.slice(fixtureThumbnailPrefix.length).replace(/\.jpg$/, '.svg');
  fixtureThumbnailNames.add(filename);
  return `http://127.0.0.1:${port}/__fixtures/thumbnail/${filename}`;
}));
const zaToday = todayWithLocalThumbnails.markets.find((market) => market.market === 'ZA');
const discoverThumbnailSvg = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96"><rect width="96" height="96" fill="#e6eeeb"/><circle cx="48" cy="34" r="15" fill="#94b1a6"/><path d="M12 90c5-20 18-30 36-30s31 10 36 30" fill="#416c61"/></svg>');
const secondSignal = structuredClone(ROOT_SIGNAL);
secondSignal.signal_id = `sig_${'b'.repeat(64)}`;
secondSignal.label = 'Shared repair knowledge';
secondSignal.signal_name = 'Shared repair knowledge';
secondSignal.discovery_mode = 'phrase';
secondSignal.evidence_state = 'ready';
const admission = {
  discovery_mode: 'phrase', evidence_state: 'ready', market: 'za',
  qualities: {velocity: 'moderate', novelty: 'high', breadth: 'moderate', independence: 'high', history: 'low', geo_confidence: 'moderate', topic_tags: ['repair']},
  receipts: [{id: 'local-receipt', url: 'https://evidence.invalid/local', platform: 'search', snippet: 'Local deterministic fixture'}],
  observation_start: '2026-08-18', observation_end: '2026-08-24', observation_method: 'closed_local_fixture',
};
Object.assign(secondSignal, admission);
const firstSignal = {...structuredClone(ROOT_SIGNAL), ...admission, signal_id: `sig_${'a'.repeat(64)}`, signal_name: ROOT_SIGNAL.label};
const fieldwork = JSON.parse(readFileSync(resolve(root, '..', '..', 'tests', 'fixtures', 'workspaces', 'fieldwork_workspace_v1.json'), 'utf8')).ready;
const discoverItems = zaToday.cards.map((card, index) => {
  const clonedCard = structuredClone(card);
  return {
    ...clonedCard,
    tag: null, order: index + 1, date: today42.date,
    explanation_status: card.explained ? 'explained' : 'failed_checks', lifecycle: null,
    novelty: 'new', diffusion: 'bottom_up', origin: null, spread_line: null,
    reach: card.numbers?.find((number) => number.unit.includes('creators')) || null,
    growth: index === 0 ? {value: 2.8, unit: 'times its usual level', query_id: 'q_gate_c_growth', run_id: 'r_gate_c', result_hash: 'sha256:gatec'} : null,
    watch_id: null,
  };
});
const discover42 = {
  date: today42.date, market: 'ZA', run_id: 'r_gate_c', items: discoverItems,
  next_cursor: null, held_back: zaToday.held_back,
  filters: {
    kinds: [...new Set(discoverItems.map((item) => item.kind))],
    states: [...new Set(discoverItems.map((item) => item.state))],
    platforms: [...new Set(discoverItems.flatMap((item) => item.evidence.map((evidence) => evidence.platform)))],
  },
};
const compareDiscover42 = {
  date: today42.date, market: 'ZA', run_id: 'r_gate_c', next_cursor: null,
  items: compare42.subjects.map(({item_id, label, market}) => ({item_id, market, title: label})),
  held_back: {count: 0, items: []}, filters: {kinds: [], states: [], platforms: []},
};
const radar42 = {
  date: today42.date, market: 'ZA',
  points: discoverItems.map((item) => ({item_id: item.item_id, label: item.title, kind: item.kind, state: item.state, flag: item.flag, growth: item.growth, reach: item.reach})),
  held_back_count: zaToday.held_back.count, note: null,
};

function json(response, value){
  response.writeHead(200, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
  response.end(JSON.stringify(value));
}

function api(path, url){
  if (path === '/api/health') return {passcode: true};
  if (path === '/api/auth/verify') return {ok: true};
  if (path === '/api/today') return todayWithLocalThumbnails;
  if (path === '/api/alerts') return {date: today42.date, alerts: []};
  if (path === '/api/investigations') return {investigations: []};
  if (path === '/api/schedules') return {schedules: []};
  if (path === '/api/discover/radar') return radar42;
  if (path === '/api/discover') return url.searchParams.has('sort') ? discover42 : compareDiscover42;
  if (path === '/api/compare') return compare42;
  if (path.startsWith('/api/desk')) return {
    topics: [], updated: '2026-08-25', freshness: {status: 'green', age_hours: 1},
    dynamic_discovery: {
      contract_version: 'desk_dynamic_signal_v2', status: 'ready', requested_market: 'za',
      run: {run_id: 'gate-c-run'}, signals: [
        {contract_version: 'desk_dynamic_signal_v2', signal: firstSignal},
        {contract_version: 'desk_dynamic_signal_v2', signal: secondSignal},
      ], error: null,
    },
  };
  if (path.startsWith('/api/internal/v2/fieldwork/read')) return fieldwork;
  if (path.startsWith('/api/v2/source-lab')) return {contract_version: '2.2.0', sources: []};
  if (path.endsWith('/status')) return {
    contract_version: 'intelligence_dossier_v1', investigation_id: 'inv_gate_c',
    research_plan: {questions: ['What changed?'], required_source_families: ['search']}, missing_work: [],
  };
  if (path.endsWith('/claims/read')) return {claims: []};
  if (path.endsWith('/decision/read')) return {decision: null};
  if (/\/artifacts\/[^/]+\/read$/.test(path)) return {artifact: {artifact_id: 'ra_gate_c', title: 'Gate C local artifact'}};
  if (path.startsWith('/api/research/recent')) return {artifacts: []};
  if (path.startsWith('/api/v2/investigations/list')) return {investigations: []};
  if (path.startsWith('/api/v2/investigations/scopes')) return {scopes: []};
  return {};
}

const preload = `<script>
localStorage.setItem('pulse_passcode', ${JSON.stringify(passcode)});
window.__gateC = {console: [], requests: [], resourceFailures: []};
for (const name of ['error', 'warn']) { const original = console[name]; console[name] = (...args) => { window.__gateC.console.push(name + ':' + args.map(String).join(' ')); original(...args); }; }
addEventListener('error', (event) => { if (event.target && event.target !== window) window.__gateC.resourceFailures.push(event.target.src || event.target.href || event.target.tagName); }, true);
const nativeFetch = fetch.bind(window);
window.fetch = async (...args) => { const url = String(args[0] && args[0].url || args[0]); const response = await nativeFetch(...args); window.__gateC.requests.push({url, status: response.status, ok: response.ok}); return response; };
if (!sessionStorage.getItem('gate-c-hard-reloaded')) { sessionStorage.setItem('gate-c-hard-reloaded', '1'); location.reload(); }
</script>`;

const runner = `<!doctype html><html lang="en"><body><iframe id="frame" style="border:0;height:1800px"></iframe><output id="result">pending</output><script>
const params = new URLSearchParams(location.search); const frame = document.getElementById('frame'); frame.style.width = params.get('width') + 'px';
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const visible = (element, view) => { const rect = element.getBoundingClientRect(); const style = view.getComputedStyle(element); return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden'; };
const target = (element) => { if (!element) return 'missing'; if (element.id) return '#' + element.id; const name = element.classList && element.classList[0]; return element.tagName.toLowerCase() + (name ? '.' + name : ''); };
function accessibility(document, view){ const violations=[]; const add=(id,impact,nodes)=>{if(nodes.length) violations.push({id,impact,targets:nodes.map(target)});};
 add('button-name','critical',[...document.querySelectorAll('button')].filter((e)=>visible(e,view)&&!(e.textContent||e.getAttribute('aria-label')||e.title||'').trim()));
 add('link-name','serious',[...document.querySelectorAll('a[href]')].filter((e)=>visible(e,view)&&!(e.textContent||e.getAttribute('aria-label')||e.title||'').trim()));
 add('label','critical',[...document.querySelectorAll('input,select,textarea')].filter((e)=>visible(e,view)&&!e.labels?.length&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby')));
 const ids=[...document.querySelectorAll('[id]')].map((e)=>e.id); add('duplicate-id-active','serious',[...document.querySelectorAll('[id]')].filter((e,i)=>ids.indexOf(e.id)!==i));
 add('aria-dialog-name','serious',[...document.querySelectorAll('[role="dialog"],dialog')].filter((e)=>visible(e,view)&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby')));
 return {method:'gate-c-serious-critical-v1',scope:'visible built-route DOM; critical and serious named rules',violations}; }
const inspect = async () => { const surface=params.get('surface'); const required=params.get('selector'); const composition=params.get('composition');
 const deadline=Date.now()+12000; let packageNode,document,view; while(Date.now()<deadline){ document=frame.contentDocument; view=frame.contentWindow; packageNode=document&&document.querySelector(required); if(packageNode&&(!composition||document.querySelector(composition))) break; await wait(50); }
 if(surface==='evidence-room'){ const button=[...document.querySelectorAll('button')].find((e)=>/evidence/i.test(e.textContent||e.getAttribute('aria-label')||'')); if(button){button.click(); await wait(100);} packageNode=document.querySelector(required); }
 const root=document.documentElement; const interactive=[...document.querySelectorAll('button,a[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')].filter((e)=>visible(e,view)&&!e.disabled&&!e.closest('[inert]')); const undersized=interactive.filter((e)=>{const r=e.getBoundingClientRect();return r.width<48||r.height<48;}).map((e)=>({target:target(e),width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height})); interactive[0]?.focus();
 const escaping=[...document.body.querySelectorAll('*')].filter((e)=>{if(!visible(e,view))return false;const r=e.getBoundingClientRect();return r.left<-.5||r.right>view.innerWidth+.5;}).map(target);
 const gate=view.__gateC||{console:['missing gate recorder'],requests:[],resourceFailures:[]}; const result={authority:'consumer-production-build',surface,requestedWidth:Number(params.get('width')),viewportWidth:view.innerWidth,clientWidth:root.clientWidth,scrollWidth:Math.max(root.scrollWidth,document.body.scrollWidth),packageSurfacePresent:!!packageNode,compositionPresent:!composition||!!document.querySelector(composition),escaping,undersized,focusEstablished:interactive.length===0||document.activeElement===interactive[0],errors:[...gate.console,...gate.resourceFailures],network:{requests:gate.requests,unfulfilled:gate.requests.filter((r)=>!r.ok)},accessibility:accessibility(document,view)};
 document.getElementById('result').textContent=btoa(unescape(encodeURIComponent(JSON.stringify(result)))); };
frame.src='/?gate-c=1#/'+params.get('route');
inspect();
</script></body></html>`;

createServer((request, response) => {
  const url = new URL(request.url, `http://127.0.0.1:${port}`);
  if (url.pathname === '/__gate_c_runner') { response.writeHead(200, {'Content-Type': 'text/html'}); response.end(runner); return; }
  if (url.pathname.startsWith('/__fixtures/thumbnail/')) {
    if (!fixtureThumbnailNames.has(url.pathname.slice('/__fixtures/thumbnail/'.length))) { response.writeHead(404); response.end(); return; }
    response.writeHead(200, {'Content-Type': 'image/svg+xml', 'Cache-Control': 'no-store'}); response.end(discoverThumbnailSvg); return;
  }
  if (url.pathname.startsWith('/api/')) { json(response, api(url.pathname, url)); return; }
  const relative = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/+/, '');
  const path = normalize(join(root, relative));
  if (!path.startsWith(root)) { response.writeHead(403); response.end(); return; }
  try {
    const stat = statSync(path); if (!stat.isFile()) throw new Error('not file');
    let body = readFileSync(path); if (relative === 'index.html') body = Buffer.from(String(body).replace('<script type="module"', preload + '<script type="module"'));
    const types = {'.html':'text/html','.js':'text/javascript','.css':'text/css','.woff2':'font/woff2','.json':'application/json'};
    response.writeHead(200, {'Content-Type': types[extname(path)] || 'application/octet-stream', 'Cache-Control': 'no-store'}); response.end(body);
  } catch { response.writeHead(404); response.end('not found'); }
}).listen(port, '127.0.0.1', () => process.stdout.write(`READY ${port}\n`));
