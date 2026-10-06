/* 42 Ask · EvidenceChip: one cited post under a claim, as platform mark and
   handle. Hover or focus previews the post (thumbnail, the quoted words
   highlighted, date and views); click pins it in the source panel. The
   formatters below are shared by SourcePanel and PostStrip. */
import {useId, useState} from 'react';
import {readerFigure} from '../api.js';
import {PlatformGlyph, platformLabel} from './PlatformGlyph.jsx';
import {safeUrl} from '../safeUrl.js';

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const SOURCE_MARKETS = new Map([['ZA', 'South Africa'], ['NG', 'Nigeria'], ['KE', 'Kenya']]);

export function sourceMarketLabel(evidence){
  if (!evidence || !Array.isArray(evidence.flags) || !evidence.flags.includes('market_assumed')) return '';
  const market = SOURCE_MARKETS.get(evidence.source_market);
  return market ? `Market assumed: ${market}` : '';
}

/* The day a post went up, as the poster's own calendar has it: the date part
   of the timestamp, never shifted into the reader's zone. */
export function postDay(value){
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  if (!match) return '';
  return Number(match[3]) + ' ' + MONTHS[Number(match[2]) - 1] + ' ' + match[1];
}

export function monthName(index){
  return MONTHS[index] || '';
}

export function clock(seconds){
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '';
  const whole = Math.floor(seconds);
  return Math.floor(whole / 60) + ':' + String(whole % 60).padStart(2, '0');
}

export function viewsLine(evidence){
  const views = evidence && evidence.engagement && evidence.engagement.views;
  if (typeof views !== 'number' || !Number.isFinite(views)) return '';
  return readerFigure(views) + (views === 1 ? ' view' : ' views');
}

const fold = (text) => String(text || '').replace(/[‘’]/g, "'").replace(/[“”]/g, '"').toLowerCase();

/* The post's words with each quoted span marked. A quote that is not in the
   text marks nothing; the contract check has already refused such an answer. */
export function Highlighted({text, quotes}){
  const source = String(text || '');
  const folded = fold(source);
  const spans = [];
  for (const quote of quotes || []){
    const needle = fold(quote).trim();
    if (!needle) continue;
    const at = folded.indexOf(needle);
    if (at !== -1) spans.push([at, at + needle.length]);
  }
  spans.sort((a, b) => a[0] - b[0]);
  const parts = [];
  let cursor = 0;
  spans.forEach(([start, end], index) => {
    if (start < cursor) return;
    if (start > cursor) parts.push(source.slice(cursor, start));
    parts.push(<mark key={index}>{source.slice(start, end)}</mark>);
    cursor = end;
  });
  if (cursor < source.length) parts.push(source.slice(cursor));
  return <>{parts}</>;
}

/* "TikTok post by @handle", or "TikTok post" when the author is not named
   (a skin report strips an unnamed author's handle; contract section 15.1). */
const YOUTUBE_CHANNEL_ID = /^UC[A-Za-z0-9_-]{22}$/i;

export function postAuthorLabel(evidence, fallback = ''){
  const handle = typeof evidence.handle === 'string' && evidence.handle.trim() ? evidence.handle : '';
  return evidence.platform === 'youtube' && YOUTUBE_CHANNEL_ID.test(handle.trim().replace(/^@+/, '')) ? fallback : handle;
}

export function postName(evidence){
  const platform = platformLabel(evidence.platform) || String(evidence.platform || '');
  const handle = postAuthorLabel(evidence);
  return (platform ? platform + ' post' : 'Post') + (handle ? ' by ' + handle : '');
}

export function EvidenceChip({evidence, quotes, pinned, onPin}){
  const [open, setOpen] = useState(false);
  const popoverId = useId();
  if (!evidence) return null;
  const platform = platformLabel(evidence.platform) || String(evidence.platform || '');
  const author = postAuthorLabel(evidence, 'YouTube channel') || 'Post';
  const day = postDay(evidence.posted_at);
  const views = viewsLine(evidence);
  const sourceMarket = sourceMarketLabel(evidence);
  const thumbnail = safeUrl(evidence.thumbnail_url);
  return (
    <span className="ask42-chip-wrap" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button
        type="button"
        className="ask42-chip"
        aria-pressed={pinned ? 'true' : 'false'}
        aria-describedby={open ? popoverId : undefined}
        aria-label={'Source: ' + postName(evidence) + (day ? ', ' + day : '') + (sourceMarket ? ', ' + sourceMarket : '')}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(event) => { if (event.key === 'Escape') setOpen(false); }}
        onClick={() => onPin && onPin(evidence.id)}
      >
        <PlatformGlyph platform={evidence.platform} />
        <span className="ask42-chip-handle">{author}</span>
      </button>
      {open && (
        <span className="ask42-popover" role="tooltip" id={popoverId}>
          {thumbnail && <img className="ask42-popover-thumb" src={thumbnail} alt="" loading="lazy" />}
          <span className="ask42-popover-body">
            <span className="ask42-popover-text"><Highlighted text={evidence.text} quotes={quotes} /></span>
            <span className="ask42-popover-meta">{[platform, day, views, sourceMarket].filter(Boolean).join(' · ')}</span>
          </span>
        </span>
      )}
    </span>
  );
}
