import {afterAll, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());
let RankedAnswer = () => null;
try {
  ({RankedAnswer} = await import('../../RankedAnswer.jsx'));
} catch (error) {
  if (!String(error).includes('Cannot find module')) throw error;
}

function fixture(){
  const pin = (value, unit, query_id = 'q_sounds') => ({value, unit, query_id, run_id: 'r_ranked', result_hash: 'sha256:' + 'a'.repeat(64)});
  return {
    market: 'ZA', question: 'Which sounds are rising?', run: {run_id: 'r_ranked', window: {from: '2026-09-29', to: '2026-10-05'},
      ranked_list: {kind: 'sounds', scope: 'retained_whole_store_claims', order: 'creators_desc_posts_desc',
        query_id: 'q_sounds', market: 'ZA', platform: 'tiktok', measure_columns: {creators: 'creators', posts: 'posts'},
        window: {from: '2026-09-29', to: '2026-10-05'}, previous_window: {from: '2026-09-22', to: '2026-09-28'},
        items: [
          {claim_id: 'c1', title: 'Named sound', usage_handle: 'maker_a', creators_index: 0, posts_index: 1, previous_posts_index: 2, tied_with_previous: false},
          {claim_id: 'c2', title: null, usage_handle: 'maker_b', creators_index: 0, posts_index: 1, previous_posts_index: null, tied_with_previous: false},
        ]}},
    answer: {status: 'complete', claims: [
      {id: 'c1', text: 'Named sound appeared in clips.', numbers: [pin(11, 'creators'), pin(13, 'posts'), pin(0, 'posts in previous week', 'q_prior')]},
      {id: 'c2', text: 'An untitled sound was used by maker_b.', numbers: [pin(5, 'creators'), pin(5, 'posts')]},
    ]},
  };
}

function rendered(record){
  const host = document.createElement('main');
  host.innerHTML = renderToStaticMarkup(<><RankedAnswer record={record} windowLabel="29 Sep to 5 Oct 2026" /><p className="summary">Summary prose.</p></>);
  return host;
}

test('the comparable list leads with named and usage-only sounds and their checked counts', () => {
  const record = fixture();
  const frozen = JSON.stringify(record);
  const host = rendered(record);
  const section = host.querySelector('[aria-label="Comparable checked sounds"]');
  expect(section).not.toBeNull();
  expect(host.firstElementChild).toBe(section);
  const items = [...section.querySelectorAll('li')];
  expect(items).toHaveLength(2);
  expect(items[0].textContent).toContain('Named sound');
  expect(items[0].textContent).toContain('11 creators');
  expect(items[0].textContent).toContain('13 posts');
  expect(items[1].textContent).toContain('Original sound used by @maker_b');
  expect(items[1].textContent).toContain('Title not verified');
  expect(section.textContent).toContain('South Africa');
  expect(section.textContent).toContain('29 Sep to 5 Oct');
  expect(section.textContent).toContain('creators, then posts');
  expect(JSON.stringify(record)).toBe(frozen);
});

test('checked zero is distinct from an absent previous count and no percentage is invented', () => {
  const host = rendered(fixture());
  const items = [...host.querySelectorAll('li')];
  expect(items).toHaveLength(2);
  expect(items[0].textContent).toContain('No stored posts in the previous week');
  expect(items[1].textContent).toContain('Previous week not verified');
  expect(host.textContent).not.toContain('%');
  expect(host.textContent).not.toContain('Infinity');
});

test('ties and the all-markets basis are visible', () => {
  const record = fixture();
  record.run.ranked_list.market = null;
  record.run.ranked_list.items[1].tied_with_previous = true;
  record.answer.claims[1].numbers[0].value = 11;
  record.answer.claims[1].numbers[1].value = 13;
  const host = rendered(record);
  expect(host.textContent).toContain('All markets');
  expect(host.textContent).toContain('Tied on creators and posts');
});

