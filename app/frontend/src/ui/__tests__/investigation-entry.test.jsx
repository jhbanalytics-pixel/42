/* The investigation entry point.

   Two surfaces sit on the two approved reads: a landing that lists what
   exists, and a framing surface that creates one. The rule that shapes both is
   the answer to section 7. With no released run the framing surface refuses to
   create and says so, rather than sending a run identity it invented to
   satisfy a required field. */

import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {InvestigationIndex} from '../InvestigationIndex.jsx';
import {InvestigationFraming} from '../InvestigationFraming.jsx';

import {
  buildFramingState,
  buildIndexState,
  creationRequestFrom,
  createInvestigationFrom,
} from '../../investigationIndex.js';

const scopePayload = (over = {}) => ({
  contract_version: 'intelligence_dossier_v1',
  resource_version: 'investigation_scope_index_v1',
  default_scope_id: 'ogilvy_default',
  scopes: [{
    client_scope_id: 'ogilvy_default',
    market_labels: ['South Africa', 'Nigeria', 'Kenya'],
    market_scope: ['za', 'ng', 'ke'],
    brand_config_id: null,
    audience_lens_ids: [],
    theme_id: null,
    latest_run_id: 'run_2026_08_29_a',
  }],
  ...over,
});

const indexPayload = (over = {}) => ({
  contract_version: 'intelligence_dossier_v1',
  resource_version: 'investigation_index_v1',
  investigations: [{
    investigation_id: 'inv_one',
    decision_question: 'Is repair becoming social?',
    market_labels: ['South Africa'],
    created_at: '2026-08-28T09:00:00Z',
    status: 'plan_ready',
    readiness_state: 'blocked',
    candidate_artifact_id: null,
  }],
  total_count: 1,
  truncated: false,
  ...over,
});

describe('The landing list', () => {
  test('a payload with investigations is ready', () => {
    const view = buildIndexState({payload: indexPayload()});
    expect(view.state).toBe('ready');
    expect(view.investigations).toHaveLength(1);
  });

  test('an empty estate is empty, which is a fact about the estate', () => {
    const view = buildIndexState({payload: indexPayload({investigations: [], total_count: 0})});
    expect(view.state).toBe('empty');
  });

  test('a read that has not returned is loading, not empty', () => {
    expect(buildIndexState({loading: true}).state).toBe('loading');
  });

  test('an absent payload is unavailable, not an empty estate', () => {
    /* Empty says there are no investigations. Unavailable says we do not know.
       Collapsing them would state something never established. */
    expect(buildIndexState({}).state).toBe('error');
    expect(buildIndexState({payload: null}).state).toBe('error');
  });

  test('a payload whose list is not a list is unavailable', () => {
    expect(buildIndexState({payload: indexPayload({investigations: 'none'})}).state).toBe('error');
  });

  test('an unsupported resource version is named rather than rendered', () => {
    const view = buildIndexState({payload: indexPayload({resource_version: 'investigation_index_v2'})});
    expect(view.state).toBe('unsupported_contract');
  });

  test('a truncated list carries what it did not show', () => {
    const view = buildIndexState({payload: indexPayload({truncated: true, total_count: 80})});
    expect(view.truncated).toBe(true);
    expect(view.totalCount).toBe(80);
  });

  test('an entry with no identity is never offered as a row', () => {
    const view = buildIndexState({payload: indexPayload({
      investigations: [{investigation_id: null, decision_question: 'x'}],
    })});
    expect(view.investigations).toEqual([]);
  });
});

