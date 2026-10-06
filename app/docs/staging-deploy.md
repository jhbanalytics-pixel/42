# 42 Open Intelligence staging deployment

`infra/cloud-run/deploy-open-intelligence-staging.sh` is the authoritative staging path. It targets only `listening-post-staging` in `ogilvy-trends-v2`, region `us-central1`, from branch `feat/42-redesign-staging`.

Run its safe plan first:

```bash
infra/cloud-run/deploy-open-intelligence-staging.sh --plan
```

The plan performs no build and no network call. It prints the source deploy arguments and the fields a real deployment will read back.

For an approved deployment, run the same script without `--plan` from a clean tracked worktree. It builds `frontend`, confirms `web/dist/index.html`, prints the previous ready revision, deploys with the dedicated staging identity, and reads the Cloud Run service back. The only secret binding is `UI_PASSCODE=ui-passcode-staging:latest`.

The isolated runtime values are `BQ_DATASET=trends_v2_staging`, `CACHE_BUCKET=listening-post-staging-cache`, `CACHE_PREFIX=open-intelligence/v2/staging/`, `APPLICATION_SOURCE=open-intelligence-staging`, `DEPLOYMENT_PROFILE=open-intelligence-staging`, and the full current `SOURCE_SHA`. Resource and revision template labels carry `environment=staging`, `application-source=open-intelligence-staging`, and that same full source SHA.

If readback passes, the script prints the exact rollback command. Rollback only routes traffic to the recorded `status.latestReadyRevisionName`, never a traffic-list entry. It does not deploy, alter production, or delete any revision.

The former manual staging command is retired. It used `trends_v2_dev`, the default compute identity, and a retired vendor secret, all outside this contract. Do not use it.