for (const invalid of [-1, 99, 0.5, '0', null]){
  test(`malformed number index ${String(invalid)} hides the optional projection`, () => {
    const record = fixture();
    record.run.ranked_list.items[0].creators_index = invalid;
    expect(rendered(record).querySelector('.ask42-ranked')).toBeNull();
  });
}

test('a legacy record or masked skin record has no optional list', () => {
  const record = fixture();
  delete record.run.ranked_list;
  const legacy = rendered(record);
  expect(legacy.querySelector('.ask42-ranked')).toBeNull();
  expect(legacy.textContent).toContain('A comparable ranked list was not recorded for this saved answer');
  const masked = fixture();
  masked.skin_id = 'skin_fixture';
  expect(rendered(masked).querySelector('.ask42-ranked')).toBeNull();
});

test('a mismatched prior week or result hash hides the optional projection', () => {
  const badWeek = fixture();
  badWeek.run.ranked_list.previous_window.to = '2026-09-27';
  expect(rendered(badWeek).querySelector('.ask42-ranked')).toBeNull();
  const badHash = fixture();
  for (const number of badHash.answer.claims[1].numbers) number.result_hash = 'sha256:' + 'b'.repeat(64);
  expect(rendered(badHash).querySelector('.ask42-ranked')).toBeNull();
});

test('a held answer cannot display an old optional projection', () => {
  const record = fixture();
  record.answer.status = 'insufficient_evidence';
  expect(rendered(record).querySelector('.ask42-ranked')).toBeNull();
});

test('a creator question mentioning sounds does not show a sound list or missing-sound-list notice', () => {
  const record = fixture();
  record.question = 'Which creators use these sounds?';
  const host = rendered(record);
  expect(host.querySelector('.ask42-ranked')).toBeNull();
  expect(host.querySelector('.ask42-ranked-unavailable')).toBeNull();
});

for (const question of ['Which sound engineers are popular?', 'Which audio platforms are growing?', 'What audio equipment is trending?']){
  test(`the primary subject ${question} is outside sound ranking`, () => {
    const record = fixture();
    record.question = question;
    const host = rendered(record);
    expect(host.querySelector('.ask42-ranked')).toBeNull();
    expect(host.querySelector('.ask42-ranked-unavailable')).toBeNull();
    expect(host.querySelector('.summary').textContent).toBe('Summary prose.');
  });
}

for (const window of [{from: '2026-02-31', to: '2026-03-06'}, {from: '2026-10-05', to: '2026-09-29'}]){
  test(`invalid current window ${JSON.stringify(window)} hides the optional list`, () => {
    const record = fixture();
    record.run.window = window;
    record.run.ranked_list.window = window;
    record.run.ranked_list.previous_window = null;
    for (const item of record.run.ranked_list.items) item.previous_posts_index = null;
    const host = rendered(record);
    expect(host.querySelector('.ask42-ranked')).toBeNull();
    expect(host.querySelector('.summary').textContent).toBe('Summary prose.');
  });
}

test('a null optional item cannot take down checked prose', () => {
  const record = fixture();
  record.run.ranked_list.items = [null];
  const host = rendered(record);
  expect(host.querySelector('.ask42-ranked')).toBeNull();
  expect(host.querySelector('.summary').textContent).toBe('Summary prose.');
});

function hashtagFixture(){
  const record = fixture();
  record.question = 'Which hashtags are rising?';
  record.run.ranked_list.kind = 'hashtags';
  for (const [i, tag] of ['#amapiano', '#e\u0301lan'].entries()){
    record.run.ranked_list.items[i].title = tag;
    record.run.ranked_list.items[i].usage_handle = null;
    record.answer.claims[i].text = tag + ' appeared in posts.';
  }
  return record;
}

