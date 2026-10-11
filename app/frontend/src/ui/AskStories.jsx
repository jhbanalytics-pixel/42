/* Ask · the ranked briefing: one block per story, in rank order. A block has its name, its own figures with their
   units, the lines the answer holds for it (what it is, when it peaked or whether it is still rising,
   why it is moving), one or two of the real posts behind it and, last, the line for a brand marked as interpretation.
   The view comes from askStories.js, which keeps only what the answer's own claims bear out. */
import {useState} from 'react';
import {isComment} from '../askEvidence.js';
import {PostTile} from './AskBriefing.jsx';
import '../styles/askbrief42.css';

const TILES_SHOWN = 2;

function StoryPosts({story, records, quoteMap, pinnedId, onPin}){
  const [all, setAll] = useState(false);
  const seen = new Set();
  const posts = [];
  for (const line of story.lines){
    for (const id of line.claim.evidence_ids || []){
      const record = records.get(id);
      if (record && !isComment(record) && !seen.has(id)){
        seen.add(id);
        posts.push(record);
      }
    }
  }
  if (posts.length === 0) return null;
  const shown = all ? posts : posts.slice(0, TILES_SHOWN);
  const hidden = posts.length - shown.length;
  return (
    <>
      <ul className="ask42-tiles ask42-story-tiles" aria-label={'Posts behind ' + story.name}>
        {shown.map((post) => <PostTile key={post.id} post={post} quote={(quoteMap.get(post.id) || [])[0]} pinned={pinnedId === post.id} onPin={onPin} />)}
      </ul>
      {posts.length > TILES_SHOWN && (
        <button type="button" className="ask42-quiet ask42-tiles-more" aria-expanded={all ? 'true' : 'false'} aria-label={all ? 'Fewer posts' : 'and ' + hidden + ' more ' + (hidden === 1 ? 'post' : 'posts')} onClick={() => setAll((value) => !value)}>
          {all ? 'Fewer' : 'and ' + hidden + ' more'}
        </button>
      )}
    </>
  );
}

function Story({story, records, quoteMap, confidence, pinnedId, onPin}){
  return (
    <li className="ask42-story">
      <header className="ask42-story-head">
        {story.rank !== null && <span className="ask42-story-rank">{story.rank}</span>}
        <h3 className="ask42-story-name">{story.name}</h3>
      </header>
      {story.figures.length > 0 && (
        <ul className="ask42-story-figures" aria-label={'Figures for ' + story.name}>
          {story.figures.map((figure) => (
            <li className="ask42-story-figure" key={figure.key} data-query-id={figure.queryId || undefined}>
              <b className="ask42-story-figure-value">{figure.value}</b>{' '}<span className="ask42-story-figure-unit">{figure.unit}</span>
            </li>
          ))}
        </ul>
      )}
      <ul className="ask42-story-lines">
        {story.lines.map((line) => {
          const mark = confidence[line.claim.label];
          return (
            <li className="ask42-story-item" key={line.claim.id}>
              {mark && (
                <span className="ask42-confidence ask42-story-confidence" data-label={line.claim.label}>
                  <span className="ask42-confidence-shape" aria-hidden="true">{mark.shape}</span>
                  {mark.word}
                </span>
              )}
              <p className="ask42-story-line">{line.body}</p>
            </li>
          );
        })}
      </ul>
      <StoryPosts story={story} records={records} quoteMap={quoteMap} pinnedId={pinnedId} onPin={onPin} />
      {story.soWhat.map((item) => (
        <p className="ask42-story-sowhat" key={item.index}>
          <span className="ask42-story-sowhat-label">For a brand, interpretation</span>
          <span className="ask42-story-sowhat-text">{item.text}</span>
        </p>
      ))}
    </li>
  );
}

export function AskStories({view, records, quoteMap, confidence, pinnedId, onPin}){
  /* The page cannot tell which span each story's count covers, so it names no window and claims no more than that the
     listed figures do not contradict the order. */
  const caption = view.ordered
    ? 'Ranked by the figures below'
    : 'In the order 42 listed them; the figures below do not rank them';
  return (
    <section className="ask42-stories-wrap" aria-label="The stories">
      <p className="ask42-stories-caption ask42-muted">{caption}</p>
      <ol className="ask42-stories">
        {view.stories.map((story) => (
          <Story key={story.key} story={story} records={records} quoteMap={quoteMap} confidence={confidence} pinnedId={pinnedId} onPin={onPin} />
        ))}
      </ol>
    </section>
  );
}
