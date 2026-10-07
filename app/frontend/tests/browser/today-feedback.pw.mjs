import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';

const fixture = () => JSON.parse(readFileSync(new URL('../../src/ui/__tests__/fixtures/today42.json', import.meta.url), 'utf8'));
const ORIGIN = 'http://127.0.0.1:4207';

async function openToday(page, data, market, theme){
  const blocked = [];
  await page.addInitScript(({market, theme}) => {
    localStorage.setItem('pulse_passcode', 'today-feedback-fixture-only');
    localStorage.setItem('pulse-region', market);
    localStorage.setItem('oi-theme', theme);
  }, {market, theme});
  await page.context().routeWebSocket('**/*', (socket) => socket.close());
  await page.context().route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== ORIGIN) return route.abort('blockedbyclient');
    if (!url.pathname.startsWith('/api/')) return route.continue();
    const reads = {
      '/api/health': {ok: true, passcode: true, auth_mode: 'passcode', checks: {auth: 'ok'}},
      '/api/today': data,
      '/api/alerts': {alerts: []},
      '/api/desk': {topics: [], lexicon: []},
      '/api/investigations': {investigations: []},
      '/api/schedules': {schedules: []},
      '/api/watches': {watches: []},
      '/api/research/recent': {artifacts: []},
      '/api/v2/investigations/list': {investigations: [], total_count: 0, truncated: false},
      '/api/v2/investigations/scopes': {scopes: [], default_scope_id: null},
    };
    if (request.method() === 'POST' && url.pathname === '/api/auth/verify'){
      return route.fulfill({json: {ok: true}});
    }
    if (request.method() !== 'GET' || !Object.hasOwn(reads, url.pathname)){
      blocked.push(request.method() + ' ' + url.pathname);
      return route.fulfill({status: 409, json: {error: 'fixture_boundary'}});
    }
    return route.fulfill({json: reads[url.pathname]});
  });
  await page.goto('/#/pulse');
  await expect(page.locator('[data-today-loaded]')).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('data-dir', theme);
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => Promise.all(document.getAnimations()
    .filter((animation) => Number.isFinite(animation.effect.getTiming().iterations))
    .map((animation) => animation.finished.catch(() => {}))));
  return blocked;
}

