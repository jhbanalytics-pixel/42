/* The version line (11 October 2026): every 42 release carries a semantic
   version, so the team can say which one they are on. The build reads
   VERSION and the short git sha, bundles CHANGELOG.md, and the rail foot
   shows one quiet line under the theme that opens a What's new panel. When
   the server answers with another sha than the one the page was built from,
   a quiet hint offers a reload. The expected versions, dates and release ids
   are written here, never read from the file under test. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, describe, expect, test} from 'bun:test';
import React from 'react';
import {execFileSync} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {VersionNote, parseChangelog, shaDiffers, compareVersions, CHECK_GAP_MS, CHECK_EVERY_MS} = await import('../VersionNote.jsx');
const {Rail42} = await import('../../rail42.jsx');
const viteConfig = (await import('../../../vite.config.mjs')).default;
const {releaseFacts} = await import('../../../vite.config.mjs');

const REPO = fileURLToPath(new URL('../../../../../', import.meta.url));
const VERSION_TEXT = readFileSync(join(REPO, 'VERSION'), 'utf8');
const CHANGELOG = readFileSync(join(REPO, 'CHANGELOG.md'), 'utf8');
const HEAD = execFileSync('git', ['log', '-1', '--format=%H'], {cwd: REPO, encoding: 'utf8'}).trim();

/* A placeholder sha: no live build pairs 2.3.0 with a real commit yet. */
const BUILT = Object.freeze({version: '2.3.0', sha: '0123456789ab', changelog: CHANGELOG});
const realFetch = globalThis.fetch;

let host = null;
let root = null;
let calls = [];

const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));
const key = (el, name) => flushSync(() => el.dispatchEvent(new KeyboardEvent('keydown', {key: name, bubbles: true, cancelable: true})));
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function settle(){ for (let i = 0; i < 12; i++) await pause(0); }
const versionButton = () => host.querySelector('.rail42-version-button');
const panel = () => host.querySelector('dialog.rail42-whatsnew');
const hint = () => [...host.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Version changed, reload') || null;
const minutes = (n) => n * 60 * 1000;
/* Moves the clock the line reads, so a check can run under the default gap. */
function shiftClock(ms){
  const real = Date.now;
  Date.now = () => real() + ms;
  return () => { Date.now = real; };
}
const hiddenOrInert = (node) => { for (let at = node; at; at = at.parentElement) if (at.hidden || at.hasAttribute('inert')) return at; return null; };

function serveHealth(version, {fail = false} = {}){
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    if (fail) throw new TypeError('synthetic network failure');
    if (String(url) === '/api/health') return {ok: true, status: 200, json: async () => ({ok: true, service: 'f42-api', version})};
    return {ok: false, status: 404, json: async () => ({})};
  };
}

function render(node){
  flushSync(() => root.render(node));
}

function setHidden(hidden){
  Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
  Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => (hidden ? 'hidden' : 'visible')});
}

beforeEach(() => {
  calls = [];
  window.happyDOM.setViewport({width: 1280, height: 900});
  setHidden(false);
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  globalThis.fetch = realFetch;
  delete globalThis.__F42_RELEASE__;
  delete document.hidden;
  delete document.visibilityState;
});

afterAll(() => { GlobalRegistrator.unregister(); });

