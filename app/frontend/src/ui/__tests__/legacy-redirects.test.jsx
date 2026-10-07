import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {GlobalRegistrator} from '@happy-dom/global-registrator';

/* Page port, 3 October 2026. Board, Listen, Network, Browse, Source Lab,
   Historical, the older console and the older topic and creator pages read
   the desk API, which f42-api does not serve, so each opened on an error.
   Their links now land on the 42 page that holds the same job, and none of
   them is listed under More. */

beforeAll(() => {
  if (typeof window === 'undefined') GlobalRegistrator.register({url: 'http://localhost/'});
});

afterAll(() => {
  if (GlobalRegistrator.isRegistered) GlobalRegistrator.unregister();
});

const ITEM = 'a'.repeat(64);

describe('legacyHashTarget', () => {
  test('sends each desk page to the 42 page that does its job', async () => {
    const {legacyHashTarget} = await import('../../legacyRoutes.js');
    const cases = [
      ['#/browse', '#/explore'],
      ['#/browse?region=za', '#/explore'],
      ['#/network', '#/communities'],
      ['#/board', '#/alerts'],
      ['#/source-lab', '#/fieldwork'],
      ['#/historical', '#/history'],
      ['#/historical/inv_case/analogue', '#/history'],
      ['#/listen', '#/seedpath'],
      ['#/listen/amapiano?region=za', '#/seedpath/amapiano?region=za'],
      ['#/listen/japa%20season?region=ng', '#/seedpath/japa%20season?region=ng'],
      ['#/listen/amapiano?region=all', '#/seedpath/amapiano'],
      ['#/listen/amapiano', '#/seedpath/amapiano'],
      ['#/listen/piano?region=ng&region=ke', '#/seedpath/piano?region=ng&region=ke'],
      ['#/listen/piano?region=us', '#/seedpath/piano?region=us'],
    ];
    for (const [from, to] of cases) expect([from, legacyHashTarget(from)]).toEqual([from, to]);
  });

  test('a question carried on the older console opens in Ask without spending', async () => {
    const {legacyHashTarget} = await import('../../legacyRoutes.js');
    expect(legacyHashTarget('#/console/What%20is%20amapiano%3F')).toBe('#/ask?q=What+is+amapiano%3F&draft=1');
    expect(legacyHashTarget('#/chat/sapa')).toBe('#/ask?q=sapa&draft=1');
    expect(legacyHashTarget('#/chat?work=ask')).toBe('#/ask');
    expect(legacyHashTarget('#/research?work=ask')).toBe('#/ask');
  });

  test('Ask aliases are canonical before mount and retain question, draft and scope parameters', async () => {
    const {legacyHashTarget} = await import('../../legacyRoutes.js');
    for (const view of ['console', 'chat', 'research']){
      expect(legacyHashTarget('#/' + view + '?work=ask')).toBe('#/ask');
      expect(legacyHashTarget('#/' + view + '?work=ask&q=What%20changed%3F&market=NG&draft=1'))
        .toBe('#/ask?q=What+changed%3F&market=NG&draft=1');
      expect(legacyHashTarget('#/' + view + '?work=ask&question=fixture&follow=a_fixture&item=item_fixture&date=2026-10-06&fit=creator_fixture'))
        .toBe('#/ask?question=fixture&follow=a_fixture&item=item_fixture&date=2026-10-06&fit=creator_fixture');
      expect(legacyHashTarget('#/' + view + '?work=ask&request=0b5c1a7e-3c1f-4d1a-9a1e-1c2d3e4f5a6b&q=fixture&draft=1'))
        .toBe('#/history');
    }
  });

  test('a blank or unreadable console question lands on Build, not the older console', async () => {
    const {legacyHashTarget} = await import('../../legacyRoutes.js');
    expect(legacyHashTarget('#/console/%20')).toBe('#/console');
    expect(legacyHashTarget('#/console/%zz')).toBe('#/console');
  });

  test('older console links land on Build, an investigation or History', async () => {
    const {legacyHashTarget} = await import('../../legacyRoutes.js');
    expect(legacyHashTarget('#/console?work=brief')).toBe('#/console');
    expect(legacyHashTarget('#/research')).toBe('#/console');
    expect(legacyHashTarget('#/console?work=brief&investigation=inv_case')).toBe('#/investigations/inv_case');
    expect(legacyHashTarget('#/console?work=brief&investigation=inv_case&artifact=ra_doc')).toBe('#/investigations/inv_case');
    expect(legacyHashTarget('#/console?work=ask&request=0b5c1a7e-3c1f-4d1a-9a1e-1c2d3e4f5a6b')).toBe('#/history');
  });

  test('a 42 item id on the older topic route opens the 42 topic page', async () => {
    const {legacyHashTarget} = await import('../../legacyRoutes.js');
    expect(legacyHashTarget('#/topic/' + ITEM + '?region=ke')).toBe('#/t/' + ITEM + '?market=KE');
    expect(legacyHashTarget('#/topic/' + ITEM + '?region=all')).toBe('#/t/' + ITEM);
    expect(legacyHashTarget('#/topic/' + ITEM)).toBe('#/t/' + ITEM);
    expect(legacyHashTarget('#/topic/' + ITEM.toUpperCase())).toBe('#/t/' + ITEM);
  });

  test('42 pages, the plain Build page and Ask hashes are left alone', async () => {
    const {legacyHashTarget} = await import('../../legacyRoutes.js');
    for (const hash of ['', '#/pulse', '#/explore', '#/console', '#/ask?q=x', '#/seedpath/x?region=za', '#/t/' + ITEM, '#/seeds', '#/lexicon/sapa?region=ng', '#/topic/older-desk-id?region=za', '#/creator/someone']) {
      expect([hash, legacyHashTarget(hash)]).toEqual([hash, null]);
    }
  });
});

