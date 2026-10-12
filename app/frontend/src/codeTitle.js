/* Albert, 12 October 2026, "code titles": a topic or hashtag card whose
   written title did not pass its checks shows a plain title built here from
   its cluster label, "Posts about Bafana Bafana, Mofokeng and Relebohile",
   not "bafanabafana, mofokeng, relebohile". The rule is
   core/api/code_title.py's, line for line, and one list of cases pins both
   (core/api/tests/fixtures/code_titles.json):
   the label splits at commas and dashes, a long word written twice is spaced,
   and each term is said once; a term holding a mask, a handle, a link or
   profile path, or a birth label is left out (the privacy projection masks a hidden person's
   name or handle), and with nothing left the title says "this topic"; a term
   cased in the label keeps its casing, else it takes the form the most posts
   write it in, lower case included, and only the casing, never a space,
   comes from a post. It never adds a word and never says why. */
const KINDS = ['topic', 'hashtag'];
const FALLBACK = 'this topic';
const UNNAMED = /^[Tt][Oo][Pp][Ii][Cc] [0-9a-fA-F]{8}$/u;
const DROPPED = ['@', '*', '[link]'];
const SEPARATORS = /,|\u2013|\u2014|\s-\s/u;
const DOUBLED_HALF = 5;
const WORD = /[\p{L}\p{M}\p{N}]/u;
const WORD_OR_UNDERSCORE = /[\p{L}\p{M}\p{N}_]/u;
const SPACE = /\s/u;
const LETTERS = /^\p{L}+$/u;

/* Python and JavaScript disagree on these six as whitespace, so both read
   them alike: the zero width no-break space goes, the rest are spaces. */
export const clean = (text) => text.replace(/\ufeff/gu, '').replace(/[\u0085\u001C-\u001F]/gu, ' ').normalize('NFC');
const squash = (text) => text.split(/\s+/u).filter(Boolean).join(' ');
const key = (text) => Array.from(text).filter((ch) => WORD.test(ch)).join('').toUpperCase();
const isLower = (ch) => ch === ch.toLowerCase() && ch !== ch.toUpperCase();
const words = (term) => term.split(' ').map((w) => w.replace(/^#+/u, '').toUpperCase());
const sameList = (a, b) => a.length === b.length && a.every((w, i) => w === b[i]);

/* The runs of words in a text that only whitespace separates. Any other
   character ends a run, and a hashtag's body is not a word here. */
function runs(text){
  const out = [];
  let run = [], word = '', tag = false;
  for (const ch of Array.from(text + '\n')){
    if (WORD.test(ch)){ word += ch; continue; }
    if (word && tag){ out.push(run); run = []; }
    else if (word) run.push(word);
    word = '';
    tag = ch === '#';
    if (!SPACE.test(ch)){ out.push(run); run = []; }
  }
  out.push(run);
  return out.filter((r) => r.length > 0);
}

function hashtags(text){
  const chars = Array.from(text);
  const out = [];
  let i = 0;
  while (i < chars.length){
    if (chars[i] === '#'){
      let j = i + 1;
      while (j < chars.length && WORD_OR_UNDERSCORE.test(chars[j])) j++;
      if (j > i + 1) out.push(chars.slice(i, j).join(''));
      i = j;
    } else i++;
  }
  return out;
}

function doubled(term){
  const chars = Array.from(term);
  if (term.startsWith('#') || term.includes(' ') || !LETTERS.test(term) || chars.length % 2 || chars.length < 2 * DOUBLED_HALF) return term;
  const half = chars.length / 2;
  const a = chars.slice(0, half).join(''), b = chars.slice(half).join('');
  return a === b ? a + ' ' + b : term;
}

/* A link or a profile path ("https://t.co/x", "u/thabo", "r/southafrica",
   "x.com/thabo"), not a word with a slash in it ("ac/dc", "24/7"). */
function isPath(word){
  const head = word.split('/')[0];
  return word.includes('/') && (word.includes('://') || ['u', 'r'].includes(head.toLowerCase()) || head.includes('.'));
}

const dropped = (term) => !key(term) || UNNAMED.test(term) || DROPPED.some((mark) => term.includes(mark))
  || term.split(' ').some(isPath);

function terms(label){
  const raw = label.split(SEPARATORS).map(squash).filter(Boolean);
  const kept = raw.filter((t) => !dropped(t)).map(doubled);
  const out = [];
  for (const term of kept){
    const mine = words(term);
    if (out.some((o) => key(o) === key(term) || sameList([...mine].sort(), [...words(o)].sort()))) continue;
    if (kept.some((o) => words(o).length > mine.length && mine.every((w) => words(o).includes(w)))) continue;
    out.push(term);
  }
  return {list: out, dropped: raw.length > kept.length};
}

function texts(card){
  const out = (Array.isArray(card.evidence) ? card.evidence : [])
    .filter((e) => e && typeof e === 'object' && typeof e.text === 'string').map((e) => e.text);
  if (card.explained === true && typeof card.explanation === 'string') out.push(card.explanation);
  return out.map(clean);
}

/* The form the most texts write the term in, exactly its letters and spaces
   in any casing, the first seen on a tie; null when no text holds it. */
function postForm(term, sources){
  const want = term.toUpperCase();
  const n = term.split(' ').length;
  const counts = new Map();
  for (const text of sources){
    const found = [];
    if (term.startsWith('#')) for (const f of hashtags(text)) { if (f.toUpperCase() === want) found.push(f); }
    else for (const run of runs(text)) for (let i = 0; i + n <= run.length; i++){
      const form = run.slice(i, i + n).join(' ');
      if (form.toUpperCase() === want) found.push(form);
    }
    for (const form of new Set(found)) counts.set(form, (counts.get(form) || 0) + 1);
  }
  let best = null;
  for (const [form, count] of counts) if (best === null || count > counts.get(best)) best = form;
  return best;
}

function capitalised(term){
  if (term.startsWith('#')) return term;
  return term.split(' ').map((w) => {
    const [first, ...rest] = Array.from(w);
    return isLower(first) ? first.toUpperCase() + rest.join('') : w;
  }).join(' ');
}

function cased(term, sources){
  if (term !== term.toLowerCase()) return term;
  const form = postForm(term, sources);
  if (form !== null && (form !== form.toLowerCase() || term.startsWith('#'))) return form;
  return capitalised(term);
}

function subject(card, label){
  if (!card || !KINDS.includes(card.kind) || typeof label !== 'string') return null;
  const tidy = squash(clean(label));
  if (!tidy || UNNAMED.test(tidy)) return null;
  const {list, dropped: some} = terms(tidy);
  if (list.length === 0) return some ? FALLBACK : null;
  const sources = texts(card);
  const names = list.map((t) => cased(t, sources));
  return names.length === 1 ? names[0] : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
}

/* The plain title for a topic or hashtag card from its label, or null when the card keeps its label. */
export function codeTitle(card, label = card && card.title){
  const said = subject(card, label);
  return said ? 'Posts about ' + said : null;
}

/* A merged card's count line ends "(<label> only)"; under a code title it
   names the card the same way. coded is the code title the card shows, if
   any (TrendCard.jsx codedTitle). */
export function codeCountLine(card, label, line, coded){
  const said = coded ? subject(card, label) : null;
  const end = typeof label === 'string' ? ' (' + label + ' only)' : null;
  return said && typeof line === 'string' && end && line.endsWith(end) ? line.slice(0, -end.length) + ' (' + said + ' only)' : line;
}
