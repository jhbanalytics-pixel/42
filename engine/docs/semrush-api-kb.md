# SEMrush API Knowledge Base

Authoritative reference for integrating SEMrush into the Trends Engine V2 (TEV2) and Listening Post.

## 1. Access & subscriptions

API access requires the Business tier of the SEO Toolkit subscription. The Standard API covers Analytics and Projects and uses a prepaid unit pool. These units are available in scalable packages starting at 2,000,000 units. The Trends API requires a separate Trends subscription, either Basic or Premium, and operates on a monthly limit of 10,000 requests. It bypasses the Standard API unit ledger completely.

For JHP Analytics context, confirm our specific unit pool size before scaling up cron requests. Find the API key in the Semrush UI under Subscription Info, then API units.

## 2. Authentication

Authentication varies by API surface. The v3 Analytics API requires the key as a query parameter (`?key=YOUR_API_KEY`). The v4 Keywords API requires the key in the header (`Authorization: Apikey YOUR_API_KEY`). The MCP server uses OAuth 2.0 for interactive sessions.

## 3. Rate limits & quotas

The API enforces a strict rate limit of 10 requests per second and 10 concurrent requests from a single IP address.

Budget exhaustion returns an ERROR 132 code. The API does not provide Retry-After behaviour, so implement exponential backoff manually on 429 or 132 responses. An ERROR 132 requires manual unit top-up.

## 4. API surfaces map

The v3 Analytics API returns CSV data and covers Domain Overview, Organic Research, and the Keyword Magic Tool. The v4 Keywords API returns JSON data and covers Keyword Metrics. The Trends API returns CSV data and covers Traffic and Market data. The Projects API uses OAuth 2.0 and provides read-only access via MCP.

## 5. Unit economics

Semrush uses a consumption-based model tied to the number of rows returned. Live data costs 10 units per line. Historical data costs 50 units per line, which is a 5x multiplier. The Trends API costs 1 unit per line.

The `display_limit` gotcha is critical. The default return size is 10,000 lines. If you omit `display_limit`, a single live data request costs 100,000 units. Always set `display_limit` to exactly what you need.

Monthly budget math for the 3-market cron works as follows. If we track N keywords per market, fetching the top 20 related phrases daily: N keywords x 20 units x 30 days. For example, 100 keywords x 20 units x 30 days = 60,000 units per month.

## 6. Endpoints we care about for Jo-style research

| Endpoint | URL | Format | Notes |
|---|---|---|---|
| Keyword Metrics | `https://api.semrush.com/apis/v4/keywords/v1/metrics?keyword={}&country={}&month={}` | JSON | v4 API. Requires `Authorization: Apikey` header. |
| Phrase Related | `https://api.semrush.com/?type=phrase_related&key={}&phrase={}&database={}&display_limit={}` | CSV | v3 API. Best for content gap analysis. |
| Domain Overview | `https://api.semrush.com/?type=domain_ranks&key={}&domain={}&database={}` | CSV | v3 API. High-level organic and paid snapshot. |
| Organic Research | `https://api.semrush.com/?type=domain_organic&key={}&domain={}&database={}&display_limit={}` | CSV | v3 API. Competitor keyword discovery. |
| Audience/Traffic | `https://api.semrush.com/analytics/ta/api/v3/summary?key={}&targets={}` | CSV | Trends API. Requires Trends subscription. |

Example curl for Keyword Metrics:

```bash
curl -H 'Authorization: Apikey YOUR_API_KEY' "https://api.semrush.com/apis/v4/keywords/v1/metrics?keyword=amapiano&country=ZA&month=2026-06"
```

## 7. MCP vs production REST

The official Semrush MCP server runs at `https://mcp.semrush.com/v2/mcp`. It uses OAuth to authenticate the user and provides discovery tools like `domain_overview`, `keyword_research`, `execute_report`, and `get_report_schema`.

The TEV2 cron uses the REST API directly instead of MCP. MCP is built for interactive chat and relies on OAuth browser redirects, which fails in a headless Cloud Run environment. The cron requires deterministic API key authentication.

## 8. TEV2 integration contract

The connector pattern will live at `src/ingestion/connectors/semrush.py`. It requires the `SEMRUSH_API_KEY` environment variable. The `sources.yaml` shape needs a `semrush:` block with `enabled: false` by default.

The connector must parse Semrush CSV or JSON into the standard TEV2 BigQuery schema. Semrush data will feed into the `search_velocity_score`, which currently holds a 0.05 weight in `configs/scoring.yaml`. WPP compliance mandates that we pull public search data in but never push client BigQuery data out to Semrush.

## 9. Live probe checklist

Albert runs these steps before flipping `enabled: true`:

1. Verify `SEMRUSH_API_KEY` is in the local `.env` and GCP Secret Manager.
2. Run a single curl against the v3 and v4 endpoints to confirm the key has the correct subscription tier.
3. Check the API unit balance in the Semrush UI.
4. Run `py -3.13 scripts/run_rss_now.py` locally with the Semrush connector isolated.
5. Verify the BigQuery dry run passes.

## 10. Semrush MCP setup

To use the official Semrush MCP in a local dev environment, register the server URL in your MCP client config:

```json
{
  "mcpServers": {
    "semrush": {
      "url": "https://mcp.semrush.com/v2/mcp"
    }
  }
}
```

## 11. Troubleshooting

OAuth redirect errors occur when using MCP in a headless environment. Switch to the REST API with an API key.

ERROR 132 indicates budget exhaustion. The API unit pool is empty and requires the account owner to purchase more units.

Missing Trends access means the Trends API requires a separate subscription. A standard Business tier SEO Toolkit plan will return errors on `/analytics/ta/api/v3/` endpoints.

Per the Jo meeting, ensure all keyword tracking aligns with the broader search team strategy to avoid duplicating unit spend.
