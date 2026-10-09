/* The cost and item counts under an answer. A record that does not carry a
   figure says so; it never shows the missing figure as zero (wave 8). A
   recorded zero is still a zero. */
import {readerFigure} from './api.js';

const known = (value) => typeof value === 'number' && Number.isFinite(value);

export function costWords(run){
  const source = run || {};
  return [
    known(source.credits) ? readerFigure(source.credits) + ' credits' : 'Credits not recorded',
    known(source.seconds) ? source.seconds + ' s' : null,
  ].filter(Boolean).join(' · ');
}

export const itemsWords = (items) => (known(items) ? readerFigure(items) + ' items' : 'items not recorded');
