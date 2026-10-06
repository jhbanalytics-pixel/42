import {existsSync} from 'node:fs';
import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {build} from '../frontend/node_modules/esbuild/lib/main.js';

const chromeCandidates = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter(Boolean);
const chrome = chromeCandidates.find((candidate) => existsSync(candidate));
if (!chrome) {
  console.error('FAIL: Chrome or Chromium is required for the research generation owner test');
  process.exit(1);
}

const mutation = process.env.RESEARCH_GENERATION_MUTATION || '';
const mutationPlugin = mutation ? {
  name: 'research-generation-mutation',
  setup(buildContext){
    buildContext.onLoad({filter: /ResearchDocPanel\.jsx$/}, async (args) => {
      const source = await readFile(args.path, 'utf8');
      let contents = source;
      const cleanup = "  useEffect(() => () => { abort(); clearLandedTimers(); }, [abort, clearLandedTimers]);";
      if (mutation === 'route-effect') {
        contents = contents.replace(cleanup, "  useEffect(() => { if (personaId && personas.length) runGenerate(); }, [personaId, personas, runGenerate]);\n\n" + cleanup);
      } else if (mutation === 'timer') {
        contents = contents.replace(cleanup, "  useEffect(() => { const id = setTimeout(runGenerate, 0); return () => clearTimeout(id); }, [runGenerate]);\n\n" + cleanup);
      } else if (mutation === 'artifact-callback') {
        contents = contents.replace('    loadArtifactById(params.artifactId);', '    loadArtifactById(params.artifactId);\n    runGenerate();');
      } else if (mutation === 'persona-resolver') {
        contents = contents.replace(
          "    setMarkets(selection.markets);\n  }, [params.personaId, params.markets, personas, personaId, productFrame]);",
          "    setMarkets(selection.markets);\n    if (selection.personaId) generateResearch(selection.personaId, selection.markets, selection.productFrame);\n  }, [params.personaId, params.markets, personas, personaId, productFrame]);",
        );
      } else if (mutation === 'market-callback') {
        contents = contents.replace("  const toggleMarket = (mk) => {", "  const toggleMarket = (mk) => {\n    runGenerate();");
      } else if (mutation === 'automatic-retry') {
        contents = contents.replace(
          "      setPhase('error');\n    }\n  }, [abort, applyBriefResult",
          "      setPhase('error');\n      if (e && e.code === 'capacity_busy' && !globalThis.__researchMutationRetried) { globalThis.__researchMutationRetried = true; setTimeout(runGenerate, 0); }\n    }\n  }, [abort, applyBriefResult",
        );
      }
      if (contents === source) throw new Error('MUTATION_NOT_APPLIED: ' + mutation);
      return {contents, loader: 'jsx'};
    });
  },
} : null;

