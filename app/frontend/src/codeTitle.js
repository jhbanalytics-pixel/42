/* Albert, 12 October 2026, "code titles": a topic or hashtag card whose
   written title did not pass its checks shows a plain title built here from
   its cluster label, "Posts about Bafana Bafana, Mofokeng and Relebohile",
   not "bafanabafana, mofokeng, relebohile". The rule is
   core/api/code_title.py's, line for line, and one list of cases pins both
   (core/api/tests/fixtures/code_titles.json). It never adds a word the label
   does not hold and never says why. It reads the card as the API returned
   it, after the privacy projection, so a masked handle or name leaves the
   label as it is. */
const KINDS = ['topic', 'hashtag'];
const UNNAMED = /^Topic [0-9a-f]{8}$/i;
const MASKED = ['@', '*', '[link]', '://'];
const ALNUM = /[\p{L}\p{N}]+/gu;
const RUN = /[\p{L}\p{N}]+(?:\s+[\p{L}\p{N}]+)*/gu;
const HASHTAG = /#([\p{L}\p{N}_]+)/gu;
const WINDOW = 6;

const squash = (text) => text.split(/\s+/u).filter(Boolean).join(' ');
const key = (text) => (text.match(ALNUM) || []).join('').toLowerCase();
const words = (term) => new Set(term.split(' ').map((w) => w.replace(/^#+/u, '').toLowerCase()));
const within = (a, b) => a.size < b.size && [...a].every((w) => b.has(w));

function terms(label){
  const all = label.split(',').map(squash).filter(Boolean);
  if (all.length === 0 || all.some((t) => !key(t))) return null;
  return all.filter((term, i) => !all.slice(0, i).some((other) => key(other) === key(term))
    && !all.some((other) => within(words(term), words(other))));
}

function texts(card){
  const out = (Array.isArray(card.evidence) ? card.evidence : [])
    .filter((e) => e && typeof e === 'object' && typeof e.text === 'string').map((e) => e.text);
  if (card.explained === true && typeof card.explanation === 'string') out.push(card.explanation);
  return out;
}

/* A hashtag's most used form in the texts, lower case included, so one
   post's casing never outvotes the rest; a word's most used cased form of
   its letters, spaced forms first. Null when the texts give none. */
function postForm(term, sources){
  const forms = [];
  if (term.startsWith('#')){
    const body = term.slice(1).toLowerCase();
    for (const t of sources) for (const m of t.matchAll(HASHTAG)) if (m[1].toLowerCase() === body) forms.push('#' + m[1]);
  } else {
    const want = key(term);
    for (const t of sources){
      for (const run of t.match(RUN) || []){
        const tokens = run.split(/\s+/u);
        for (let i = 0; i < tokens.length; i++){
          for (let j = i + 1; j <= Math.min(i + WINDOW, tokens.length); j++){
            if (tokens.slice(i, j).join('').toLowerCase() === want) forms.push(tokens.slice(i, j).join(' '));
          }
        }
      }
    }
  }
  const counts = new Map();
  for (const form of forms) if (term.startsWith('#') || form !== form.toLowerCase()) counts.set(form, (counts.get(form) || 0) + 1);
  const order = [...counts.keys()];
  if (order.length === 0) return null;
  const rank = (f) => [-f.split(' ').length, -counts.get(f), order.indexOf(f)];
  return order.reduce((best, f) => {
    const a = rank(f), b = rank(best);
    return a[0] < b[0] || (a[0] === b[0] && (a[1] < b[1] || (a[1] === b[1] && a[2] < b[2]))) ? f : best;
  });
}

const isLower = (ch) => ch === ch.toLowerCase() && ch !== ch.toUpperCase();

function plain(term){
  const chars = Array.from(term);
  if (!term.startsWith('#') && !term.includes(' ') && /^\p{L}+$/u.test(term) && chars.length >= 6 && chars.length % 2 === 0){
    const half = chars.length / 2;
    if (chars.slice(0, half).join('') === chars.slice(half).join('')) term = chars.slice(0, half).join('') + ' ' + chars.slice(half).join('');
  }
  if (term.startsWith('#')) return term;
  return term.split(' ').map((w) => {
    const [first, ...rest] = Array.from(w);
    return isLower(first) ? first.toUpperCase() + rest.join('') : w;
  }).join(' ');
}

const cased = (term, sources) => (term !== term.toLowerCase() ? term : postForm(term, sources) || plain(term));

function subject(card, label){
  if (!card || !KINDS.includes(card.kind) || typeof label !== 'string') return null;
  const tidy = squash(label);
  if (!tidy || UNNAMED.test(tidy) || MASKED.some((mark) => tidy.includes(mark))) return null;
  const list = terms(tidy);
  if (!list || list.length === 0) return null;
  const sources = texts(card);
  const names = list.map((t) => cased(t, sources));
  return names.length === 1 ? names[0] : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
}

/* The plain title for a topic or hashtag card from its label, or null when the card keeps its label. */
export function codeTitle(card, label = card && card.title){
  const words = subject(card, label);
  return words ? 'Posts about ' + words : null;
}

/* A merged card's count line ends "(<label> only)"; under a code title it
   names the card the same way. coded is the code title the card shows, if
   any (TrendCard.jsx codedTitle). */
export function codeCountLine(card, label, line, coded){
  const words = coded ? subject(card, label) : null;
  const end = typeof label === 'string' ? ' (' + label + ' only)' : null;
  return words && typeof line === 'string' && end && line.endsWith(end) ? line.slice(0, -end.length) + ' (' + words + ' only)' : line;
}