test('checked hashtags lead with exact names, counts and the existing prior distinction', () => {
  const record = hashtagFixture();
  const frozen = JSON.stringify(record);
  const host = rendered(record);
  const section = host.querySelector('[aria-label="Comparable checked hashtags"]');
  expect(section).not.toBeNull();
  expect(host.firstElementChild).toBe(section);
  const items = section.querySelectorAll('li');
  expect(items).toHaveLength(2);
  expect(items[0].textContent).toContain('#amapiano');
  expect(items[1].textContent).toContain('#e\u0301lan');
  expect(items[0].textContent).toContain('11 creators, 13 posts.');
  expect(items[0].textContent).toContain('No stored posts in the previous week.');
  expect(items[1].textContent).toContain('Previous week not verified.');
  expect(section.textContent).not.toContain('Original sound');
  expect(JSON.stringify(record)).toBe(frozen);
});

for (const title of [null, '', '#', '#unknown', '#amapianomore']){
  test(`unverified hashtag title ${String(title)} preserves prose without a list`, () => {
    const record = hashtagFixture();
    record.run.ranked_list.items[0].title = title;
    const host = rendered(record);
    expect(host.querySelector('.ask42-ranked')).toBeNull();
    expect(host.querySelector('.summary').textContent).toBe('Summary prose.');
    expect(host.textContent).not.toContain('Untitled sound');
  });
}

for (const text of ['#amapianomore', '#amapiano\u0301', 'x#amapiano', '##amapiano']){
  test(`hashtag token boundary rejects ${text}`, () => {
    const record = hashtagFixture();
    record.answer.claims[0].text = text;
    expect(rendered(record).querySelector('.ask42-ranked')).toBeNull();
  });
}

test('hashtag metadata cannot carry a sound handle or disagree with the question kind', () => {
  const record = hashtagFixture();
  record.run.ranked_list.items[0].usage_handle = 'maker_a';
  expect(rendered(record).querySelector('.ask42-ranked')).toBeNull();
  record.run.ranked_list.items[0].usage_handle = null;
  record.question = 'Which sounds are rising?';
  expect(rendered(record).querySelector('.ask42-ranked')).toBeNull();
});

test('hashtag ties stay distinct and source rendering receives each original claim', () => {
  const record = hashtagFixture();
  record.run.ranked_list.items[1].tied_with_previous = true;
  record.answer.claims[1].numbers[0].value = 11;
  record.answer.claims[1].numbers[1].value = 13;
  const seen = [];
  const html = renderToStaticMarkup(<RankedAnswer record={record} windowLabel="29 Sep to 5 Oct 2026" renderSources={(claim) => {
    seen.push(claim);
    return <button>{claim.id}</button>;
  }} />);
  expect(seen).toEqual(record.answer.claims);
  expect(html).toContain('Tied on creators and posts.');
  expect(html).toContain('<button>c1</button>');
});

test('a hashtag strategy question stays outside the item contract', () => {
  const record = hashtagFixture();
  record.question = 'Which hashtag strategies work?';
  const host = rendered(record);
  expect(host.querySelector('.ask42-ranked')).toBeNull();
  expect(host.querySelector('.ask42-ranked-unavailable')).toBeNull();
});

