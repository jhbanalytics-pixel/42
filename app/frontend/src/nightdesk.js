/* Night Desk helpers (styles/nightdesk.css). Pure text splits the layer
   needs so a poster or an answer can mark its key words. */

/* The Today poster names one trend. Its name is marked so the eye finds it
   first; when the name is not in the sentence the sentence stays plain. */
export function headlineParts(text, term){
  const sentence = String(text || '');
  const name = String(term || '').trim();
  const at = name ? sentence.toLowerCase().indexOf(name.toLowerCase()) : -1;
  if (at < 0) return {before: sentence, term: '', after: ''};
  return {before: sentence.slice(0, at), term: sentence.slice(at, at + name.length), after: sentence.slice(at + name.length)};
}

/* An answer leads with its first sentence. A full stop ends a sentence only
   when a space and a capital or a digit follow, so "2.5" and "S.A." stay whole. */
export function leadSentence(text){
  const answer = String(text || '');
  const end = /[.!?](?=\s+["“(]?[A-Z0-9#@])/g;
  let match;
  while ((match = end.exec(answer))){
    const before = answer.slice(0, match.index);
    if (/\b[A-Z]$/.test(before) && /^\s+[A-Z]\./.test(answer.slice(match.index + 1))) continue;
    if (/(?:^|\s)(?:[A-Z]\.)+[A-Z]$/.test(before)) continue;
    return {lead: answer.slice(0, match.index + 1), rest: answer.slice(match.index + 1)};
  }
  return {lead: answer, rest: ''};
}
