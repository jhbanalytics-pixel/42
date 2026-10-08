import {afterAll, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());
let EntityLists = () => null;
try {
  ({EntityLists} = await import('../../EntityLists.jsx'));
} catch (error) {
  if (!String(error).includes('Cannot find module')) throw error;
}

let produced;
function records(){
  if (produced) return structuredClone(produced);
  const source = `
import json
from core.agent.tests.test_remaining_lists import fixture, entity_lists, QUESTIONS
out = {}
for kind in QUESTIONS:
    ctx, answer, discovered, _ = fixture(kind, prior=0 if kind == "topics" else None, split_dates=kind == "topics")
    groups, notices = entity_lists(QUESTIONS[kind], answer, ctx, creator_discovery=discovered)
    out[kind] = {"question": QUESTIONS[kind], "answer": answer, "run": {"run_id": ctx.run_id,
        "window": {"from": ctx.window_start.isoformat(), "to": ctx.window_end.isoformat()},
        "entity_lists": groups, "notices": notices}}
ctx, answer, discovered, _ = fixture("creators")
for claim in answer["claims"]:
    claim["text"] = claim["text"].replace("@", "")
groups, notices = entity_lists(QUESTIONS["creators"], answer, ctx, creator_discovery=discovered)
out["creators_plain"] = {"question": QUESTIONS["creators"], "answer": answer, "run": {"run_id": ctx.run_id,
    "window": {"from": ctx.window_start.isoformat(), "to": ctx.window_end.isoformat()}, "entity_lists": groups, "notices": notices}}
print(json.dumps(out, default=str))
`;
  const result = spawnSync(process.platform === 'win32' ? 'py' : 'python3',
    [...(process.platform === 'win32' ? ['-3.13'] : []), '-c', source],
    {cwd: fileURLToPath(new URL('../../../../../', import.meta.url)), input: '', encoding: 'utf8',
      env: {...process.env, PYTHONUTF8: '1'}});
  expect(result.error).toBeUndefined();
  expect(result.status).toBe(0);
  produced = JSON.parse(result.stdout);
  return structuredClone(produced);
}

function rendered(record, sources){
  const host = document.createElement('main');
  host.innerHTML = renderToStaticMarkup(<><EntityLists record={record}
    windowLabel={(window) => `${window.from} to ${window.to}`} renderSources={sources} />
    <p className="summary">Checked prose stays visible.</p></>);
  return host;
}

for (const kind of ['platforms', 'topics', 'creators']){
  test(`actual ${kind} producer output leads prose and preserves checked inputs`, () => {
    const record = records()[kind];
    const frozen = JSON.stringify(record);
    const seen = [];
    const host = rendered(record, (claim, ids) => {
      seen.push([claim.id, ids]);
      return <button>{ids.join(', ')}</button>;
    });
    expect(host.querySelectorAll('.ask42-entity-list li')).toHaveLength(2);
    expect(host.firstElementChild.classList.contains('ask42-entity-list')).toBe(true);
    expect(seen).toHaveLength(2);
    expect(JSON.stringify(record)).toBe(frozen);
    expect(host.textContent).not.toContain('query');
    expect(host.textContent).not.toContain('detection');
    expect(host.textContent).not.toContain('warehouse');
    if (kind === 'platforms'){
      expect(host.querySelector('li').textContent).toContain('Instagram');
      expect(host.textContent).toContain('20 posts');
      expect(host.textContent).toContain('Ranked by posts, then creators');
      expect(host.textContent).toContain('Previous week: Not recorded');
    } else if (kind === 'topics'){
      expect(host.querySelectorAll('.ask42-entity-list')).toHaveLength(2);
      expect(host.textContent).toContain('Activity over three days');
      expect(host.textContent).toContain('No posts recorded in the same three days a week earlier');
      expect(host.textContent).toContain('Not recorded');
    } else {
      expect(host.querySelector('ol')).toBeNull();
      expect(host.textContent).toContain('@maker_a');
      expect(host.textContent).toContain('ranking unavailable');
      expect(host.textContent).toContain('Creator total: Not recorded');
      expect(host.textContent).toContain('Post total: Not recorded');
    }
  });
}

