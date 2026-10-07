import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {
  cpSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import {tmpdir} from 'node:os';
import {extname, join, relative, resolve, sep} from 'node:path';

const PACKAGE_NAME = 'ogilvy-intelligence-design-system';
const DEPENDENCY = 'file:vendor/ogilvy-intelligence-design-system-2.0.22.tgz';
const RESOLUTION = 'ogilvy-intelligence-design-system@vendor/ogilvy-intelligence-design-system-2.0.22.tgz';
const INTEGRITY = 'sha512-j7HRpNTWINmkGP25Q4knlZ9x7FTde62rMlx9kZAHm/ErZDw737C82rRkOE6lIc71OyJjn5B+KbYGWV8Sc+k0Xw==';

export function buildProduction({frontendRoot, outDir, logLevel='error'}){
  const result = spawnSync('node', [
    join(frontendRoot, 'node_modules', 'vite', 'bin', 'vite.js'),
    'build', '--outDir', outDir, '--emptyOutDir', '--logLevel', logLevel,
  ], {cwd: frontendRoot, encoding: 'utf8', timeout: 60000, windowsHide: true});
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`Production build exited ${result.status}: ${result.stderr || result.stdout}`);
}

function parseBunTextLock(lock){
  return JSON.parse(lock.replace(/,\s*([}\]])/g, '$1'));
}

export function assertPinnedPackageLock(lock){
  const parsed = parseBunTextLock(lock);
  const dependency = parsed.workspaces?.['']?.dependencies?.[PACKAGE_NAME];
  const record = parsed.packages?.[PACKAGE_NAME];
  if (dependency !== DEPENDENCY) throw new Error(`Unexpected package dependency: ${dependency}`);
  if (!Array.isArray(record) || record.length !== 3) throw new Error('Malformed installed package record');
  const [resolution, metadata, integrity] = record;
  if (resolution !== RESOLUTION) throw new Error(`Unexpected installed resolution: ${resolution}`);
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)){
    throw new Error('Malformed installed package metadata');
  }
  if (integrity !== INTEGRITY) throw new Error(`Unexpected installed integrity: ${integrity}`);
  return {dependency, integrity, resolution};
}

export function resolveChrome(){
  return [
    process.env.CHROME_PATH,
    process.env.CHROME_BIN,
    String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
    String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
    '/usr/bin/google-chrome',
    '/usr/bin/chromium-browser',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean).find((candidate) => existsSync(candidate)) || null;
}

function walkFiles(root, directory=root, found=[]){
  for (const name of readdirSync(directory)){
    const path = join(directory, name);
    if (statSync(path).isDirectory()) walkFiles(root, path, found);
    else found.push(relative(root, path).split(sep).join('/'));
  }
  return found.sort();
}

function fileHashes(root){
  return Object.fromEntries(walkFiles(root).map((path) => [
    path,
    createHash('sha256').update(readFileSync(join(root, path))).digest('hex'),
  ]));
}

export function compareCopiedOutputs(normalHashes, failedHashes, sentinelPath){
  const normalInventory = Object.keys(normalHashes).sort();
  const failedInventory = Object.keys(failedHashes).sort();
  if (JSON.stringify(normalInventory) !== JSON.stringify(failedInventory)){
    const added = failedInventory.filter((path) => !normalInventory.includes(path));
    const removed = normalInventory.filter((path) => !failedInventory.includes(path));
    throw new Error(
      `Copied output inventory mismatch: added ${added.join(', ') || 'none'}; removed ${removed.join(', ') || 'none'}`,
    );
  }

  const union = [...new Set([...normalInventory, ...failedInventory])].sort();
  const changedFiles = union.filter((path) => normalHashes[path] !== failedHashes[path]);
  if (changedFiles.length !== 1 || changedFiles[0] !== sentinelPath){
    throw new Error(`Unexpected copied output changes: ${changedFiles.join(', ') || 'none'}`);
  }
  return {changedFiles, inventory: normalInventory};
}

function treeSnapshot(root, excluded=new Set()){
  if (!existsSync(root)) return {digest: null, exists: false, files: [], hashes: {}};
  const files = walkFiles(root).filter((path) => !excluded.has(path.split('/')[0]));
  const hashes = Object.fromEntries(files.map((path) => [
    path,
    createHash('sha256').update(readFileSync(join(root, path))).digest('hex'),
  ]));
  return {
    digest: createHash('sha256').update(JSON.stringify(hashes)).digest('hex'),
    exists: true,
    files,
    hashes,
  };
}

function assertSnapshotUnchanged(before, after, label){
  if (JSON.stringify(before) !== JSON.stringify(after)){
    throw new Error(`${label} changed during the isolated browser proof`);
  }
  return {after, before, unchanged: true};
}

function waitFor(promise, milliseconds){
  const deadline = Symbol('deadline');
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve(deadline), milliseconds);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer))
    .then((value) => ({deadline, value}));
}

