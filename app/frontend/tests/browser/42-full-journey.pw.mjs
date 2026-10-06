/* BUILD.md 1.15: open Today, open a trend's posts, ask about it, see the log,
   open a source, export the answer. Runs on the fixtures by default and on
   staging when F42_BASE_URL is set (playwright.42.config.mjs). */
import {expect, test} from '@playwright/test';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {LOCAL_PASSCODE} from '../../playwright.42.config.mjs';

const SHOTS = new URL('../../test-results/', import.meta.url);
const CONFIDENCE = /Observed|Corroborated|Single source|Inferred/;

function passcode(){
  if (!process.env.F42_BASE_URL) return LOCAL_PASSCODE;
  const value = process.env.F42_SMOKE_PASSCODE || '';
  if (!value) throw new Error('F42_SMOKE_PASSCODE must be set when F42_BASE_URL is set');
  return value;
}

/* The export page escapes text the way Python's html.escape does. */
function escapeHtml(text){
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#x27;');
}

test('42 journey: Today, posts, Ask with its log, a source, the export', async ({page}) => {
  await page.addInitScript((code) => {
    localStorage.setItem('pulse_passcode', code);
    localStorage.setItem('pulse-region', 'ZA');
  }, passcode());

  const remote = Boolean(process.env.F42_BASE_URL);
  await page.goto('/#/today');
  await expect(page.getByRole('heading', {level: 1, name: remote ? /^Taking off/ : /^Taking off, /})).toBeVisible({timeout: 20000});
  await expect(page.getByRole('tab', {name: 'South Africa'})).toHaveAttribute('aria-selected', 'true');

  const cardsIn = (name) => page.getByRole('region', {name}).getByRole('listitem').filter({has: page.getByRole('heading', {level: 3})});
  let cards = cardsIn('South Africa');
  if (!remote){
    await expect(cards).toHaveCount(5);
  } else {
    /* Staging has whatever the morning published: the first market tab
       with cards, and one to five of them. */
    cards = null;
    for (const name of ['South Africa', 'Nigeria', 'Kenya']){
      const tab = page.getByRole('tab', {name, exact: true});
      await tab.click();
      await expect(tab).toHaveAttribute('aria-selected', 'true');
      await expect(page.getByRole('region', {name})).toBeVisible();
      if (await cardsIn(name).count() > 0){ cards = cardsIn(name); break; }
    }
    expect(cards, 'no market on staging has cards today').not.toBeNull();
    const count = await cards.count();
    expect(count).toBeGreaterThanOrEqual(1);
    expect(count).toBeLessThanOrEqual(5);
  }
  await page.screenshot({path: fileURLToPath(new URL('42-journey-today.png', SHOTS)), fullPage: true});

  const card = cards.first();
  const posts = card.getByRole('button', {name: 'Posts', exact: true});
  await posts.click();
  await expect(posts).toHaveAttribute('aria-expanded', 'true');
  const panel = page.locator('#' + cssId(await posts.getAttribute('aria-controls')));
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('aria-busy', 'false');
  await expect(panel.getByRole('listitem').first()).toBeVisible();
  expect(await panel.getByRole('listitem').count()).toBeGreaterThan(0);

  const askLink = card.getByRole('link', {name: 'Ask about this'});
  const question = new URLSearchParams((await askLink.getAttribute('href')).split('?')[1]).get('q');
  expect(question).toBeTruthy();

  const started = page.waitForResponse((res) => res.request().method() === 'POST' && new URL(res.url()).pathname === '/api/ask');
  /* Demo run, 2 Oct 2026: Ask about this starts a paid live ask, so it names
     the cost and waits for Ask before the Ask page opens. */
  const before = page.url();
  await askLink.click();
  const confirm = page.getByRole('dialog', {name: 'Ask 42 about this now?'});
  await expect(confirm).toContainText('This starts a new live ask, up to 60 credits.');
  expect(page.url()).toBe(before);
  await confirm.getByRole('button', {name: 'Ask', exact: true}).click();
  await expect(page).toHaveURL(/#\/ask\?/);
  const startedRes = await started;
  expect(startedRes.status()).toBe(202);
  const askId = (await startedRes.json()).ask_id;
  expect(askId).toMatch(/^[A-Za-z0-9_-]+$/);

  /* The research log fills from the event stream before the answer exists. */
  const log = page.getByRole('region', {name: 'Researching'});
  await expect(log.getByRole('listitem').first()).toBeVisible({timeout: 20000});
  await expect(page.getByRole('button', {name: 'Export answer'})).toHaveCount(0);
  const stepsWhileRunning = await log.getByRole('listitem').count();
  expect(stepsWhileRunning).toBeGreaterThan(0);

  const answer = page.getByRole('article');
  await expect(answer).toBeVisible({timeout: 45000});
  await expect(answer.getByRole('heading', {level: 2, name: question})).toBeVisible();
  await expect(answer).toHaveAccessibleDescription(/\S.{20,}/);
  const claim = answer.getByRole('list', {name: 'Claims'}).getByRole('listitem').first();
  await expect(claim).toContainText(CONFIDENCE);
  const chip = claim.getByRole('button', {name: /^Source: /}).first();
  await expect(chip).toBeVisible();
  await expect(page.getByText(/^How this was researched · \d+ steps?$/)).toBeVisible();

  await chip.focus();
  await expect(page.getByRole('tooltip')).toBeVisible();
  const post = /^Source: (.+? post by [^,]+)/.exec(await chip.getAttribute('aria-label'))[1];
  await chip.click();
  await expect(chip).toHaveAttribute('aria-pressed', 'true');
  const source = page.getByRole('complementary', {name: 'Source'});
  await expect(source.getByRole('heading', {level: 3})).toHaveText(post);
  await expect(source.getByRole('heading', {level: 3})).toBeFocused();
  await expect(source.getByRole('link', {name: 'Open the post'})).toBeVisible();
  await page.screenshot({path: fileURLToPath(new URL('42-journey-answer.png', SHOTS)), fullPage: true});

  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', {name: 'Export answer'}).click();
  const file = await downloaded;
  expect(file.suggestedFilename()).toBe('42-answer-' + askId + '.html');
  const html = await readFile(await file.path(), 'utf8');
  expect(html).toContain(escapeHtml(question));
});

function cssId(id){
  return String(id || '').replace(/([^A-Za-z0-9_-])/g, '\\$1');
}
