/* "Ask about this" on a trend card and a topic page. The Ask page starts a
   live ask as soon as it opens with a question, and a live ask spends
   credits, so the link asks first: the confirm names the ceiling and Cancel
   takes first focus. The link keeps its address, so the question, market,
   item and date it carries stay readable. Wave 8: that address is a draft
   (draft=1), so a new tab, a copied link or a reload fills in the question
   and spends nothing; only the confirm goes to the live address (live=1). */
import {Suspense, lazy, useState} from 'react';
import {go} from '../router.js';

/* The confirm loads on first use, so the trend card does not pull the spike
   confirm into every page that shows a card. */
const CostConfirm = lazy(() => import('./SpikeConfirm.jsx').then((module) => ({default: module.CostConfirm})));

/* With no tier the agent picks T0 or T1, so the T1 ceiling is the one to name. */
const ASK_CEILING = 60;

const DRAFT = /([?&])draft=1(?:&|$)/;
const withDraft = (href) => (DRAFT.test(href) ? href : href + (String(href).includes('?') ? '&' : '?') + 'draft=1');
const withoutDraft = (href) => String(href).replace(/&draft=1(?=&|$)/, '').replace(/\?draft=1&/, '?').replace(/\?draft=1$/, '');

const withLive = (href) => (/([?&])live=1(?:&|$)/.test(href) ? href : href + (String(href).includes('?') ? '&' : '?') + 'live=1');

export function AskAboutThis({href, className, question}){
  const [open, setOpen] = useState(false);
  const onClick = (event) => {
    event.preventDefault();
    setOpen(true);
  };
  const confirm = () => {
    setOpen(false);
    go(withLive(withoutDraft(href)).replace(/^#/, ''));
  };
  return (
    <>
      <a className={className} href={withDraft(href)} onClick={onClick}>Ask about this</a>
      {open && (
        <Suspense fallback={null}>
          <CostConfirm
            title="Ask 42 about this now?"
            where={question || ''}
            line={'This starts a new live ask, up to ' + ASK_CEILING + ' credits.'}
            does="42 reads fresh posts about this trend to answer the question above."
            get="An answer on the Ask page, with the posts behind each point."
            confirmWord="Ask"
            onConfirm={confirm}
            onClose={() => setOpen(false)}
          />
        </Suspense>
      )}
    </>
  );
}