const entry = String.raw`
  import React from 'react';
  import {createRoot} from 'react-dom/client';
  import {clearCache} from './api.js';
  import {ResearchPage} from './ResearchDocPanel.jsx';

  (async () => {
  const testCase = window.TEST_CASE;
  const requests = [];
  const personas = [
    {id: 'strategy_director', label: 'Strategy director', product_frame: 'Cultural strategy', product_frame_options: [{value: 'Cultural strategy'}, {value: 'Brand role'}], default_markets: ['za']},
    {id: 'research_lead', label: 'Research lead', product_frame: 'Research planning', product_frame_options: [{value: 'Research planning'}, {value: 'Product role'}], default_markets: ['ng']},
  ];
  const behaviour = {id: 'za:music_amapiano', market: 'za', query_group: 'music_amapiano', signal_topic: 'Amapiano', behaviour: 'Listeners are comparing new releases', examples: [], metric: {post_count: 2}};
  let postMode = 'pending';
  let deferPersonas = false;
  let resolvePersonas;
  const response = (status, body, headers = {}) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json', ...headers}});
  const personaResponse = () => response(200, {personas});
  window.fetch = async (input, options = {}) => {
    const url = String(input);
    const method = options.method || 'GET';
    requests.push({url, method, body: options.body || null});
    if (url === '/api/research/personas') {
      if (deferPersonas) return new Promise((resolve) => { resolvePersonas = () => resolve(personaResponse()); });
      return personaResponse();
    }
    if (url.startsWith('/api/research/behaviours')) return response(200, {client_metrics_default: true, behaviours: {za: [behaviour], ng: [], ke: []}});
    if (url.startsWith('/api/research/ra_valid')) return response(200, {artifact_id: 'ra_valid', status: 'completed', persona_id: 'strategy_director', markets: ['za'], doc: {title: 'Valid doc'}});
    if (url.startsWith('/api/research/ra_missing')) return response(404, {detail: 'Research doc not found.'});
    if (method === 'POST' && url.startsWith('/api/research/generate')) {
      if (postMode === 'capacity') return response(429, {error: 'capacity_busy', retry_after_seconds: 1}, {'Retry-After': '1'});
      return response(202, {job_id: 'job_' + requests.length});
    }
    if (url.startsWith('/api/research/status')) return response(200, {pending: true, status: 'gathering'});
    return response(404, {detail: 'Unexpected test request: ' + method + ' ' + url});
  };

  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const generationPosts = () => requests.filter((request) => request.method === 'POST' && request.url.startsWith('/api/research/generate'));
  const waitFor = async (find, label) => {
    const deadline = Date.now() + 1200;
    while (Date.now() < deadline) {
      const value = find();
      if (value) return value;
      await delay(20);
    }
    throw new Error('timed out waiting for ' + label);
  };
  const buttonByText = (text) => [...document.querySelectorAll('button')].find((button) => button.textContent.trim().includes(text));
  const directRoute = {work: 'brief', artifactId: null, personaId: 'strategy_director', markets: ['za'], topics: [{market: 'za', query_group: 'music_amapiano'}]};
  let root;
  const mount = async (routeState, embedded = false) => {
    clearCache();
    root = createRoot(document.getElementById('root'));
    root.render(<ResearchPage routeState={routeState} embedded={embedded} onAuth={() => {}} />);
    await delay(280);
    return root;
  };

  const cases = {
    async 'direct-link'(){
      await mount(directRoute);
      assert(generationPosts().length === 0, 'direct route hydration produced ' + generationPosts().length + ' generation POST');
    },
    async 'hard-reload'(){
      await mount(directRoute);
      assert(generationPosts().length === 0, 'fresh browser reload produced ' + generationPosts().length + ' generation POST');
    },
    async 'route-changes'(){
      await mount(directRoute);
      (await waitFor(() => buttonByText('Brand role'), 'product frame preset')).click();
      (await waitFor(() => buttonByText('Research lead'), 'second persona')).click();
      buttonByText('NG').click();
      root.render(<ResearchPage routeState={{...directRoute, personaId: 'research_lead', markets: ['ng'], topics: [{market: 'ng', query_group: 'fashion_ankara_asoebi'}]}} onAuth={() => {}} />);
      await delay(300);
      assert(generationPosts().length === 0, 'route controls produced ' + generationPosts().length + ' generation POST');
    },
    async 'artifact-loading'(){
      await mount({...directRoute, artifactId: 'ra_valid'});
      root.render(<ResearchPage routeState={{...directRoute, artifactId: 'ra_missing'}} onAuth={() => {}} />);
      await delay(300);
      assert(generationPosts().length === 0, 'artifact loading produced ' + generationPosts().length + ' generation POST');
    },
    async 'explicit-generate'(){
      await mount(directRoute);
      const generate = await waitFor(() => document.querySelector('.research-generate-btn:not([disabled])'), 'enabled Generate');
      generate.click();
      await delay(80);
      const posts = generationPosts();
      assert(posts.length === 1, 'explicit Generate produced ' + posts.length + ' generation POSTs');
      assert(posts[0].url === '/api/research/generate', 'standard Generate used ' + posts[0].url);
      const body = JSON.parse(posts[0].body);
      assert(body.persona_id === 'strategy_director', 'Generate lost the visible persona');
      assert(JSON.stringify(body.markets) === JSON.stringify(['za']), 'Generate lost the visible markets');
      assert(body.product_frame === 'Cultural strategy', 'Generate lost the visible product frame');
      assert(String(body.focus_note || '').includes('music_amapiano'), 'Generate lost the approved topic state');
    },
    async 'duplicate-busy'(){
      await mount(directRoute);
      const generate = await waitFor(() => document.querySelector('.research-generate-btn:not([disabled])'), 'enabled Generate');
      generate.click();
      generate.click();
      await delay(100);
      assert(generationPosts().length === 1, 'duplicate activation while busy produced ' + generationPosts().length + ' generation POSTs');
    },
    async 'separate-owners'(){
      await mount({...directRoute, topics: [...directRoute.topics, {market: 'ng', query_group: 'fashion_ankara_asoebi'}]});
      (await waitFor(() => document.querySelector('.research-generate-btn:not([disabled])'), 'batch Generate')).click();
      await delay(80);
      assert(generationPosts().length === 1 && generationPosts()[0].url === '/api/research/generate-batch', 'batch button did not exclusively own the batch request');
      root.unmount();
      requests.length = 0;
      document.getElementById('root').remove();
      const replacement = document.createElement('div');
      replacement.id = 'root';
      document.body.prepend(replacement);
      await mount({...directRoute, topics: null}, true);
      assert(generationPosts().length === 0, 'behaviour scan started generation before activation');
      (await waitFor(() => document.querySelector('.bscan-approve'), 'behaviour approval')).click();
      await delay(50);
      (await waitFor(() => buttonByText('Build consolidated doc'), 'consolidated button')).click();
      await delay(100);
      assert(generationPosts().length === 1 && generationPosts()[0].url === '/api/research/generate-consolidated', 'consolidated button did not exclusively own the consolidated request');
    },
    async 'nonstandard-capacity'(){
      postMode = 'capacity';
      await mount({...directRoute, topics: [...directRoute.topics, {market: 'ng', query_group: 'fashion_ankara_asoebi'}]});
      (await waitFor(() => document.querySelector('.research-generate-btn:not([disabled])'), 'batch Generate')).click();
      await delay(100);
      assert(!buttonByText('Retry'), 'batch capacity failure exposed the standard Retry owner');
    },
    async 'capacity-retry'(){
      postMode = 'capacity';
      await mount(directRoute);
      (await waitFor(() => document.querySelector('.research-generate-btn:not([disabled])'), 'enabled Generate')).click();
      const retry = await waitFor(() => buttonByText('Retry'), 'capacity Retry');
      assert(generationPosts().length === 1, 'capacity state changed the original request count');
      postMode = 'pending';
      retry.click();
      retry.click();
      await delay(1700);
      assert(generationPosts().length === 2, 'one Retry activation produced ' + (generationPosts().length - 1) + ' retry POSTs');
    },
    async 'stale-capacity-artifact'(){
      postMode = 'capacity';
      await mount(directRoute);
      (await waitFor(() => document.querySelector('.research-generate-btn:not([disabled])'), 'enabled Generate')).click();
      await waitFor(() => buttonByText('Retry'), 'capacity Retry');
      root.render(<ResearchPage routeState={{...directRoute, artifactId: 'ra_missing'}} onAuth={() => {}} />);
      await waitFor(() => {
        const error = document.querySelector('.research-error');
        return error && error.textContent.includes('Research doc not found.') ? error : null;
      }, 'missing artifact error');
      assert(!buttonByText('Retry'), 'missing artifact error retained the stale capacity Retry');
    },
    async 'stale-hydration'(){
      deferPersonas = true;
      clearCache();
      root = createRoot(document.getElementById('root'));
      root.render(<ResearchPage routeState={directRoute} onAuth={() => {}} />);
      await waitFor(() => resolvePersonas, 'deferred persona request');
      root.render(<ResearchPage routeState={{...directRoute, personaId: 'research_lead', markets: ['ng']}} onAuth={() => {}} />);
      resolvePersonas();
      await delay(320);
      assert(generationPosts().length === 0, 'stale persona hydration produced ' + generationPosts().length + ' generation POST');
    },
  };

  const result = document.getElementById('result');
  try {
    assert(cases[testCase], 'unknown test case ' + testCase);
    await cases[testCase]();
    result.dataset.status = 'pass';
    result.textContent = 'PASS ' + testCase;
  } catch (error) {
    result.dataset.status = 'fail';
    result.textContent = 'FAIL ' + testCase + ': ' + error.message;
  }
  })();
`;

