/* Build, #/console, on the 42 API. The page starts an investigation, a
   dossier or a question, and lists recent work from 42's own endpoints, each
   list with its loading, empty and error words. The header's market narrows
   every list that has a market. A question, a stored request or a brief
   link on the console still opens the workbench. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import React from 'react';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());

const {createRoot} = await import('react-dom/client');
const {act} = await import('react');
const {Build42} = await import('../../build42.jsx');
const {consoleShowsBuild, consoleShowsAsk} = await import('../../App.jsx');

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const INVESTIGATIONS = [
  {investigation_id: 'i_za_done', status: 'complete', question: 'Why is amapiano dance spreading in Durban?', market: 'ZA', created_at: '2026-10-01T09:00:00+02:00'},
  {investigation_id: 'i_ng_draft', status: 'draft', question: 'Who is driving the Lagos street food trend?', market: 'NG', created_at: '2026-09-30T09:00:00+01:00'},
];
const DOSSIERS = [
  {dossier_id: 'd_za', state: 'draft', title: 'Amapiano in Durban', market: 'ZA', kept: 2, created_at: '2026-10-01T10:00:00+02:00'},
];
const ASKS = [
  {ask_id: 'a_done', question: 'Where did the heritage challenge start?', at: '2026-09-30T08:00:00+02:00', status: 'complete', answer_status: 'complete'},
  {ask_id: 'a_failed', question: 'What is behind the step trend?', at: '2026-09-30T09:00:00+02:00', status: 'failed', answer_status: null},
];
const BRIEFS = [
  {date: '2026-09-30', markets: [{market: 'ZA', status: 'published'}, {market: 'KE', status: 'data_issue'}]},
];

function fakeApi(overrides = {}){
  const calls = [];
  const api = {
    listInvestigations: async () => ({investigations: INVESTIGATIONS}),
    listDossiers: async () => ({dossiers: DOSSIERS, next_before: null}),
    fetchHistoryAsks: async () => ({asks: ASKS, next_before: null}),
    fetchHistoryBriefs: async () => ({dates: BRIEFS, next_before: null}),
    listSchedules: async () => ({schedules: []}),
    createInvestigation: async (body) => { calls.push(['createInvestigation', body]); return {investigation_id: 'i_new', status: 'draft'}; },
    createInvestigationDossier: async (id) => { calls.push(['createInvestigationDossier', id]); return {dossier_id: 'd_new'}; },
    createDossier: async (id) => { calls.push(['createDossier', id]); return {dossier_id: 'd_from_ask'}; },
    ...overrides,
  };
  return {api, calls};
}

let host;
let root;
beforeEach(() => {
  window.location.hash = '#/console';
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => { root.unmount(); });
  host.remove();
});

async function mount(props){
  await act(async () => { root.render(<Build42 onAuth={() => {}} {...props} />); });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

const text = () => host.textContent;
const section = (id) => host.querySelector('[aria-labelledby="' + id + '"]');

test('the page is called Build and lists recent work from the 42 API', async () => {
  const {api} = fakeApi();
  await mount({region: 'ALL', api});
  expect(host.querySelector('h1').textContent).toBe('Build');
  expect(section('b42-recent-inv').textContent).toContain('Why is amapiano dance spreading in Durban?');
  expect(section('b42-recent-inv').textContent).toContain('Who is driving the Lagos street food trend?');
  expect(section('b42-recent-dossiers').textContent).toContain('Amapiano in Durban');
  expect(section('b42-recent-asks').querySelector('a[href="#/ask?follow=a_done"]')).not.toBeNull();
  expect(section('b42-recent-briefs').textContent).toContain('South Africa: Published');
  expect(section('b42-recent-schedules').textContent).toContain('No scheduled questions yet.');
});

test('none of the retired workbench words or calls are on the page', async () => {
  const {api} = fakeApi();
  await mount({region: 'ZA', api});
  for (const gone of ['Workbench', 'What do you need from the signal?', 'No such API route', 'Nothing is known', 'estate', 'Explore', 'Deliver']){
    expect(text()).not.toContain(gone);
  }
  const source = readFileSync(fileURLToPath(new URL('../../build42.jsx', import.meta.url)), 'utf8');
  for (const retired of ['/api/chat/coverage', '/api/research/recent', '/api/v2/']) expect(source).not.toContain(retired);
  expect(source).not.toMatch(/[–—]/);
});

test('the header market narrows investigations, dossiers and briefs', async () => {
  const {api} = fakeApi();
  await mount({region: 'NG', api});
  expect(section('b42-recent-inv').textContent).toContain('Lagos street food');
  expect(section('b42-recent-inv').textContent).not.toContain('Durban');
  expect(section('b42-recent-dossiers').textContent).toContain('No dossiers in Nigeria yet.');
  expect(section('b42-recent-briefs').textContent).toContain('No briefs in Nigeria yet.');
  expect(host.querySelector('#b42-inv-market').value).toBe('NG');
});

test('empty lists say so in plain words', async () => {
  const {api} = fakeApi({
    listInvestigations: async () => ({investigations: []}),
    listDossiers: async () => ({dossiers: []}),
    fetchHistoryAsks: async () => ({asks: []}),
    fetchHistoryBriefs: async () => ({dates: []}),
  });
  await mount({region: 'ALL', api});
  expect(section('b42-recent-inv').textContent).toContain('No investigations yet. Start one above.');
  expect(section('b42-recent-dossiers').textContent).toContain('No dossiers yet.');
  expect(section('b42-recent-asks').textContent).toContain('No questions asked yet.');
  expect(section('b42-dossier-title').textContent).toContain('No finished answers yet.');
});

test('a list that cannot be read says so and offers to try again, without blanking the rest', async () => {
  let fail = true;
  const {api} = fakeApi({
    listInvestigations: async () => {
      if (fail) throw Object.assign(new Error('The Ask service cannot be reached.'), {status: 503});
      return {investigations: INVESTIGATIONS};
    },
  });
  await mount({region: 'ALL', api});
  const inv = section('b42-recent-inv');
  /* A service that is down reads the same in every list, never naming another page. */
  expect(inv.querySelector('[role="alert"]').textContent).toContain('Your investigations could not be read right now. Try again in a minute.');
  expect(section('b42-recent-dossiers').textContent).toContain('Amapiano in Durban');
  fail = false;
  await act(async () => { inv.querySelector('button').click(); });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  expect(section('b42-recent-inv').textContent).toContain('Durban');
});

