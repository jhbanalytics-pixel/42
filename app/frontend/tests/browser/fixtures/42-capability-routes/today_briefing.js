/* Today reads GET /api/today (core/api/contract.md section 4). Each state is
   the checked-in brief of 30 September 2026, the fixture the Today unit tests
   build from core/api/today.build_today, shaped as the producer shapes that
   state. The page's side reads answer empty, so no other read is needed. */
import {readFileSync} from 'node:fs';

const brief = () => JSON.parse(readFileSync(new URL('../../../../src/ui/__tests__/fixtures/today42.json', import.meta.url), 'utf8'));

export const THIN_BANNER = 'Thin coverage: 2 of 4 collection series usable today';
export const FAILED_MESSAGE = 'The brief store did not answer.';

const SIDE_READS = {
  '/api/alerts': {alerts: []},
  '/api/investigations': {investigations: []},
  '/api/schedules': {schedules: []},
};

function emptied(body, {keepHeld}){
  for (const market of body.markets){
    market.cards = [];
    market.more = [];
    if (!keepHeld) market.held_back = {count: 0, items: []};
  }
  body.heading = 'Today, 30 September 2026';
  body.headline = null;
  return body;
}

function todayFor(state){
  if (state === 'loading') return {__hold: true};
  if (state === 'failed') return {__status: 503, body: {error: 'unavailable', message: FAILED_MESSAGE}};
  const body = brief();
  if (state === 'empty') return {...emptied(body, {keepHeld: false}), status: 'published'};
  if (state === 'held') return emptied(body, {keepHeld: true});
  if (state === 'thin'){
    const za = body.markets.find((market) => market.market === 'ZA');
    za.banners = [...za.banners, {kind: 'thin_coverage', text: THIN_BANNER}];
  }
  /* stale and populated serve the brief as stored: opened today, its date is
     an earlier day, which the page says for the stale state. */
  return body;
}

export default {
  capability_id: 'today_briefing',
  route: '#/pulse',
  fixtures: (state) => ({...SIDE_READS, '/api/today': todayFor(state)}),
  gaps: {},
};
