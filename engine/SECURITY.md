# Security Policy

## Reporting a Vulnerability

If you discover a security issue, email albert.meintjes@ogilvy.co.za with details.
Do not open a public issue.

Response time: within 48 hours for acknowledgement.

## Supported Versions

Only the latest master branch receives security fixes during active development.

## Credential Handling

API keys and tokens go to the correct environment:

- Dev: `.env` file (gitignored, never committed)
- Staging and prod: GCP Secret Manager via `src/utils/secrets.py`

Never paste credentials into code, comments, or issue trackers.
