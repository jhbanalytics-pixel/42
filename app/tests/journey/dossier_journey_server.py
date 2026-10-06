"""The local server the dossier browser journey drives: the real app on 127.0.0.1.

Run from app/ with the app's interpreter:

    python -m tests.journey.dossier_journey_server --port 4191

The signing key comes from DOSSIER_JOURNEY_SIGNING_KEY (hex, 32 bytes or
more), which the browser test generates for each run. The server serves the
built page from web/dist through the app's own index route, with the real API
routes and stores behind it. The stand-ins are those in dossier_journey_seams.

A few control routes under /__journey/ exist in this process only. They create
nothing the app could not: they publish a dossier for an investigation the API
created (the engine producer's step), make the renderer double refuse, hold one
request back for a moment so a late answer can be proved harmless, report the
stored object names so a test can prove what was and was not written, and move
the server scope between the configured client scopes, which a deployment sets
once, so one run can read a bsa_pulse investigation as that scope's server does.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]


def build_app(key: bytes, subject: str):
    sys.path.insert(0, str(APP_ROOT))
    from tests.journey import dossier_journey_seams as seams

    seams.refuse_deployed(os.environ)
    os.environ.update(seams.review_environment(subject))
    os.environ["LP_ALLOW_OPEN_GATE"] = "true"
    os.environ.pop("UI_PASSCODE", None)
    os.environ["CACHE_PREFIX"] = "open-intelligence/v2/staging/"

    from fastapi import Request
    from fastapi.responses import JSONResponse
    from src.api import (
        dossier_artifacts,
        investigation_scopes,
        main,
        pdf_exporter,
        workspace_scope,
    )
    from tests.journey import dossier_journey_fixture as fixture
    from tests.unit import dossier_pdf_fakes

    bucket = seams.GrantedBucket()
    renderer = dossier_pdf_fakes.FakeRenderer()
    seams.install(
        main=main,
        workspace_scope=workspace_scope,
        pdf_exporter=pdf_exporter,
        dossier_artifacts=dossier_artifacts,
        key=key,
        bucket=bucket,
        renderer=renderer,
        font=seams.approved_face(pdf_exporter, APP_ROOT / "web" / "dist" / "assets"),
    )
    held: list[dict] = []
    app = main.app
    # The configured default scope unless a test moves it to another
    # configured scope; the resolver reads it on every request.
    configured_default = investigation_scopes.default_client_scope_id
    server_scope: dict = {"client_scope_id": None}

    def _server_scope(path=None):
        return server_scope["client_scope_id"] or configured_default(path)

    investigation_scopes.default_client_scope_id = _server_scope

    @app.middleware("http")
    async def _hold_back(request: Request, call_next):
        target = request.url.path + ("?" + request.url.query if request.url.query else "")
        for rule in list(held):
            if rule["contains"] in target:
                held.remove(rule)
                await asyncio.sleep(rule["seconds"])
                break
        return await call_next(request)

    @app.post("/__journey/seed", include_in_schema=False)
    async def _seed(request: Request):
        body = await request.json()
        return JSONResponse(
            fixture.publish(
                bucket=bucket,
                investigation_id=body["investigation_id"],
                kind=body["kind"],
                label=body.get("label", ""),
            )
        )

    @app.post("/__journey/renderer", include_in_schema=False)
    async def _renderer(request: Request):
        body = await request.json()
        renderer.refusal = body.get("refusal")
        return JSONResponse({"refusal": renderer.refusal, "calls": len(renderer.calls)})

    @app.post("/__journey/hold", include_in_schema=False)
    async def _hold(request: Request):
        body = await request.json()
        held.append({"contains": str(body["contains"]), "seconds": float(body["seconds"])})
        return JSONResponse({"held": len(held)})

    @app.post("/__journey/scope", include_in_schema=False)
    async def _scope(request: Request):
        body = await request.json()
        scope_id = body.get("client_scope_id")
        if scope_id not in (None, "ogilvy_default", "bsa_pulse"):
            return JSONResponse({"refused": scope_id}, status_code=400)
        server_scope["client_scope_id"] = scope_id
        return JSONResponse({"client_scope_id": _server_scope()})

    @app.get("/__journey/storage", include_in_schema=False)
    async def _storage():
        return JSONResponse(bucket.summary())

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--subject", default="100000000000000000042")
    arguments = parser.parse_args()
    raw = os.environ.get("DOSSIER_JOURNEY_SIGNING_KEY", "")
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        key = b""
    app = build_app(key, arguments.subject)
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=arguments.port, log_level="warning")


if __name__ == "__main__":
    main()
