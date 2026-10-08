/* Ask on the 42 API (core/api/contract.md section 6). Every request sends the
   passcode header. The event stream is read with fetch rather than
   EventSource, because EventSource cannot send that header; when the stream
   drops, the record is polled every two seconds until it stops running. */
import {PASS_KEY, storedValue} from './api.js';
import {releaseExportUrlLater} from './exportUrl.js';

const POLL_MS = 2000;

function headers(extra){
  return {...(extra || {}), 'X-Passcode': storedValue(PASS_KEY)};
}

async function failure(res){
  let body = null;
  try { body = await res.json(); } catch (_error){ body = null; }
  const message = body && typeof body.message === 'string' && body.message.trim()
    ? body.message
    : res.status === 401 ? 'Passcode required' : 'The request failed (' + res.status + ').';
  const error = new Error(message);
  error.status = res.status;
  if (body && typeof body.error === 'string') error.code = body.error;
  if (res.status === 401) error.auth = true;
  return error;
}

async function request(path, init){
  const res = await fetch(path, {...init, headers: headers(init && init.headers)});
  if (!res.ok) throw await failure(res);
  return res;
}

export async function startAsk(body){
  const res = await request('/api/ask', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  return res.json();
}

export async function getAsk(askId, signal){
  const res = await request('/api/ask/' + encodeURIComponent(askId), {signal});
  return res.json();
}

export async function stopAsk(askId){
  const res = await request('/api/ask/' + encodeURIComponent(askId) + '/stop', {method: 'POST'});
  return res.json();
}

export async function downloadExport(askId){
  const res = await request('/api/ask/' + encodeURIComponent(askId) + '/export?format=html', {});
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement('a');
    link.href = url;
    link.download = '42-answer-' + askId + '.html';
    link.rel = 'noopener';
    document.body.appendChild(link);
    link.click();
    link.remove();
  } catch (error){
    URL.revokeObjectURL(url);
    throw error;
  }
  releaseExportUrlLater(url);
}

/* Server-sent events, pushed as text in whatever chunks the network gives.
   A line can arrive split across two chunks, and a CR LF pair across two, so
   only whole lines are read and the tail waits for the next push. */
export function createSSEParser(onMessage){
  let buffer = '';
  let id = null;
  let event = '';
  let data = [];
  const line = (text) => {
    if (text === ''){
      if (data.length) onMessage({id, event: event || 'message', data: data.join('\n')});
      event = '';
      data = [];
      return;
    }
    if (text.startsWith(':')) return;
    const colon = text.indexOf(':');
    const field = colon === -1 ? text : text.slice(0, colon);
    let value = colon === -1 ? '' : text.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'id') id = value;
    else if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  };
  return {
    push(chunk){
      buffer += chunk;
      for (;;){
        const match = /\r\n|\r|\n/.exec(buffer);
        if (!match) break;
        /* a lone CR at the very end may be the first half of CR LF */
        if (match[0] === '\r' && match.index === buffer.length - 1) break;
        line(buffer.slice(0, match.index));
        buffer = buffer.slice(match.index + match[0].length);
      }
    },
  };
}

const wait = (ms, signal) => new Promise((resolve, reject) => {
  const timer = setTimeout(resolve, ms);
  if (signal) signal.addEventListener('abort', () => { clearTimeout(timer); reject(signal.reason || new Error('aborted')); }, {once: true});
});

/* Reads the live stream and hands each event to onEvent as {type, id, data}.
   Resolves with the done event's data. If the stream cannot open, ends early
   or breaks, it polls the record instead, passing on the steps it had not
   seen, and resolves with {status, record}. A 401 is thrown, never polled. */
export async function streamAsk(askId, onEvent, options = {}){
  const {signal, pollMs = POLL_MS} = options;
  let lastEventId = options.lastEventId != null ? String(options.lastEventId) : null;
  let lastSeq = lastEventId != null && Number.isFinite(Number(lastEventId)) ? Number(lastEventId) : 0;
  let done = null;
  const aborted = () => signal && signal.aborted;

  try {
    const extra = {Accept: 'text/event-stream'};
    if (lastEventId != null) extra['Last-Event-ID'] = lastEventId;
    const res = await fetch('/api/ask/' + encodeURIComponent(askId) + '/events', {headers: headers(extra), signal});
    if (res.status === 401) throw await failure(res);
    if (res.ok && res.body){
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      const parser = createSSEParser((message) => {
        if (done) return;
        let payload = null;
        try { payload = JSON.parse(message.data); } catch (_error){ return; }
        if (message.id != null){
          lastEventId = message.id;
          if (Number.isFinite(Number(message.id))) lastSeq = Math.max(lastSeq, Number(message.id));
        }
        if (message.event === 'done') done = payload;
        onEvent({type: message.event, id: message.id, data: payload});
      });
      try {
        while (!done){
          const {value, done: ended} = await reader.read();
          if (ended) break;
          parser.push(decoder.decode(value, {stream: true}));
        }
      } finally {
        if (done && reader.cancel) reader.cancel().catch(() => {});
      }
      if (done) return done;
    }
  } catch (error){
    if (error && error.auth) throw error;
    if (aborted()) throw error;
  }

  for (;;){
    if (aborted()) throw signal.reason || new Error('aborted');
    const record = await getAsk(askId, signal);
    for (const step of Array.isArray(record.steps) ? record.steps : []){
      if (typeof step.seq === 'number' && step.seq > lastSeq){
        lastSeq = step.seq;
        onEvent({type: 'step', id: String(step.seq), data: step});
      }
    }
    if (record.status !== 'running') return {status: record.status, record};
    await wait(pollMs, signal);
  }
}
