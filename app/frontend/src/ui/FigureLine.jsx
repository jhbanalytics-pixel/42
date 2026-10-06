/* 42 Ask · FigureLine: one figure from a claim written as a sentence. The
   query that produced it travels with it as its data attribute and hover
   title, so every number carries its query without printing the id. */
import {readerFigure} from '../api.js';
import {plainUnit, unitFor} from '../readerUnits.js';

export function FigureLine({number}){
  if (!number || typeof number.value !== 'number') return null;
  return (
    <p className="ask42-figure" data-query-id={number.query_id} title={'From query ' + number.query_id}>
      <span className="ask42-figure-text">{readerFigure(number.value) + ' ' + unitFor(number.value, plainUnit(number.unit)) + '.'}</span>
    </p>
  );
}