test('actual producer output also preserves creator handles checked without an at prefix', () => {
  const host = rendered(records().creators_plain);
  expect(host.querySelectorAll('.ask42-entity-list li')).toHaveLength(2);
  expect(host.querySelector('.ask42-ranked-name').textContent).toBe('maker_a');
  expect(host.textContent).toContain('Creator total: Not recorded');
});

for (const mutate of [
  (record) => { record.run.entity_lists = [null]; },
  (record) => { record.answer.claims = [null]; },
  (record) => { record.run.entity_lists[0].items = [null]; },
  (record) => { record.run.entity_lists[0].items[0].posts_index = 99; },
  (record) => { record.run.entity_lists[0].items[0].evidence_ids = ['unknown']; },
  (record) => { record.run.entity_lists[0].items[0].name = 'Other platform'; },
  (record) => { record.run.entity_lists[0].window = {from: '2026-02-31', to: '2026-03-06'}; },
  (record) => { record.run.entity_lists[0].unavailable.posts = 'not_recorded'; },
  (record) => { record.answer.claims[0].numbers[1].run_id = 'other'; },
  (record) => { record.answer.claims[0].numbers = null; },
]){
  test(`malformed optional platform metadata ${String(mutate)} preserves prose`, () => {
    const record = records().platforms;
    mutate(record);
    const host = rendered(record);
    expect(host.querySelector('.ask42-entity-list')).toBeNull();
    expect(host.querySelector('.summary').textContent).toBe('Checked prose stays visible.');
  });
}

test('topic metadata cannot imply a full week or accept an incompatible prior date', () => {
  const record = records().topics;
  record.run.entity_lists[0].previous_window.to = '2026-09-27';
  const host = rendered(record);
  expect(host.querySelectorAll('.ask42-entity-list')).toHaveLength(1);
  expect(host.textContent).not.toContain('No posts recorded');
});

test('a validly formatted topic snapshot outside the Ask window cannot relabel current pins', () => {
  const record = records().topics;
  const group = record.run.entity_lists[0];
  group.window = {from: '2026-09-26', to: '2026-09-28'};
  group.previous_window = null;
  group.previous_basis = null;
  group.unavailable.previous_week = 'not_recorded';
  group.items[0].previous_posts_index = null;
  expect(rendered(record).querySelectorAll('.ask42-entity-list')).toHaveLength(1);
});

test('creator metadata cannot invent a count or bind another cited creator', () => {
  const record = records().creators;
  record.run.entity_lists[0].items[0].creators_index = 0;
  expect(rendered(record).querySelector('.ask42-entity-list')).toBeNull();
  record.run.entity_lists[0].items[0].creators_index = null;
  record.run.entity_lists[0].items[0].evidence_ids = ['p_b'];
  expect(rendered(record).querySelector('.ask42-entity-list')).toBeNull();
});

test('masking, held answers and other primary subjects hide optional lists', () => {
  for (const mutate of [(record) => {record.skin_id = 'masked';}, (record) => {record.answer.status = 'insufficient_evidence';},
    (record) => {record.question = 'Which platform engineers are growing?';}]){
    const record = records().platforms;
    mutate(record);
    expect(rendered(record).querySelector('.ask42-entity-list')).toBeNull();
  }
});