test('a refusal in a list shows the server own words', async () => {
  const {api} = fakeApi({listDossiers: async () => { throw Object.assign(new Error('Dossiers need the agent tables.'), {status: 409}); }});
  await mount({region: 'ALL', api});
  expect(section('b42-recent-dossiers').querySelector('[role="alert"]').textContent).toContain('Dossiers need the agent tables.');
});

test('a list reads as loading until its answer arrives', async () => {
  const {api} = fakeApi({listInvestigations: () => new Promise(() => {})});
  await mount({region: 'ALL', api});
  expect(section('b42-recent-inv').querySelector('[role="status"]').textContent).toBe('Reading investigations');
});

test('drafting an investigation posts the question with the market and opens the draft', async () => {
  const {api, calls} = fakeApi();
  await mount({region: 'ZA', api});
  const box = host.querySelector('#b42-inv-question');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(box, 'What do students in Soweto wear to weekend parties?');
    box.dispatchEvent(new window.Event('input', {bubbles: true}));
  });
  await act(async () => { box.form.querySelector('button[type="submit"]').click(); });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  expect(calls).toEqual([['createInvestigation', {question: 'What do students in Soweto wear to weekend parties?', market: 'ZA'}]]);
  expect(window.location.hash).toBe('#/investigations/i_new');
});

