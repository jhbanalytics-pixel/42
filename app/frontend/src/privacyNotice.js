/* What the pages say when 42 has taken people out of a copy (C5 v2 sections
   5.4, 5.7 and 6). The server projects a record or a dossier version and sets
   a privacy marker beside the content. The pages read the marker only when it
   has exactly the shape the server writes, and say their own words for it:
   nothing from the marker is shown as text, so a marker cannot put words on
   the page.

   UNAVAILABLE_NOTE is the server's note (core/api/privacy.py) word for word.
   The other two are the browser's: one for content left out, one for a
   request the server refused because it could not read its hidden-people
   list. */

export const APPLIED_NOTICE = 'Some content was left out of this answer because it concerned people 42 no longer shows.';
export const UNAVAILABLE_NOTE = '42 could not check its hidden-people list just now, so the posts behind this answer are not shown.';
export const PEOPLE_UNAVAILABLE_WORDS = '42 could not check its hidden-people list just now, so this could not be done. Try again in a moment.';

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const count = (value) => Number.isInteger(value) && value >= 0;

/* The notice for a record or dossier version, or an empty string when it
   carries no marker or one that is not the server's. */
export function privacyNotice(subject){
  const marker = isObject(subject) ? subject.privacy : undefined;
  if (!isObject(marker) || marker.v !== 1) return '';
  const keys = Object.keys(marker);
  const allowed = marker.state === 'unavailable' ? ['v', 'state', 'withheld', 'note'] : ['v', 'state', 'withheld'];
  if (!keys.includes('withheld') || keys.some((key) => !allowed.includes(key))) return '';
  if (Object.hasOwn(marker, 'note') && typeof marker.note !== 'string') return '';
  const withheld = marker.withheld;
  if (!isObject(withheld) || Object.keys(withheld).length !== 3) return '';
  if (!count(withheld.posts) || !count(withheld.claims) || typeof withheld.summary !== 'boolean') return '';
  if (marker.state === 'applied') return APPLIED_NOTICE;
  if (marker.state === 'unavailable') return UNAVAILABLE_NOTE;
  return '';
}

/* The words for a failed request: the plain sentence when the server refused
   because it could not read its hidden-people list, else the server's own
   words, else the fallback. */
export function peopleWords(error, fallback){
  if (error && error.code === 'people_unavailable') return PEOPLE_UNAVAILABLE_WORDS;
  return (error && error.message) || fallback;
}
