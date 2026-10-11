/* 42 Ask · the briefing parts: a finding card with its real posts as tiles,
   what commenters said under the post they belong to, the number tiles and
   the platform marks. A card names the posts its claim rests on and nothing
   else; a comment is never a tile (askEvidence.js). */
import {Fragment, useState} from 'react';
import {readerFigure} from '../api.js';
import {safeUrl} from '../safeUrl.js';
import {isComment, parentShown, parentWords} from '../askEvidence.js';
import {EvidenceChip, clock, postDay, sourceMarketLabel, viewsLine} from './EvidenceChip.jsx';
import {PlatformLogo} from './PlatformLogo.jsx';
import {platformLabel} from './PlatformGlyph.jsx';
import '../styles/askbrief42.css';

const TILES_SHOWN = 4;

function oneLine(text){
  return String(text || '').replace(/\s+/g, ' ').trim();
}

function PostTile({post, quote, pinned, onPin}){
  const [stillFailed, setStillFailed] = useState(false);
  const url = safeUrl(post.url);
  const still = safeUrl(post.thumbnail_url);
  const duration = clock(post.duration_s);
  const facts = [postDay(post.posted_at), viewsLine(post), duration, sourceMarketLabel(post)].filter(Boolean);
  /* One line: the words of the post the claim rests on, or what was said at a
     moment of the video, with its time. */
  const span = post.transcript_span && typeof post.transcript_span === 'object' && post.transcript_span.text ? post.transcript_span : null;
  const line = oneLine((span && span.text) || quote || post.text);
  return (
    <li className="ask42-tile">
      {still && !stillFailed && <img className="ask42-tile-still" src={still} alt="" loading="lazy" onError={() => setStillFailed(true)} />}
      <span className="ask42-tile-body">
        <span className="ask42-tile-head">
          <PlatformLogo platform={post.platform} size={16} />
          <span className="ask42-tile-platform">{platformLabel(post.platform) || String(post.platform || '')}</span>
          <EvidenceChip evidence={post} quotes={quote ? [quote] : []} pinned={pinned} onPin={onPin} glyph={false} />
        </span>
        {facts.length > 0 && <span className="ask42-muted ask42-tile-facts">{facts.join(' · ')}</span>}
        <span className="ask42-tile-line" title={line}>{span ? <span className="ask42-muted">{clock(span.start_s) + ' '}</span> : null}{line}</span>
        {url && <a className="ask42-tile-open" href={url} target="_blank" rel="noopener noreferrer" aria-label={'Open post by ' + String(post.handle || 'this account')}>Open post</a>}
      </span>
    </li>
  );
}

/* Comments belong to a post but are not it: a quiet row under the posts,
   one group per post they sit under. */
