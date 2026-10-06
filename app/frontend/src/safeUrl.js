/* Evidence links and thumbnails come from collected posts, so they are used
   only as plain http or https addresses with no user name or password in
   them. Anything else (javascript:, data:, a relative path) returns null and
   the caller shows text or the neutral placeholder instead. */
export function safeUrl(value){
  if (typeof value !== 'string' || !value.trim()) return null;
  let parsed;
  try { parsed = new URL(value.trim()); } catch (_error){ return null; }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  if (parsed.username || parsed.password) return null;
  return parsed.href;
}
