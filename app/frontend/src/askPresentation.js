/* What the Ask page says about the dates it covers and the limits of a reply.

   The covered window is the server's: /api/chat/coverage reads the registry the
   question worker selects its source profile from, so the page never names a
   date of its own. Nothing here changes a reply. The helpers decide what the
   page shows beside it, and every limitation the engine supplied stays on the
   page once. */

import {validateIntelligenceReply} from './generalIntelligence.js';
import {QUESTION_REQUEST_ID} from './router.js';
import {buildWorkbenchHash} from './workbenchRoute.js';

export const COVERAGE_PATH = '/api/chat/coverage';
export const COVERAGE_VERSION = 'general_question_coverage_v1';

const DAY = /^\d{4}-\d{2}-\d{2}$/;
const MARKET = {za: 'South Africa', ng: 'Nigeria', ke: 'Kenya'};

function day(value){
  if (typeof value !== 'string' || !DAY.test(value)) return null;
  const at = Date.parse(value + 'T00:00:00Z');
  return Number.isFinite(at) && new Date(at).toISOString().slice(0, 10) === value ? value : null;
}

/* The covered window, or null when the server has none to offer or said
   something this page cannot read. A null never becomes a guessed date. */
export function askCoverage(value){
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  if (value.contract_version !== COVERAGE_VERSION || value.state !== 'covered') return null;
  const window = value.window;
  if (!window || typeof window !== 'object' || Object.keys(window).length !== 2) return null;
  const start = day(window.start);
  const end = day(window.end);
  if (!start || !end || start > end || value.cutoff_date !== end) return null;
  return {start, end};
}

/* Dates as a reader says them: the day without a leading zero, the short
   month, and the year once. A window inside one month names the month once
   ("23 to 29 Aug 2026"), a window inside one year names the year once
   ("25 Aug to 7 Sept 2026"). Machine dates stay ISO in the data. */
function dateParts(value){
  const at = new Date(String(value).slice(0, 10) + 'T00:00:00Z');
  if (!Number.isFinite(at.getTime())) return null;
  return {
    day: String(at.getUTCDate()),
    month: at.toLocaleDateString('en-ZA', {timeZone: 'UTC', month: 'short'}),
    year: String(at.getUTCFullYear()),
  };
}

export function coverageDate(value){
  const date = dateParts(value);
  return date ? date.day + ' ' + date.month + ' ' + date.year : '';
}

export function readableWindow(start, end){
  const from = dateParts(start);
  const to = dateParts(end);
  if (!from || !to) return '';
  if (from.year !== to.year) return coverageDate(start) + ' to ' + coverageDate(end);
  if (from.month !== to.month) return from.day + ' ' + from.month + ' to ' + coverageDate(end);
  if (from.day !== to.day) return from.day + ' to ' + coverageDate(end);
  return coverageDate(end);
}

export function coverageText(coverage){
  return coverage ? readableWindow(coverage.start, coverage.end) : '';
}

/* The page head's one line on what Ask can answer: the market it covers and
   the dates, stated once. */
export function coverageLine(coverage, region){
  if (!coverage) return '';
  const code = String(region || 'ZA').toLowerCase();
  const names = code === 'all' ? Object.values(MARKET) : [MARKET[code]].filter(Boolean);
  const place = names.length > 1 ? names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1] : names[0];
  return 'Covers ' + (place ? place + ', ' : '') + coverageText(coverage) + '.';
}

export function marketNames(markets){
  return (Array.isArray(markets) ? markets : []).map((market) => MARKET[market] || String(market).toUpperCase()).join(' / ');
}

/* A coverage refusal about dates: the engine said the evidence does not cover
   the request, and the window it resolved runs outside the dates the server
   says a question can be answered for. Coverage it cannot read, or a window
   inside the covered dates, is not claimed to be a date problem. */
export function outsideCoverage(intelligence, coverage){
  const window = intelligence && intelligence.window;
  if (!coverage || !window || !Array.isArray(intelligence.missing_work)) return false;
  if (!intelligence.missing_work.includes('coverage_incomplete')) return false;
  return window.end > coverage.end || window.start < coverage.start;
}

/* The rephrase the planner resolves against the source ceiling rather than the
   calendar, so it lands inside the covered dates whatever they are. */
