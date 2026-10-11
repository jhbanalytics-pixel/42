/* Ask · the ranked briefing, as the page reads it back. core/agent/briefing.py works the stories out of the stored
   claims on every read and puts ids and indexes in run.briefing, with no text of its own. This page does not take that
   on trust. A story's name is the start of its claims' own words, a figure is the claim's own pinned number and a
   so-what line is the answer's own; whatever the answer does not bear out is left off, and fewer than two stories left
   means the usual layout. Pure functions, so the page and its tests say the same things. */
import {readerFigure} from './api.js';
import {plainUnit, unitFor} from './readerUnits.js';

const KIND = 'ranked_stories';
const STORY_KINDS = ['observation', 'interpretation'];
const SIZE_UNITS = ['posts', 'creators', 'platforms'];

const isClaim = (claim) => Boolean(claim && typeof claim === 'object' && typeof claim.id === 'string' && typeof claim.text === 'string');
const capital = (text) => text.charAt(0).toUpperCase() + text.slice(1);

/* A number that carries a pin: a positive count, the query it came from, the run and the hash of that query's rows. The
   shape is all this page can check; K2 compared the figure with the recorded rows when the claim was kept. */
function pinned(number){
  return Boolean(number && typeof number === 'object' && typeof number.value === 'number' && Number.isFinite(number.value) && number.value > 0
    && typeof number.query_id === 'string' && number.query_id !== '' && typeof number.run_id === 'string' && number.run_id !== ''
    && typeof number.result_hash === 'string' && /^sha256:[0-9a-f]{64}$/.test(number.result_hash));
}

/* The figures a story's size claim bears out, all from one query. */
function figuresOf(size, claims){
  const claim = size && claims.get(size.claim_id);
  if (!claim || !Array.isArray(size.figures)) return [];
  const out = [];
  let query = null;
  for (const figure of size.figures){
    const unit = figure && figure.unit;
    const number = Number.isInteger(figure && figure.index) ? (claim.numbers || [])[figure.index] : null;
    if (!SIZE_UNITS.includes(unit) || !pinned(number) || String(number.unit || '').trim().toLowerCase() !== unit) continue;
    if (query !== null && number.query_id !== query) continue;
    query = number.query_id;
    out.push({key: unit, value: readerFigure(number.value), unit: unitFor(number.value, plainUnit(number.unit)), queryId: number.query_id});
  }
  return out;
}

export function storyView(record){
  const answer = record && record.answer;
  const brief = record && record.run && record.run.briefing;
  if (!answer || !Array.isArray(answer.claims) || !brief || brief.kind !== KIND || !Array.isArray(brief.stories)) return null;
  const claims = new Map(answer.claims.filter(isClaim).map((claim) => [claim.id, claim]));
  const soWhat = Array.isArray(answer.so_what) ? answer.so_what : [];
  const taken = new Set();
  const attached = new Set();
  const stories = [];
  for (const entry of brief.stories){
    const name = entry && typeof entry.name === 'string' ? entry.name : '';
    if (!name || !Array.isArray(entry.claim_ids)) continue;
    const lines = [];
    for (const id of entry.claim_ids){
      const claim = claims.get(id);
      if (!claim || taken.has(id) || !STORY_KINDS.includes(claim.kind) || !claim.text.startsWith(name + ': ') || claim.text.length <= name.length + 2) continue;
      taken.add(id);
      lines.push({claim, body: capital(claim.text.slice(name.length + 2))});
    }
    if (lines.length === 0) continue;
    const own = new Set(lines.map((line) => line.claim.id));
    const lined = [];
    for (const index of Array.isArray(entry.so_what) ? entry.so_what : []){
      const item = Number.isInteger(index) ? soWhat[index] : null;
      const ids = item && Array.isArray(item.claim_ids) ? item.claim_ids : [];
      if (!item || typeof item.text !== 'string' || attached.has(index) || ids.length === 0 || !ids.every((id) => own.has(id))) continue;
      attached.add(index);
      lined.push({index, text: item.text});
    }
    stories.push({key: name, name, figures: figuresOf(entry.size && own.has(entry.size.claim_id) ? entry.size : null, claims), lines, soWhat: lined});
  }
  if (stories.length < 2) return null;
  const ordered = brief.ordered === true;
  return {
    ordered,
    stories: stories.map((story, position) => ({...story, rank: ordered ? position + 1 : null})),
    others: answer.claims.filter((claim) => isClaim(claim) && !taken.has(claim.id)),
    restSoWhat: soWhat.map((item, index) => ({index, text: item && item.text})).filter((item) => typeof item.text === 'string' && !attached.has(item.index)),
  };
}
