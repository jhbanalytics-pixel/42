"""The local server the client lens browser journey drives: the real app on 127.0.0.1.

Run from app/ with the app's interpreter:

    python -m tests.journey.lens_journey_server 4192

The FastAPI app runs with its real Ask routes, lens roster, Fieldwork detail
route and answer export. The server scope is the configured bsa_pulse scope,
the one the lens registry authorizes the Brand South Africa Pulse lens for, so
both general 42 and that lens can be asked here.

The only stand-in on the question path sits at the engine boundary and is
described in lens_journey_engine: the worker object GeneralQuestionRoutes calls,
and the store reader the Fieldwork detail read calls. The shell's desk, Ask
coverage, storage and PDF renderer stand-ins are the dossier journey's
(dossier_journey_seams), refused in any deployed process. The PDF renderer
double here writes the same page text the renderer's audit reads, in the
Latin 1 range the synthetic PDF can carry.

One control route exists in this process only: /__journey/engine reports what
the stand-in engine admitted and refused, so a test can prove which lens and
parent each request was bound to on the server side.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
SERVER_SCOPE_ID = "bsa_pulse"


def scope_for(investigation_scopes, scope_id):
    """The five configured values of one configured scope, as _current_scope returns them."""
    config = investigation_scopes._load(investigation_scopes.INVESTIGATION_SCOPES_PATH)
    configured = config["scopes"][scope_id]
    fields = (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
    )
    resolved = investigation_scopes.resolve_investigation_scope(
        **{key: configured[key] for key in fields}
    )
    return {
        "client_scope_id": resolved.client_scope_id,
        "market_scope": sorted(resolved.market_scope),
        "brand_config_id": resolved.brand_config_id,
        "audience_lens_ids": list(resolved.audience_lens_ids),
        "theme_id": resolved.theme_id,
    }


def build_app():
    sys.path.insert(0, str(APP_ROOT))
    from tests.journey import dossier_journey_seams as seams

    seams.refuse_deployed(os.environ)
    os.environ["LP_ALLOW_OPEN_GATE"] = "true"
    os.environ.pop("UI_PASSCODE", None)
    os.environ["CACHE_PREFIX"] = "open-intelligence/v2/staging/"

    from fastapi.responses import JSONResponse
    from src.api import (
        dossier_artifacts,
        general_question_routes,
        general_question_startup,
        investigation_scopes,
        main,
        pdf_exporter,
        question_worker_store,
        workspace_scope,
    )
    from tests.journey.lens_journey_engine import (
        DEPLOYMENT_DIGEST,
        POLICY_DIGEST,
        StandInEngine,
    )
    from tests.unit.dossier_pdf_fakes import page_text, synthetic_pdf

    def render(html_bytes, *, asset_root):
        lines = [line.encode("latin-1", "replace").decode("latin-1") for line in page_text(html_bytes)]
        return synthetic_pdf(lines)

    seams.install(
        main=main,
        workspace_scope=workspace_scope,
        pdf_exporter=pdf_exporter,
        dossier_artifacts=dossier_artifacts,
        key=secrets.token_bytes(32),
        bucket=seams.GrantedBucket(),
        renderer=render,
        font=b"unused by the Ask export",
    )
    engine = StandInEngine()
    scope = scope_for(investigation_scopes, SERVER_SCOPE_ID)
    general_question_routes._current_scope = lambda: dict(scope)
    question_worker_store.read_observed_question_detail = engine.read_observed_question_detail

    class _Queue:
        def enqueue(self, invocation, *, deadline):
            return {"state": "verified"}

    def configure(app, *, try_acquire, release):
        app.state.general_question_routes = general_question_routes.GeneralQuestionRoutes(
            engine,
            _Queue(),
            policy_digest=POLICY_DIGEST,
            deployment_digest=DEPLOYMENT_DIGEST,
        )
        app.state.general_question_startup_code = "question_worker_configured"

    general_question_startup.configure_general_question_startup = configure
    app = main.app

    @app.get("/__journey/engine", include_in_schema=False)
    async def _engine():
        return JSONResponse(engine.summary())

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", type=int)
    arguments = parser.parse_args()
    app = build_app()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=arguments.port, log_level="warning")


if __name__ == "__main__":
    main()