async function appearance(locator){
  return locator.evaluate((node) => {
    const style = getComputedStyle(node);
    let background = 'rgb(255, 255, 255)';
    for (let ancestor = node; ancestor; ancestor = ancestor.parentElement){
      const value = getComputedStyle(ancestor).backgroundColor;
      if (value !== 'rgba(0, 0, 0, 0)' && value !== 'transparent'){
        background = value;
        break;
      }
    }
    const luminance = (colour) => {
      const channels = colour.match(/[\d.]+/g).slice(0, 3).map(Number).map((value) => {
        const channel = value / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
    };
    const foregroundLuminance = luminance(style.color);
    const backgroundLuminance = luminance(background);
    const bounds = node.getBoundingClientRect();
    return {
      colour: style.color,
      underline: style.textDecorationLine,
      contrast: (Math.max(foregroundLuminance, backgroundLuminance) + 0.05) / (Math.min(foregroundLuminance, backgroundLuminance) + 0.05),
      focusVisible: node.matches(':focus-visible'),
      outlineWidth: parseFloat(style.outlineWidth),
      outlineStyle: style.outlineStyle,
      left: bounds.left,
      right: bounds.right,
    };
  });
}

for (const theme of ['daylight', 'midnight']){
  for (const width of [1280, 390]){
    test(`evidence actions are distinct, readable and keyboard visible at ${theme} ${width}`, async ({page}, testInfo) => {
      await page.setViewportSize({width, height: 900});
      const data = fixture();
      const market = data.markets.find((entry) => entry.market === 'NG');
      market.cards = [];
      market.more = [];
      const blocked = await openToday(page, data, 'NG', theme);
      const disclosure = page.locator('[data-section="held-for-evidence"] .t42-held-more > summary').first();
      await disclosure.scrollIntoViewIfNeeded();
      const plainColour = await page.locator('.t42-held-title').first().evaluate((node) => getComputedStyle(node).color);
      await disclosure.focus();
      await page.keyboard.press('Shift+Tab');
      await page.keyboard.press('Tab');
      const closed = await appearance(disclosure);
      await page.keyboard.press('Enter');
      const post = page.locator('.t42-held-more[open] .t42-post > a').first();
      await expect(post).toBeVisible();
      await page.locator('.t42-held-more[open]').first().evaluate((node) => Promise.all(node.getAnimations({subtree: true})
        .map((animation) => animation.finished.catch(() => {}))));
      const link = await appearance(post);
      expect(link.colour).not.toBe(plainColour);
      expect(closed.colour).toBe(link.colour);
      for (const action of [closed, link]){
        expect(action.underline).toContain('underline');
        expect(action.contrast).toBeGreaterThanOrEqual(4.5);
        expect(action.left).toBeGreaterThanOrEqual(0);
        expect(action.right).toBeLessThanOrEqual(width);
      }
      expect(closed.focusVisible).toBe(true);
      expect(closed.outlineWidth).toBeGreaterThan(0);
      expect(closed.outlineStyle).not.toBe('none');
      await post.hover();
      const hoveredLink = await appearance(post);
      expect(hoveredLink.colour).toBe(link.colour);
      expect(hoveredLink.underline).toContain('underline');
      await post.focus();
      await page.keyboard.press('Shift+Tab');
      await page.keyboard.press('Tab');
      const focusedLink = await appearance(post);
      expect(focusedLink.focusVisible).toBe(true);
      expect(focusedLink.outlineWidth).toBeGreaterThan(0);
      expect(focusedLink.outlineStyle).not.toBe('none');
      await expect(post).toHaveAttribute('target', '_blank');
      await expect(post).toHaveAttribute('rel', /noopener.*noreferrer/);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
      expect(blocked).toEqual([]);
      await testInfo.attach('appearance', {body: JSON.stringify({theme, width, closed, link, hoveredLink, focusedLink}, null, 2), contentType: 'application/json'});
      await page.screenshot({path: testInfo.outputPath('evidence-actions.png'), animations: 'disabled'});
    });
  }
}

test('selected market uses its admitted explanation while All and empty markets keep their meaning', async ({page}) => {
  const data = fixture();
  const market = data.markets.find((entry) => entry.market === 'NG');
  const first = market.cards[0];
  const blocked = await openToday(page, data, 'NG', 'daylight');
  await expect(page.locator('.t42-headline')).toHaveText(first.explanation);
  const queryIds = first.numbers.map((number) => number.query_id);
  const shownQuery = await page.locator('[data-today-lead-side] [data-query-id]').first().getAttribute('data-query-id');
  expect(queryIds).toContain(shownQuery);
  await page.locator('.t42-tabs').getByText('All', {exact: true}).click();
  await expect(page.locator('.t42-headline')).toHaveText(data.headline.text);
  await page.locator('.t42-tabs').getByText('Kenya', {exact: true}).click();
  await expect(page.locator('[data-today-lead]')).toHaveCount(0);
  expect(blocked).toEqual([]);
});

test('a selected-market explanation that fails admission never becomes the headline', async ({page}) => {
  const data = fixture();
  const market = data.markets.find((entry) => entry.market === 'NG');
  market.cards[0].specificity.why_now = 'This explanation was not checked.';
  const blocked = await openToday(page, data, 'NG', 'midnight');
  await expect(page.locator('[data-today-lead]')).toHaveCount(0);
  await expect(page.locator('.t42-today-market[data-market="NG"] .t42-card')).toHaveCount(0);
  expect(blocked).toEqual([]);
});
