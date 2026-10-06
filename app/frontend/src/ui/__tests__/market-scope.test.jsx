/* Albert, 4 October 2026: with Nigeria picked in the header, lists of saved
   work still showed South Africa. The scope helper keeps the picked market's
   rows, keeps rows with no recorded market, and keeps everything for All. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();
const {renderToStaticMarkup} = await import('react-dom/server');
const {MarketScopeNote, inMarket, pickedMarket} = await import('../../marketScope.jsx');
afterAll(() => GlobalRegistrator.unregister());

test('the picked market keeps its rows and rows with no market; All keeps every row', () => {
  expect(pickedMarket('NG')).toBe('NG');
  expect(pickedMarket('ALL')).toBe('');
  expect(pickedMarket(undefined)).toBe('');
  const rows = [{market: 'ZA'}, {market: 'NG'}, {market: null}, {market: 'KE'}];
  expect(rows.filter((r) => inMarket(r.market, 'NG'))).toEqual([{market: 'NG'}, {market: null}]);
  expect(rows.filter((r) => inMarket(r.market, ''))).toEqual(rows);
});

test('the note says how many rows other markets hold, and nothing when none are hidden', () => {
  expect(renderToStaticMarkup(<MarketScopeNote picked="NG" hidden={2} what="dossiers are" />))
    .toContain('Showing Nigeria only. 2 dossiers are in other markets; pick All markets at the top to see them.');
  expect(renderToStaticMarkup(<MarketScopeNote picked="NG" hidden={0} what="dossiers are" />)).toBe('');
  expect(renderToStaticMarkup(<MarketScopeNote picked="" hidden={3} what="dossiers are" />)).toBe('');
});