describe('The framing surface, and section 7', () => {
  test('a scope with a released run may create', () => {
    const view = buildFramingState({payload: scopePayload()});
    expect(view.state).toBe('ready');
    expect(view.canCreate).toBe(true);
  });

  test('no released run refuses to create and says why', () => {
    const view = buildFramingState({payload: scopePayload({scopes: [{
      ...scopePayload().scopes[0], latest_run_id: null,
    }]})});
    expect(view.state).toBe('no_released_run');
    expect(view.canCreate).toBe(false);
    expect(view.reason).toMatch(/no released run/i);
  });

  test('the creation request is refused outright when there is no run', () => {
    /* Not merely a disabled button. Nothing may assemble a request that would
       send an invented identity. */
    const view = buildFramingState({payload: scopePayload({scopes: [{
      ...scopePayload().scopes[0], latest_run_id: null,
    }]})});
    expect(() => creationRequestFrom(view, {
      decisionQuestion: 'Is repair becoming social?', markets: ['za'], timeHorizonDays: 30,
    })).toThrow(/no released run/i);
  });

  test('a creation request carries the scope identity exactly as the server gave it', () => {
    const view = buildFramingState({payload: scopePayload()});
    const request = creationRequestFrom(view, {
      decisionQuestion: 'Is repair becoming social?', markets: ['za', 'ng'], timeHorizonDays: 30,
    });
    expect(request.client_scope_id).toBe('ogilvy_default');
    expect(request.brand_config_id).toBeNull();
    expect(request.theme_id).toBeNull();
    expect(request.audience_lens_ids).toEqual([]);
    expect(request.run_id).toBe('run_2026_08_29_a');
    expect(request.market_scope).toEqual(['za', 'ng']);
  });

  test('a market outside the scope is refused rather than sent', () => {
    const view = buildFramingState({payload: scopePayload()});
    expect(() => creationRequestFrom(view, {
      decisionQuestion: 'x', markets: ['uk'], timeHorizonDays: 30,
    })).toThrow(/market/i);
  });

  test('an empty market selection is refused', () => {
    const view = buildFramingState({payload: scopePayload()});
    expect(() => creationRequestFrom(view, {
      decisionQuestion: 'x', markets: [], timeHorizonDays: 30,
    })).toThrow(/market/i);
  });

  test('a blank decision question is refused', () => {
    const view = buildFramingState({payload: scopePayload()});
    expect(() => creationRequestFrom(view, {
      decisionQuestion: '   ', markets: ['za'], timeHorizonDays: 30,
    })).toThrow(/question/i);
  });

  test('an absent scope payload is unavailable, never a default scope', () => {
    expect(buildFramingState({}).state).toBe('error');
    expect(buildFramingState({payload: scopePayload({scopes: []})}).state).toBe('error');
  });
});

test('the request builder refuses a scope with no run even if told it may create', () => {
  /* Section 7 defended twice. The state machine refuses first, so this path is
     unreachable today. It is asserted anyway because the two guards fail
     independently: remove the first and add a fallback identity, and an
     investigation would be created resting on a run nobody made, which is
     exactly what the answer to section 7 forbids. */
  const view = {
    state: 'ready',
    canCreate: true,
    scope: {
      client_scope_id: 'ogilvy_default',
      market_scope: ['za'],
      audience_lens_ids: [],
      brand_config_id: null,
      theme_id: null,
      latest_run_id: null,
    },
  };
  expect(() => creationRequestFrom(view, {
    decisionQuestion: 'Is repair becoming social?', markets: ['za'], timeHorizonDays: 30,
  })).toThrow(/no released run/i);
});

test('framing sends one scoped request and returns the verified investigation', async () => {
  const calls = [];
  const view = buildFramingState({payload: scopePayload()});
  const values = {decisionQuestion: 'What is changing in repair culture?', markets: ['za', 'ng'], timeHorizonDays: 30};
  const record = await createInvestigationFrom(view, values, async (path, body) => {
    calls.push({path, body});
    return {...body, investigation_id: 'inv_local', status: 'plan_ready', research_plan: {questions: ['What changed?']}};
  });
  expect(calls).toHaveLength(1);
  const apiSource = readFileSync(new URL('../../../../src/api/main.py', import.meta.url), 'utf8');
  const registeredPath = apiSource.match(/@app\.post\("([^"]+)"\)\s+def api_create_investigation/)?.[1];
  expect(registeredPath).toBeTruthy();
  expect(calls[0].path).toBe(registeredPath);
  expect(calls[0].body).toEqual(creationRequestFrom(view, values));
  expect(record.investigation_id).toBe('inv_local');
});

test('invalid framing never reaches the write boundary', async () => {
  let calls = 0;
  await expect(createInvestigationFrom(buildFramingState({payload: scopePayload()}), {
    decisionQuestion: 'Question', markets: [], timeHorizonDays: 30,
  }, async () => { calls += 1; })).rejects.toThrow(/market/i);
  expect(calls).toBe(0);
});

