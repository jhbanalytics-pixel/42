/* Page port, 3 October 2026: the older topic and creator pages read the desk
   API (/api/topic, /api/creator), which f42-api does not serve. A link to one
   that names no 42 id (legacyRoutes.js sends those that do to the 42 topic
   page) lands here: it says the link cannot open in this version and offers
   the 42 page that holds the same job. It reads nothing. */
import {NextActions, PageHero} from './ui/index.js';

const OLDER = Object.freeze({
  topic: {
    title: 'This topic link is from an older version',
    sub: 'It names a topic record this version of 42 does not hold, so there is nothing to open. Find the topic in Discover instead.',
    next: [{label: 'Open Discover', route: '/explore'}, {label: 'Open Seed path', route: '/seedpath'}],
  },
  creator: {
    title: 'This creator link is from an older version',
    sub: 'It names a creator profile this version of 42 does not hold, so there is nothing to open. Creators who share topics are listed in Communities.',
    next: [{label: 'Open Communities', route: '/communities'}, {label: 'Open Discover', route: '/explore'}],
  },
});

export function OlderLink42({kind}){
  const page = OLDER[kind] || OLDER.topic;
  return (
    <section className="page">
      <div className="page-shell">
        <PageHero title={page.title} sub={page.sub} />
        <NextActions actions={page.next} />
      </div>
    </section>
  );
}

export default OlderLink42;
