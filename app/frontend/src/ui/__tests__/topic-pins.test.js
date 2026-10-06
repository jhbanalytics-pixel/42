import {expect, test} from 'bun:test';
import {readTopicPins, changeTopicPins, resolveTopicPin, sourceTopicPin, topicPinKey, clearTopicPrior} from '../../topicPins.js';
import {buildTopicHash, parseWorkspaceHash} from '../../router.js';

function storageFor(value){
  let raw = typeof value === 'string' ? value : JSON.stringify(value);
  return {getItem: () => raw, setItem: (_key, value) => {raw = value;}, raw: () => raw};
}
const rows = [{id: 'football', region: 'KE', topic: 'Kenya football', series: [100, 200]}, {id: 'football', region: 'NG', topic: 'Nigeria football', series: [100, 100]}];

test('country and topic jointly identify a pin and its previous reading', () => {
  expect(topicPinKey({id: 'football', market: 'ke'})).toBe('topic-pin:ke:football');
  expect(topicPinKey({id: 'football', market: 'ng'})).toBe('topic-pin:ng:football');
  for (const input of [rows, [...rows].reverse()]){
    expect(resolveTopicPin({id: 'football', market: 'ng'}, input).series).toEqual([100, 100]);
    expect(resolveTopicPin({id: 'football', market: 'ke'}, input).series).toEqual([100, 200]);
  }
});

test('a scoped prior cannot collide with any legacy topic-ID prefix', () => {
  const legacyId = 'ng:football';
  expect(topicPinKey({id: 'football', market: 'ng'})).not.toBe('topic:' + legacyId);
});

test('a legacy pin stays unresolved even when only one market currently matches', () => {
  const storage = storageFor(['football']);
  const before = storage.raw();
  const pin = readTopicPins(storage).pins[0];
  const result = resolveTopicPin(pin, [rows[1]]);
  expect(result.state).toBe('legacy');
  expect(result.market).toBeNull();
  expect(result.found).toBe(false);
  expect(result.choices).toEqual(['ng']);
  expect(storage.raw()).toBe(before);
});

test('invalid or future pin shapes remain unreadable and are never overwritten', () => {
  for (const value of ['{broken', {}, [{id: 'football', market: 'ng', future: true}], [{id: 'football', market: 'all'}]]){
    const storage = storageFor(value), before = storage.raw();
    expect(readTopicPins(storage).status).toBe('unavailable');
    expect(changeTopicPins(() => [], storage).status).toBe('unavailable');
    expect(storage.raw()).toBe(before);
  }
});

test('a fresh-storage mutation preserves a pin added after an earlier read', () => {
  const storage = storageFor([{id: 'football', market: 'ng'}]);
  readTopicPins(storage);
  storage.setItem('', JSON.stringify([{id: 'football', market: 'ng'}, {id: 'football', market: 'ke'}]));
  const result = changeTopicPins(pins => pins.filter(pin => pin.market !== 'ng'), storage);
  expect(result.pins).toEqual([{id: 'football', market: 'ke'}]);
});

test('failed persistence does not report the requested pin as saved', () => {
  const storage = storageFor(['football']);
  storage.setItem = () => {throw new Error('Quota exceeded');};
  const result = changeTopicPins(pins => [...pins, {id: 'football', market: 'ng'}], storage);
  expect(result).toEqual({status: 'unavailable', pins: ['football']});
  expect(storage.raw()).toBe('["football"]');
});

test('an already satisfied mutation needs no storage write', () => {
  const storage = storageFor([{id: 'football', market: 'ng'}]);
  storage.setItem = () => {throw new Error('Unexpected write');};
  expect(changeTopicPins(pins => pins, storage).status).toBe('ready');
});

test('source identity uses the recorded market and refuses missing or ALL markets', () => {
  expect(sourceTopicPin(rows[1])).toEqual({id: 'football', market: 'ng'});
  expect(sourceTopicPin({id: 'football'})).toBeNull();
  expect(sourceTopicPin({id: 'football', region: 'ALL'})).toBeNull();
});

test('unavailable, absent and ambiguous topic records remain distinct', () => {
  const pin = {id: 'football', market: 'ng'};
  expect(resolveTopicPin(pin, null).state).toBe('unavailable');
  expect(resolveTopicPin(pin, [{id: 'football'}]).state).toBe('unavailable');
  expect(resolveTopicPin(pin, [rows[0]]).state).toBe('missing');
  expect(resolveTopicPin(pin, [rows[1], {...rows[1], series: [100, 700]}]).state).toBe('ambiguous');
});

test('unknown series values are not converted to zero or silently dropped', () => {
  const pin = {id: 'football', market: 'ng'};
  for (const series of [[1, null], [1, '2'], [1, Infinity], [1, -1]]) expect(resolveTopicPin(pin, [{...rows[1], series}]).series).toEqual([]);
  expect(resolveTopicPin(pin, [{...rows[1], series: [1, 0]}]).series).toEqual([1, 0]);
});

test('explicit Topic scope survives the link independently of stored preferences', () => {
  for (const region of ['ZA', 'NG', 'KE', 'ALL']){
    const hash = buildTopicHash('sports_football', region);
    expect(parseWorkspaceHash(hash)).toMatchObject({view: 'topic', error: null, topicRegion: region});
  }
  expect(parseWorkspaceHash('#/topic/sports_football').topicRegion).toBeUndefined();
  for (const query of ['region=', 'region=unknown', 'region=ke&region=ng', 'region=%']) expect(parseWorkspaceHash('#/topic/sports_football?' + query).error).toBe('topic_scope_invalid');
});

test('clearing one scoped prior preserves other countries and unreadable history', () => {
  const storage = storageFor({'topic-pin:ng:football': {v: 3}, 'topic-pin:ke:football': {v: 4}, 'topic:football': {v: 9}});
  expect(clearTopicPrior({id: 'football', market: 'ng'}, storage)).toBe(true);
  expect(JSON.parse(storage.raw())).toEqual({'topic-pin:ke:football': {v: 4}, 'topic:football': {v: 9}});
  const broken = storageFor('[broken');
  expect(clearTopicPrior({id: 'football', market: 'ng'}, broken)).toBe(false);
  expect(broken.raw()).toBe('[broken');
});
