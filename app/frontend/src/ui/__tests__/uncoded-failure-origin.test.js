/* A failed read names a producer only when the producer's own body carried
   a code. main.py registers no exception handler, so a read that raises
   inside its handler is answered by the framework's server error middleware
   in plain text: status 500, text/plain; charset=utf-8, body "Internal Server
   Error". That body has no code in it, and a code this page supplies in its
   place must not be printed under the producer's name. */
import {afterEach, expect, test} from 'bun:test';
import {apiGet, clearCache, failureCode, failureOrigin} from '../../api.js';

const priorFetch = globalThis.fetch;

afterEach(() => {
  clearCache();
  globalThis.fetch = priorFetch;
});

function serve(status, body, contentType){
  globalThis.fetch = async (path) => {
    const response = new Response(body, {status, headers: {'content-type': contentType}});
    Object.defineProperty(response, 'url', {value: 'http://127.0.0.1' + path});
    return response;
  };
}

async function failure(path){
  try {
    await apiGet(path);
  } catch (error){
    return error;
  }
  throw new Error('the read did not fail');
}

test('the framework plain text server error carries no code the producer did not send', async () => {
  serve(500, 'Internal Server Error', 'text/plain; charset=utf-8');
  const error = await failure('/api/voices?region=za');
  expect(error.status).toBe(500);
  expect(error.code).toBeUndefined();
  expect(failureOrigin(error.code)).not.toBe('producer');
});

test('an uncoded plain text failure on the desk read names no producer either', async () => {
  serve(502, 'Bad Gateway', 'text/plain; charset=utf-8');
  const error = await failure('/api/desk?region=za');
  expect(error.status).toBe(502);
  expect(error.code).toBeUndefined();
});

test('an HTML error page from a proxy in front of the service carries no producer code', async () => {
  serve(500, '<!doctype html><title>Error</title>', 'text/html');
  const error = await failure('/api/intel/mentions?region=za');
  expect(error.code).toBeUndefined();
});

test('a JSON failure body with no code carries no code', async () => {
  serve(500, '{}', 'application/json');
  const error = await failure('/api/voices?region=ng');
  expect(error.code).toBeUndefined();
});

test('a code the producer did send is still read as the producer reason', async () => {
  serve(500, JSON.stringify({detail: {code: 'voices_store_unavailable', message: 'The voices store did not answer.'}}), 'application/json');
  const error = await failure('/api/voices?region=ke');
  expect(error.code).toBe('voices_store_unavailable');
  expect(failureOrigin(error.code)).toBe('producer');
  expect(error.message).toBe('The voices store did not answer.');
});

test('a successful status whose body is not JSON is not credited to the producer', async () => {
  serve(200, 'Internal Server Error', 'text/plain; charset=utf-8');
  const error = await failure('/api/voices?region=gh');
  expect(error.status).toBe(200);
  expect(failureOrigin(error.code)).not.toBe('producer');
});

test('the plain text server error reaches the page as its status, announced as a status', async () => {
  serve(500, 'Internal Server Error', 'text/plain; charset=utf-8');
  const error = await failure('/api/intel/mentions?region=ng');
  expect(failureCode(error)).toBe('http_500');
  expect(failureOrigin(failureCode(error))).toBe('status');
});

test('a successful status whose body is not JSON is announced as derived by this page', async () => {
  serve(200, '<!doctype html><title>Offline</title>', 'text/html');
  const error = await failure('/api/desk?region=ke');
  expect(failureOrigin(failureCode(error))).toBe('derived');
});

test('a read with no response is announced as the transport, and a coded body as the producer', () => {
  expect(failureCode(new TypeError('Failed to fetch'))).toBe('network_unreachable');
  expect(failureOrigin(failureCode(new TypeError('Failed to fetch')))).toBe('transport');
  expect(failureCode(Object.assign(new Error('x'), {status: 500, code: 'voices_store_unavailable'}))).toBe('voices_store_unavailable');
});
