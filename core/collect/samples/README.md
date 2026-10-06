# SocialCrawl probe samples

This folder holds the output of the Stage 0 SocialCrawl probe (docs/full-42/BUILD.md task 0.5). It stays empty until the probe has run on staging.

The probe runs as the Cloud Run job f42-probe (`python -m core.collect.probe`, identity f42-collector) and prints its run_id. Then, locally as f42-builder:

```
py -3.13 -m core.collect.probe_report probe-<utc timestamp>
```

That reads the run's raw_responses and credit_ledger rows and writes two things here:

| File | What it holds |
|---|---|
| `REPORT.md` | Route, status, charged and reported credits and row count per ledger row; the feed=local share of in-country videos per market and the NG to KE overlap, both computed in memory from the stored responses; the samples the gate withheld |
| `<probe id>_<route slug>.json` | The probe id, the route and the key-and-type skeleton of the response body |

## What a sample holds

A sample shows the shape of a response and no value from it: no text, no names, no numbers, no dates and no ids.

| In the response | In the sample |
|---|---|
| a string, int, float, bool or null | its type name: `<str>`, `<int>`, `<float>`, `<bool>`, `<null>` |
| a list | `<list of N>` and the skeleton of its first element |
| a dict whose keys are all known field names | the same keys, each holding its value's skeleton |
| a dict with any other key, such as a handle or an id used as a key | one `<key>` entry holding the skeleton of its first value |

The known field names are the 272 keys that appear in the key-and-type dump of the Stage 0 probe responses, plus the 15 top-level response envelope names seen in a live tiktok/trending body (`success`, `data`, `pagination`, `credits_used` and the like; 7 of them were not already in the dump). Nothing else was added, so a field SocialCrawl adds later shows as `<key>` until the list is updated.

After that a gate scans each sample. If it still finds a number, an email, a url, a domain, an @handle or a run of 9 or more digits (spaces, dots, dashes, slashes, underscores, commas and brackets ignored), the file is not written and REPORT.md lists it as withheld.

These samples are never evidence.

To see the planned calls and their credit holds without any network: `py -3.13 -m core.collect.probe --plan`.
