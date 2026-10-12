# Changelog

The version number tracks what people see: the website and Ask. History stays as it is: 2.0.0 is Release A, 2.1.0 is Release B, 2.2.0 is Release C, and 2.3.0 is D0.

Major goes up for a breaking change to stored data or a removed page. Minor goes up for a website release with new features. Patch goes up for a website release with fixes only.

From D0 on, a jobs-only release keeps the current number and gets its own dated "Jobs update" entry under that version in CHANGELOG.md. The one exception is a jobs-only release that breaks stored data. Line 2 wins there: it takes the next major number, and the website is rebuilt with it so the menu shows it.

A jobs update reaches the live What's new panel only with the next website build. That is accepted, because the changelog in the repo is the record and the menu shows what the website runs.

At bind for every release, the version entry's lines are re-derived from the merged branch list, and the go-live date is written in.

The newest release comes first, and VERSION at the root of the repository holds its number. Each version is a heading with the version and the date in brackets, or "next, not live yet" until the date is written in, then one line naming the release and one plain line per change. A jobs update is a "### Jobs update" heading with its date in brackets under its version, followed by its own lines. The panel in the menu marks the version the page runs as This version.

## 2.3.0 (next, not live yet)

Release D0, the website and Ask.

- The menu shows the version 42 is on, with this list of what changed.
- A quiet hint offers a reload when the live version changes while the page is open, on phones too.
- Ask's "The numbers" tiles say what each figure counts.
- Each claim in "The numbers" has one tile that shows the whole claim, with posts and accounts posting side by side only when one search counted both.
- Every trend card, Compare and the reach alerts say accounts posting, and Method says Radar counts the accounts that post.
- The credit confirm covers the whole screen, a click inside it no longer opens the card behind it, and the page stays still behind every dialog.
- Try again after a failed ask goes through the same credit confirm before anything is asked again.
- Investigations shows the credits and the USD of model time a plan needs, and a refusal says in plain words which budget is short.
- A gap search names each source route as its platform.
- History, Build and Dossiers say Answered and Partial answer the same way, and Schedules no longer announces a delivery that is not built.
- The Peaking and Spike chips are easier to read in dark mode.

### Jobs update (12 October 2026)

Release D jobs, the brief, collect and watchdog jobs.

- A card with no accounts posting or no posts counted in its last 3 days is held back from Today instead of shown.
- Today puts its cards in order of accounts posting in the last 3 days, then posts, then worth, so the lead card is the one the most accounts are posting about. Which cards are shown does not change. On the first morning the moved up and held place tags compare against the old order, so they are wrong that day only.
- The South Africa Instagram gossip panel reads three live publisher pages in place of three that had stopped returning posts.
- The watchdog no longer emails an alert when a source adds a field to its replies. It still emails when a field every reply carried goes missing or changes type, and it logs a new field as a notice.

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
