import {platformWord} from './TrendCard.jsx';
import '../styles/platform-logo.css';

/* Platform marks, vendored as inline paths: no request, no package, no font.
   Each is a simplified one-colour mark on a 24 by 24 grid that takes the text
   colour, so it follows the theme. They are decorative: the platform's name
   always sits beside them as text, so they are hidden from assistive
   technology. A platform with no mark here reads as a text monogram. */

/* Strokes use the .pl-logo stroke rules; parts marked fill are solid. */
const MARKS = {
  spotify: (
    <>
      <circle cx="12" cy="12" r="9.6" />
      <path d="M6.6 9.3c3.7-1.1 7.7-.8 11 1.1" />
      <path d="M7.4 12.7c3-.9 6.2-.6 8.9 1" />
      <path d="M8.2 15.9c2.4-.6 4.7-.4 6.6.8" />
    </>
  ),
  apple_music: (
    <>
      <path d="M10 6.1 19.2 4v11.4" />
      <path d="M10 6.1V17" />
      <ellipse className="pl-fill" cx="7.7" cy="17.4" rx="2.7" ry="2.2" />
      <ellipse className="pl-fill" cx="16.9" cy="15.6" rx="2.7" ry="2.2" />
    </>
  ),
  shazam: (
    <>
      <circle cx="12" cy="12" r="9.6" />
      <path d="M15.2 8.9C13.8 7.5 10.7 7.7 9.8 9.7c-.8 1.9 1 2.6 2.3 2.4 1.5-.2 3.3.6 2.5 2.6-.9 2-4.1 2.2-5.5.8" />
    </>
  ),
  app_store: (
    <>
      <rect x="2.6" y="2.6" width="18.8" height="18.8" rx="5" />
      <path d="M8 17.2 12 7.4l4 9.8" />
      <path d="M8.9 14.4h6.2" />
    </>
  ),
  tiktok: (
    <>
      <path d="M14.1 3.8v10.5a3.7 3.7 0 1 1-3.7-3.7" />
      <path d="M14.1 3.8c.3 2.5 1.9 4.2 4.7 4.5" />
    </>
  ),
  youtube: (
    <path className="pl-fill" fillRule="evenodd" d="M21.6 7.3a2.5 2.5 0 0 0-1.8-1.8C18.2 5.1 12 5.1 12 5.1s-6.2 0-7.8.4A2.5 2.5 0 0 0 2.4 7.3C2 8.9 2 12 2 12s0 3.1.4 4.7a2.5 2.5 0 0 0 1.8 1.8c1.6.4 7.8.4 7.8.4s6.2 0 7.8-.4a2.5 2.5 0 0 0 1.8-1.8c.4-1.6.4-4.7.4-4.7s0-3.1-.4-4.7ZM10 15.2V8.8l5.6 3.2Z" />
  ),
  boomplay: (
    <>
      <circle cx="12" cy="12" r="9.6" />
      <path className="pl-fill" d="M10 8.2v7.6l6.2-3.8Z" />
    </>
  ),
  google: (
    <>
      <path d="M20.2 10.9H12.4" />
      <path d="M18.8 7.1A8.3 8.3 0 1 0 20.3 12.7" />
    </>
  ),
};

export const LOGO_KEYS = Object.keys(MARKS);

/* A platform as the stable id the rest of the page uses: lower case words
   joined by underscores, and the X board named for its current name. */
export function platformId(platform){
  const id = String(platform ?? '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return id === 'twitter' ? 'x' : id;
}

/* Exact spellings a producer may use for a platform with a mark, then the
   families whose variants ("app_store_iphone", "tiktok_hashtag") share it. */
const ALIASES = {
  applemusic: 'apple_music',
  kworb_spotify: 'spotify',
  appstore: 'app_store',
  apple_app_store: 'app_store',
  ios_app_store: 'app_store',
  google_trends: 'google',
  google_trending: 'google',
  google_search: 'google',
};
const FAMILIES = ['apple_music', 'spotify', 'shazam', 'app_store', 'tiktok', 'youtube', 'boomplay'];

export function logoKey(platform){
  const id = platformId(platform);
  if (ALIASES[id]) return ALIASES[id];
  if (Object.prototype.hasOwnProperty.call(MARKS, id)) return id;
  return FAMILIES.find((family) => id.startsWith(family + '_')) || null;
}

/* The mark for a platform, or its first letter in a ring when there is none.
   Decorative either way: pair it with the platform's name as text. */
export function PlatformLogo({platform, size = 20, className = ''}){
  const key = logoKey(platform);
  const classes = (key ? 'pl-logo' : 'pl-monogram') + (className ? ' ' + className : '');
  if (!key){
    const word = platformWord(platform).trim();
    return <span className={classes} aria-hidden="true" style={{width: size, height: size, fontSize: Math.round(size * 0.55)}}>{word ? word.charAt(0).toUpperCase() : '?'}</span>;
  }
  return (
    <svg className={classes} data-logo={key} viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" focusable="false">
      {MARKS[key]}
    </svg>
  );
}
