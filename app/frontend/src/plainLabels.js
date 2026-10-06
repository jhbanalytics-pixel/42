/* Words for the machine values Source Lab, Discover and the recent asks list
   show. The value itself is never changed or dropped: a label stands where a
   reader looks, and the exact reference stays one disclosure away for anyone
   who has to quote it. A value this build has no word for is shown with its
   underscores read as spaces rather than guessed at. */

const LANES = Object.freeze({ogilvy_funded: 'Ogilvy funded lane', jhb_core: 'JHB core lane'});
const ACCOUNTS = Object.freeze({ogilvy_albert: 'Ogilvy account (Albert)', jhb_analytics: 'JHB Analytics account'});
const PLATFORMS = Object.freeze({
  tiktok: 'TikTok', youtube: 'YouTube', instagram: 'Instagram', facebook: 'Facebook', reddit: 'Reddit',
  linkedin: 'LinkedIn', twitter: 'X', x: 'X', threads: 'Threads', pinterest: 'Pinterest', snapchat: 'Snapchat',
  twitch: 'Twitch', telegram: 'Telegram', whatsapp: 'WhatsApp', google: 'Google', news: 'News', search: 'Search',
});

/* Route statuses in the lower case the other state words on the page use. */
const ROUTE_STATUS = Object.freeze({
  active: 'active', pilot: 'pilot', available_unwired: 'available, not wired', blocked: 'blocked',
  permanently_rejected: 'permanently rejected', inventory_only: 'catalogue only',
});

function spaced(value){
  return String(value).replace(/_+/g, ' ').trim();
}

export function laneLabel(value){
  return Object.hasOwn(LANES, value) ? LANES[value] : spaced(value) + ' lane';
}

export function fundingAccountLabel(value){
  return Object.hasOwn(ACCOUNTS, value) ? 'Funded from the ' + ACCOUNTS[value] : 'Funding account ' + spaced(value);
}

/* A producer sentence with its lane and account keys named in words: "the
   ogilvy_funded lane is exhausted" reads "the Ogilvy funded lane is
   exhausted". Only a whole key this build has a word for is replaced, so a
   longer id, a path or a setting that contains one keeps its form. A lane key
   that already stands next to the word lane takes its name alone, so the
   sentence never says lane twice. Every other word stays as written. */
const KNOWN_KEY = new RegExp(`(?<![\\w\\-/=.])(${[...Object.keys(LANES), ...Object.keys(ACCOUNTS)].join('|')})(?![\\w\\-/=])`, 'g');
const LANE_BEFORE = /\blanes?[\s:]+$/i;
const LANE_AFTER = /^\s+lanes?(?![a-z])/i;

export function laneWords(text){
  if (typeof text !== 'string') return text;
  return text.replace(KNOWN_KEY, (key, _match, offset, whole) => {
    if (!Object.hasOwn(LANES, key)) return ACCOUNTS[key];
    const nearLane = LANE_BEFORE.test(whole.slice(0, offset)) || LANE_AFTER.test(whole.slice(offset + key.length));
    return nearLane ? LANES[key].replace(/ lane$/, '') : LANES[key];
  });
}

/* A state word as the page prints it: one word as given, and a compound
   value with its underscores read as spaces. */
export function stateText(value){
  if (value == null || value === '') return 'Unknown';
  return spaced(value);
}

export function routeStatusText(value){
  if (value == null || value === '') return 'Unknown';
  return Object.hasOwn(ROUTE_STATUS, value) ? ROUTE_STATUS[value] : spaced(value);
}

export function platformLabel(value){
  if (typeof value !== 'string' || !value) return 'Unnamed platform';
  if (Object.hasOwn(PLATFORMS, value)) return PLATFORMS[value];
  const words = spaced(value);
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function dayText(year, month, day){
  const at = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day || 1)));
  if (Number.isNaN(at.getTime()) || at.getUTCMonth() !== Number(month) - 1) return null;
  const options = day ? {day: 'numeric', month: 'short', year: 'numeric'} : {month: 'long', year: 'numeric'};
  return at.toLocaleDateString('en-ZA', {timeZone: 'UTC', ...options});
}

/* socialcrawl_catalog_2026_09_07 reads as the SocialCrawl catalogue of
   7 Sept 2026; a version of another shape is only called the catalogue. */
export function catalogueLabel(version){
  const match = /^socialcrawl_catalog_(\d{4})_(\d{2})(?:_(\d{2}))?$/.exec(String(version || ''));
  const when = match ? dayText(match[1], match[2], match[3]) : null;
  return when ? 'SocialCrawl catalogue of ' + when : 'Documented catalogue';
}

/* run_20260912_dynamic_apply_v2_r1 reads as the released run of
   12 Sept 2026; a run id that names no date is only called the released run. */
export function releasedRunLabel(runId){
  const match = /^run_(\d{4})(\d{2})(\d{2})(?:_|$)/.exec(String(runId || ''));
  const when = match ? dayText(match[1], match[2], match[3]) : null;
  return when ? 'Released run of ' + when : 'Released run';
}

