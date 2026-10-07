# Rules

These rules hold for every change to 42. Code and docs cite them as "RULES.md rule N".

1. Nothing about Gen Z and no age lens anywhere. Never infer age. Describe audiences by language, place, interest, community, creator type.
2. Google Trends via SocialCrawl google_trends routes is allowed as a search-interest signal, labelled as Google search data, never as post evidence and never enough on its own for Today. For Phase 1, only SocialCrawl google_trends/trending is active for query triage. SocialCrawl google_trends/explore and google_trends/rising, plus public BigQuery search signals, stay parked. Search interest guides query triage only; it cannot be used as post evidence, post counts, country or location proof, a quote, why-now evidence, or standalone Today support. Prism earliness and trend-board remain banned.
3. SocialCrawl is the main source. Every SocialCrawl call goes through core/collect/socialcrawl_client.py, which checks the credit ledger and the daily and monthly caps first.
4. Generated Gemini or Nano Banana output is never evidence. Models are chosen by blind test.
5. Every claim carries evidence ids; every number carries the query that produced it. Nothing reaches Today or an answer without passing the trust gate in docs/full-42/TRUST.md; held-back items are shown with their reason, never dropped silently.
6. Staging only (project ogilvy-trends-v2). Production is Albert's step through the production workflow.
7. Never delete data: no drop, truncate, delete, table replace or expiry. Old datasets are read-only.
8. IAM only adds. No owner or editor. Only core/setup/bootstrap.py grants, and Albert runs it.
9. Never print, echo, save or paste a secret value or access token.
10. Commits are authored as Albert Meintjes <albert.meintjes@ogilvy.co.za>, with no trailers and no em dashes or double hyphens.
11. Nobody reviews their own work. Every change gets an independent review, and blockers are fixed before committing.
12. Use py -3.13 on Albert's PC. Never use the bq CLI.
13. Before Google Cloud work, verify the project and the separate caller, build and runtime identities against the reviewed staging paste. A paste may explicitly authorise Albert's login as caller for that release-specific exception; it does not authorise that caller for other work. The recorded 7 October services paste uses f42-deployer as its build service account, with f42-agent and f42-web as the service runtime identities. Keep f42-builder impersonation for warehouse reads unless the work contract says otherwise. Stop on any identity mismatch; never change gcloud configuration or IAM to make a command pass. SocialCrawl calls run inside Cloud Run jobs, never from a local shell.
14. Caps live in one table in docs/full-42/SETUP.md. Evaluations run in replay mode and spend no live credits.
