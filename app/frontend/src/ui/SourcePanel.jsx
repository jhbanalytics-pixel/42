/* 42 Ask · SourcePanel: the pinned source beside the answer, so the answer
   stays in view while one post is read in full. Focus moves to the panel
   when a source is pinned and back to the answer when it closes, by Close or
   Escape. */
import {useEffect, useRef, useState} from 'react';
import {Highlighted, clock, postDay, postName, sourceMarketLabel, viewsLine} from './EvidenceChip.jsx';
import {safeUrl} from '../safeUrl.js';

export function SourcePanel({evidence, quotes, onClose}){
  const heading = useRef(null);
  const panel = useRef(null);
  const opener = useRef(null);
  const [failedThumbnails, setFailedThumbnails] = useState(() => new Set());
  const id = evidence ? evidence.id : null;
  useEffect(() => {
    if (id){
      const active = typeof document !== 'undefined' ? document.activeElement : null;
      if (active && active !== document.body && !(panel.current && panel.current.contains(active))) opener.current = active;
      if (heading.current) heading.current.focus();
      return;
    }
    const back = opener.current;
    opener.current = null;
    if (back && back.isConnected && typeof back.focus === 'function') back.focus();
  }, [id]);
  /* Nothing is shown until a source is picked, so the answer keeps the full
     width; the layout opens its second column only while this panel is in it. */
  if (!evidence) return null;
  const span = evidence.transcript_span && typeof evidence.transcript_span === 'object' ? evidence.transcript_span : null;
  const thumbnail = safeUrl(evidence.thumbnail_url);
  const showThumbnail = thumbnail && !failedThumbnails.has(thumbnail);
  const url = safeUrl(evidence.url);
  return (
    <aside className="ask42-source" aria-label="Source" ref={panel}
      onKeyDown={(event) => { if (event.key === 'Escape' && onClose){ event.stopPropagation(); onClose(); } }}>
      <h3 className="ask42-source-title" tabIndex={-1} ref={heading}>{postName(evidence)}</h3>
      {thumbnail && (showThumbnail
        ? <img className="ask42-source-thumb" src={thumbnail} alt={'Still from the ' + postName(evidence)} loading="lazy"
            onError={() => setFailedThumbnails((failed) => failed.has(thumbnail) ? failed : new Set(failed).add(thumbnail))} />
        : <div className="ask42-source-thumb ask42-source-thumb-empty" role="img" aria-label={'Still unavailable for ' + postName(evidence)}>Still unavailable</div>)}
      <p className="ask42-source-text"><Highlighted text={evidence.text} quotes={quotes} /></p>
      {span && span.text && <p className="ask42-source-span"><span className="ask42-muted">{clock(span.start_s)}</span> {span.text}</p>}
      <p className="ask42-muted">{[postDay(evidence.posted_at), viewsLine(evidence), sourceMarketLabel(evidence)].filter(Boolean).join(' · ')}</p>
      <p className="ask42-source-actions">
        {url
          ? <a href={url} target="_blank" rel="noopener noreferrer">Open the post</a>
          : <span className="ask42-muted">No link to this post</span>}
        <button type="button" className="ask42-quiet" onClick={onClose}>Close</button>
      </p>
    </aside>
  );
}
