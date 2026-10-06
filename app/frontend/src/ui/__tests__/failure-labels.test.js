/* The words each failure origin is announced under, pinned as the reader sees
   them. Only a code the producer's own body carried is announced as the
   producer's reason; every other origin says the producer sent none, or that
   no producer said it. */
import {expect, test} from 'bun:test';
import {FAILURE_ORIGIN_LABEL} from '../../api.js';
import {SERVED_VIEWS, normalizeView} from '../../redesignContract.js';

const PRODUCER = 'Reason the producer gave:';

test('each failure origin is announced under its own pinned words', () => {
  expect(FAILURE_ORIGIN_LABEL).toEqual({
    producer: PRODUCER,
    status: 'The producer sent no reason; the read failed with status:',
    transport: 'The producer sent no reason and no answer arrived:',
    unstated: 'The producer sent no reason:',
    derived: 'No producer said this; this page derived it from the records it read:',
  });
});

test('no origin but the producer is credited with a reason', () => {
  for (const origin of ['status', 'transport', 'unstated', 'derived']){
    expect(FAILURE_ORIGIN_LABEL[origin], origin).not.toContain('Reason the producer');
    expect(FAILURE_ORIGIN_LABEL[origin], origin).not.toBe(FAILURE_ORIGIN_LABEL.producer);
  }
});

test('the labels cannot be rewritten at runtime', () => {
  expect(Object.isFrozen(FAILURE_ORIGIN_LABEL)).toBe(true);
});

test('the served view list cannot be widened or narrowed at runtime', () => {
  const size = SERVED_VIEWS.size;
  const attempt = (fn) => { try { fn(); } catch (_error){ /* a refusal is a pass */ } };
  attempt(() => SERVED_VIEWS.add('forged'));
  attempt(() => SERVED_VIEWS.delete('network'));
  attempt(() => SERVED_VIEWS.clear());
  expect(SERVED_VIEWS.has('forged')).toBe(false);
  expect(SERVED_VIEWS.has('network')).toBe(true);
  expect(SERVED_VIEWS.size).toBe(size);
  expect(normalizeView('forged')).toBe('pulse');
  expect(normalizeView('network')).toBe('network');
});