test('a refused draft shows the server own words', async () => {
  const {api} = fakeApi({createInvestigation: async () => { throw Object.assign(new Error("Today's model budget is spent; try tomorrow."), {status: 429}); }});
  await mount({region: 'ZA', api});
  const box = host.querySelector('#b42-inv-question');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(box, 'Anything at all here');
    box.dispatchEvent(new window.Event('input', {bubbles: true}));
  });
  await act(async () => { box.form.querySelector('button[type="submit"]').click(); });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  expect(section('b42-inv-title').querySelector('[role="alert"]').textContent).toBe("Today's model budget is spent; try tomorrow.");
});

test('a dossier starts from a finished investigation or an answered question, and opens', async () => {
  const {api, calls} = fakeApi();
  await mount({region: 'ALL', api});
  const sources = section('b42-dossier-title');
  const buttons = [...sources.querySelectorAll('button')];
  /* The finished investigation and the answered question; the draft and the failed ask are not offered. */
  expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual([
    'Start a dossier from: Why is amapiano dance spreading in Durban?',
    'Start a dossier from: Where did the heritage challenge start?',
  ]);
  await act(async () => { buttons[0].click(); });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  expect(calls).toEqual([['createInvestigationDossier', 'i_za_done']]);
  expect(window.location.hash).toBe('#/dossiers/d_new');
});

test('asking hands the question and the market to Ask', async () => {
  const {api} = fakeApi();
  await mount({region: 'KE', api});
  const box = host.querySelector('#b42-ask-question');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(box, 'What is trending in Nairobi?');
    box.dispatchEvent(new window.Event('input', {bubbles: true}));
  });
  await act(async () => { box.form.querySelector('button[type="submit"]').click(); });
  expect(window.location.hash).toBe('#/ask?q=What+is+trending+in+Nairobi%3F&market=KE&draft=1');
});

test('a question goes to Ask in the market chosen beside it, and the lists link as one family', async () => {
  const {api} = fakeApi();
  await mount({region: 'ZA', api});
  expect(host.querySelector('#b42-ask-market').value).toBe('ZA');
  const select = host.querySelector('#b42-ask-market');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
    setter.call(select, 'NG');
    select.dispatchEvent(new window.Event('change', {bubbles: true}));
  });
  const box = host.querySelector('#b42-ask-question');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(box, 'What is trending in Lagos?');
    box.dispatchEvent(new window.Event('input', {bubbles: true}));
  });
  await act(async () => { box.form.querySelector('button[type="submit"]').click(); });
  expect(window.location.hash).toBe('#/ask?q=What+is+trending+in+Lagos%3F&market=NG&draft=1');
  const links = [...host.querySelectorAll('.b42-more')].map((a) => a.textContent);
  expect(links).toEqual(['All investigations', 'All dossiers', 'All questions', 'All briefs', 'All schedules']);
  /* The four lists sit under one heading, a level below it. */
  expect(host.querySelector('#b42-recent-title').tagName).toBe('H2');
  expect(host.querySelector('#b42-recent-inv').tagName).toBe('H3');
});

test('the plain console is Build; a question, a request or a brief link still opens the workbench', () => {
  expect(consoleShowsBuild('#/console')).toBe(true);
  expect(consoleShowsBuild('')).toBe(true);
  expect(consoleShowsBuild('#/console?work=ask')).toBe(false);
  expect(consoleShowsBuild('#/console?request=req_1')).toBe(false);
  expect(consoleShowsBuild('#/console/what%20is%20new')).toBe(false);
  expect(consoleShowsBuild('#/console?work=brief&investigation=inv_one')).toBe(false);
  expect(consoleShowsAsk('#/console?request=req_1')).toBe(true);
  const app = readFileSync(fileURLToPath(new URL('../../App.jsx', import.meta.url)), 'utf8');
  expect(app).toContain("consoleShowsBuild(window.location.hash) && <Build42 region={region} onAuth={onAuth} />");
});

test('the stylesheet keeps the wrapping rules and the sans title', () => {
  const css = readFileSync(fileURLToPath(new URL('../../styles/build42.css', import.meta.url)), 'utf8');
  expect(css).toContain('text-wrap: balance');
  expect(css).toContain('text-wrap: pretty');
  expect(css).not.toMatch(/var\(--serif\)|letter-spacing|text-transform:\s*uppercase/);
});
