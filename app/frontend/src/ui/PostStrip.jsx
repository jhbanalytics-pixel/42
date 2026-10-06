/* 42 Ask · PostStrip: up to eight of the cited posts. A post with a still is a
   9:16 card with duration, creator, date, views and the transcript line with
   its timestamp. A post with no usable still is never an empty frame: it is a
   text row with the same facts, its creator the link to the post. Stills
   only; nothing plays on its own. */
import {useState} from 'react';
import {platformLabel} from './PlatformGlyph.jsx';
import {clock, postAuthorLabel, postDay, postName, sourceMarketLabel, viewsLine} from './EvidenceChip.jsx';
import {safeUrl} from '../safeUrl.js';
import {Facts} from './Facts.jsx';
import '../styles/poststrip42.css';

const MAX_POSTS = 8;

export function PostStrip({evidence}){
  const [failedThumbnails, setFailedThumbnails] = useState(() => new Set());
  const posts = (Array.isArray(evidence) ? evidence : [])
    .filter((record) => record && typeof record === 'object')
    .slice()
    .sort((a, b) => Number(Boolean(safeUrl(b.thumbnail_url))) - Number(Boolean(safeUrl(a.thumbnail_url))))
    .slice(0, MAX_POSTS);
  if (!posts.length) return null;
  const stillOf = (post) => {
    const thumbnail = safeUrl(post.thumbnail_url);
    return thumbnail && !failedThumbnails.has(thumbnail) ? thumbnail : null;
  };
  const withStill = posts.filter((post) => stillOf(post));
  const withoutStill = posts.filter((post) => !stillOf(post));
  const facts = (post) => [postDay(post.posted_at), viewsLine(post), sourceMarketLabel(post)];
  const line = (post) => {
    const span = post.transcript_span && typeof post.transcript_span === 'object' ? post.transcript_span : null;
    return span && span.text
      ? <span className="ask42-post-line"><span className="ask42-muted">{clock(span.start_s)}</span> {span.text}</span>
      : null;
  };
  return (
    <section className="ask42-section" aria-labelledby="ask42-posts-title">
      <h3 className="ask42-section-title" id="ask42-posts-title">The posts</h3>
      {/* One list: stills first as cards, then posts without a still as text
          rows that span the full width. */}
      <ul className="ask42-posts" aria-label="Posts">
        {withStill.map((post) => {
          const duration = clock(post.duration_s);
          const author = postAuthorLabel(post, 'YouTube channel');
          const url = safeUrl(post.url);
          const thumbnail = stillOf(post);
          const still = (
            <>
              <img src={thumbnail} alt="" loading="lazy"
                onError={() => setFailedThumbnails((failed) => failed.has(thumbnail) ? failed : new Set(failed).add(thumbnail))} />
              {duration && <span className="ask42-post-duration">{duration}</span>}
            </>
          );
          return (
            <li className="ask42-post" key={post.id}>
              {url
                ? <a className="ask42-post-frame" href={url} target="_blank" rel="noopener noreferrer" aria-label={'Open the ' + postName(post)}>{still}</a>
                : <span className="ask42-post-frame">{still}</span>}
              <span className="ask42-post-creator">{author}</span>
              <span className="ask42-muted"><Facts parts={facts(post)} /></span>
              {line(post)}
            </li>
          );
        })}
        {withoutStill.map((post) => {
          const duration = clock(post.duration_s);
          const platform = platformLabel(post.platform) || String(post.platform || '') || 'Post';
          const author = postAuthorLabel(post, 'YouTube channel');
          const url = safeUrl(post.url);
          return (
            <li className="ask42-post ask42-post-row" key={post.id} data-post-row="">
              {/* No frame is drawn; the missing still is said to assistive tech only. */}
              <span className="ask42-post-blank" role="img" aria-label={'Still unavailable for ' + postName(post)} />
              {url
                ? <a className="ask42-post-open" href={url} target="_blank" rel="noopener noreferrer" aria-label={'Open the ' + postName(post)}>
                    <span className="ask42-post-creator">{author}</span>
                  </a>
                : <span className="ask42-post-creator">{author}</span>}
              <span className="ask42-muted"><Facts parts={[platform, ...facts(post), duration]} /></span>
              {line(post)}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