export async function runBoundedSubprocess(run, {
  exitDeadlineMs = 20000,
  terminateDeadlineMs = 2000,
  forceExitDeadlineMs = 2000,
}={}){
  const exited = Promise.resolve(run.exited);
  let result = await waitFor(exited, exitDeadlineMs);
  if (result.value !== result.deadline){
    return {exited: true, forceKilled: false, status: result.value, terminated: false};
  }

  run.kill('SIGTERM');
  result = await waitFor(exited, terminateDeadlineMs);
  if (result.value !== result.deadline){
    return {exited: true, forceKilled: false, status: result.value, terminated: true};
  }

  run.kill('SIGKILL');
  result = await waitFor(exited, forceExitDeadlineMs);
  if (result.value === result.deadline){
    throw new Error('Browser process did not exit after force kill');
  }
  return {exited: true, forceKilled: true, status: result.value, terminated: true};
}

export function cleanupTemporaryPath(path){
  for (let attempt = 0; existsSync(path); attempt += 1){
    try {
      rmSync(path, {recursive: true, force: true});
      break;
    } catch (error){
      if (attempt >= 10 || !['EBUSY', 'EPERM', 'ENOTEMPTY'].includes(error.code)) throw error;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 100);
    }
  }
  if (existsSync(path)) throw new Error(`Temporary path remains after cleanup: ${path}`);
}

function instrumentIndex(root){
  const indexPath = join(root, 'index.html');
  const index = readFileSync(indexPath, 'utf8');
  const probe = `<script>
    (() => {
      const started = Date.now();
      const finish = () => {
        const state = document.documentElement.getAttribute('data-product-styles');
        const alert = document.querySelector('[role="alert"]');
        const unsettled = state !== 'ready' && state !== 'failed';
        const failedBeforeRender = state === 'failed' && !alert;
        if ((unsettled || failedBeforeRender) && Date.now() - started < 8000){
          setTimeout(finish, 25);
          return;
        }
        const frame = document.querySelector('.gate-v4--style-error');
        const frameStyle = frame ? getComputedStyle(frame) : null;
        const alertStyle = alert ? getComputedStyle(alert) : null;
        const frameRect = frame ? frame.getBoundingClientRect() : null;
        const alertRect = alert ? alert.getBoundingClientRect() : null;
        const centerX = alertRect ? alertRect.left + alertRect.width / 2 : null;
        const centerY = alertRect ? alertRect.top + alertRect.height / 2 : null;
        const centerHit = alertRect ? document.elementFromPoint(centerX, centerY) : null;
        const centerHitWithinAlert = Boolean(alert && centerHit && alert.contains(centerHit));
        const result = {
          state,
          alertCount: document.querySelectorAll('[role="alert"]').length,
          alertRole: alert?.getAttribute('role') ?? null,
          alertText: alert?.textContent?.replace(/\\s+/g, ' ').trim() ?? '',
          viewport: {width: innerWidth, height: innerHeight},
          frame: frameStyle && frameRect ? {
            backgroundColor: frameStyle.backgroundColor,
            color: frameStyle.color,
            display: frameStyle.display,
            height: frameRect.height,
            position: frameStyle.position,
            width: frameRect.width,
          } : null,
          alert: alertStyle ? {
            backgroundColor: alertStyle.backgroundColor,
            color: alertStyle.color,
            borderTopColor: alertStyle.borderTopColor,
            borderTopWidth: alertStyle.borderTopWidth,
            centerHitWithinAlert,
            display: alertStyle.display,
            height: alertRect.height,
            intersectsViewport: alertRect.bottom > 0 && alertRect.right > 0
              && alertRect.top < innerHeight && alertRect.left < innerWidth,
            occludingElement: centerHitWithinAlert ? null
              : centerHit && (centerHit.id || centerHit.className || centerHit.tagName),
            opacity: alertStyle.opacity,
            paddingTop: alertStyle.paddingTop,
            visibility: alertStyle.visibility,
            width: alertRect.width,
          } : null,
        };
        const output = document.createElement('pre');
        output.id = 'task-4-style-proof';
        output.textContent = JSON.stringify(result);
        document.body.append(output);
      };
      finish();
    })();
  </script>`;
  writeFileSync(indexPath, index.replace('</body>', `${probe}</body>`), 'utf8');
}

const CONTENT_TYPES = {
  '.css': 'text/css',
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.json': 'application/json',
  '.woff2': 'font/woff2',
};

