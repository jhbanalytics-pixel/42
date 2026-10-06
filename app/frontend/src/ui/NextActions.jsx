/* PULSE ui · NextActions: the next action row under a route. Where Seeds and
   Explorer carried a breadcrumb above the hero, the places a reader goes
   next now sit under the page as a labelled row of controls, the row the
   workspaces already carry. Links are real anchors (href="#/...") so a
   middle click and open in new tab work; a plain click goes through the
   router's go() so the route moves without a document navigation. Renders
   nothing for an empty list. */
import {go} from '../router.js';

function isPlainClick(e){
  return !(e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button === 1);
}

export function NextActions({actions, label = 'Next'}){
  const list = Array.isArray(actions) ? actions.filter((entry) => entry && entry.route && entry.label) : [];
  if (list.length < 1) return null;
  return (
    <nav className="ui-next" aria-label={label}>
      <span className="ui-next-label">{label}</span>
      {list.map((entry) => {
        const href = entry.route.startsWith('/') ? '#' + entry.route : '#/' + entry.route;
        return (
          <a
            key={entry.route}
            href={href}
            onClick={(e) => {
              if (!isPlainClick(e)) return;
              e.preventDefault();
              go(entry.route);
            }}
          >
            {entry.label}
          </a>
        );
      })}
    </nav>
  );
}