describe('the release files', () => {
  test('VERSION holds 2.3.0 and nothing else', () => {
    expect(VERSION_TEXT.trim()).toBe('2.3.0');
    expect(VERSION_TEXT).toMatch(/^2\.3\.0\r?\n?$/);
  });

  test('CHANGELOG.md opens with the numbering rule', () => {
    const head = CHANGELOG.split(/\n## /)[0];
    expect(head).toMatch(/major/i);
    expect(head).toMatch(/breaking change to stored data or a page removed/);
    expect(head).toMatch(/minor[^.]*release with new features/i);
    expect(head).toMatch(/patch[^.]*fixes only/i);
  });

  test('CHANGELOG.md says the number tracks the website, and a jobs-only release keeps it', () => {
    const head = CHANGELOG.split(/\n## /)[0];
    expect(head).toMatch(/number tracks what people see, the website/);
    expect(head).toMatch(/From D0 on, a jobs-only release keeps the version number/);
    expect(head).toMatch(/"Jobs update" entry under the current version/);
  });

  test('the changelog lists 2.3.0 next, then Release C, B and A, each with its date and plain lines', () => {
    const releases = parseChangelog(CHANGELOG);
    expect(releases.map((r) => r.version)).toEqual(['2.3.0', '2.2.0', '2.1.0', '2.0.0']);
    expect(releases.map((r) => r.next)).toEqual([true, false, false, false]);
    expect(releases[0].when).toBe('Next, not live yet');
    expect(releases[0].summary).toMatch(/\bD0\b/);
    /* The line naming a release stays true once it is live: release state
       is the heading's alone, and the panel reads it against VERSION. */
    for (const release of releases) expect(release.summary).not.toMatch(/\bnext\b|not live/i);
    expect(releases[1].when).toBe('11 October 2026');
    expect(releases[1].summary).toContain('Release C');
    expect(releases[1].summary).toContain('rel-d0aebe7-01');
    expect(releases[1].summary).toMatch(/website/i);
    expect(releases[1].summary).toMatch(/Ask/);
    expect(releases[2].when).toBe('11 October 2026');
    expect(releases[2].summary).toContain('Release B');
    expect(releases[2].summary).toContain('rel-89d4629-01');
    expect(releases[2].summary).toMatch(/jobs/i);
    expect(releases[3].summary).toContain('Release A');
    expect(releases[3].summary).toContain('rel-3710c5e-01');
    for (const release of releases) expect(release.lines.length).toBeGreaterThan(0);
    expect(releases[0].lines[0]).toBe('The menu shows the version 42 is on, with this list of what changed.');
    expect(releases[0].lines).toContain('Ask\'s "The numbers" tiles say what each figure counts.');
    expect(releases[1].lines).toContain('Instagram and X show their official marks.');
    expect(releases[2].lines).toContain('Stored runs record which build produced them.');
    expect(releases[3].lines).toContain('Today and Fieldwork read the culture desk and its kept creators in both the old and the new stored shape.');
    expect(releases.map((r) => r.updates)).toEqual([[], [], [], []]);
  });

  test('a Jobs update sits under its version with its own date and lines', () => {
    const text = [
      '## 2.3.0 (20 October 2026)', '', 'Release D0.', '', '- A website line.', '',
      '### Jobs update (24 October 2026)', '', '- A jobs line.', '- Another jobs line.', '',
      '## 2.2.0 (11 October 2026)', '', 'Release C.', '', '- An older line.',
    ].join('\n');
    const releases = parseChangelog(text);
    expect(releases.map((r) => r.version)).toEqual(['2.3.0', '2.2.0']);
    expect(releases[0].lines).toEqual(['A website line.']);
    expect(releases[0].updates).toEqual([{title: 'Jobs update', when: '24 October 2026', lines: ['A jobs line.', 'Another jobs line.']}]);
    expect(releases[1].lines).toEqual(['An older line.']);
  });

  test('VERSION is the newest changelog entry', () => {
    expect(parseChangelog(CHANGELOG)[0].version).toBe(VERSION_TEXT.trim());
  });

  test('the changelog is plain copy: no dashes and no retired names', () => {
    expect(CHANGELOG).not.toMatch(/\u2014|\u2013|[^-!|]-{2}[^\w-]/);
    expect(CHANGELOG).not.toMatch(/gen ?z|millennial|youth|Listening Post|\bLP\b/i);
  });
});

describe('the build', () => {
  test('releaseFacts reads VERSION, the changelog and the short sha of the checkout', () => {
    const facts = releaseFacts();
    expect(facts.version).toBe('2.3.0');
    expect(facts.changelog).toBe(CHANGELOG);
    expect(facts.sha).toBe(HEAD.slice(0, 12));
  });

  test('the vite config bundles those facts as one define', () => {
    expect(JSON.parse(viteConfig.define.__F42_RELEASE__)).toEqual(releaseFacts());
  });

  test('without git the sha comes from F42_VERSION, and with neither it is left out', () => {
    const noGit = () => { throw new Error('not a git checkout'); };
    expect(releaseFacts({git: noGit, env: {F42_VERSION: 'D0AEBE767B06'}}).sha).toBe('d0aebe767b06');
    expect(releaseFacts({git: noGit, env: {F42_VERSION: 'dev'}}).sha).toBeNull();
    expect(releaseFacts({git: noGit, env: {}}).sha).toBeNull();
  });

  test('git wins over F42_VERSION, so a stale variable cannot rename the build', () => {
    expect(releaseFacts({git: () => HEAD, env: {F42_VERSION: 'abcdefabcdef'}}).sha).toBe(HEAD.slice(0, 12));
  });

  test('a missing VERSION leaves the version out and a malformed one stops the build', () => {
    const dir = mkdtempSync(join(tmpdir(), 'f42-version-'));
    try {
      const missing = releaseFacts({root: dir, git: () => HEAD, env: {}});
      expect(missing.version).toBeNull();
      expect(missing.changelog).toBeNull();
      writeFileSync(join(dir, 'VERSION'), 'two point three\n');
      expect(() => releaseFacts({root: dir, git: () => HEAD, env: {}})).toThrow(/VERSION/);
    } finally {
      rmSync(dir, {recursive: true, force: true});
    }
  });
});

describe('shaDiffers', () => {
  test('the same commit, at any short length or case, is not a difference', () => {
    expect(shaDiffers('d0aebe767b06', 'd0aebe767b06')).toBe(false);
    expect(shaDiffers('d0aebe767b06', 'd0aebe7')).toBe(false);
    expect(shaDiffers('d0aebe7', 'D0AEBE767B064AF2')).toBe(false);
  });

  test('another commit is a difference', () => {
    expect(shaDiffers('d0aebe767b06', '3710c5e593027')).toBe(true);
  });

  test('a served value that is not a sha, or no built sha, is never a difference', () => {
    expect(shaDiffers('d0aebe767b06', 'dev')).toBe(false);
    expect(shaDiffers('d0aebe767b06', undefined)).toBe(false);
    expect(shaDiffers('d0aebe767b06', 'abc')).toBe(false);
    expect(shaDiffers(null, 'd0aebe767b06')).toBe(false);
  });
});

describe('the version line and What\'s new', () => {
  test('nothing renders when the build carries no version', () => {
    render(<VersionNote release={null} />);
    expect(host.innerHTML).toBe('');
    render(<VersionNote release={{version: null, sha: '0123456789ab', changelog: CHANGELOG}} />);
    expect(host.innerHTML).toBe('');
  });

  test('one quiet line names the version with the sha as secondary text', () => {
    render(<VersionNote release={BUILT} />);
    const button = versionButton();
    expect(button.getAttribute('type')).toBe('button');
    expect(button.querySelector('.rail42-version-name').textContent).toBe('42 v2.3.0');
    expect(button.querySelector('.rail42-version-sha').textContent).toBe('0123456');
    expect(button.getAttribute('aria-haspopup')).toBe('dialog');
    expect(button.getAttribute('aria-expanded')).toBe('false');
  });

  test('the accessible name starts with the visible text', () => {
    render(<VersionNote release={BUILT} />);
    const button = versionButton();
    const visible = [...button.children].filter((node) => !node.classList.contains('sr-only')).map((node) => node.textContent).join(' ');
    expect(visible).toBe('42 v2.3.0 0123456');
    expect(button.textContent).toBe('42 v2.3.0 0123456. What\'s new');
    expect(button.textContent.startsWith(visible)).toBe(true);
  });

  test('clicking opens What\'s new with every version, its date and its lines', () => {
    render(<VersionNote release={BUILT} />);
    expect(panel().open).toBe(false);
    click(versionButton());
    expect(panel().open).toBe(true);
    expect(versionButton().getAttribute('aria-expanded')).toBe('true');
    const title = document.getElementById(panel().getAttribute('aria-labelledby'));
    expect(title.textContent).toBe('What\'s new');
    const entries = [...panel().querySelectorAll('.rail42-whatsnew-release')];
    expect(entries.map((e) => e.querySelector('.rail42-whatsnew-version').textContent)).toEqual(['2.3.0', '2.2.0', '2.1.0', '2.0.0']);
    expect(entries[1].querySelector('.rail42-whatsnew-when').textContent).toBe('11 October 2026');
    const lines = entries.map((entry) => [...entry.querySelectorAll('li')].map((li) => li.textContent));
    expect(lines[0][0]).toBe('The menu shows the version 42 is on, with this list of what changed.');
    expect(lines[1]).toContain('Instagram and X show their official marks.');
    expect(lines[2]).toContain('Stored runs record which build produced them.');
    expect(lines[3]).toContain('Today and Fieldwork read the culture desk and its kept creators in both the old and the new stored shape.');
    expect(lines.map((list) => list.length)).toEqual([3, 8, 4, 3]);
    for (const line of lines.flat()) expect(line).not.toMatch(/^-/);
  });

  test('the version the page runs is marked This version and never Next, not live yet', () => {
    render(<VersionNote release={BUILT} />);
    click(versionButton());
    const entries = [...panel().querySelectorAll('.rail42-whatsnew-release')];
    expect(entries[0].querySelector('.rail42-whatsnew-version').textContent).toBe('2.3.0');
    expect(entries[0].textContent).not.toContain('Next, not live yet');
    expect(entries[0].querySelector('.rail42-whatsnew-current').textContent).toBe('This version');
    expect(entries.filter((entry) => entry.querySelector('.rail42-whatsnew-current'))).toHaveLength(1);
    expect(panel().textContent).not.toContain('Next, not live yet');
  });

  test('an entry above the running version, and only that, reads Next, not live yet', () => {
    render(<VersionNote release={{...BUILT, version: '2.2.0'}} />);
    click(versionButton());
    const entries = [...panel().querySelectorAll('.rail42-whatsnew-release')];
    expect(entries[0].querySelector('.rail42-whatsnew-when').textContent).toBe('Next, not live yet');
    expect(entries[0].querySelector('.rail42-whatsnew-current')).toBeNull();
    expect(entries[1].querySelector('.rail42-whatsnew-when').textContent).toBe('11 October 2026');
    expect(entries[1].querySelector('.rail42-whatsnew-current').textContent).toBe('This version');
    expect(entries.slice(1).some((entry) => entry.textContent.includes('Next, not live yet'))).toBe(false);
  });

  test('a Jobs update renders under its version with its date', () => {
    const changelog = '## 2.3.0 (20 October 2026)\n\nRelease D0.\n\n- A website line.\n\n### Jobs update (24 October 2026)\n\n- A jobs line.\n';
    render(<VersionNote release={{...BUILT, changelog}} />);
    click(versionButton());
    const update = panel().querySelector('.rail42-whatsnew-update');
    expect(update.querySelector('.rail42-whatsnew-update-title').textContent).toBe('Jobs update');
    expect(update.querySelector('.rail42-whatsnew-when').textContent).toBe('24 October 2026');
    expect([...update.querySelectorAll('li')].map((li) => li.textContent)).toEqual(['A jobs line.']);
  });

  test('a click on the backdrop closes the panel, a click inside it does not', () => {
    render(<VersionNote release={BUILT} />);
    click(versionButton());
    click(panel().querySelector('.rail42-whatsnew-title'));
    expect(panel().open).toBe(true);
    click(panel());
    expect(panel().open).toBe(false);
  });

  test('Escape closes the panel, goes no further, and gives focus back to the line', () => {
    let outer = 0;
    render(<div onKeyDown={(event) => { if (event.key === 'Escape') outer += 1; }}><VersionNote release={BUILT} /></div>);
    versionButton().focus();
    click(versionButton());
    key(panel(), 'Escape');
    expect(panel().open).toBe(false);
    expect(versionButton().getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(versionButton());
    expect(outer).toBe(0);
  });

  test('the Close button closes the panel and gives focus back to the line', () => {
    render(<VersionNote release={BUILT} />);
    click(versionButton());
    const close = [...panel().querySelectorAll('button')].find((b) => b.textContent.trim() === 'Close');
    click(close);
    expect(panel().open).toBe(false);
    expect(document.activeElement).toBe(versionButton());
  });
});

describe('the reload hint', () => {
  test('a newer server sha shows the hint when the reader comes back to the tab', async () => {
    serveHealth('3710c5e59302');
    render(<VersionNote release={BUILT} gapMs={0} />);
    expect(hint()).toBeNull();
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(calls).toEqual(['/api/health']);
    expect(hint()).not.toBeNull();
  });

  test('the slow interval checks too', async () => {
    serveHealth('3710c5e59302');
    render(<VersionNote release={BUILT} gapMs={0} everyMs={15} />);
    await pause(60);
    await settle();
    expect(calls.length).toBeGreaterThan(0);
    expect(hint()).not.toBeNull();
  });

  test('the same sha, a dev server and a failed read show nothing', async () => {
    for (const setup of [() => serveHealth('0123456789ab'), () => serveHealth('0123456'), () => serveHealth('dev'), () => serveHealth('', {fail: true})]){
      setup();
      render(<VersionNote key={calls.length} release={BUILT} gapMs={0} />);
      document.dispatchEvent(new Event('visibilitychange'));
      await settle();
      expect(hint()).toBeNull();
    }
  });

  test('a hidden tab is not checked, even with no gap', async () => {
    serveHealth('3710c5e59302');
    setHidden(true);
    render(<VersionNote release={BUILT} gapMs={0} />);
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(calls).toEqual([]);
    setHidden(false);
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(calls).toEqual(['/api/health']);
  });

  test('the defaults check every 15 minutes and never within 5 minutes of the last check', async () => {
    expect(CHECK_GAP_MS).toBe(5 * 60 * 1000);
    expect(CHECK_EVERY_MS).toBe(15 * 60 * 1000);
    const realInterval = window.setInterval;
    const delays = [];
    window.setInterval = (fn, ms, ...rest) => { delays.push(ms); return realInterval(fn, ms, ...rest); };
    let restore = () => {};
    try {
      serveHealth('3710c5e59302');
      render(<VersionNote release={BUILT} />);
      expect(delays).toContain(15 * 60 * 1000);
      restore = shiftClock(minutes(4));
      document.dispatchEvent(new Event('visibilitychange'));
      await settle();
      expect(calls).toEqual([]);
      restore();
      restore = shiftClock(minutes(6));
      document.dispatchEvent(new Event('visibilitychange'));
      await settle();
      expect(calls).toEqual(['/api/health']);
    } finally {
      restore();
      window.setInterval = realInterval;
    }
  });

  test('a build with no sha never polls', async () => {
    serveHealth('3710c5e59302');
    render(<VersionNote release={{...BUILT, sha: null}} gapMs={0} everyMs={10} />);
    document.dispatchEvent(new Event('visibilitychange'));
    await pause(40);
    expect(calls).toEqual([]);
  });

  test('a health answer that is not ok shows nothing', async () => {
    globalThis.fetch = async (url) => { calls.push(String(url)); return {ok: false, status: 503, json: async () => ({version: '3710c5e59302'})}; };
    render(<VersionNote release={BUILT} gapMs={0} />);
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(calls).toEqual(['/api/health']);
    expect(hint()).toBeNull();
  });

  test('a rollback reads Version changed, not New version live, and the hint clears when the build is live again', async () => {
    serveHealth('3710c5e59302');
    render(<VersionNote release={BUILT} gapMs={0} />);
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(hint()).not.toBeNull();
    expect(host.textContent).not.toMatch(/new version/i);
    expect(hint().closest('[role="status"]')).not.toBeNull();
    serveHealth('0123456789ab');
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(hint()).toBeNull();
  });

  test('no check runs on mount: the page was just served, and App already reads health', async () => {
    serveHealth('3710c5e59302');
    render(<VersionNote release={BUILT} gapMs={0} />);
    await settle();
    expect(calls).toEqual([]);
  });

  test('the hint reloads the page', async () => {
    serveHealth('3710c5e59302');
    let reloads = 0;
    render(<VersionNote release={BUILT} gapMs={0} onReload={() => { reloads += 1; }} />);
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    click(hint());
    expect(reloads).toBe(1);
  });

  test('the check stops when the line unmounts', async () => {
    serveHealth('3710c5e59302');
    render(<VersionNote release={BUILT} gapMs={0} everyMs={10} />);
    flushSync(() => root.unmount());
    root = createRoot(host);
    document.dispatchEvent(new Event('visibilitychange'));
    await pause(40);
    expect(calls).toEqual([]);
  });
});

describe('in the rail', () => {
  const footOrder = (scope) => [...scope.querySelectorAll('select, button')]
    .filter((node) => node.tagName === 'SELECT' || ['rail42-version-button', 'rail42-signout-button'].some((name) => node.classList.contains(name)))
    .map((node) => (node.tagName === 'SELECT' ? 'Theme' : node.classList.contains('rail42-version-button') ? 'Version' : 'Log out'));
  const mountRail = () => render(
    <Rail42 route="pulse" askOpen={false} theme="daylight" onThemeChange={() => {}} onSignOut={() => {}} />,
  );

  test('the wide rail foot reads Theme, then the version line, then Log out', () => {
    globalThis.__F42_RELEASE__ = BUILT;
    mountRail();
    const foot = host.querySelector('.rail42-foot');
    expect(footOrder(foot)).toEqual(['Theme', 'Version', 'Log out']);
    expect(host.querySelectorAll('.rail42-version-button')).toHaveLength(1);
  });

  test('without a built version the foot is as before', () => {
    mountRail();
    expect(footOrder(host.querySelector('.rail42-foot'))).toEqual(['Theme', 'Log out']);
  });

  test('at phone width the More sheet reads Theme, the version line, then Log out', () => {
    globalThis.__F42_RELEASE__ = BUILT;
    window.happyDOM.setViewport({width: 390, height: 844});
    mountRail();
    const more = [...host.querySelectorAll('button')].find((b) => b.textContent.trim().startsWith('More'));
    click(more);
    const sheet = document.getElementById(more.getAttribute('aria-controls'));
    expect(footOrder(sheet)).toEqual(['Theme', 'Version', 'Log out']);
  });

  test('Escape in the panel inside the phone sheet closes only the panel', () => {
    globalThis.__F42_RELEASE__ = BUILT;
    window.happyDOM.setViewport({width: 390, height: 844});
    mountRail();
    const more = [...host.querySelectorAll('button')].find((b) => b.textContent.trim().startsWith('More'));
    click(more);
    versionButton().focus();
    click(versionButton());
    expect(panel().open).toBe(true);
    key(panel(), 'Escape');
    expect(panel().open).toBe(false);
    expect(more.getAttribute('aria-expanded')).toBe('true');
    expect(document.activeElement).toBe(versionButton());
  });

  test('when the phone sheet shuts under an open panel, the panel closes with it', async () => {
    globalThis.__F42_RELEASE__ = BUILT;
    window.happyDOM.setViewport({width: 390, height: 844});
    mountRail();
    const more = [...host.querySelectorAll('button')].find((b) => b.textContent.trim().startsWith('More'));
    click(more);
    click(versionButton());
    expect(panel().open).toBe(true);
    render(<Rail42 route="ask" askOpen={false} theme="daylight" onThemeChange={() => {}} onSignOut={() => {}} />);
    await settle();
    expect(more.getAttribute('aria-expanded')).toBe('false');
    expect(panel().open).toBe(false);
  });

  test('at 390 px a changed version is shown and announced outside the closed More sheet', async () => {
    globalThis.__F42_RELEASE__ = BUILT;
    window.happyDOM.setViewport({width: 390, height: 844});
    serveHealth('3710c5e59302');
    mountRail();
    const more = [...host.querySelectorAll('button')].find((b) => b.textContent.trim().startsWith('More'));
    expect(more.getAttribute('aria-expanded')).toBe('false');
    expect(host.querySelector('.rail42-more-dot')).toBeNull();
    const restore = shiftClock(minutes(6));
    try {
      document.dispatchEvent(new Event('visibilitychange'));
      await settle();
    } finally {
      restore();
    }
    expect(calls).toEqual(['/api/health']);
    expect(more.getAttribute('aria-expanded')).toBe('false');
    const dot = more.querySelector('.rail42-more-dot');
    expect(dot).not.toBeNull();
    expect(hiddenOrInert(dot)).toBeNull();
    expect(more.textContent).toContain('version changed');
    const sheet = document.getElementById(more.getAttribute('aria-controls'));
    const announced = [...host.querySelectorAll('[role="status"]')].filter((node) => !sheet.contains(node));
    expect(announced).toHaveLength(1);
    expect(hiddenOrInert(announced[0])).toBeNull();
    expect(announced[0].textContent).toBe('Version changed. Open More to reload.');
    click(more);
    expect(hint()).not.toBeNull();
    expect(hiddenOrInert(hint())).toBeNull();
  });

  test('the wide rail announces a changed version once, beside the line', async () => {
    globalThis.__F42_RELEASE__ = BUILT;
    serveHealth('3710c5e59302');
    mountRail();
    const restore = shiftClock(minutes(6));
    try {
      document.dispatchEvent(new Event('visibilitychange'));
      await settle();
    } finally {
      restore();
    }
    expect(hint()).not.toBeNull();
    expect(hiddenOrInert(hint())).toBeNull();
    expect(host.querySelector('.rail42-more-dot')).toBeNull();
    expect([...host.querySelectorAll('[role="status"]')].filter((node) => node.textContent.trim())).toHaveLength(1);
  });

  test('the rail sheet keeps red to focus and every new rule on rail tokens', async () => {
    const css = await Bun.file(new URL('../../styles/rail42.css', import.meta.url)).text();
    const rules = css.replace(/\/\*[^]*?\*\//g, '').split('}').filter((rule) => /rail42-(version|whatsnew)/.test(rule));
    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules){
      if (/--accent/.test(rule)) expect(rule).toMatch(/:focus-visible/);
      expect(rule).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    }
  });
});

describe('compareVersions', () => {
  test('orders major, minor and patch as numbers', () => {
    expect(compareVersions('2.3.0', '2.2.0')).toBeGreaterThan(0);
    expect(compareVersions('2.2.0', '2.3.0')).toBeLessThan(0);
    expect(compareVersions('2.3.0', '2.3.0')).toBe(0);
    expect(compareVersions('2.10.0', '2.9.0')).toBeGreaterThan(0);
    expect(compareVersions('3.0.0', '2.99.99')).toBeGreaterThan(0);
  });
});
