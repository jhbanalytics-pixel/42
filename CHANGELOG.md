# Changelog

The version number tracks what people see: the website and Ask. History stays as it is: 2.0.0 is Release A, 2.1.0 is Release B, 2.2.0 is Release C, and 2.3.0 is D0.

Major goes up for a breaking change to stored data or a removed page. Minor goes up for a website release with new features. Patch goes up for a website release with fixes only.

From D0 on, a jobs-only release keeps the current number and gets its own dated "Jobs update" entry under that version in CHANGELOG.md. The one exception is a jobs-only release that breaks stored data. Line 2 wins there: it takes the next major number, and the website is rebuilt with it so the menu shows it.

A jobs update reaches the live What's new panel only with the next website build. That is accepted, because the changelog in the repo is the record and the menu shows what the website runs.

At bind for every release, the version entry's lines are re-derived from the merged branch list, and the go-live date is written in.

The newest release comes first, and VERSION at the root of the repository holds its number. Each version is a heading with the version and the date in brackets, or "next, not live yet" until the date is written in, then one line naming the release and one plain line per change. A jobs update is a "### Jobs update" heading with its date in brackets under its version, followed by its own lines. The panel in the menu marks the version the page runs as This version.

A website release that is merged but not numbered yet keeps its lines under a "### Next release (not numbered yet)" heading under the newest version. At bind the release lead moves them under the release's own version heading, sets VERSION to match and writes in the go-live date.

## 2.4.0 (15 October 2026)

Release Alpha, data quality, trend identity and research reliability.

- Ordinary event reactions need evidence of a distinct cultural pattern before publication. Stored cards face the same check, and unnamed topics stay held with their evidence available.
- Today's checked cards consolidate fragments only when their event and date agree. Uncertain fragments stay held, with unsupported explanations removed.
- Brief support and critic checks retry a 504 once when a full call fits within the existing time and spending limits.
- Ask keeps usable research when one research lane fails and marks the missing coverage. Completed polling results remain available even if a later read would fail.
- Ask's number checks reject a count presented as posts per 1,000 accounts without a matching rate result.
- Semantic searches apply platform, account and engagement filters before selecting their closest results. Historical searches use the question's dates and the publication window.
- Detection excludes old recollected posts from fresh signals and applies dated topic links to historical counts.
- Quality comparisons require matching question definitions and calculation methods.
- Collection rejects malformed provider replies without reporting a false empty result. Embeddings require complete, finite vectors before matching.
- Accounts posting are counted separately by platform. Search location no longer implies a source's market.
- Missing model usage keeps an explicit spending estimate. Missing counts and spending on the website remain unknown.
- Dossier reloads preserve drafts, saved figures retain their source checks, and hidden evidence stays excluded from lexical searches.
- Discover retains the context of its displayed results during a failed refresh. Timed out reads offer another attempt and discard late replies.

### Jobs update (21 October 2026)

The jobs only release of 21 October 2026: the morning collection, detection and brief changes that answer the zero card mornings of 18 to 21 October.

- The morning collection's credit share is back to 2,000 a day, so the chain reaches the brief well before 06:15. At 4,000 the 20 October collection ran 162 minutes and 29 of 30 candidates went unexplained. The monthly cap is 180,000, raised from 80,000 on 16 October.
- A creator panel's collection day is no longer held invalid for a quiet or busy day among its creators. Its item count is what those creators posted, so the day is judged on how many of its profiles answered; feeds, counters and searches keep the item band.
- The brief's pattern pool counts the video model's format for each clip beside the enriched formats, so a format three creators share across two days reaches the adopted pattern bucket even when captions do not name it.
- The writer sees each clip's video format and the same line of simpler explanations the critic checks, and is told to name the rival event and show the pattern beyond it, or to write no pattern claim when the posts only react to an event.
- Sound items are named once a title is read instead of staying raw ids, searched posts' sounds are named the same way, and the watched sounds are the ones most creators adopted rather than the lexically smallest ids.
- The morning Instagram reel search asks for each market's own posts at no extra credit, X posts carry their author's stated location for placement, and a refunded failed call no longer shows as a stray refund in the nightly credit check.
- Detection's coordination check holds one compact embedding a post instead of a copy per item, after the 21 October detection was stopped at its memory limit twice, and records its peak memory in its counts.
- The release tooling takes any chain after the previous jobs release as the baseline when its job definitions match, and records a typed acceptance of a chain that did not qualify instead of refusing the release.

