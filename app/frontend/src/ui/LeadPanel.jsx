/* The evidence panel beside the Today lead story: the creators figure large
   with the posts count beside it, the posts a day line at the panel's full
   width, the platforms of the evidence, one or two real example posts and the
   two ways on (Ask about this, Watch). The figure and the line repeat what the
   card below says in full, so they are drawn for the eye only; the examples
   and the actions are real controls and stay in the reading order. Only
   fields the Today payload sends for the lead item are drawn, and a field that
   is missing takes its row out of the panel. */
import {AskAboutThis} from './AskAboutThis.jsx';
import {PlatformLogo} from './PlatformLogo.jsx';
import {PlatformMarks, cardWords} from './StoryCard.jsx';
import {Sparkline, bigFigures, figureWords, isYoutubeChannelAuthor, longDate, platformWord, postTextView} from './TrendCard.jsx';
import {readerFigure} from '../api.js';
import {safeUrl} from '../safeUrl.js';

const text = (value) => (typeof value === 'string' && value.trim() !== '' ? value : '');

function Example({item}){
  const channel = isYoutubeChannelAuthor(item);
  const who = channel ? 'YouTube channel' : text(item.handle) || platformWord(item.platform);
  const when = item.posted_at ? longDate(item.posted_at) : '';
  const line = postTextView(item).preview;
  const url = safeUrl(item.url);
  return (
    <li className="t42-lead-example" data-lead-example={item.id}>
      <p className="t42-lead-example-who">
        <span className="t42-lead-example-mark" aria-hidden="true"><PlatformLogo platform={item.platform} size={16} /></span>
        <span className="t42-lead-example-handle">{who}</span>
        <span className="t42-lead-example-meta">{[platformWord(item.platform), when].filter(Boolean).join(' · ')}</span>
      </p>
      {line && <p className="t42-lead-example-text">{line}</p>}
      {url && <a className="t42-link t42-lead-example-open" href={url} target="_blank" rel="noopener noreferrer">Open the post</a>}
    </li>
  );
}

export function LeadPanel({card, specificity, market, date, watch}){
  if (!card) return null;
  const [lead, second] = bigFigures(card);
  const points = card.sparkline && Array.isArray(card.sparkline.points) ? card.sparkline.points : [];
  /* The line comes only when it can be drawn (three measured days, as on the
     card); the panel never carries the card's "not enough days" note. */
  const drawn = points.filter((p) => p && typeof p.value === 'number' && Number.isFinite(p.value)).length >= 3;
  const examples = specificity && Array.isArray(specificity.examples) ? specificity.examples.slice(0, 2) : [];
  const words = cardWords(card, market, date);
  const marked = watch && watch.mark ? watch.mark(card, market) : card;
  const watching = Boolean(marked.watch_id);
  const value = lead ? readerFigure(lead.value) : '';
  const secondValue = second ? readerFigure(second.value) : '';
  return (
    <div className="t42-lead-panel" data-today-lead-panel="">
      {(lead || drawn) && (
        <aside className="t42-lead-side" aria-hidden="true" data-today-lead-side="">
          {lead && (
            <div className="t42-lead-numbers">
              <p className="t42-lead-figure" data-query-id={lead.query_id} title={'From query ' + lead.query_id}>
                <span className="t42-lead-value">{value}</span>
                <span className="t42-lead-unit">{figureWords(lead).slice(value.length)}</span>
              </p>
              {second && (
                <p className="t42-lead-posts" data-lead-posts="" data-query-id={second.query_id} title={'From query ' + second.query_id}>
                  <span className="t42-lead-posts-value">{secondValue}</span>
                  <span className="t42-lead-unit">{figureWords(second).slice(secondValue.length)}</span>
                </p>
              )}
            </div>
          )}
          {drawn && <Sparkline sparkline={card.sparkline} wide />}
        </aside>
      )}
      <PlatformMarks evidence={card.evidence} attribute="data-lead-platform" className="t42-lead-platforms" />
      {examples.length > 0 && (
        <ul className="t42-lead-examples" aria-label="Example posts">
          {examples.map((item) => <Example key={item.id} item={item} />)}
        </ul>
      )}
      <div className="t42-lead-actions">
        <AskAboutThis className="t42-action t42-action-primary" href={words.href} question={words.question} />
        {watch && watch.onWatch
          ? <button type="button" className="t42-action" aria-pressed={watching ? 'true' : 'false'} onClick={() => watch.onWatch(words.written ? {...card, title: words.written} : card, market)}>
              {watching ? 'Watching' : 'Watch'}
            </button>
          : watching && <span className="tc-watching">Watching</span>}
      </div>
    </div>
  );
}
