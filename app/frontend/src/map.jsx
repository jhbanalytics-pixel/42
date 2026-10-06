/* All pages (the Map): every page in the menu, in the menu's own groups,
   each with the one question it answers. Names, groups and questions come
   from the menu (rail42.jsx, pageQuestions.js), so a page renamed there is
   renamed here; map-pages.test.jsx fails if a menu page is missing or listed
   twice. Pure navigation: it reads no data, so it never waits on the API. */
import {PageHero} from './ui/index.js';
import {RAIL_ALL, RAIL_MORE_GROUPS, RAIL_TOP, RAIL_WORK} from './rail42.jsx';
import {pageQuestion} from './pageQuestions.js';
import './styles/map42.css';

const MENU = new Map(RAIL_ALL.map((item) => [item.href, item]));

const BLURBS = Object.freeze({
  top: 'Open Today first. Open a trend to see why it is moving, or ask 42 about it.',
  work: 'Keep what matters: watch a trend, research it, save a checked answer.',
  deeper: 'Follow one trend into its words, its people and its rivals.',
  how: 'For checking where a figure came from before you quote it.',
});

/* One list, in the menu's reading order. */
export const MAP_GROUPS = Object.freeze([
  {id: 'top', title: 'Start here', items: RAIL_TOP},
  {id: 'work', title: 'Your work', items: RAIL_WORK},
  ...RAIL_MORE_GROUPS,
].map((group) => Object.freeze({
  id: group.id,
  title: group.title,
  blurb: BLURBS[group.id],
  pages: group.items.map((item) => ({href: item.href, desc: pageQuestion(item.href)})),
})));

function MapNode({page}){
  const item = MENU.get(page.href);
  const here = page.href === '#/map';
  return (
    <li className="map42-item">
      <a href={page.href} className="map-node map42-node" aria-current={here ? 'page' : undefined}>
        <span className="map42-node-top">
          <span className="map-node-label">{item.label}</span>
          {here ? <span className="map42-where">You are here</span> : null}
        </span>
        <span className="map42-desc">{page.desc}</span>
      </a>
    </li>
  );
}

export function MapPage(){
  return (
    <div className="page map42">
      <div className="page-shell">
        <PageHero
          title="All pages"
          sub="Every page in the menu, in its groups, with the question each one answers. Topic and creator pages open from the pages that list them."
        />
        {MAP_GROUPS.map((group) => (
          <section key={group.id} className="map42-group" aria-labelledby={'map42-' + group.id}>
            <div className="map42-group-head">
              <h2 id={'map42-' + group.id} className="map42-group-title">{group.title}</h2>
              <p className="map42-group-blurb">{group.blurb}</p>
            </div>
            <ul className="map42-nodes">
              {group.pages.map((page) => <MapNode key={page.href} page={page} />)}
            </ul>
          </section>
        ))}
      </div>
    </div>
  );
}
