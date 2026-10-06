import {expect, test} from 'bun:test';
import {spawnSync} from 'node:child_process';
import {existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {build} from 'esbuild';

function resolveChrome(){
  const candidates = [
    process.env.CHROME_PATH,
    process.env.CHROME_BIN,
    String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
    String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
    '/usr/bin/google-chrome',
    '/usr/bin/chromium-browser',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate)) || null;
}

const CHROME = resolveChrome();

async function mountedGateGeometry({width, height, screenshotName, transitionProbe = false}){
  const root = fileURLToPath(new URL('../../../../', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'lp-gate-geometry-'));
  const bundlePath = join(directory, 'probe.js');
  const htmlPath = join(directory, 'probe.html');
  const profilePath = join(directory, 'chrome-profile');
  const styles = [
    '../../tokens.css',
    '../../app.css',
    '../../styles/ogilvy-intelligence.css',
    '../../styles/gate.css',
  ].map((path) => readFileSync(new URL(path, import.meta.url), 'utf8')).join('\n');
  const entry = `
    import React from 'react';
    import {createRoot} from 'react-dom/client';
    import {flushSync} from 'react-dom';
    import {PasscodeScreen} from './frontend/src/passcode.jsx';
    const result = document.getElementById('result');
    const wait = (ms=25) => new Promise((resolve) => setTimeout(resolve, ms));
    const encode = (value) => btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(value))));
    (async () => {
      try {
        const frame = document.getElementById('frame');
        const deadline = Date.now() + 10000;
        let frameDocument;
        let mount;
        while (Date.now() < deadline){
          frameDocument = frame.contentDocument;
          mount = frameDocument && frameDocument.getElementById('root');
          if (mount && frameDocument.readyState !== 'loading') break;
          await wait();
        }
        if (!mount) throw new Error('frame mount never appeared within 10s');
        const frameWindow = frame.contentWindow;
        const transitionProbe = ${transitionProbe ? 'true' : 'false'};
        let responsiveQuery;
        let addedListeners = 0;
        let removedListeners = 0;
        if (transitionProbe){
          const nativeMatchMedia = frameWindow.matchMedia.bind(frameWindow);
          const listeners = new Set();
          responsiveQuery = {
            matches: false,
            addEventListener: (event, listener) => {
              if (event !== 'change') throw new Error('unexpected media query event');
              addedListeners += 1;
              listeners.add(listener);
            },
            removeEventListener: (event, listener) => {
              if (event !== 'change') throw new Error('unexpected media query event');
              if (listeners.delete(listener)) removedListeners += 1;
            },
            emit: () => listeners.forEach((listener) => listener()),
            listenerCount: () => listeners.size,
          };
          frameWindow.matchMedia = (query) => (
            query === '(max-width: 760px)' ? responsiveQuery : nativeMatchMedia(query)
          );
        }
        const reactRoot = createRoot(mount);
        flushSync(() => reactRoot.render(
          React.createElement(PasscodeScreen, {onSubmit: () => {}, failed: false})));
        if (frameDocument.fonts && frameDocument.fonts.ready) await frameDocument.fonts.ready;
        await wait();
        const documentElement = frameDocument.documentElement;
        const rect = (selector) => {
          const value = frameDocument.querySelector(selector).getBoundingClientRect();
          return {top: value.top, right: value.right, bottom: value.bottom, left: value.left,
            width: value.width, height: value.height};
        };
        const access = rect('.gate-v4-access');
        const field = rect('.gate-v4-field');
        const input = rect('#gate-passcode');
        const submit = rect('.gate-v4-submit');
        const domOrder = Array.from(frameDocument.querySelector('.gate-v4-layout').children)
          .map((element) => element.className);
        const escaping = Array.from(frameDocument.querySelectorAll('.gate-v4 *'))
          .filter((element) => {
            const value = element.getBoundingClientRect();
            if (value.width === 0 && value.height === 0) return false;
            return value.right > documentElement.clientWidth + 0.5 || value.left < -0.5;
          })
          .map((element) => element.className || element.id || element.tagName);
        const measured = {
          viewport: [frameWindow.innerWidth, frameWindow.innerHeight],
          documentWidth: documentElement.clientWidth,
          scrollWidth: documentElement.scrollWidth,
          bodyScrollWidth: frameDocument.body.scrollWidth,
          access,
          field,
          input,
          submit,
          domOrder,
          escaping,
          reducedMotion: frameWindow.matchMedia('(prefers-reduced-motion: reduce)').matches,
          progressTransition: frameWindow.getComputedStyle(frameDocument.querySelector('.gate-v4-progress')).transitionDuration,
          submitTransition: frameWindow.getComputedStyle(frameDocument.querySelector('.gate-v4-submit')).transitionDuration,
        };
        if (transitionProbe){
          const initialInput = frameDocument.getElementById('gate-passcode');
          const valueSetter = Object.getOwnPropertyDescriptor(
            frameWindow.HTMLInputElement.prototype, 'value').set;
          valueSetter.call(initialInput, 'typed-value');
          initialInput.dispatchEvent(new frameWindow.Event('input', {bubbles: true}));
          initialInput.focus();
          await wait();
          responsiveQuery.matches = true;
          responsiveQuery.emit();
          await wait();
          const mobileInput = frameDocument.getElementById('gate-passcode');
          const mobileOrder = Array.from(frameDocument.querySelector('.gate-v4-layout').children)
            .map((element) => element.className);
          responsiveQuery.matches = false;
          responsiveQuery.emit();
          await wait();
          const desktopInput = frameDocument.getElementById('gate-passcode');
          const desktopOrder = Array.from(frameDocument.querySelector('.gate-v4-layout').children)
            .map((element) => element.className);
          measured.lifecycle = {
            mobileOrder,
            desktopOrder,
            mobileSameInput: mobileInput === initialInput,
            desktopSameInput: desktopInput === initialInput,
            mobileValue: mobileInput.value,
            desktopValue: desktopInput.value,
            mobileFocused: frameDocument.activeElement === mobileInput,
            desktopFocused: frameDocument.activeElement === desktopInput,
            addedListeners,
          };
          flushSync(() => reactRoot.unmount());
          measured.lifecycle.removedListeners = removedListeners;
          measured.lifecycle.remainingListeners = responsiveQuery.listenerCount();
        }
        result.textContent = encode(measured);
      } catch (error) {
        result.textContent = encode({error: String(error && error.stack || error)});
      }
    })();
  `;
  try {
    await build({
      stdin: {contents: entry, loader: 'jsx', resolveDir: root, sourcefile: 'gate-geometry.jsx'},
      bundle: true,
      define: {'process.env.NODE_ENV': '"production"'},
      format: 'iife',
      jsx: 'automatic',
      nodePaths: [join(root, 'frontend', 'node_modules')],
      outfile: bundlePath,
      platform: 'browser',
      plugins: [{
        name: 'css-injected-by-the-harness',
        setup(builder){
          builder.onResolve({filter: /\.css$/}, () => ({path: 'empty-css', namespace: 'gate-probe'}));
          builder.onLoad({filter: /.*/, namespace: 'gate-probe'}, () => ({contents: '', loader: 'js'}));
        },
      }],
    });
    const frameDocument = '<!doctype html><html data-dir="daylight"><head>'
      + '<meta name="viewport" content="width=device-width,initial-scale=1">'
      + `<style>${styles}</style></head><body><div id="root"></div></body></html>`;
    writeFileSync(
      htmlPath,
      '<!doctype html><html><head><style>*{margin:0;padding:0}</style></head><body>'
        + `<iframe id="frame" style="width:${width}px;height:${height}px;border:0"`
        + ` srcdoc="${frameDocument.replace(/"/g, '&quot;')}"></iframe>`
        + '<pre id="result">pending</pre><script src="./probe.js"></script></body></html>',
      'utf8',
    );
    const chromeArgs = [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--run-all-compositor-stages-before-draw', '--hide-scrollbars',
      '--force-prefers-reduced-motion',
      `--window-size=${Math.max(width, 900)},${Math.max(height, 900)}`,
      '--virtual-time-budget=6000', `--user-data-dir=${profilePath}`,
    ];
    const run = spawnSync(CHROME, [...chromeArgs, '--dump-dom', pathToFileURL(htmlPath).href], {
      encoding: 'utf8', timeout: 25000, windowsHide: true,
    });
    if (run.error) throw run.error;
    if (run.status !== 0) throw new Error(`Chrome exited ${run.status}: ${String(run.stderr).slice(0, 400)}`);
    const encoded = run.stdout.match(/<pre id="result">([^<]+)<\/pre>/)?.[1];
    if (!encoded) throw new Error('the gate probe produced no measurement block');
    const measured = JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'));
    expect(measured.error).toBeUndefined();
    if (process.env.GATE_GEOMETRY_RECEIPT === '1'){
      console.info(`GATE_GEOMETRY ${width}x${height} ${JSON.stringify(measured)}`);
    }

    if (process.env.GATE_SCREENSHOT_DIR){
      mkdirSync(process.env.GATE_SCREENSHOT_DIR, {recursive: true});
      const screenshotPath = join(process.env.GATE_SCREENSHOT_DIR, screenshotName);
      const screenshot = spawnSync(CHROME, [
        ...chromeArgs.filter((arg) => !arg.startsWith('--user-data-dir=')),
        `--user-data-dir=${join(directory, 'screenshot-profile')}`,
        `--screenshot=${screenshotPath}`,
        pathToFileURL(htmlPath).href,
      ], {encoding: 'utf8', timeout: 25000, windowsHide: true});
      if (screenshot.error) throw screenshot.error;
      if (screenshot.status !== 0){
        throw new Error(`Chrome screenshot exited ${screenshot.status}: ${String(screenshot.stderr).slice(0, 400)}`);
      }
    }
    return measured;
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}

test.skipIf(!CHROME)('mobile gate puts access first within a 390 by 844 viewport', async () => {
  const measured = await mountedGateGeometry({
    width: 390,
    height: 844,
    screenshotName: `${process.env.GATE_SCREENSHOT_PHASE || 'current'}-gate-mobile-frame.png`,
  });

  expect(measured.viewport).toEqual([390, 844]);
  expect(measured.documentWidth).toBe(390);
  expect(measured.scrollWidth).toBe(390);
  expect(measured.bodyScrollWidth).toBe(390);
  expect(measured.escaping).toEqual([]);
  expect(measured.domOrder).toEqual(['gate-v4-access', 'gate-v4-field']);
  expect(measured.access.top).toBeLessThan(measured.field.top);
  expect(measured.input.top).toBeLessThan(844);
  expect(measured.input.height).toBeGreaterThanOrEqual(48);
  expect(measured.submit.height).toBeGreaterThanOrEqual(48);
  expect(measured.reducedMotion).toBe(true);
  const isImmediate = (duration) => Number.parseFloat(duration) <= 0.000001;
  expect(isImmediate(measured.progressTransition)).toBe(true);
  expect(measured.submitTransition.split(',').every(isImmediate)).toBe(true);
}, 60000);

test.skipIf(!CHROME)('desktop gate retains its field first split composition', async () => {
  const measured = await mountedGateGeometry({
    width: 1440,
    height: 1000,
    screenshotName: `${process.env.GATE_SCREENSHOT_PHASE || 'current'}-gate-desktop-frame.png`,
  });

  expect(measured.viewport).toEqual([1440, 1000]);
  expect(measured.documentWidth).toBe(1440);
  expect(measured.scrollWidth).toBe(1440);
  expect(measured.domOrder).toEqual(['gate-v4-field', 'gate-v4-access']);
  expect(measured.field.top).toBe(measured.access.top);
  expect(measured.field.left).toBe(0);
  expect(measured.field.right).toBe(measured.access.left);
  expect(measured.field.width).toBeGreaterThan(measured.access.width);
}, 60000);

test.skipIf(!CHROME)('responsive gate preserves input state and removes its listener', async () => {
  const measured = await mountedGateGeometry({
    width: 1440,
    height: 1000,
    screenshotName: 'responsive-gate-lifecycle.png',
    transitionProbe: true,
  });

  expect(measured.lifecycle.mobileOrder).toEqual(['gate-v4-access', 'gate-v4-field']);
  expect(measured.lifecycle.desktopOrder).toEqual(['gate-v4-field', 'gate-v4-access']);
  expect(measured.lifecycle.mobileSameInput).toBe(true);
  expect(measured.lifecycle.desktopSameInput).toBe(true);
  expect(measured.lifecycle.mobileValue).toBe('typed-value');
  expect(measured.lifecycle.desktopValue).toBe('typed-value');
  expect(measured.lifecycle.mobileFocused).toBe(true);
  expect(measured.lifecycle.desktopFocused).toBe(true);
  expect(measured.lifecycle.addedListeners).toBe(1);
  expect(measured.lifecycle.removedListeners).toBe(1);
  expect(measured.lifecycle.remainingListeners).toBe(0);
}, 60000);
