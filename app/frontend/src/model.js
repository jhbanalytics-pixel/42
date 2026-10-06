/* Shared topic model: region metadata, momentum ordering, payload decoration.
   Every figure on screen comes from the API payload; absent fields drop. */
import {platformLabel} from './ui/PlatformGlyph.jsx';

/* Market marker. Code, not a flag emoji: Windows has no country-flag glyphs in
   Segoe UI Emoji, so a regional-indicator pair falls back to the two letters and
   renders next to a real code as "ZA ZA". Every market marker in the tool is the
   plain code; MarketChip is the styled version of the same rule. */
export const FLAG = {ZA: 'ZA', NG: 'NG', KE: 'KE'};
export const REGION_NAME = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya'};
export const MOMENTUM_ORDER = {rising: 0, building: 1, steady: 2, cooling: 3};
export const MOMENTA = ['rising', 'building', 'steady', 'cooling'];

/* Behaviour scan (brief scoping step). Live: the scan runs before Build brief,
   approve behaviours, then the brief is scoped to them. Backend gate is
   BEHAVIOUR_SCAN_ENABLED on Cloud Run. */
export const BEHAVIOUR_SCAN = true;

/* Watched topics raise an alert chip when the day's move crosses this. */
export const ALERT_DELTA = 0.018;

/* Static glossary of local terms. Factual definitions only; terms get a
   dotted underline with a hover definition wherever quotes and the lead
   "why" mention them. */
export const GLOSSARY = {
  'sapa': 'Nigerian slang for the state of being broke',
  'japa': 'Yoruba slang meaning to flee; emigrating abroad',
  'maandamano': 'Swahili for protests or demonstrations',
  'sheng': 'Nairobi street language mixing Swahili and English',
  'owambe': 'Lavish Nigerian party, especially weddings',
  'gele': 'Elaborately tied Nigerian headwrap',
  'asoebi': 'Coordinated celebration outfit fabric',
  'stokvel': 'South African community savings club',
  'kasi': 'Township, in South African slang',
  'danfo': 'Lagos yellow minibus',
  'duka': 'Small Kenyan corner shop',
  'matatu': 'Kenyan minibus taxi, famous for art and sound',
  'amapiano': 'South African house genre built on the log drum',
  'gengetone': 'Kenyan street rap genre',
  'log drum': 'The signature bass instrument of amapiano',
  'agege': 'Cheap soft Lagos bread, a humble-hustle icon',
  'bokke': 'The Springboks, the South African national rugby team',
  'mzansi': 'South Africa, in slang',
  'arbantone': 'Kenyan dance-music genre',
};

export function sentimentLabel(s){
  if (s > 0.15) return 'Positive';
  if (s < -0.15) return 'Negative';
  return 'Neutral';
}

/* A count with its noun in the right number: "1 post", "2 posts". The plural
   is the noun with an s unless one is named. */
export function countOf(n, singular, plural = singular + 's'){
  return n + ' ' + (n === 1 ? singular : plural);
}

/* Compact engagement number: 142M, 8.4k, 312. */
export function human(n){
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1).replace(/\.0$/, '') + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e8 ? 0 : 1).replace(/\.0$/, '') + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(Math.round(n));
}

/* A collected source address a reader may open, or null. The archive stores
   whatever address a post or feed carried, so only http and https with a host
   and no user name or password become a link; every other scheme is text. */