const bundled = await build({
  bundle: true,
  format: 'iife',
  jsx: 'automatic',
  loader: {'.css': 'empty', '.js': 'jsx', '.jsx': 'jsx'},
  stdin: {contents: entry, loader: 'jsx', resolveDir: fileURLToPath(new URL('../frontend/src/', import.meta.url)), sourcefile: 'research-generation-owner.test.jsx'},
  write: false,
  define: {'process.env.NODE_ENV': '"test"'},
  plugins: mutationPlugin ? [mutationPlugin] : [],
});
const script = bundled.outputFiles[0].text;
const requestedCase = process.env.RESEARCH_GENERATION_CASE;
const caseNames = requestedCase ? [requestedCase] : ['direct-link', 'hard-reload', 'route-changes', 'artifact-loading', 'explicit-generate', 'duplicate-busy', 'separate-owners', 'nonstandard-capacity', 'capacity-retry', 'stale-capacity-artifact', 'stale-hydration'];

async function runCase(testCase){
  const html = '<!doctype html><html><body><div id="root"></div><pre id="result">RUNNING</pre><script>window.TEST_CASE=' + JSON.stringify(testCase) + '</script><script src="/test.js"></script></body></html>';
  const server = createServer((request, responseStream) => {
    responseStream.statusCode = 200;
    responseStream.setHeader('Content-Type', request.url === '/test.js' ? 'text/javascript' : 'text/html');
    responseStream.end(request.url === '/test.js' ? script : html);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const {port} = server.address();
  const child = spawn(chrome, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--virtual-time-budget=2600', '--dump-dom', `http://127.0.0.1:${port}/`]);
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (chunk) => { stdout += chunk; });
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  const exitCode = await new Promise((resolve) => child.on('close', resolve));
  await new Promise((resolve) => server.close(resolve));
  const resultMatch = stdout.match(/<pre id="result" data-status="(pass|fail)">([^<]+)<\/pre>/);
  if (exitCode !== 0 || !resultMatch || resultMatch[1] !== 'pass') return {ok: false, output: resultMatch ? resultMatch[2] : stderr.trim() || 'browser test did not return a result'};
  return {ok: true, output: resultMatch[2]};
}

let failed = 0;
for (const testCase of caseNames) {
  const result = await runCase(testCase);
  console.log(result.output);
  if (!result.ok) failed++;
}
if (failed) {
  console.error('FAIL: ' + failed + ' explicit research generation owner case(s)');
  process.exit(1);
}
console.log('PASS: ' + caseNames.length + ' explicit research generation owner case(s)');
