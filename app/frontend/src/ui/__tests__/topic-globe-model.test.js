/* The topic globe's layout (ui/topicGlobeModel.js): the Communities list as
   points on a sphere. A community is a point sized by its creators; each
   topic its members share is a point near it, and a topic two communities
   share sits between them with a line to each. The layout reads only what
   the API sent and is the same on every load. */
import {expect, test} from 'bun:test';
import {globeModel} from '../topicGlobeModel.js';

const figure = (value) => ({value, unit: 'creators', query_id: 'q_communities', run_id: 'r1'});
const topic = (id, title) => ({item_id: id, title, market: 'NG'});
const community = (id, creators, topics, label) => ({
  community_id: id, label: label || topics.map((t) => t.title).join(', '), creators: figure(creators), topics,
});

const length = ([x, y, z]) => Math.hypot(x, y, z);
const angle = (a, b) => Math.acos(Math.max(-1, Math.min(1, (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (length(a) * length(b)))));

const LIST = [
  community('c_small', 5, [topic('t_owambe', '#owambe'), topic('t_jollof', '#jollof')]),
  community('c_big', 40, [topic('t_jollof', '#jollof'), topic('t_afcon', '#afcon'), topic('t_sound', 'Peaking sound')]),
  community('c_mid', 12, [topic('t_derby', '#derby')]),
];

test('an empty list draws nothing', () => {
  expect(globeModel([])).toEqual({communities: [], topics: [], links: []});
  expect(globeModel(null)).toEqual({communities: [], topics: [], links: []});
});

test('every community is one point on the unit sphere, largest first, with the creators the API counted', () => {
  const model = globeModel(LIST);
  expect(model.communities.map((c) => c.id)).toEqual(['c_big', 'c_mid', 'c_small']);
  expect(model.communities.map((c) => c.creators)).toEqual([40, 12, 5]);
  for (const c of model.communities) expect(length(c.pos)).toBeCloseTo(1, 6);
});

test('a bigger community gets a bigger point, and size grows by area, not by radius', () => {
  const [big, mid, small] = globeModel(LIST).communities;
  expect(big.size).toBeGreaterThan(mid.size);
  expect(mid.size).toBeGreaterThan(small.size);
  expect(big.size / small.size).toBeCloseTo(Math.sqrt(40 / 5), 6);
});

test('each topic appears once, with every community that shares it, and a line to each', () => {
  const model = globeModel(LIST);
  expect(model.topics.map((t) => t.id).sort()).toEqual(['t_afcon', 't_derby', 't_jollof', 't_owambe', 't_sound']);
  const jollof = model.topics.find((t) => t.id === 't_jollof');
  expect(jollof.communities.sort()).toEqual(['c_big', 'c_small']);
  expect(jollof.title).toBe('#jollof');
  expect(model.links.filter((l) => l.topic === 't_jollof').map((l) => l.community).sort()).toEqual(['c_big', 'c_small']);
  expect(model.links).toHaveLength(6);
  for (const t of model.topics) expect(length(t.pos)).toBeCloseTo(1, 6);
});

test('a topic one community owns sits near it; a shared topic sits between the communities that share it', () => {
  const model = globeModel(LIST);
  const at = (id) => model.communities.find((c) => c.id === id).pos;
  const own = model.topics.find((t) => t.id === 't_afcon');
  const shared = model.topics.find((t) => t.id === 't_jollof');
  const gap = angle(at('c_big'), at('c_small'));
  expect(angle(own.pos, at('c_big'))).toBeLessThan(angle(own.pos, at('c_small')));
  expect(angle(shared.pos, at('c_big'))).toBeLessThan(gap);
  expect(angle(shared.pos, at('c_small'))).toBeLessThan(gap);
  expect(shared.shared).toBe(true);
  expect(own.shared).toBe(false);
});

test('communities spread over the globe rather than bunching on one side', () => {
  const many = Array.from({length: 12}, (_, i) => community('c' + i, 5 + i, [topic('t' + i, 'Topic ' + i)]));
  const points = globeModel(many).communities.map((c) => c.pos);
  let closest = Math.PI;
  for (let i = 0; i < points.length; i++) {
    for (let j = i + 1; j < points.length; j++) closest = Math.min(closest, angle(points[i], points[j]));
  }
  expect(closest).toBeGreaterThan(0.5);
});

test('the same list gives the same globe, whatever order the API sent it in', () => {
  const again = globeModel([...LIST].reverse());
  expect(again).toEqual(globeModel(LIST));
});

test('a community whose creators were not counted still gets a point, at the smallest size', () => {
  const model = globeModel([...LIST, {community_id: 'c_uncounted', label: 'x', creators: null, topics: []}]);
  const uncounted = model.communities.find((c) => c.id === 'c_uncounted');
  expect(uncounted.creators).toBeNull();
  expect(uncounted.size).toBe(Math.min(...model.communities.map((c) => c.size)));
});

test('a topic with no id or no title is left off the globe, never shown as an id', () => {
  const model = globeModel([community('c1', 6, [topic('', 'No id'), topic('t_ok', 'Kept'), {item_id: 't_blank', title: '  '}])]);
  expect(model.topics.map((t) => t.id)).toEqual(['t_ok']);
});