export function webHref(value){
  if (typeof value !== 'string' || !value) return null;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && url.hostname && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

/* Link a creator to their profile on the platform the engine saw them on.
   Unknown platforms fall back to a name search. */
export function profileUrl(handle, platform){
  const h = String(handle || '').replace(/^@+/, '').trim();
  if (!h) return '#';
  const p = String(platform || '').toLowerCase();
  if (p.includes('tiktok')) return 'https://www.tiktok.com/@' + h;
  if (p.includes('insta')) return 'https://www.instagram.com/' + h;
  if (p.includes('thread')) return 'https://www.threads.net/@' + h;
  if (p.includes('youtube')) return 'https://www.youtube.com/@' + h;
  if (p.includes('reddit')) return 'https://www.reddit.com/user/' + h;
  if (p.includes('x') || p.includes('twitter')) return 'https://x.com/' + h;
  return 'https://www.google.com/search?q=' + encodeURIComponent(h + ' ' + p);
}

export function regionLabel(region){
  return region === 'ALL' ? 'All markets' : (REGION_NAME[region] || region);
}

/* Human label for engine query_group keys. Matches backend topic_label fallbacks. */
const TOPIC_LABELS = {
  sports_football: 'Football',
  film_nollywood: 'Nollywood',
  politics_maandamano: 'Maandamano protests',
  fintech_mpesa: 'M-Pesa',
  music_amapiano: 'Amapiano',
  music_gengetone: 'Gengetone',
  diaspora_japa: 'Japa migration',
  economy_sapa_hustle: 'Sapa hustle',
  politics_tinubu: 'Tinubu politics',
  genz_lifestyle: 'Lifestyle',
  education_matric_nsfas: 'Matric and NSFAS',
  economy_hustle: 'Hustle economy',
  transport_matatu: 'Matatu culture',
  music_afrobeats: 'Afrobeats',
  politics_crises: 'Politics',
  food_jollof: 'Jollof',
  genz_sheng: 'Sheng',
  infra_power_eskom: 'Eskom and power',
  culture_owambe: 'Owambe',
  sports_rugby: 'Rugby and Springboks',
  food_rituals_braai: 'Braai',
  fashion_mitumba: 'Mitumba fashion',
  finance_stokvel: 'Stokvel',
  fashion_ankara_asoebi: 'Ankara and asoebi',
  tech_gemini_ai: 'Gemini and AI adoption',
};

const TOPIC_PREFIXES = [
  'music_', 'sports_', 'politics_', 'economy_', 'fintech_', 'finance_', 'film_', 'food_',
  'fashion_', 'genz_', 'culture_', 'diaspora_', 'education_', 'transport_', 'infra_',
];

export function topicLabel(key){
  const k = String(key || '').trim();
  if (!k) return '';
  if (TOPIC_LABELS[k]) return TOPIC_LABELS[k];
  let body = k;
  for (const p of TOPIC_PREFIXES){
    if (body.startsWith(p)){ body = body.slice(p.length); break; }
  }
  return body.replace(/_/g, ' ').trim().replace(/\b\w/g, (c) => c.toUpperCase()) || k;
}

/* Task 62. The Evidence Room prints the research plan's known gaps straight to
   the reader, and round 14 read `closed_evidence` there on 12 of 189 cells with
   no Details above it. The gap list is written by the host and is open ended, so
   this reads the same way topicLabel does: a written phrase where one is ruled,
   and the underscores off where one is not. The phrases are register rows in the
   design system's tests/fixtures/copy-register.json. */
const WORKSPACE_GAP_LABELS = {
  closed_evidence: 'Evidence from the closed window',
};

export function workspaceGapLabel(key){
  const k = String(key || '').trim();
  if (!k) return '';
  if (WORKSPACE_GAP_LABELS[k]) return WORKSPACE_GAP_LABELS[k];
  const words = k.replace(/_/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : k;
}

/* The platform names the server gives its own keys, mirrored from
   _PLATFORM_LABELS in app/src/api/bq.py (card.py carries a subset of the same
   pairs). Exact keys only; a key the server adds should be added here too. */
const SERVER_PLATFORM_LABELS = {
  tiktok: 'TikTok',
  instagram: 'Instagram',
  youtube: 'YouTube',
  threads: 'Threads',
  reddit: 'Reddit',
  twitter: 'X',
  news: 'News',
  rss: 'News',
  web: 'Web',
  google_search: 'Google Search',
  search: 'Search',
  apple_music: 'Apple Music',
  bigquery_trends: 'Search Trends',
  gdelt: 'GDELT',
};

/* Research ref types the server sends that read better under their own name
   than as their words. Only exact keys are listed; see readerWord. */
const REF_TYPE_LABELS = {
  google_trends_rising: 'Google Trends',
};

/* Quiet register, 23 Sept 2026: labels no longer set their case in CSS, so a
   word the host sends in lower case, a platform or a source kind, is written
   in sentence case here. The product's own platform names win, and a word
   the host already cased is left as written, so "BBC News" keeps its capitals.
   platformLabel matches loosely ("search_volume" would read as Google Search,
   "newsletter" as News), so its name is taken only when the token is that
   platform's own key; a ref type is never named as a platform it merely
   contains. */
export function readerWord(token){
  const raw = String(token ?? '').trim();
  if (!raw || raw !== raw.toLowerCase()) return raw;
  if (SERVER_PLATFORM_LABELS[raw]) return SERVER_PLATFORM_LABELS[raw];
  const named = platformLabel(raw);
  if (named && named.toLowerCase().replace(/\s+/g, '_') === raw) return named;
  if (REF_TYPE_LABELS[raw]) return REF_TYPE_LABELS[raw];
  const words = raw.replace(/[_-]+/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/* Decorate an engine topic with the derived display fields the views use. */
export function decorate(t){
  if (!t || typeof t !== 'object') return null;
  const d = {...t};
  d.flag = FLAG[d.region] || '';
  d.regionName = REGION_NAME[d.region] || d.region || '';
  if (!MOMENTA.includes(d.momentum)) d.momentum = 'steady';
  if (Array.isArray(d.series) && d.series.length > 1){
    /* the headline % reads across the whole observed series, the same window the
       chart plots. It is null when there is no real baseline to measure from. */
    d.change = typeof d.change === 'number' ? d.change : seriesChange(d.series);
  } else {
    d.series = null;
  }
  return d;
}

/* Percent change from the first real-signal point to the last. The series is
   zero-padded at the head, so the baseline is the first point above a small
   floor; with none, there is no reading and this returns null. The old
   `(last - first) / (first || 0.001)` divided by the padded zero and rendered
   +29500% on real cards. */
export function seriesChange(data){
  if (!Array.isArray(data) || data.length < 2) return null;
  const last = data[data.length - 1];
  const first = data.find((v) => typeof v === 'number' && v > 0.02);
  if (!first || first <= 0) return null;
  return Math.round(((last - first) / first) * 100);
}

export function decorateAll(topics){
  return (Array.isArray(topics) ? topics : []).map(decorate).filter(Boolean);
}