test('actual producer and reader require complete creator handles and reject canonical alias collisions', () => {
  const cases = [
    {handle: 'maker_a', text: '@maker_a posted.', name: '@maker_a'},
    {handle: 'maker_a', text: 'maker_a posted.', name: 'maker_a'},
    {handle: 'maker_a', text: '(@maker_a), then another post.', name: '@maker_a'},
    {handle: 'maker_a', text: 'maker_a. Another sentence.', name: 'maker_a'},
    {handle: 'maker_a', text: 'maker_a...', name: 'maker_a'},
    {handle: 'maker_a', text: '@maker_a.extra posted.', name: null},
    {handle: 'maker_a', text: 'maker_a.extra posted.', name: null},
    {handle: 'maker_a', text: 'maker_a-extra posted.', name: null},
    {handle: 'maker_a', text: 'person@maker_a.example.test replied.', name: null},
    {handle: 'maker_a', text: 'maker_a@example.test replied.', name: null},
    {handle: 'maker_a', text: 'contact@maker_a replied.', name: null},
    {handle: 'maker_a', text: 'person@maker_a.example.test; @maker_a posted.', name: '@maker_a'},
    {handle: 'maker.a', text: '@maker.a posted.', name: '@maker.a'},
    {handle: 'maker.a', text: 'maker.a. Another sentence.', name: 'maker.a'},
    {handle: 'maker.a', text: '@maker.a.extra posted.', name: null},
    {handle: 'maker.a', text: 'person@maker.a.example.test replied.', name: null},
  ];
  const source = `
import json
import sys
from core.agent.remaining_lists import entity_lists
from core.agent.tests.test_remaining_lists import fixture, rehash, QUESTIONS
out = []
for case in json.load(sys.stdin):
    ctx, answer, discovered, _ = fixture("creators")
    iq, pq = discovered["creator_query_id"], discovered["posts_query_id"]
    ctx.queries[iq]["rows"][0]["handle"] = case["handle"]
    discovered["creator_candidates"][0]["handle"] = case["handle"]
    ctx.queries[pq]["rows"][0]["handle"] = case["handle"]
    answer["evidence"][0]["handle"] = case["handle"]
    for qid in (iq, pq):
        rehash(ctx, answer, qid)
    answer["claims"][0]["text"] = "@" + case["handle"] + " posted."
    retained, _ = entity_lists(QUESTIONS["creators"], answer, ctx, creator_discovery=discovered)
    answer["claims"][0]["text"] = case["text"]
    groups, notices = entity_lists(QUESTIONS["creators"], answer, ctx, creator_discovery=discovered)
    out.append({"case": case, "retained": retained, "record": {"question": QUESTIONS["creators"], "answer": answer,
        "run": {"run_id": ctx.run_id, "window": {"from": ctx.window_start.isoformat(), "to": ctx.window_end.isoformat()},
                "entity_lists": groups, "notices": notices}}})
ctx, answer, discovered, ids = fixture("platforms")
rows = ctx.queries[ids["totals"]]["rows"]
rows[0]["platform"] = "twitter"
rows.append({"platform": "x", "creators": 3, "posts": 4})
answer["claims"][0]["text"] = "X appeared in checked posts."
answer["evidence"][0]["platform"] = "x"
rehash(ctx, answer, ids["totals"])
groups, notices = entity_lists(QUESTIONS["platforms"], answer, ctx)
out.append({"case": "alias_collision", "record": {"question": QUESTIONS["platforms"], "answer": answer,
    "run": {"run_id": ctx.run_id, "window": {"from": ctx.window_start.isoformat(), "to": ctx.window_end.isoformat()},
            "entity_lists": groups, "notices": notices}}})
print(json.dumps(out, default=str))
`;
  const result = spawnSync(process.platform === 'win32' ? 'py' : 'python3',
    [...(process.platform === 'win32' ? ['-3.13'] : []), '-c', source],
    {cwd: fileURLToPath(new URL('../../../../../', import.meta.url)), input: JSON.stringify(cases), encoding: 'utf8',
      env: {...process.env, PYTHONUTF8: '1'}});
  expect(result.error).toBeUndefined();
  expect(result.status).toBe(0);
  for (const entry of JSON.parse(result.stdout)){
    const host = rendered(entry.record);
    if (entry.case === 'alias_collision'){
      expect([...host.querySelectorAll('.ask42-ranked-name')].map((node) => node.textContent)).toEqual(['Instagram']);
      expect(entry.record.run.notices).not.toHaveLength(0);
      continue;
    }
    const first = entry.record.run.entity_lists.flatMap((group) => group.items).find((item) => item.claim_id === 'c1');
    expect({text: entry.case.text, name: first?.name ?? null}).toEqual({text: entry.case.text, name: entry.case.name});
    expect(host.querySelectorAll('.ask42-entity-list li')).toHaveLength(entry.case.name === null ? 1 : 2);
    if (entry.case.name === null){
      const stale = {...entry.record, run: {...entry.record.run, entity_lists: entry.retained}};
      expect({text: entry.case.text, visible: Boolean(rendered(stale).querySelector('.ask42-entity-list'))})
        .toEqual({text: entry.case.text, visible: false});
    }
  }
});