function Commenters({comments, quotes, pinnedId, onPin}){
  const groups = new Map();
  for (const comment of comments){
    const key = comment.url || '';
    groups.set(key, [...(groups.get(key) || []), comment]);
  }
  return (
    <div className="ask42-commenters">
      <h4 className="ask42-commenters-title">What commenters said</h4>
      {[...groups].map(([url, items]) => {
        const whose = parentWords(items[0]);
        const link = parentShown(items[0]) ? safeUrl(url) : null;
        return (
          <div className="ask42-commenters-group" key={url}>
            <p className="ask42-muted ask42-commenters-on">{'On '}{link ? <a href={link} target="_blank" rel="noopener noreferrer">{whose}</a> : whose}</p>
            <ul className="ask42-comments">
              {items.map((comment) => (
                <li key={comment.id}>
                  <span className="ask42-comment-text">{oneLine((quotes.get(comment.id) || [])[0] || comment.text)}</span>
                  <EvidenceChip evidence={comment} quotes={quotes.get(comment.id) || []} pinned={pinnedId === comment.id} onPin={onPin} glyph={false} />
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

export function FindingCard({claim, records, quoteMap, confidence, pinnedId, onPin}){
  const [all, setAll] = useState(false);
  const cited = (claim.evidence_ids || []).map((id) => records.get(id)).filter(Boolean);
  const posts = cited.filter((item) => !isComment(item));
  const comments = cited.filter((item) => isComment(item));
  const shown = all ? posts : posts.slice(0, TILES_SHOWN);
  const hidden = posts.length - shown.length;
  const quoteOf = (id) => (quoteMap.get(id) || [])[0];
  return (
    <li className="ask42-finding ask42-claim">
      {confidence && (
        <span className="ask42-confidence" data-label={claim.label}>
          <span className="ask42-confidence-shape" aria-hidden="true">{confidence.shape}</span>
          {confidence.word}
        </span>
      )}
      <p className="ask42-claim-text">{claim.text}</p>
      {posts.length > 0 && (
        <ul className="ask42-tiles" aria-label="Posts behind this finding">
          {shown.map((post) => <PostTile key={post.id} post={post} quote={quoteOf(post.id)} pinned={pinnedId === post.id} onPin={onPin} />)}
        </ul>
      )}
      {posts.length > TILES_SHOWN && (
        <button type="button" className="ask42-quiet ask42-tiles-more" aria-expanded={all ? 'true' : 'false'} aria-label={all ? 'Fewer posts' : 'and ' + hidden + ' more ' + (hidden === 1 ? 'post' : 'posts')} onClick={() => setAll((value) => !value)}>
          {all ? 'Fewer' : 'and ' + hidden + ' more'}
        </button>
      )}
      {comments.length > 0 && <Commenters comments={comments} quotes={quoteMap} pinnedId={pinnedId} onPin={onPin} />}
    </li>
  );
}

/* The claim a tile counts, whole, with the style holding it to three lines. A
   claim long enough to run past them has a toggle, which a touch or a keyboard
   reaches, and which opens the rest in place. */
const CLAIM_SHORT = 110;
function FigureClaim({text, long}){
  const [open, setOpen] = useState(false);
  return (
    <>
      <p className="ask42-figure-claim" data-open={open ? '' : undefined}>{text}</p>
      {long && <button type="button" className="ask42-quiet ask42-figure-more" aria-expanded={open ? 'true' : 'false'} onClick={() => setOpen((value) => !value)}>{open ? 'Show less' : 'Show all'}</button>}
    </>
  );
}

/* Each tile says what it counts: the figures in one line and the claim they
   belong to. Its name is the whole sentence. */
export function FigureTiles({tiles}){
  if (!tiles.length) return null;
  return (
    <section className="ask42-section" aria-labelledby="ask42-figures-title">
      <h3 className="ask42-section-title" id="ask42-figures-title">The numbers</h3>
      <p className="ask42-muted ask42-figure-caption">Counted in the store, not only in the posts this answer cites</p>
      <ul className="ask42-figure-tiles">
        {tiles.map((tile) => (
          <li className="ask42-figure-tile" key={tile.key} aria-label={tile.sentence} data-query-id={tile.queryId || undefined}>
            <p className="ask42-figure-line">
              {tile.parts.map((part, index) => (
                <Fragment key={index}>
                  {index > 0 && <span className="ask42-figure-sep" aria-hidden="true">{' · '}</span>}
                  <span className="ask42-figure-count"><b className="ask42-figure-value">{part.value}</b>{' '}<span className="ask42-figure-unit">{part.unit}</span></span>
                </Fragment>
              ))}
            </p>
            <FigureClaim text={tile.about} long={tile.full.length > CLAIM_SHORT} />
          </li>
        ))}
      </ul>
    </section>
  );
}

export function PlatformMarks({rows, notes, caption}){
  if (!rows.length && !notes.length) return null;
  const most = Math.max(...rows.map((row) => row.posts), 1);
  return (
    <section className="ask42-section" aria-labelledby="ask42-platforms-title" data-ask-source-chart="">
      <h3 className="ask42-section-title" id="ask42-platforms-title">Posts read by platform</h3>
      {caption && <p className="ask42-muted ask42-platform-caption">{caption}</p>}
      {rows.length > 0 && (
        <ul className="ask42-platforms">
          {rows.map((row) => (
            <li className="ask42-platform-row" key={row.platform} style={{'--share': row.posts / most}}>
              <PlatformLogo platform={row.platform} size={18} />
              <span className="ask42-platform-name">{platformLabel(row.platform) || row.platform}</span>{' '}
              <span className="ask42-platform-count">{readerFigure(row.posts)}</span>
              <span className="ask42-platform-bar" aria-hidden="true" />
            </li>
          ))}
        </ul>
      )}
      {notes.length > 0 && <p className="ask42-muted ask42-platform-notes">{notes.join(' · ')}</p>}
    </section>
  );
}