describe('parseHash', () => {
  test('a console Ask alias returns the Ask route before any component can mount', async () => {
    const {parseHash} = await import('../../router.js');
    window.location.hash = '#/console?work=ask&q=Fixture%20question&market=KE&draft=1';
    const parsed = parseHash();
    expect(parsed.view).toBe('ask');
    expect(window.location.hash).toBe('#/ask?q=Fixture+question&market=KE&draft=1');
  });

  test('replaces a desk page hash with its 42 page before the route is read', async () => {
    const {parseHash} = await import('../../router.js');
    window.location.hash = '#/board';
    const parsed = parseHash();
    expect(window.location.hash).toBe('#/alerts');
    expect(parsed.view).toBe('alerts');
  });

  test('a percent-encoded desk page name lands on its 42 page, as the plain name does', async () => {
    const {parseHash} = await import('../../router.js');
    for (const [from, to, view] of [['#/%62rowse', '#/explore', 'explore'], ['#/%6eetwork', '#/communities', 'communities'], ['#/browse', '#/explore', 'explore'], ['#/network', '#/communities', 'communities']]){
      window.location.hash = from;
      const parsed = parseHash();
      expect([from, window.location.hash, parsed.view]).toEqual([from, to, view]);
    }
  });

  test('carries a Listen term to Seed path', async () => {
    const {parseHash} = await import('../../router.js');
    window.location.hash = '#/listen/amapiano?region=za';
    const parsed = parseHash();
    expect(window.location.hash).toBe('#/seedpath/amapiano?region=za');
    expect(parsed.view).toBe('seedpath');
    expect(parsed.param).toBe('amapiano');
  });
});

describe('the More menu', () => {
  test('lists no page that reads the desk API', async () => {
    /* UX pass, 3 October 2026: the menu's every page is RAIL_ALL. */
    const {RAIL_ALL} = await import('../../rail42.jsx');
    const hrefs = RAIL_ALL.map((item) => item.href);
    for (const gone of ['#/board', '#/listen', '#/network', '#/browse']) expect(hrefs).not.toContain(gone);
    expect(hrefs).toContain('#/seeds');
    expect(hrefs).toContain('#/lexicon');
  });
});
