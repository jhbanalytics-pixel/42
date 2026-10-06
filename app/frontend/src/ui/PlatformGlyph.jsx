/* PULSE ui · PlatformGlyph: a small, consistent label chip per platform.
   Today.jsx and topic.jsx currently render the raw platform string inline with
   no shared naming; this consolidates the display names in one place so every
   surface reads the same. The key is normalised so "TikTok", "tiktok", and
   "tik_tok" all resolve. Unknown platforms fall back to the raw label. */

const PLATFORMS = {
  tiktok:        {label: 'TikTok'},
  instagram:     {label: 'Instagram'},
  youtube:       {label: 'YouTube'},
  threads:       {label: 'Threads'},
  reddit:        {label: 'Reddit'},
  news:          {label: 'News'},
  web:           {label: 'Web'},
  x:             {label: 'X'},
  google_search: {label: 'Google Search'},
  aggregate:     {label: 'Aggregate'},
  facebook:      {label: 'Facebook'},
  bluesky:       {label: 'Bluesky'},
};

function normalise(platform){
  const p = String(platform || '').toLowerCase().trim().replace(/[\s-]+/g, '_');
  if (PLATFORMS[p]) return p;
  if (p.includes('tiktok') || p === 'tik_tok') return 'tiktok';
  if (p.includes('insta')) return 'instagram';
  if (p.includes('youtube') || p === 'yt') return 'youtube';
  if (p.includes('thread')) return 'threads';
  if (p.includes('reddit')) return 'reddit';
  if (p.includes('twitter') || p === 'x') return 'x';
  if (p.includes('google') || p.includes('search')) return 'google_search';
  if (p.includes('facebook') || p === 'fb') return 'facebook';
  if (p.includes('bluesky') || p === 'bsky') return 'bluesky';
  if (p.includes('news')) return 'news';
  return '';
}

/* The display name on its own, for a surface that needs the words without the
   chip. A platform this table does not know returns nothing, so a caller can
   say what it wants to print in that case rather than take a guess from here. */
export function platformLabel(platform){
  const key = normalise(platform);
  return key ? PLATFORMS[key].label : '';
}

export function PlatformGlyph({platform}){
  const key = normalise(platform);
  const label = platformLabel(platform) || String(platform || '');
  return <span className="ui-platform-glyph" data-platform={key || 'other'}>{label}</span>;
}
