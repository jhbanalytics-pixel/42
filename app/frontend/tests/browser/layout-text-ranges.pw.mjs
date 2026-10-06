/* Fixture cases for the text range measure the layout certification takes.
   Round three, 24 Sept 2026: the measure looked for a text's clipping
   container from the text's parent up, so an element that clips its own
   text was never read as clipping it. A visually hidden caption, set at no
   size with clip-path inset(50%) and overflow hidden as the Compare ribbon's
   is, paints nothing, yet its one line of text was read as running past its
   column. These cases pin both sides: text that paints and runs past its
   column is still caught, and text its own element or an ancestor clips to
   nothing is not measured. */
import {expect, test} from '@playwright/test';
import {measureTextRanges} from './support/text-ranges.mjs';

const MARK = 'data-certification-panel';
const LONG = 'A caption set on one line at the title size runs well past its column';

async function measure(page, inner){
  await page.setContent(`<!doctype html><html><body style="margin:0;font:16px/1.4 sans-serif">
    <main id="main-content" style="padding:24px">
      <div style="display:grid;grid-template-columns:200px 200px;gap:24px">
        <article ${MARK}="" style="min-width:0"><p>Short text that fits.</p>${inner}</article>
        <article ${MARK}="" style="min-width:0"><p>A second column.</p></article>
      </div>
    </main></body></html>`);
  return page.evaluate(measureTextRanges, MARK);
}

test('text ranges: visible text that runs past its column is caught', async ({page}) => {
  const ranges = await measure(page, `<p class="visible" style="white-space:nowrap">${LONG}</p>`);
  expect(ranges.checked).toBe(3);
  expect(ranges.outside).toHaveLength(1);
  expect(ranges.outside[0]).toContain('p.visible');
});

test('text ranges: visible text its own element clips at the column edge is still caught', async ({page}) => {
  const ranges = await measure(page, `<p class="clipped" style="white-space:nowrap;overflow:hidden;width:400px">${LONG}</p>`);
  expect(ranges.outside).toHaveLength(1);
  expect(ranges.outside[0]).toContain('p.clipped');
});

test('text ranges: a clip-path that leaves the text painted does not hide an overflow', async ({page}) => {
  const ranges = await measure(page, `<p class="inset" style="white-space:nowrap;clip-path:inset(0 0 0 0)">${LONG}</p>`);
  expect(ranges.outside).toHaveLength(1);
  expect(ranges.outside[0]).toContain('p.inset');
});

test('text ranges: a caption visually hidden with clip-path inset is not measured', async ({page}) => {
  const ranges = await measure(page, `<figure style="position:relative;margin:0"><figcaption class="hidden-caption" style="position:absolute;inline-size:0;block-size:0;clip-path:inset(50%);white-space:nowrap;overflow:hidden;font-size:28px">${LONG}</figcaption><p>Chart</p></figure>`);
  expect(ranges.outside).toEqual([]);
  expect(ranges.checked).toBe(3);
});

test('text ranges: text visually hidden with clip rect is not measured', async ({page}) => {
  const ranges = await measure(page, `<span class="sr" style="position:absolute;width:1px;height:1px;margin:-1px;padding:0;border:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap">${LONG}</span>`);
  expect(ranges.outside).toEqual([]);
  expect(ranges.checked).toBe(2);
});

test('text ranges: text an ancestor visually hides is not measured', async ({page}) => {
  const ranges = await measure(page, `<div style="position:absolute;inline-size:0;block-size:0;clip-path:inset(50%);overflow:hidden"><span class="nested" style="white-space:nowrap">${LONG}</span></div>`);
  expect(ranges.outside).toEqual([]);
  expect(ranges.checked).toBe(2);
});

/* Round three, 24 Sept 2026: an element set to display contents makes no
   box, so its measured rect is empty and a clip-path or clip on it clips
   nothing. Its text still paints and a range that runs past its column is
   still caught. */
test('text ranges: a clip-path on a display contents element does not hide an overflow', async ({page}) => {
  const ranges = await measure(page, `<div style="display:contents;clip-path:inset(50%)"><p class="contents" style="white-space:nowrap">${LONG}</p></div>`);
  expect(ranges.checked).toBe(3);
  expect(ranges.outside).toHaveLength(1);
  expect(ranges.outside[0]).toContain('p.contents');
});
