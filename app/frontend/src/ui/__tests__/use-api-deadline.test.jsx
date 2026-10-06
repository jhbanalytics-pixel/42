import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const api = await import('../../api.js');

const PATH = '/api/use-api-deadline-test';
const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const response = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
  headers: {get: () => null},
});

function serve(answer){
  globalThis.fetch = async (path, init) => {
    calls.push({path: String(path), init});
    return answer(String(path), calls.length);
  };
}

function controlDeadline(){
  const nativeSetTimeout = globalThis.setTimeout;
  const nativeClearTimeout = globalThis.clearTimeout;
  const scheduled = [];
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (delay === 30000) {
      const timer = {callback, args, delay, cleared: false, fired: false};
      scheduled.push(timer);
      return timer;
    }
    return nativeSetTimeout(callback, delay, ...args);
  };
  globalThis.clearTimeout = (timer) => {
    if (scheduled.includes(timer)) {
      timer.cleared = true;
      return;
    }
    return nativeClearTimeout(timer);
  };
  return {
    scheduled,
    fire: (timer = scheduled[0]) => {
      if (timer && !timer.cleared && !timer.fired) {
        timer.fired = true;
        timer.callback(...timer.args);
      }
    },
    restore: () => {
      globalThis.setTimeout = nativeSetTimeout;
      globalThis.clearTimeout = nativeClearTimeout;
    },
  };
}

function ApiHarness({timeoutMs, enabled = true, onAuth}){
  const [state, retry] = api.useApi(PATH, 0, onAuth, enabled, timeoutMs);
  return (
    <>
      <output data-state={state.state} data-code={state.code || ''} data-value={state.data && state.data.value || ''}>
        {state.message || state.state}
      </output>
      {state.state === 'error' && <button type="button" onClick={retry}>Try again</button>}
    </>
  );
}

const status = () => host.querySelector('output').getAttribute('data-state');
const code = () => host.querySelector('output').getAttribute('data-code');
const value = () => host.querySelector('output').getAttribute('data-value');
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };

function mount(props){
  flushSync(() => root.render(<ApiHarness {...props} />));
}

function clickRetry(){
  flushSync(() => host.querySelector('button').dispatchEvent(new MouseEvent('click', {bubbles: true})));
}

beforeEach(() => {
  calls = [];
  api.clearCache();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  api.clearCache();
  globalThis.fetch = realFetch;
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

test('a pending read times out, ignores its late result, and retries only on click', async () => {
  const timers = controlDeadline();
  try {
    let releaseOld;
    let releaseRetry;
    serve((_path, attempt) => new Promise((resolve) => {
      if (attempt === 1) releaseOld = resolve;
      else releaseRetry = resolve;
    }));
    mount({timeoutMs: 30000});
    await settle();
    expect(status()).toBe('loading');
    expect(calls).toHaveLength(1);
    expect(timers.scheduled).toHaveLength(1);
    expect(timers.scheduled[0].delay).toBe(30000);
    flushSync(() => timers.fire());
    await settle();
    expect(status()).toBe('error');
    expect(code()).toBe('request_timeout');
    expect(host.querySelector('output').textContent).toContain('timed out');
    expect(calls).toHaveLength(1);
    clickRetry();
    await settle();
    expect(status()).toBe('loading');
    expect(calls).toHaveLength(2);
    releaseOld(response(200, {value: 'old'}));
    await settle();
    expect(status()).toBe('loading');
    releaseRetry(response(200, {value: 'new'}));
    await settle();
    expect(status()).toBe('ready');
    expect(value()).toBe('new');
    expect(calls).toHaveLength(2);
  } finally {
    timers.restore();
  }
});

test('a late failed read does not evict the retry promise from the shared cache', async () => {
  const timers = controlDeadline();
  try {
    let rejectOld;
    let releaseRetry;
    serve((_path, attempt) => new Promise((resolve, reject) => {
      if (attempt === 1) rejectOld = reject;
      else releaseRetry = resolve;
    }));
    mount({timeoutMs: 30000});
    await settle();
    flushSync(() => timers.fire());
    await settle();
    expect(status()).toBe('error');
    clickRetry();
    await settle();
    expect(calls).toHaveLength(2);
    rejectOld(new Error('late failure from the timed out request'));
    await settle();
    const sharedRetry = api.apiGet(PATH);
    expect(calls).toHaveLength(2);
    releaseRetry(response(200, {value: 'retry'}));
    await expect(sharedRetry).resolves.toEqual({value: 'retry'});
    await settle();
    expect(status()).toBe('ready');
    expect(value()).toBe('retry');
  } finally {
    timers.restore();
  }
});

test('unmount clears a deadline without aborting or retrying the shared read', async () => {
  const timers = controlDeadline();
  try {
    let release;
    serve(() => new Promise((resolve) => { release = resolve; }));
    mount({timeoutMs: 30000});
    await settle();
    expect(calls).toHaveLength(1);
    expect(calls[0].init.signal).toBeUndefined();
    expect(timers.scheduled).toHaveLength(1);
    flushSync(() => root.unmount());
    root = null;
    expect(timers.scheduled[0].cleared).toBe(true);
    flushSync(() => timers.fire());
    release(response(200, {value: 'late'}));
    await settle();
    expect(calls).toHaveLength(1);
  } finally {
    timers.restore();
  }
});

test('the default useApi call keeps its prior unbounded loading behavior', async () => {
  const timers = controlDeadline();
  try {
    let release;
    serve(() => new Promise((resolve) => { release = resolve; }));
    mount({});
    await settle();
    expect(status()).toBe('loading');
    expect(timers.scheduled).toHaveLength(0);
    expect(calls).toHaveLength(1);
    release(response(200, {value: 'ready'}));
    await settle();
    expect(status()).toBe('ready');
    expect(value()).toBe('ready');
  } finally {
    timers.restore();
  }
});

test('a 401 before the deadline still enters auth and clears its timer', async () => {
  const timers = controlDeadline();
  try {
    let asked = 0;
    serve(() => response(401, {detail: 'Passcode required'}));
    mount({timeoutMs: 30000, onAuth: () => { asked += 1; }});
    await settle();
    expect(status()).toBe('auth');
    expect(asked).toBe(1);
    expect(code()).toBe('http_401');
    expect(timers.scheduled).toHaveLength(1);
    expect(timers.scheduled[0].cleared).toBe(true);
  } finally {
    timers.restore();
  }
});

test('request timeout is a derived failure code, not a producer reason', () => {
  expect(api.failureOrigin('request_timeout')).toBe('derived');
  expect(api.FAILURE_ORIGIN_LABEL.derived).toContain('No producer said this');
});