export function latestWeekQuestion(markets){
  const names = (Array.isArray(markets) ? markets : []).map((market) => MARKET[market]).filter(Boolean);
  const place = names.length === 1 ? 'in ' + names[0]
    : names.length > 1 ? 'across ' + names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1]
    : '';
  return 'What stood out' + (place ? ' ' + place : '') + ' in the latest available week?';
}

/* The address that reopens a reply from the server. The stored request route
   carries no lens, so a reply asked under a named lens keeps the lens route
   rather than a link that would drop the configuration it was asked under. */
export function shareableRequestHash(intelligence, clientLensId){
  if (clientLensId) return null;
  const requestId = intelligence && intelligence.request_id;
  if (typeof requestId !== 'string' || !QUESTION_REQUEST_ID.test(requestId)) return null;
  if (!validateIntelligenceReply(intelligence).ok) return null;
  return buildWorkbenchHash({work: 'ask', requestId});
}

/* A reserved allowance in dollars: cents always, and the micro dollars only
   when they are there. */
export function allowanceText(microusd){
  const whole = Math.floor(microusd / 1000000);
  const fraction = String(microusd % 1000000).padStart(6, '0').replace(/0+$/, '').padEnd(2, '0');
  return 'US$' + whole + '.' + fraction;
}

export function storedWindowText(requested, resolved, {answered = true, fromPlan = false} = {}){
  if (requested) return 'Requested window: ' + coverageText(requested) + '.';
  /* No window was sent, so the question's own words or the default chose it;
     the page cannot tell which, and says only what the reply covers. A reply
     that was not answered covers nothing, so it names what the question
     resolved to, and only when a plan resolved it: a reply written before any
     plan carries the engine's default window, which the question never
     resolved to. */
  if (resolved && answered) return 'Requested window: none was sent with the question. The reply covers ' + coverageText(resolved) + '.';
  if (resolved && fromPlan) return 'Requested window: none was sent with the question. It resolved to ' + coverageText(resolved) + '.';
  if (resolved) return 'Requested window: none was sent with the question.';
  return 'Requested window: Unspecified.';
}

const CONTINUITY = 'Authenticated parent continuity was not supplied; prior conversation prose is context only.';
const ADMITTED_UNITS = /^Coverage counts name their units: (\d+) collected records, (\d+) unique observations, (\d+) source families, (\d+) verified independent origins; (\d+) observations carry no verified origin\. None of these counts is population prevalence\.$/;
const CITED_UNITS = /^Cited evidence names its units: (\d+) collected records, (\d+) unique observations, (\d+) source families, (\d+) verified independent origins; (\d+) cited observations carry no verified origin\. None of these counts is population prevalence\.$/;

function units(pattern, lines){
  for (const line of lines){
    const match = pattern.exec(line);
    if (match) return {line, counts: match.slice(1).join(',')};
  }
  return null;
}

/* The reply's own limits, as the Limits section shows them.

   Three things repeat there without adding anything. The parent continuity
   line qualifies prior conversation prose, and a first question has none. The
   admitted and cited unit lines say the same counts twice when every admitted
   record is cited. And a line printed under a claim on the same page is
   printed again in the list. Each of those is shown once; every other line is
   kept exactly as the engine wrote it. */
export function presentedLimitations(intelligence, {firstQuestion = false, shownNotes = []} = {}){
  const lines = Array.isArray(intelligence?.limitations) ? intelligence.limitations : [];
  const shown = new Set(shownNotes);
  const admitted = units(ADMITTED_UNITS, lines);
  const cited = units(CITED_UNITS, lines);
  const repeatedUnits = admitted && cited && admitted.counts === cited.counts ? admitted.line : null;
  return [...new Set(lines)].filter((line) => !(firstQuestion && line === CONTINUITY) && line !== repeatedUnits && !shown.has(line));
}

/* The limitation lines a rendered reply prints beside its claims. */
export function claimNotes(intelligence){
  const claims = new Map((intelligence?.claims || []).map((claim) => [claim.claim_id, claim]));
  const notes = [];
  for (const section of intelligence?.sections || []){
    for (const id of section.claim_ids || []){
      const claim = claims.get(id);
      if (claim && Array.isArray(claim.limitations)) notes.push(...claim.limitations);
    }
  }
  return notes;
}
