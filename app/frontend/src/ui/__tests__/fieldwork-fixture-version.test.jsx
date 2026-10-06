/* Restated 2 October 2026: this pinned the retired v1 and v2 workspace
   fixtures and their contract versions. The roster has no contract version;
   its fixture is what core/api/fieldwork.py builds from the API fixtures, and
   a payload of the wrong shape is refused before anything renders. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {buildFieldworkState, FieldworkPage} from '../../fieldwork.jsx';

const days = JSON.parse(readFileSync(new URL('./fixtures/fieldwork42_days.json', import.meta.url), 'utf8'));

test('the retained roster days keep their real state, markets and render', () => {
  const ready = buildFieldworkState({payload: days.ready});
  const absent = buildFieldworkState({payload: days.absent});
  expect([ready.state, ready.data.markets.length, ready.data.health_state]).toEqual(['ready', 4, 'recorded']);
  expect([absent.state, absent.data.health_state]).toEqual(['ready', 'absent']);
  expect(renderToStaticMarkup(<FieldworkPage payload={days.ready} />)).toContain('Own subreddits');
  expect(renderToStaticMarkup(<FieldworkPage payload={days.absent} />)).toContain('Not recorded yet');
});

test('every retained day refuses a payload with no markets or no day, without rendering sources', () => {
  for (const payload of [days.ready, days.absent]){
    const variants = [
      {...payload, markets: undefined},
      (() => { const missing = {...payload}; delete missing.date; return missing; })(),
    ];
    for (const invalid of variants){
      const view = buildFieldworkState({payload: invalid});
      expect(view.state).toBe('error');
      const html = renderToStaticMarkup(<FieldworkPage payload={invalid} />);
      expect(html).toContain('not with a source list this page can read');
      expect(html).not.toContain('fieldwork-source');
    }
  }
});