async function browserReadback({chromePath, root, profile}){
  const resolvedRoot = resolve(root);
  const server = Bun.serve({
    port: 0,
    async fetch(request){
      const pathname = decodeURIComponent(new URL(request.url).pathname);
      if (pathname === '/api/health' && request.method === 'GET'){
        return new Response(JSON.stringify({
          ok: true,
          service: 'f42-api',
          version: 'fixture',
          time: '2026-09-29T10:00:00+02:00',
          auth_mode: 'passcode',
          passcode: true,
          checks: {auth: 'ok', bigquery: 'ok', agent: 'ok', today: 'published'},
        }), {headers: {'content-type': 'application/json'}});
      }
      const requested = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '');
      const path = resolve(resolvedRoot, requested);
      if (path !== resolvedRoot && !path.startsWith(`${resolvedRoot}${sep}`)){
        return new Response('Not found', {status: 404});
      }
      const file = Bun.file(path);
      if (!await file.exists()) return new Response('Not found', {status: 404});
      return new Response(file, {headers: {'content-type': CONTENT_TYPES[extname(path)] || 'application/octet-stream'}});
    },
  });
  let run;
  let lifecycle;
  try {
    run = Bun.spawn([
      chromePath,
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      '--run-all-compositor-stages-before-draw',
      '--window-size=900,900',
      '--virtual-time-budget=10000',
      `--user-data-dir=${profile}`,
      '--dump-dom',
      `http://127.0.0.1:${server.port}/`,
    ], {stdout: 'pipe', stderr: 'pipe'});
    const [bounded, stdout, stderr] = await Promise.all([
      runBoundedSubprocess(run),
      new Response(run.stdout).text(),
      new Response(run.stderr).text(),
    ]);
    lifecycle = bounded;
    if (bounded.status !== 0) throw new Error(`Chrome exited ${bounded.status}: ${stderr.slice(0, 400)}`);
    const encoded = stdout.match(/<pre id="task-4-style-proof">([^<]+)<\/pre>/)?.[1];
    if (!encoded) throw new Error(`Production style probe returned no result: ${stdout.slice(-800)}`);
    const measured = JSON.parse(encoded.replaceAll('&quot;', '"').replaceAll('&amp;', '&'));
    cleanupTemporaryPath(profile);
    return {...measured, lifecycle: {...lifecycle, profileExistsAfter: existsSync(profile)}};
  } finally {
    server.stop(true);
    cleanupTemporaryPath(profile);
  }
}

export async function probeProductionStyleBoundary({chromePath, frontendRoot}){
  if (!chromePath || !existsSync(chromePath)){
    throw new Error(`Chrome executable is required: ${chromePath}`);
  }
  const directory = mkdtempSync(join(tmpdir(), 'lp-task-4-production-style-'));
  const built = join(directory, 'built');
  const normal = join(directory, 'normal');
  const failed = join(directory, 'failed');
  const preexistingOutputRoot = resolve(frontendRoot, '..', 'web', 'dist');
  const sourceBefore = treeSnapshot(frontendRoot, new Set(['node_modules']));
  const preexistingOutputBefore = treeSnapshot(preexistingOutputRoot);
  let proof;
  try {
    buildProduction({frontendRoot, outDir: built, logLevel: 'silent'});
    cpSync(built, normal, {recursive: true});
    cpSync(built, failed, {recursive: true});
    instrumentIndex(normal);
    instrumentIndex(failed);

    const index = readFileSync(join(failed, 'index.html'), 'utf8');
    const cssPaths = [...index.matchAll(/href="([^"]+\.css)"/g)]
      .map((match) => match[1].replace(/^\//, ''));
    const criticalCssPath = cssPaths.find((path) => (
      readFileSync(join(failed, path), 'utf8').includes('--font-serif:')
    ));
    if (!criticalCssPath) throw new Error('Critical package stylesheet was not linked by the production build');
    const cssPath = join(failed, criticalCssPath);
    const css = readFileSync(cssPath, 'utf8');
    const sentinelMutations = css.split('--font-serif:').length - 1;
    if (sentinelMutations !== 1) throw new Error(`Expected one package sentinel, found ${sentinelMutations}`);
    const corrupted = css.replace('--font-serif:', '--font-serif-disabled:');
    writeFileSync(cssPath, corrupted, 'utf8');

    const normalHashes = fileHashes(normal);
    const failedHashes = fileHashes(failed);
    const copiedOutputComparison = compareCopiedOutputs(normalHashes, failedHashes, criticalCssPath);
    const bootFallbackRetained = corrupted.includes('.gate-v4--style-error')
      && corrupted.includes('background:#151214')
      && corrupted.includes('.gate-v4-style-error')
      && corrupted.includes('background:#faf8f9');

    const failedReadback = await browserReadback({
      chromePath,
      root: failed,
      profile: join(directory, 'failed-profile'),
    });
    const normalReadback = await browserReadback({
      chromePath,
      root: normal,
      profile: join(directory, 'normal-profile'),
    });
    const sourceAfter = treeSnapshot(frontendRoot, new Set(['node_modules']));
    const preexistingOutputAfter = treeSnapshot(preexistingOutputRoot);

    proof = {
      bootFallbackRetained,
      changedFiles: copiedOutputComparison.changedFiles,
      copiedOutputInventory: copiedOutputComparison.inventory,
      criticalCssPath,
      failed: failedReadback,
      normal: normalReadback,
      preexistingOutput: assertSnapshotUnchanged(
        preexistingOutputBefore,
        preexistingOutputAfter,
        'Pre-existing production output',
      ),
      sentinelMutations,
      sourceInputs: assertSnapshotUnchanged(sourceBefore, sourceAfter, 'Frontend source inputs'),
    };
  } finally {
    cleanupTemporaryPath(directory);
  }
  return {...proof, temporaryRootExistsAfter: existsSync(directory)};
}
