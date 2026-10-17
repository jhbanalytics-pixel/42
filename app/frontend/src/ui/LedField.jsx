/* The LED field behind the Today lead (Control room, slice 2). A matrix of
   square cells, lit in the accent red, draws the lead's posts a day as
   columns rising from the base: one band per measured day, as tall as that
   day's share of the busiest day. The cells brighten towards the right, away
   from the headline, cool towards each column's top, and stay dim enough that
   every text colour on the lead keeps its contrast (at most 28% red over the
   canvas in Dark, 16% in Light). Unlit cells are a faint ink, so the matrix
   is the ground of the whole lead, and the busiest day throws a few sparks
   of the ramp's white heat.

   It is drawn once, and again only when its box changes size: no frame loop,
   so the motion law's ban on idle loops holds. The sheet fades it in once
   (controlroom.css), and it is hidden from assistive technology, because the
   panel beside it states the same series in words and figures. */
import React, {useLayoutEffect, useRef} from 'react';

const PITCH = 10;
const CELL = 7;

/* A stable per cell variation, so the field has texture without randomness
   that would change between paints. */
function grain(column, row){
  let h = (column * 374761393 + row * 668265263) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967295;
}

export function ledLevels(points){
  const values = (Array.isArray(points) ? points : [])
    .map((point) => (point && typeof point.value === 'number' && Number.isFinite(point.value) ? Math.max(0, point.value) : null));
  const top = Math.max(0, ...values.filter((value) => value !== null));
  return values.map((value) => (value === null || top === 0 ? null : value / top));
}

function paint(canvas, levels){
  const box = canvas.getBoundingClientRect();
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  const width = Math.max(0, Math.round(box.width));
  const height = Math.max(0, Math.round(box.height));
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const context = canvas.getContext && canvas.getContext('2d');
  if (!context || !width || !height) return;
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const style = getComputedStyle(canvas);
  const light = document.documentElement.getAttribute('data-dir') === 'daylight';
  const peak = light ? 0.16 : 0.28;
  const red = style.getPropertyValue('--accent').trim() || '#E4002B';
  const ink = style.getPropertyValue('--ink').trim() || '#F8EDEA';
  const heat = style.getPropertyValue('--heat-4').trim() || '#FFD9CF';
  const columns = Math.floor(width / PITCH);
  const rows = Math.floor(height / PITCH);
  const days = levels.length;
  for (let column = 0; column < columns; column += 1){
    const across = columns > 1 ? column / (columns - 1) : 1;
    const level = days ? levels[Math.min(days - 1, Math.floor(across * days))] : null;
    const litRows = level === null ? 0 : Math.round(level * rows * 0.6);
    for (let row = 0; row < rows; row += 1){
      const fromBase = rows - 1 - row;
      const texture = 0.8 + 0.4 * grain(column, row);
      if (fromBase < litRows){
        /* A lit column is hottest at its base and cools to 40% at its top. */
        const height = litRows > 1 ? fromBase / (litRows - 1) : 0;
        context.fillStyle = red;
        context.globalAlpha = Math.min(peak, peak * (0.35 + 0.65 * across) * (1 - 0.6 * height) * texture);
        context.fillRect(column * PITCH, row * PITCH, CELL, CELL);
        /* The busiest day throws sparks of white heat off its top rows. */
        if (level === 1 && fromBase >= litRows - 2 && grain(row, column) > 0.92){
          context.fillStyle = heat;
          context.globalAlpha = light ? 0.22 : 0.35;
          context.fillRect(column * PITCH, row * PITCH, CELL, CELL);
        }
      } else {
        /* Unlit cells are the ground the whole lead sits on, in a faint ink. */
        context.fillStyle = ink;
        context.globalAlpha = (light ? 0.05 : 0.07) * texture;
        context.fillRect(column * PITCH, row * PITCH, CELL, CELL);
      }
    }
  }
  context.globalAlpha = 1;
}

export function LedField({points}){
  const ref = useRef(null);
  const levels = ledLevels(points);
  const key = levels.join(',');
  useLayoutEffect(() => {
    const canvas = ref.current;
    if (!canvas || typeof window === 'undefined') return undefined;
    paint(canvas, levels);
    /* A theme change swaps the red and the strength, so it repaints too. */
    const theme = typeof MutationObserver === 'function' ? new MutationObserver(() => paint(canvas, levels)) : null;
    if (theme) theme.observe(document.documentElement, {attributes: true, attributeFilter: ['data-dir']});
    if (typeof ResizeObserver !== 'function') return () => theme && theme.disconnect();
    let width = canvas.clientWidth;
    let height = canvas.clientHeight;
    const watch = new ResizeObserver(() => {
      if (canvas.clientWidth === width && canvas.clientHeight === height) return;
      width = canvas.clientWidth;
      height = canvas.clientHeight;
      paint(canvas, levels);
    });
    watch.observe(canvas);
    return () => { watch.disconnect(); if (theme) theme.disconnect(); };
    // levels is derived from key; repaint only when the series changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  if (!levels.some((level) => level !== null)) return null;
  return <canvas ref={ref} className="t42-led-field" aria-hidden="true" data-today-led-field="" />;
}
