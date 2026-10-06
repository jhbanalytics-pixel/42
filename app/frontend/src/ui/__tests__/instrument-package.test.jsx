import {expect, test} from 'bun:test';
import {createHash} from 'node:crypto';
import {existsSync, mkdtempSync, readFileSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';

import {
  assertPinnedPackageLock,
  cleanupTemporaryPath,
  compareCopiedOutputs,
  probeProductionStyleBoundary,
  resolveChrome,
  runBoundedSubprocess,
} from './task4-proof-helpers.js';

const APPROVED_ARCHIVE_SHA256 = '2E86002374B1C4BA4127932D22319091EA9FCBC00443776D41E81DDD344276E6';
const APPROVED_EVIDENCE_COMMIT = 'bf1e45a2e01e814ba47f64b307684d746f3e691f';
const APPROVED_SOURCE_COMMIT = '61c42d1be2658e1f3dacdbf317683e81a86f4b6a';
const frontendRoot = fileURLToPath(new URL('../../../', import.meta.url));
const archivePath = join(frontendRoot, 'vendor', 'ogilvy-intelligence-design-system-2.0.22.tgz');
const shaPath = join(frontendRoot, 'vendor', 'ogilvy-intelligence-design-system-2.0.22.sha256');
const EXPECTED_LOCK_RESOLUTION = 'ogilvy-intelligence-design-system@vendor/ogilvy-intelligence-design-system-2.0.22.tgz';
const EXPECTED_LOCK_INTEGRITY = 'sha512-j7HRpNTWINmkGP25Q4knlZ9x7FTde62rMlx9kZAHm/ErZDw737C82rRkOE6lIc71OyJjn5B+KbYGWV8Sc+k0Xw==';
const CHROME = resolveChrome();

function archivedJson(path){
  const result = Bun.spawnSync([
    'tar',
    '-xOzf',
    archivePath,
    path,
  ]);
  if (result.exitCode !== 0) throw new Error(result.stderr.toString());
  return JSON.parse(result.stdout.toString());
}

test('vendored package bytes match the approved signed archive receipt', () => {
  const recordedSha = readFileSync(shaPath, 'utf8').trim();
  const archiveSha = createHash('sha256').update(readFileSync(archivePath)).digest('hex').toUpperCase();

  expect(recordedSha).toBe(APPROVED_ARCHIVE_SHA256);
  expect(archiveSha).toBe(APPROVED_ARCHIVE_SHA256);
});

test('vendored archive metadata matches the approved package contract and commits', async () => {
  const packageMetadata = archivedJson('package/package.json');
  const archivedManifest = archivedJson('package/dist/manifest.json');
  const installedManifest = await import('ogilvy-intelligence-design-system/manifest.json', {
    with: {type: 'json'},
  }).then((module) => module.default);

  expect(packageMetadata.name).toBe('ogilvy-intelligence-design-system');
  expect(packageMetadata.version).toBe('2.0.22');
  expect(packageMetadata.exports['./style.css']).toBe('./dist/style.css');
  expect(packageMetadata.exports['./manifest.json']).toBe('./dist/manifest.json');
  expect(archivedManifest.package).toEqual({name: packageMetadata.name, version: packageMetadata.version});
  expect(archivedManifest.contractVersion).toBe('1.2.0');
  expect(archivedManifest.sourceCommit).toBe(APPROVED_SOURCE_COMMIT);
  expect(archivedManifest.packageSourceCommit).toBe(APPROVED_SOURCE_COMMIT);
  expect(archivedManifest.evidenceCommit).toBe(APPROVED_EVIDENCE_COMMIT);
  expect(archivedManifest.gateBEvidence).toMatchObject({
    receiptCommit: APPROVED_EVIDENCE_COMMIT,
    sourceCommit: APPROVED_SOURCE_COMMIT,
    status: 'passed',
  });
  expect(installedManifest).toEqual(archivedManifest);
});

test('package dependency and lock resolve only the vendored archive', () => {
  const packageJson = JSON.parse(readFileSync(join(frontendRoot, 'package.json'), 'utf8'));
  const lock = readFileSync(join(frontendRoot, 'bun.lock'), 'utf8');
  const dependency = 'file:vendor/ogilvy-intelligence-design-system-2.0.22.tgz';

  expect(packageJson.dependencies['ogilvy-intelligence-design-system']).toBe(dependency);
  expect(assertPinnedPackageLock(lock)).toEqual({
    dependency,
    integrity: EXPECTED_LOCK_INTEGRITY,
    resolution: EXPECTED_LOCK_RESOLUTION,
  });
});

test('lock proof rejects alternate local linked workspace malformed and changed-integrity records', () => {
  const lock = readFileSync(join(frontendRoot, 'bun.lock'), 'utf8');
  const mutations = [
    ['alternate local', EXPECTED_LOCK_RESOLUTION, 'ogilvy-intelligence-design-system@vendor/other-2.0.0.tgz'],
    ['linked', EXPECTED_LOCK_RESOLUTION, 'ogilvy-intelligence-design-system@link:vendor/ogilvy-intelligence-design-system-2.0.22.tgz'],
    ['workspace', EXPECTED_LOCK_RESOLUTION, 'ogilvy-intelligence-design-system@workspace:*'],
    ['malformed', EXPECTED_LOCK_RESOLUTION, 'ogilvy-intelligence-design-system'],
    ['changed integrity', EXPECTED_LOCK_INTEGRITY, `sha512-${'A'.repeat(88)}`],
  ];

  for (const [name, from, to] of mutations){
    const mutated = lock.replace(from, to);
    expect(mutated, `${name} mutation must alter the lock`).not.toBe(lock);
    expect(() => assertPinnedPackageLock(mutated), name).toThrow();
  }
});

test('required production browser executable must exist', async () => {
  expect(CHROME).not.toBeNull();
  expect(existsSync(CHROME)).toBe(true);
  const missing = join(frontendRoot, 'missing-task-4-chrome.exe');
  await expect(probeProductionStyleBoundary({chromePath: missing, frontendRoot}))
    .rejects.toThrow(`Chrome executable is required: ${missing}`);
});

test('bounded browser lifecycle terminates a hang and escalates to force kill', async () => {
  const fakeProcess = (exitSignal) => {
    let finish;
    const signals = [];
    return {
      exited: new Promise((resolve) => { finish = resolve; }),
      kill(signal){
        signals.push(signal);
        if (signal === exitSignal) finish(signal === 'SIGTERM' ? 143 : 137);
      },
      signals,
    };
  };
  const graceful = fakeProcess('SIGTERM');
  const gracefulResult = await runBoundedSubprocess(graceful, {
    exitDeadlineMs: 5,
    forceExitDeadlineMs: 20,
    terminateDeadlineMs: 10,
  });
  expect(graceful.signals).toEqual(['SIGTERM']);
  expect(gracefulResult).toEqual({exited: true, forceKilled: false, status: 143, terminated: true});

  const forced = fakeProcess('SIGKILL');
  const forcedResult = await runBoundedSubprocess(forced, {
    exitDeadlineMs: 5,
    forceExitDeadlineMs: 20,
    terminateDeadlineMs: 5,
  });
  expect(forced.signals).toEqual(['SIGTERM', 'SIGKILL']);
  expect(forcedResult).toEqual({exited: true, forceKilled: true, status: 137, terminated: true});
});

test('temporary profile cleanup removes nested browser state', () => {
  const profile = mkdtempSync(join(tmpdir(), 'lp-task-4-cleanup-mutation-'));
  writeFileSync(join(profile, 'SingletonLock'), 'held', 'utf8');

  expect(existsSync(profile)).toBe(true);
  cleanupTemporaryPath(profile);
  expect(existsSync(profile)).toBe(false);
});

test.skipIf(process.platform !== 'win32')('temporary profile cleanup waits for a releasing Windows file lock', async () => {
  const profile = mkdtempSync(join(tmpdir(), 'lp-cleanup-locked-'));
  const file = join(profile, 'locked.txt');
  const ready = join(profile, 'ready.txt');
  writeFileSync(file, 'temporary fixture', 'utf8');
  const quote = (value) => value.replaceAll("'", "''");
  const script = `$stream = [IO.File]::Open('${quote(file)}', 'Open', 'ReadWrite', 'None'); [IO.File]::WriteAllText('${quote(ready)}', 'ready'); Start-Sleep -Milliseconds 500; $stream.Dispose()`;
  const child = spawn('C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {windowsHide: true, stdio: 'ignore'});
  const exited = new Promise((resolve) => child.once('exit', resolve));
  try {
    const deadline = Date.now() + 5000;
    while (!existsSync(ready) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 20));
    expect(existsSync(ready)).toBe(true);
    cleanupTemporaryPath(profile);
    expect(existsSync(profile)).toBe(false);
  } finally {
    await exited;
    cleanupTemporaryPath(profile);
  }
}, 10000);

test('copied-output proof rejects a failed-only extra file', () => {
  const normal = {'assets/index.css': 'normal-css', 'index.html': 'same-html'};
  const failed = {
    'assets/index.css': 'failed-css',
    'assets/unexpected.js': 'failed-only',
    'index.html': 'same-html',
  };

  expect(() => compareCopiedOutputs(normal, failed, 'assets/index.css'))
    .toThrow('Copied output inventory mismatch: added assets/unexpected.js; removed none');
});

test('copied-output proof rejects a failed-only removed file', () => {
  const normal = {
    'assets/index.css': 'normal-css',
    'assets/required.js': 'same-js',
    'index.html': 'same-html',
  };
  const failed = {'assets/index.css': 'failed-css', 'index.html': 'same-html'};

  expect(() => compareCopiedOutputs(normal, failed, 'assets/index.css'))
    .toThrow('Copied output inventory mismatch: added none; removed assets/required.js');
});

test('copied-output proof rejects a non-sentinel hash change', () => {
  const normal = {'assets/index.css': 'normal-css', 'index.html': 'normal-html'};
  const failed = {'assets/index.css': 'failed-css', 'index.html': 'changed-html'};

  expect(() => compareCopiedOutputs(normal, failed, 'assets/index.css'))
    .toThrow('Unexpected copied output changes: assets/index.css, index.html');
});

test('production output exposes a visible boot-owned fallback when the package sentinel is corrupted', async () => {
  const proof = await probeProductionStyleBoundary({chromePath: CHROME, frontendRoot});

  expect(proof.sourceInputs.unchanged).toBe(true);
  expect(proof.sourceInputs.before).toEqual(proof.sourceInputs.after);
  expect(proof.preexistingOutput.unchanged).toBe(true);
  expect(proof.preexistingOutput.before).toEqual(proof.preexistingOutput.after);
  expect(proof.changedFiles).toEqual([proof.criticalCssPath]);
  expect(proof.sentinelMutations).toBe(1);
  expect(proof.bootFallbackRetained).toBe(true);
  expect(proof.normal).toMatchObject({state: 'ready', alertCount: 0});
  expect(proof.normal.lifecycle).toMatchObject({exited: true, profileExistsAfter: false});
  expect(proof.failed.state).toBe('failed');
  expect(proof.failed.alertCount).toBe(1);
  expect(proof.failed.alertRole).toBe('alert');
  expect(proof.failed.alertText).toContain('42 could not finish opening.');
  expect(proof.failed.frame).toMatchObject({
    backgroundColor: 'rgb(21, 18, 20)',
    color: 'rgb(245, 241, 243)',
    display: 'grid',
    position: 'fixed',
  });
  expect(proof.failed.alert).toMatchObject({
    backgroundColor: 'rgb(250, 248, 249)',
    borderTopColor: 'rgb(157, 38, 59)',
    color: 'rgb(38, 30, 35)',
    borderTopWidth: '1px',
    centerHitWithinAlert: true,
    display: 'block',
    intersectsViewport: true,
    occludingElement: null,
    opacity: '1',
    paddingTop: '48px',
    visibility: 'visible',
  });
  expect(proof.failed.alert.width).toBeGreaterThan(0);
  expect(proof.failed.alert.height).toBeGreaterThan(0);
  expect(proof.failed.frame.width).toBe(proof.failed.viewport.width);
  expect(proof.failed.frame.height).toBe(proof.failed.viewport.height);
  expect(proof.failed.lifecycle).toMatchObject({exited: true, profileExistsAfter: false});
  expect(proof.temporaryRootExistsAfter).toBe(false);
}, 60000);