### Next release (not numbered yet)

- Ask transcribes the speech of a TikTok video that has no captions when a question needs it.
- The Control room look: a near black red field with Ogilvy red as the one filled action, new interface faces and Material 3 motion timing. Dark is now the default theme; Light and Match system stay in the menu.
- Today's lead draws its posts a day as a field of red LED cells behind its header. Screen readers skip it, because the panel states the same series.
- Ask answers a question about figures, such as poll percentages, with the figures its sources report: one bar chart per source, with who ran it, when, the sample and the quoted words. A figure is kept only when its quote is found word for word in the record, and a hidden publisher's table is left out.
- Ask searches live news it can cite and opens the article behind a news result to read it. A question about figures searches news first.
- Ask's Deep level, several researchers in parallel and then a critic, is open to plain questions where the service has it switched on. The app does not offer a depth control yet.
- The morning collection also searches Bluesky, LinkedIn and each market's subreddit, only in the room the day's collection share leaves after the earlier sources.

## 2.3.0 (12 October 2026)

Release D0, the website and Ask.

- The menu shows the version 42 is on, with this list of what changed.
- A quiet hint offers a reload when the live version changes while the page is open, on phones too.
- Ask's "The numbers" tiles say what each figure counts.
- Each claim in "The numbers" has one tile that shows the whole claim, with posts and accounts posting side by side only when one search counted both.
- Every trend card, Compare and the reach alerts say accounts posting, and Method says Radar counts the accounts that post.
- The credit confirm covers the whole screen, a click inside it no longer opens the card behind it, and the page stays still behind the credit confirm and the Watch, Hide and skill dialogs.
- Try again after a failed ask goes through the same credit confirm before anything is asked again.
- Investigations shows the credits and the USD of model time a plan needs, and a refusal says in plain words which budget is short.
- A gap search names each source route as its platform.
- History, Build and Dossiers say Answered and Partial answer the same way, and Schedules no longer announces a delivery that is not built.
- The Peaking and Spike chips are easier to read in dark mode.

### Jobs update (12 October 2026)

Release D jobs, the brief, collect and watchdog jobs.

- A card with no accounts posting or no posts counted in its last 3 days is held back from Today instead of shown.
- Today puts its cards in order of accounts posting in the last 3 days, then posts, then worth, so the lead card is the one the most accounts are posting about. Which cards make the brief does not change, only their order, so a card can move into or out of the first five. On the first morning, and on the first morning after a rollback, the moved up and held place tags compare against the other order.
- The South Africa Instagram gossip panel reads three live publisher pages in place of three that had stopped returning posts.
- The watchdog no longer emails an alert when a source adds a field to its replies. It still emails when a field every reply carried goes missing, now also emails when a field's value changes type, and logs a new field as a notice.

## 2.2.0 (11 October 2026)

Release C, rel-d0aebe7-01, the website and Ask.

- Today has a swipeable band of board cards with real platform logos, a rebuilt hero and new story cards.
- A card no longer shows a measured figure of zero.
- Instagram and X show their official marks.
- Discover filters change the data, take several platforms at once, and the header follows the Kind you pick.
- "Accounts posting" replaces "Creators in 7 days", and Today and Discover name the same time window.
- Ask reads as a briefing: it leads with the real story, keeps a rail on the right, and drops repeats and internal text.
- Ask reads far more data: one failed search no longer ends the answer, and it searches tags, tag families and topics.
- A comment under a hidden person's post stays hidden, and hidden people stay hidden in skin runs, dossiers and History.

## 2.1.0 (11 October 2026)

Release B, rel-89d4629-01, the daily jobs.

- All 14 daily jobs run one new build, from collecting posts to writing the brief.
- New tables and columns for local signals and early signals sit beside the old ones, and nothing was removed.
- Stored runs record which build produced them.
- The groundwork for more trends on Today, from the first daily run on 12 October.

## 2.0.0 (10 October 2026)

Release A, rel-3710c5e-01, the website and Ask.

- Ask's helper hides people marked hidden before it hands an answer to the website, and the website checks again before it shows it.
- Today and Fieldwork read the culture desk and its kept creators in both the old and the new stored shape.
- The website and Ask go live through a reviewed release: a test copy first, one checked Ask, then the switch, with the earlier version kept to roll back to.