/* Dates written for a reader, the way the design system's reader text writes
   them: day, short month and year ("18 to 24 Aug 2026"), a range inside one
   month or one year naming that month or year once. The package does not
   export its helper, so the hosts keep this twin of it. It is written by hand
   rather than through toLocaleDateString, which pads the day in some runtimes.
   A date is only a date when no letter, digit, underscore or hyphen touches
   it, and a colon, slash or full stop with an id character on its far side
   is part of an id, so "run_20260912" and "2026-09-12.json" keep their form.
   A string that is not a real calendar date is left as it was written. */
const SHORT_MONTHS = Object.freeze(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sept', 'Oct', 'Nov', 'Dec']);
const DATE_BEFORE = '(?<![\\w-])(?<!\\w[:/.])';
const DATE_AFTER = '(?![\\w-])(?![:/.]\\w)';
const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;
const ISO_RANGE = new RegExp(`${DATE_BEFORE}(\\d{4}-\\d{2}-\\d{2})\\s+to\\s+(\\d{4}-\\d{2}-\\d{2})${DATE_AFTER}`, 'g');
const ISO_SINGLE = new RegExp(`${DATE_BEFORE}\\d{4}-\\d{2}-\\d{2}${DATE_AFTER}`, 'g');

function calendarDate(text){
  const match = ISO_DAY.exec(text);
  if (!match) return null;
  const [year, month, day] = match.slice(1).map(Number);
  if (month < 1 || month > 12) return null;
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  if (Number.isNaN(date.getTime()) || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  return date;
}

function writtenDate(date, {day = true, month = true, year = true} = {}){
  const words = [];
  if (day) words.push(String(date.getUTCDate()));
  if (month) words.push(SHORT_MONTHS[date.getUTCMonth()]);
  if (year) words.push(String(date.getUTCFullYear()));
  return words.join(' ');
}

/* One ISO date as "12 Sept 2026"; anything else comes back unchanged. */
export function readableDate(value){
  if (typeof value !== 'string') return value;
  const date = calendarDate(value.trim());
  return date ? writtenDate(date) : value;
}

/* Every ISO date and ISO date range inside a sentence, written for a reader:
   "Window 2026-08-18 to 2026-08-24" reads "Window 18 to 24 Aug 2026". */
export function readableDates(value){
  if (typeof value !== 'string') return value;
  return value
    .replace(ISO_RANGE, (whole, from, to) => {
      const start = calendarDate(from);
      const end = calendarDate(to);
      if (!start || !end) return whole;
      const sameYear = start.getUTCFullYear() === end.getUTCFullYear();
      const sameMonth = sameYear && start.getUTCMonth() === end.getUTCMonth();
      const head = sameMonth
        ? writtenDate(start, {month: false, year: false})
        : writtenDate(start, {year: !sameYear});
      return `${head} to ${writtenDate(end)}`;
    })
    .replace(ISO_SINGLE, (whole) => {
      const date = calendarDate(whole);
      return date ? writtenDate(date) : whole;
    });
}

export function askCountText(count){
  return count === 1 ? '1 ask' : count + ' asks';
}

const SAST_PARTS = new Intl.DateTimeFormat('en-ZA', {
  timeZone: 'Africa/Johannesburg', year: 'numeric', month: 'numeric', day: 'numeric',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

/* "4 Sept 2026, 08:30 SAST", the shape every stamp in 42 takes. */
export function snapshotTime(stamp){
  const at = new Date(stamp);
  if (Number.isNaN(at.getTime())) return String(stamp);
  const part = Object.fromEntries(SAST_PARTS.formatToParts(at).map((p) => [p.type, p.value]));
  return Number(part.day) + ' ' + SHORT_MONTHS[Number(part.month) - 1] + ' ' + part.year + ', ' + part.hour + ':' + part.minute + ' SAST';
}

/* Demo polish, 2 October 2026. The API words the Rising rule as a statistics
   test ("Significant on 2 days with ratio 2 or more"). The marker reads it
   the way a strategist would say it; any other rule is already plain and
   passes through unchanged. */
const SIGNIFICANT_RULE = /^Significant on (\d+) (days?) with ratio (\d+(?:\.\d+)?) or more(, or on 1 day plus another platform or market)?$/;
export function lifecycleRuleWords(rule){
  if (typeof rule !== 'string' || !rule.trim()) return '';
  const match = SIGNIFICANT_RULE.exec(rule.trim());
  if (!match) return rule;
  const [, days, dayWord, ratio, alsoElsewhere] = match;
  const times = ratio === '2' ? 'twice' : ratio + ' times';
  return 'At least ' + times + ' its usual posting on ' + days + ' ' + dayWord
    + (alsoElsewhere ? ', or on 1 day when another platform or market also rises clearly' : '');
}
