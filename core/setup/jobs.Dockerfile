# One image for every 42 Cloud Run job. Each job sets its own command (python -m <module>) at deploy
# time, see core/setup/deploy_jobs.py. Built in Cloud Build from a git archive of HEAD, so the context
# holds core/ and the SocialCrawl routes file only. No secrets here: the key is mounted by Cloud Run.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# UMAP's numba functions cache compiled code. The job user below has no home and cannot write site-packages, so
# numba finds no cache folder and clustering fails ("no locator available"); /tmp is writable on Cloud Run.
ENV NUMBA_CACHE_DIR=/tmp/numba-cache
WORKDIR /app

COPY core/setup/requirements-jobs.txt /tmp/requirements-jobs.txt
# f42-understand runs on this image too: its cluster step (core/understand/cluster.py) needs the packages in
# core/understand/requirements.txt, resolved together with the jobs pins (each package the two share is pinned the
# same). BERTopic goes in after them with no dependencies, as that file says: its declared sentence-transformers would
# pull torch, and clustering fits on stored embeddings without an embedding model.
COPY core/understand/requirements.txt /tmp/requirements-understand.txt
RUN pip install --no-cache-dir --only-binary=numpy,scipy,statsmodels,pandas \
        -r /tmp/requirements-jobs.txt -r /tmp/requirements-understand.txt \
    && pip install --no-cache-dir --no-deps bertopic==0.17.4

COPY core/ /app/core/
COPY docs/full-42/reference/sc_routes.json /app/docs/full-42/reference/sc_routes.json

RUN useradd --system --uid 10001 --no-create-home f42
USER 10001

# A job deployed without its own command fails loudly instead of exiting 0 from an idle interpreter.
CMD ["python", "-c", "raise SystemExit('no job command set: deploy with python -m <module>')"]