test.each([
  {client_scope_id: 'another_client'}, {run_id: 'another_run'}, {market_scope: ['ke']},
  {brand_config_id: 'another_brand'}, {theme_id: 'another_theme'}, {audience_lens_ids: ['another_lens']},
  {contract_version: 'unknown'}, {investigation_id: '../other'}, {status: 'error'},
])('a mismatched framing response cannot open an investigation', async (changed) => {
  await expect(createInvestigationFrom(buildFramingState({payload: scopePayload()}), {
    decisionQuestion: 'Question', markets: ['za'], timeHorizonDays: 30,
  }, async (_path, body) => ({...body, investigation_id: 'inv_local', status: 'plan_ready', ...changed})))
    .rejects.toThrow(/verified/);
});

test('a blank run identity is refused the same way as an absent one', () => {
  const view = {
    state: 'ready', canCreate: true,
    scope: {client_scope_id: 'ogilvy_default', market_scope: ['za'], audience_lens_ids: [],
      brand_config_id: null, theme_id: null, latest_run_id: '   '},
  };
  expect(() => creationRequestFrom(view, {
    decisionQuestion: 'x', markets: ['za'], timeHorizonDays: 30,
  })).toThrow(/no released run/i);
});

describe('The landing renders what it knows and nothing more', () => {
  const render = (props) => renderToStaticMarkup(<InvestigationIndex {...props} />);
  const visible = (props) => render(props).replace(/<[^>]*>/g, ' ');

  test('each investigation is a row carrying its question and markets', () => {
    const markup = render({payload: indexPayload()});
    expect(markup).toContain('Is repair becoming social?');
    expect(markup).toContain('South Africa');
    expect(markup).toContain('inv_one');
  });

  /* Ampersands arrive escaped, which is correct HTML, so the hash is compared
     decoded rather than raw. */
  const hrefs = (markup) =>
    (markup.match(/href="[^"]*"/g) || []).map((h) => h.slice(6, -1).replace(/&amp;/g, '&'));

  test('a row links to the investigation and to nothing else', () => {
    const markup = render({payload: indexPayload()});
    expect(hrefs(markup)).toContain('#/console?work=brief&investigation=inv_one');
    expect(markup).not.toMatch(/href="https?:/);
  });

  test('an investigation with an artifact links to its client read', () => {
    const markup = render({payload: indexPayload({investigations: [{
      ...indexPayload().investigations[0], candidate_artifact_id: 'ra_1',
    }]})});
    expect(hrefs(markup)).toContain('#/console?work=brief&investigation=inv_one&artifact=ra_1');
  });

  test('an empty estate says so plainly and is not an error', () => {
    const text = visible({payload: indexPayload({investigations: [], total_count: 0})});
    expect(text).toMatch(/no investigation/i);
    expect(text).not.toMatch(/unavailable/i);
  });

  test('a read that never returned says unavailable, never empty', () => {
    const text = visible({});
    expect(text).toMatch(/unavailable|could not be read/i);
    expect(text).not.toMatch(/no investigations have been/i);
  });

  test('a truncated list says what it is not showing', () => {
    const text = visible({payload: indexPayload({truncated: true, total_count: 80})});
    expect(text).toContain('80');
    expect(text).toMatch(/most recent/i);
  });

  test('one h1, rows not cards', () => {
    const markup = render({payload: indexPayload()});
    expect((markup.match(/<h1/g) || []).length).toBe(1);
    expect(markup).not.toContain('card');
  });
});

describe('The framing surface refuses before it invites', () => {
  const render = (props) => renderToStaticMarkup(<InvestigationFraming {...props} />);
  const visible = (props) => render(props).replace(/<[^>]*>/g, ' ');

  test('with a released run it offers the frame', () => {
    const markup = render({payload: scopePayload(), onFrame: () => {}});
    expect(markup).toContain('<form');
    expect(markup).not.toContain('disabled=""');
  });

  test('without a frame callback the action is disabled and explains why', () => {
    const markup = render({payload: scopePayload()});
    expect(markup).toContain('disabled=""');
    expect(markup).toContain('Framing is unavailable in this workspace.');
  });

  test('with no released run it refuses and says why, and offers no form control', () => {
    const withoutRun = scopePayload({scopes: [{...scopePayload().scopes[0], latest_run_id: null}]});
    const markup = render({payload: withoutRun});
    const text = markup.replace(/<[^>]*>/g, ' ');
    expect(text).toMatch(/no released run/i);
    expect(markup).not.toContain('<form');
  });

  test('the markets offered are exactly the scope markets, by value and label', () => {
    /* Asserted on the values, not the labels. An out of scope market offered
       with no label falls back to its identifier, so a label check would miss
       exactly the case that matters: the request would carry a market the
       server refuses, and the strategist would not know why. */
    const markup = render({payload: scopePayload()});
    const values = (markup.match(/name="market" value="[a-z]+"/g) || [])
      .map((m) => m.split('value="')[1].slice(0, -1));
    expect(values).toEqual(['za', 'ng', 'ke']);
    for (const label of ['South Africa', 'Nigeria', 'Kenya']) expect(markup).toContain(label);
  });

  test('no scope is unavailable, never a default invented on screen', () => {
    const text = visible({payload: scopePayload({scopes: []})});
    expect(text).toMatch(/unavailable/i);
    expect(text).not.toContain('ogilvy_default');
  });

  test('nothing on the surface starts work on load', () => {
    const markup = render({payload: scopePayload()});
    expect(markup).not.toMatch(/autofocus[^>]*submit|onload/i);
  });
});

describe('The entrance is finished, not only functional', () => {
  const sheet = () => readFileSync(new URL('../../styles/console.css', import.meta.url), 'utf8');

  test('every class either surface renders has a rule', () => {
    /* An unstyled entrance is a surface that was built and never finished, and
       it is the first thing a strategist meets. */
    const markup = [
      renderToStaticMarkup(<InvestigationIndex payload={indexPayload()} />),
      renderToStaticMarkup(<InvestigationFraming payload={scopePayload()} />),
      renderToStaticMarkup(<InvestigationFraming payload={scopePayload({scopes: [{
        ...scopePayload().scopes[0], latest_run_id: null,
      }]})} />),
    ].join('\n');
    const css = sheet();
    const classes = new Set();
    for (const m of markup.matchAll(/class="([^"]+)"/g)){
      m[1].split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    }
    const missing = [...classes].filter((c) => c !== 'page' && !css.includes(`.${c}`));
    expect(missing).toEqual([]);
  });

  test('a size token is never used as a font shorthand', () => {
    /* The tokens are plain sizes, so `font: 15px` has no family and the whole
       declaration is dropped without a word. It shipped once already. */
    const offences = [...sheet().matchAll(/font:\s*var\(--dossier-type-[^)]*\)[^;]*;/g)];
    expect(offences).toEqual([]);
  });

  test('the entrance carries a mobile state', () => {
    expect(sheet()).toMatch(/@media[^{]*max-width/);
  });

  test('rows, not cards', () => {
    const css = sheet();
    const rowRules = css.split('}').filter((b) => b.includes('.investigation-row'));
    expect(rowRules.length).toBeGreaterThan(0);
    for (const block of rowRules){
      expect(block).not.toMatch(/box-shadow\s*:\s*(?!none)/);
    }
  });
});

describe('The door actually fetches', () => {
  /* Found by independent review: the two entrance effects called apiPost
     while the file imported only apiGetFresh, so the workbench threw a
     ReferenceError on first mount and the whole console died. A static
     guard, because the build does not catch an unbound identifier. */
  test('every api helper the workbench calls is imported from api.js', () => {
    const src = readFileSync(
      new URL('../../ConsoleWorkbench.jsx', import.meta.url), 'utf8');
    const importLine = src.match(/import\s*\{([^}]+)\}\s*from\s*'\.\/api\.js'/);
    expect(importLine).toBeTruthy();
    const imported = new Set(importLine[1].split(',').map((s) => s.trim()));
    for (const helper of ['apiGet', 'apiGetFresh', 'apiPost', 'useApi']) {
      const used = new RegExp(`[^.\\w]${helper}\\s*\\(`).test(src);
      if (used) expect({helper, imported: imported.has(helper)}).toEqual({helper, imported: true});
    }
  });
});
