import {describe, expect, test} from 'bun:test';

import {UTILITY_NAV, normalizeView} from '../../redesignContract.js';
import {buildWorkspaceHash, parseWorkspaceHash} from '../../router.js';

describe('approved workspace hash grammar', () => {
  test('builds every canonical workspace route', () => {
    expect(buildWorkspaceHash({view: 'source-lab'})).toBe('#/source-lab');
    expect(buildWorkspaceHash({view: 'console'})).toBe('#/console?work=brief');
    expect(buildWorkspaceHash({view: 'console', investigationId: 'inv_case'}))
      .toBe('#/console?work=brief&investigation=inv_case');
    expect(buildWorkspaceHash({view: 'console', investigationId: 'inv_case', artifactId: 'ra_doc'}))
      .toBe('#/console?work=brief&investigation=inv_case&artifact=ra_doc');
    for (const mode of ['analogue', 'recurrence', 'replay']) {
      expect(buildWorkspaceHash({view: 'historical', investigationId: 'inv_case', mode}))
        .toBe(`#/historical/inv_case/${mode}`);
    }
  });

  test('parses exact investigation, artifact and historical identity', () => {
    expect(parseWorkspaceHash('#/console?work=brief&investigation=inv_case&artifact=ra_doc'))
      .toMatchObject({view: 'console', investigationId: 'inv_case', artifactId: 'ra_doc', error: null});
    expect(parseWorkspaceHash('#/historical/inv_case/recurrence'))
      .toMatchObject({view: 'historical', investigationId: 'inv_case', mode: 'recurrence', error: null});
  });

  test('keeps malformed workspace links in their owning error state', () => {
    const cases = [
      ['#/console?work=brief&artifact=ra_doc', 'console', 'workspace_request_invalid'],
      ['#/console?work=brief&investigation=inv_a&investigation=inv_b', 'console', 'workspace_request_invalid'],
      ['#/historical/inv_case/diffusion', 'historical', 'historical_mode_ineligible'],
      ['#/historical/inv_case/analogue/extra', 'historical', 'workspace_request_invalid'],
      ['#/historical/%zz/analogue', 'historical', 'workspace_request_invalid'],
      ['#/source-lab/extra', 'source-lab', 'workspace_request_invalid'],
    ];
    for (const [hash, view, code] of cases) {
      expect(parseWorkspaceHash(hash)).toMatchObject({view, error: code});
      expect(normalizeView(view)).toBe(view);
    }
  });

  test('places Source Lab and Historical in the exact utility order', () => {
    expect(UTILITY_NAV.map((item) => [item.label, item.path])).toEqual([
      ['Method', '/method'],
      ['Source Lab', '/source-lab'],
      ['Historical', '/historical'],
      ['Network', '/network'],
      ['Lexicon', '/lexicon'],
      ['My board', '/board'],
      ['Browse', '/browse'],
      ['Journey map', '/map'],
    ]);
  });
});

describe('Fieldwork direct-link reload', () => {
  /* The one matrix cell recorded UNPROVEN: the state was named in
     FIELDWORK_STATES and nothing tested it. A direct link is the whole route
     grammar exercised cold: build, parse, refusal on its own hash, and a
     first paint that says loading rather than claiming an estate. */

  test('the canonical hash builds and parses to itself', () => {
    expect(buildWorkspaceHash({view: 'fieldwork'})).toBe('#/fieldwork');
    expect(parseWorkspaceHash('#/fieldwork')).toMatchObject({view: 'fieldwork', error: null});
  });

  test('a child segment or any query is refused on its own hash, never Briefing', () => {
    for (const bad of ['#/fieldwork/child', '#/fieldwork?market=za', '#/fieldwork?x=']) {
      const parsed = parseWorkspaceHash(bad);
      expect(parsed.view).toBe('fieldwork');
      expect(parsed.error).toBe('workspace_request_invalid');
    }
  });

  test('a cold mount paints loading, not an invented estate', async () => {
    const {renderToStaticMarkup} = await import('react-dom/server');
    const React = (await import('react')).default;
    const {FieldworkWorkspace} = await import('../../fieldwork.jsx');
    const markup = renderToStaticMarkup(React.createElement(FieldworkWorkspace, {}));
    expect(markup).toContain('data-fieldwork-state="loading"');
    expect(markup).toContain('aria-busy="true"');
    /* No estate claims on first paint: neither an empty workspace nor rows. */
    expect(markup).not.toContain('No fieldwork has been planned');
  });
});

describe('Fieldwork day changes on the settled route', () => {
  /* Restated 2 October 2026: the Source Lab hand-off and inventory paging went
     with the retired dossier. The rule it held stays: the hand-off to Coverage
     is a canonical route this router accepts cold, and changing the day or
     the market is a control, not a route, so Back and Forward never replay
     it. */
  test('the Coverage hand-off is a canonical route and the day and market controls are not links', async () => {
    const {renderToStaticMarkup} = await import('react-dom/server');
    const React = (await import('react')).default;
    const {readFileSync} = await import('node:fs');
    const {FieldworkPage} = await import('../../fieldwork.jsx');
    const days = JSON.parse(readFileSync(new URL('./fixtures/fieldwork42_days.json', import.meta.url), 'utf8'));
    const markup = renderToStaticMarkup(React.createElement(FieldworkPage, {payload: days.absent, onDate: () => {}}));
    expect(markup).toContain('open <a href="#/coverage">Coverage</a>');
    expect(parseWorkspaceHash('#/coverage').error ?? null).toBeNull();
    expect(markup).toMatch(/<button type="button" class="fieldwork-link-button"[^>]*>Back to 30 September 2026<\/button>/);
    expect(markup).toMatch(/<input [^>]*type="date"/);
    expect(markup).toMatch(/<button[^>]*role="tab"/);
    expect(markup).not.toMatch(/href="[^"]*(date|market|day)[^"]*"/);
  });
});