test('actual hashtag producer output and the reader share comparison-only ASCII case folding', () => {
  const cases = [
    {key: 'amapiano', text: '#AMAPIANO appeared', included: true},
    {key: 'i', text: '#i appeared', included: true},
    {key: 'i', text: '#I appeared', included: true},
    {key: 'i', text: '#ı appeared', included: false},
    {key: 'i', text: '#İ appeared', included: false},
    {key: 'ı', text: '#ı appeared', included: true},
    {key: 'İ', text: '#İ appeared', included: true},
    {key: 'ı', text: '#i appeared', included: false},
    {key: 'İ', text: '#i appeared', included: false},
    {key: 'élan', text: '#éLAN appeared', included: true},
    {key: 'élan', text: '#ÉLAN appeared', included: false},
    {key: 'e\u0301lan', text: '#e\u0301LAN appeared', included: true},
    {key: 'e\u0301lan', text: '#élan appeared', included: false},
    {key: 'k', text: '#K appeared', included: false},
    {key: 's', text: '#ſ appeared', included: false},
    {key: 'ß', text: '#SS appeared', included: false},
    {key: 'élan', text: '#élan\u0301 appeared', included: false},
    {key: '𐐨', text: '#𐐨 appeared', included: true},
    {key: '𐐨', text: '#𐐀 appeared', included: false},
  ];
  const source = `
import copy
import json
import sys
from core.agent.ranked import ranked_list
from core.agent.tests.test_ranked_list import fixture, rehash
outputs = []
for case in json.load(sys.stdin):
    ctx, answer, ids = fixture(kind="hashtags", prior=3)
    ctx.queries[ids["hashtags"]]["rows"][1]["hashtag"] = "other_tag"
    answer["claims"][1]["text"] = "#other_tag appeared"
    for qid in (ids["hashtags"], ids["before"]):
        ctx.queries[qid]["rows"][0]["hashtag"] = case["key"]
        rehash(ctx, answer, qid)
    question = "Which hashtags are rising on TikTok this week?"
    answer["claims"][0]["text"] = "#" + case["key"] + " appeared"
    retained_projection, _ = ranked_list(question, answer, ctx)
    answer["claims"][0]["text"] = case["text"]
    frozen = copy.deepcopy((answer, ctx.queries))
    projection, notices = ranked_list(question, answer, ctx)
    outputs.append({"case": case, "unchanged": frozen == (answer, ctx.queries), "retained_projection": retained_projection,
        "raw_current": ctx.queries[ids["hashtags"]]["rows"][0]["hashtag"],
        "raw_previous": ctx.queries[ids["before"]]["rows"][0]["hashtag"],
        "record": {"question": question, "answer": answer, "run": {"run_id": ctx.run_id,
            "window": {"from": ctx.window_start.isoformat(), "to": ctx.window_end.isoformat()},
            "ranked_list": projection, "notices": notices}}})
print(json.dumps(outputs))
`;
  const command = process.platform === 'win32' ? 'py' : 'python3';
  const args = [...(process.platform === 'win32' ? ['-3.13'] : []), '-c', source];
  const produced = spawnSync(command, args, {cwd: fileURLToPath(new URL('../../../../../', import.meta.url)),
    input: JSON.stringify(cases), encoding: 'utf8', env: {...process.env, PYTHONUTF8: '1'}});
  expect(produced.error).toBeUndefined();
  expect(produced.status).toBe(0);
  const outputs = JSON.parse(produced.stdout);
  expect(outputs).toHaveLength(cases.length);
  for (const {case: example, record, retained_projection, unchanged, raw_current, raw_previous} of outputs){
    expect(unchanged).toBe(true);
    expect(raw_current).toBe(example.key);
    expect(raw_previous).toBe(example.key);
    const host = rendered(record);
    const section = host.querySelector('[aria-label="Comparable checked hashtags"]');
    expect({text: example.text, visible: Boolean(section)}).toEqual({text: example.text, visible: true});
    expect(section.querySelectorAll('li')).toHaveLength(example.included ? 2 : 1);
    expect(Boolean(section.querySelector('[data-claim-id="c1"]'))).toBe(example.included);
    expect(record.run.ranked_list.items.some((item) => item.claim_id === 'c1')).toBe(example.included);
    if (example.included){
      expect(record.run.ranked_list.items[0].title).toBe('#' + example.key);
      expect(record.run.ranked_list.items[0].previous_posts_index).toBe(2);
    } else {
      const stale = {...record, run: {...record.run, ranked_list: retained_projection}};
      expect({text: example.text, visible: Boolean(rendered(stale).querySelector('.ask42-ranked'))})
        .toEqual({text: example.text, visible: false});
    }
  }
});
