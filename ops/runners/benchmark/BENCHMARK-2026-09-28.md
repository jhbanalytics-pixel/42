# 42 recall check: Cultural Moments 28 Sep to 4 Oct 2026

Answer key: "Cultural Moments 28 Sep - 04 Oct.xlsx" (Albert upload 5 Oct 11:59Z, tester sheet). 24 moments: ZA 8, NG 9, KE 7. Columns date, country, moment, why it mattered, scheduled or unscheduled, link. It extends Thabang's 2 Oct weekly sheet (25 moments, 28 Sep to 2 Oct) to 4 Oct.

Status: repo reading done (live release d5bbb025); staging data not read yet. The cloud has no BigQuery credentials (`gcloud auth list`: no credentialed accounts), so "caught or missed" per moment waits on one read-only run of `benchmark_moments.py` on Albert's machine.

## What is already checked

- Today: 0 of 24. Today had no cards from the start of collection (29 Sep, per WHY-ZERO-CARDS-2026-10-03.md) until 5 Oct, when NG got 2 cards (#timiniegbuson, northeast governors; smoke readout in the lead thread 5 Oct 11:30Z). Neither is on the sheet.
- 42 started collecting on 29 Sep, so 28 Sep moments (ZA7, ZA8, NG8, NG9) fall before day one; their posts can still be collected by publish date, but with no history they sit in warm-up.
- The 2 Oct YouTube trending outage held every YouTube-led candidate (G1) for three days, which covers 30 Sep to 3 Oct moments on Today.

## What the gates predict (repo, not data)

Source of rules: docs/full-42/TRUST.md section 2, core/brief/political.yaml, Albert's 2 Oct news-driven rule (news reaches Today only when local creators add their own reaction).

| id | date | moment | kind | most likely outcome in 42 |
|----|------|--------|------|---------------------------|
| ZA1 | 4 Oct | Bafana 1-0 Egypt | scheduled sport, social | Should form a topic; in the 5 Oct brief window. Ramaphosa in a title would trip G4b (political) |
| ZA2 | 2 Oct | Mass shootings | news, sensitive | News feeds only; no creator reaction rule for Today |
| ZA3 | 1 Oct | Green ID book end | admin news | News only; 2 Oct tracker marked it denied |
| ZA4 | 30 Sep | Bafana 5-0 Eritrea | scheduled sport, social | Best ZA candidate; in the colleague calendar (G8 seasonal); inside the outage hold window |
| ZA5 | 29 Sep | US ambassador Bozell | political news | News only; political |
| ZA6 | 29 Sep | Tyla body-shaming on X | X-only debate | Likely not collected: X creator timelines return old posts, X search runs only in Ask |
| ZA7 | 28 Sep | Ekurhuleni killings | news, sensitive | Before day one; news only |
| ZA8 | 28 Sep | Soweto Derby rivalry | scheduled sport, social | Before day one; TikTok/YouTube football may still show |
| NG1 | 4 Oct | BBNaija finale | scheduled TV, social | Strong candidate in the 5 Oct brief window; not one of NG's 2 cards, so held or never formed |
| NG2 | 1 Oct | Nigeria at 66 | scheduled, social | Should form a topic; G8 seasonal; outage window |
| NG3 | 1 Oct | Nollywood clips on TikTok | diffuse genre | Not one topic; hard to score |
| NG4 | 1 Oct | NYSC abduction, Imo | news, sensitive | News only |
| NG5 | 30 Sep | Super Eagles 0-3 Guinea-Bissau | scheduled sport, social | Should form a topic; outage window |
| NG6 | 30 Sep | Olu Jacobs tributes | news + social | Possible topic |
| NG7 | 29 Sep | Opposition 2027 regroup | political news | G4b holds unless corroborated |
| NG8 | 28 Sep | MRS petrol price cut | price news | Sheet itself found no social posts; news only |
| NG9 | 28 Sep | Davido "walking billboard" | entertainment news | Davido's X timeline is in the panel but returns old posts |
| KE1 | 3 Oct | Pilgrims crash, 17 dead | news, sensitive | News only |
| KE2 | 3 Oct | CAF keeps AFCON 2027 in KE | sport admin news | News only |
| KE3 | 2 Oct | Dangote refinery legal challenge | business news | News only; Ruto would trip G4b |
| KE4 | 1 Oct | Chivayo death reaction | pan-African, social | Located-in-KE (G6) unlikely; tracker denied the crash |
| KE5 | 1 Oct | Harambee 0-1 Guinea | scheduled sport | Possible topic; tracker denied the result |
| KE6 | 30 Sep | Inflation 6.8% | statistic release | News only |
| KE7 | 29 Sep | Malindi court on Lamu land | court news | News only |

Fair denominator: about 12 of the 24 are social moments 42 is built to find (ZA1, ZA4, ZA6, ZA8, NG1, NG2, NG5, NG6, NG9, KE4, KE5, and possibly NG3). The other 12 are hard news, which by Albert's 2 Oct rule reaches Today only with local creator reaction. Score both, but judge 42 on the first group.

## The run (read-only)

Files: `benchmark_moments.py` and `moments-2026-09-28.csv` in this folder. One SELECT per stage, maximum 3 GB billed per query, saves a new CSV to ~/dev/42-readback. For each moment it reports matching posts (any market, the market's collection, located, own feeds, news, lanes, platforms, example URLs), map items and clusters, Today cards and held items with reasons, Google search terms and GDELT entities, then a verdict from ON TODAY down to NOT COLLECTED.

## Reusing it each week

Turn the new sheet into a moments CSV (id, date, market, moment, any_re, and_re: a pattern every post about the moment should contain, and an optional second pattern to narrow it), then run the same script. Writing the patterns is the only manual step.
